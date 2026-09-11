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

    async def test_proxy_self_identity_is_rate_limited_but_never_locked(self):
        # Trusted proxy peer whose forwarded X-Real-IP equals its own address: this is
        # the collapsed-identity case (client_ip.get_client_ip_info flags
        # is_proxy_self_identity=True). Locking this bucket would lock out every
        # visitor site-wide, admin included, so it must degrade to "no lock" while
        # password checks (and the 401s / rate limiter) keep working normally.
        peer = "172.18.0.1"

        def request():
            return _FakeRequest(peer, {"X-Real-IP": peer})

        # Far more than the 5-failure threshold that would lock a real identity.
        for _ in range(8):
            with self.assertRaises(HTTPException) as cm:
                await self._login(request())
            self.assertEqual(cm.exception.status_code, 401)
            self.assertEqual(cm.exception.detail, "密码错误")

    async def test_proxy_self_identity_still_hits_the_request_rate_limiter(self):
        # Non-lethal lockout must not be confused with "no limiting at all": the plain
        # request-rate limiter (10/min) still applies to the same collapsed identity.
        peer = "172.18.0.2"

        def request():
            return _FakeRequest(peer, {"X-Real-IP": peer})

        for _ in range(10):
            with self.assertRaises(HTTPException) as cm:
                await self._login(request())
            self.assertEqual(cm.exception.status_code, 401)

        with self.assertRaises(HTTPException) as cm:
            await self._login(request())
        self.assertEqual(cm.exception.status_code, 429)
        self.assertIn("频繁", cm.exception.detail)


if __name__ == "__main__":
    unittest.main()
