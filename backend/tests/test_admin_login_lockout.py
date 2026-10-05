import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import admin


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    def __init__(self, peer: str, headers: dict | None = None):
        self.client = _FakeClient(peer)
        self.headers = headers or {}


class AdminLoginLockoutTest(unittest.IsolatedAsyncioTestCase):
    """Covers item 2: the login failure lockout must stay fully intact for a genuine
    per-visitor identity, and must never lock (while still being rate-limited) when
    the attributed identity is provably just the trusted proxy's own address.
    """

    async def asyncSetUp(self):
        # _failure_tracker / _limiter_login are module-level singletons shared across
        # the whole test process. Every test in this class uses its own untouched
        # fake peer/IP so runs never interfere with each other regardless of order.
        self._log_patch = patch.object(admin, "log_operation", new=AsyncMock())
        self._log_patch.start()
        self.addCleanup(self._log_patch.stop)

        self._password_patch = patch.object(
            admin, "verify_admin_password", new=AsyncMock(return_value=False)
        )
        self._password_patch.start()
        self.addCleanup(self._password_patch.stop)

    async def _login(self, request):
        return await admin.login(admin.AdminLoginRequest(password="wrong"), request)

    async def test_genuine_per_visitor_identity_still_locks_after_five_failures(self):
        # Untrusted peer -> counted by its own direct connection address, a real
        # per-visitor identity. Brute-force protection must be unchanged.
        peer = "203.0.113.50"

        for _ in range(4):
            with self.assertRaises(HTTPException) as cm:
                await self._login(_FakeRequest(peer))
            self.assertEqual(cm.exception.status_code, 401)

        # 5th failure trips the lock.
        with self.assertRaises(HTTPException) as cm:
            await self._login(_FakeRequest(peer))
        self.assertEqual(cm.exception.status_code, 429)

        # A 6th attempt is rejected purely because of the lock, even before password
        # verification would run again.
        with self.assertRaises(HTTPException) as cm:
            await self._login(_FakeRequest(peer))
        self.assertEqual(cm.exception.status_code, 429)
        self.assertIn("失败次数过多", cm.exception.detail)

    async def asyncTearDown(self):
        # The shared-identity cooldown is a single site-wide singleton (by design: it
        # has no per-IP bucket to isolate on), so it must be reset between tests
        # regardless of test order or failure.
        admin._shared_identity_cooldown.record_success()

    async def test_proxy_self_identity_first_four_failures_behave_as_today(self):
        # Trusted proxy peer whose forwarded X-Real-IP equals its own address: this is
        # the collapsed-identity case (client_ip.get_client_ip_info flags
        # is_proxy_self_identity=True). The first 4 consecutive failures must still be
        # plain 401s, exactly like a real per-visitor identity, since locking this
        # shared bucket would lock out every visitor site-wide, admin included.
        peer = "172.18.0.1"

        def request():
            return _FakeRequest(peer, {"X-Real-IP": peer})

        for _ in range(4):
            with self.assertRaises(HTTPException) as cm:
                await self._login(request())
            self.assertEqual(cm.exception.status_code, 401)
            self.assertEqual(cm.exception.detail, "密码错误")

    async def test_proxy_self_identity_still_hits_the_request_rate_limiter(self):
        # The plain request-rate limiter (10/min) still applies to the shared identity,
        # in front of any cooldown logic.
        peer = "172.18.0.2"

        def request():
            return _FakeRequest(peer, {"X-Real-IP": peer})

        for _ in range(10):
            with self.assertRaises(HTTPException):
                await self._login(request())

        with self.assertRaises(HTTPException) as cm:
            await self._login(request())
        self.assertEqual(cm.exception.status_code, 429)
        self.assertIn("频繁", cm.exception.detail)

    async def test_proxy_self_identity_cooldown_after_fifth_failure(self):
        # From the 5th consecutive failure on, the shared identity enters a cooldown:
        # further attempts are rejected with 429 before the password is even checked.
        peer = "172.18.0.3"

        def request():
            return _FakeRequest(peer, {"X-Real-IP": peer})

        with patch("app.routes.admin.time") as fake_time:
            fake_time.time.return_value = 1_000_000.0

            for _ in range(4):
                with self.assertRaises(HTTPException) as cm:
                    await self._login(request())
                self.assertEqual(cm.exception.status_code, 401)

            # 5th failure: still checks the password (401), but arms the cooldown.
            with self.assertRaises(HTTPException) as cm:
                await self._login(request())
            self.assertEqual(cm.exception.status_code, 401)

            # Immediately after, still inside the 60s cooldown window: rejected
            # without checking the password.
            fake_time.time.return_value = 1_000_000.0 + 30
            with self.assertRaises(HTTPException) as cm:
                await self._login(request())
            self.assertEqual(cm.exception.status_code, 429)
            self.assertIn("登录失败次数过多，请稍后再试", cm.exception.detail)

            # Past the 60s cooldown: password is checked again (401, and this failure
            # arms the next, doubled cooldown).
            fake_time.time.return_value = 1_000_000.0 + 61
            with self.assertRaises(HTTPException) as cm:
                await self._login(request())
            self.assertEqual(cm.exception.status_code, 401)

            # Cooldown after the 6th failure doubles to 120s: still active at +90s.
            fake_time.time.return_value = 1_000_000.0 + 61 + 90
            with self.assertRaises(HTTPException) as cm:
                await self._login(request())
            self.assertEqual(cm.exception.status_code, 429)

    async def test_proxy_self_identity_cooldown_doubles_and_caps_at_900_seconds(self):
        # Exercise _SharedIdentityCooldown directly: the doubling/capping arithmetic
        # is a pure function of the failure count, so there is no need to drive a
        # long timeline of login() calls to assert it.
        cooldown = admin._SharedIdentityCooldown(
            max_free_failures=4, initial_cooldown=60, max_cooldown=900
        )
        now = 1_000_000.0
        expected = [None, None, None, None, 60, 120, 240, 480, 900, 900, 900]
        for want in expected:
            cooldown.record_failure(now)
            if want is None:
                self.assertFalse(cooldown.is_active(now))
            else:
                self.assertAlmostEqual(cooldown.cooldown_until - now, want)
            # Advance just enough to clear whatever cooldown was armed, but nowhere
            # near the 1h window so the streak keeps counting.
            now += 5

    async def test_proxy_self_identity_sustained_attack_never_resets_streak(self):
        # A steady attacker retries the moment each cooldown ends. The 1h window is
        # measured from the most recent failure, so the streak never resets and the
        # cooldown stays at the 900s cap for as long as the attack continues.
        cooldown = admin._SharedIdentityCooldown(
            max_free_failures=4, initial_cooldown=60, max_cooldown=900
        )
        start = now = 1_000_000.0
        for _ in range(4):
            cooldown.record_failure(now)
            now += 1
        for _ in range(20):
            cooldown.record_failure(now)
            now = cooldown.cooldown_until
        self.assertGreater(now - start, 3 * 3600)
        cooldown.record_failure(now)
        self.assertAlmostEqual(cooldown.cooldown_until - now, 900)

    async def test_proxy_self_identity_success_resets_cooldown(self):
        peer = "172.18.0.5"

        def request():
            return _FakeRequest(peer, {"X-Real-IP": peer})

        with patch("app.routes.admin.time") as fake_time:
            fake_time.time.return_value = 3_000_000.0

            for _ in range(4):
                with self.assertRaises(HTTPException):
                    await self._login(request())

            with patch.object(admin, "verify_admin_password", new=AsyncMock(return_value=True)), \
                 patch.object(admin, "get_admin_api_key", new=AsyncMock(return_value="fake-key")):
                result = await admin.login(admin.AdminLoginRequest(password="correct"), request())
                self.assertEqual(result["status"], "ok")

            self.assertEqual(admin._shared_identity_cooldown.count, 0)
            self.assertEqual(admin._shared_identity_cooldown.cooldown_until, 0.0)

            # Back to behaving like a fresh identity: 4 more free failures.
            for _ in range(4):
                with self.assertRaises(HTTPException) as cm:
                    await self._login(request())
                self.assertEqual(cm.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
