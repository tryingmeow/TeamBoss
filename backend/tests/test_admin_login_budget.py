import _isolation  # noqa: F401  must precede any app import
import asyncio
import time
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database, security
from app.client_ip import rate_limit_key
from app.routes import admin


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    def __init__(self, peer: str, headers: dict | None = None):
        self.client = _FakeClient(peer)
        self.headers = headers or {}


def _nginx_request(client_ip: str) -> _FakeRequest:
    """What production Nginx forwards to 127.0.0.1:18087 for a visitor."""
    return _FakeRequest(
        "127.0.0.1",
        {
            "X-Real-IP": client_ip,
            "X-Forwarded-For": client_ip,
            "X-Forwarded-Proto": "https",
        },
    )


class AdminLoginBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for target, value in (
            ("log_operation", AsyncMock()),
            ("verify_admin_password", AsyncMock(return_value=False)),
        ):
            p = patch.object(admin, target, new=value)
            p.start()
            self.addCleanup(p.stop)
        # Fresh singletons for every test: the login limiter, the per-identity
        # tracker and the site-wide budget are module-level state.
        for name, value in (
            ("_limiter_login", admin._RateLimiter(max_requests=10, window_seconds=60)),
            ("_failure_tracker", admin._FailureTracker(max_failures=5, lockout_seconds=900)),
            (
                "_global_login_cooldown",
                admin._SharedIdentityCooldown(max_free_failures=10, initial_cooldown=60, max_cooldown=900),
            ),
        ):
            p = patch.object(admin, name, new=value)
            p.start()
            self.addCleanup(p.stop)

    async def _login(self, request, password="wrong"):
        return await admin.login(admin.AdminLoginRequest(password=password), request)

    async def _expect(self, request, code, password="wrong"):
        with self.assertRaises(HTTPException) as cm:
            await self._login(request, password)
        self.assertEqual(cm.exception.status_code, code)
        return cm.exception

    def test_ipv6_addresses_share_their_slash_64(self):
        self.assertEqual(rate_limit_key("2001:db8:1:2::1"), "2001:db8:1:2::/64")
        self.assertEqual(rate_limit_key("2001:db8:1:2:ffff:ffff:ffff:ffff"), "2001:db8:1:2::/64")
        self.assertNotEqual(rate_limit_key("2001:db8:1:3::1"), "2001:db8:1:2::/64")
        self.assertEqual(rate_limit_key("198.51.100.9"), "198.51.100.9")
        self.assertEqual(rate_limit_key("unknown"), "unknown")

    async def test_rotating_addresses_inside_one_ipv6_slash_64_still_locks(self):
        # Reported reproduction: 40 wrong guesses from distinct addresses of one /64
        # never locked. Now the /64 is one identity: 4 x 401, then the lock.
        for i in range(1, 5):
            await self._expect(_nginx_request(f"2001:db8:1:2::{i:x}"), 401)
        await self._expect(_nginx_request("2001:db8:1:2::5"), 429)
        for i in range(6, 41):
            await self._expect(_nginx_request(f"2001:db8:1:2::{i:x}"), 429)
        self.assertEqual(admin.verify_admin_password.await_count, 5)

    async def test_global_budget_applies_across_many_source_addresses(self):
        # Every address is fresh, so the per-address lockout never triggers. The
        # site-wide budget still allows only 10 free failures, then cools down.
        with patch("app.routes.admin.time") as fake_time:
            fake_time.time.return_value = 5_000_000.0
            for i in range(10):
                await self._expect(_nginx_request(f"198.51.100.{i + 1}"), 401)
            # 11th failure is still checked (401) and arms the 60s cooldown.
            await self._expect(_nginx_request("198.51.100.11"), 401)
            self.assertEqual(admin.verify_admin_password.await_count, 11)

            # A brand-new address, even with the right password, is refused without
            # the password being checked while the cooldown runs.
            exc = await self._expect(_nginx_request("203.0.113.200"), 429, password="right")
            self.assertIn("稍后再试", exc.detail)
            self.assertEqual(admin.verify_admin_password.await_count, 11)

            # After the cooldown one more guess is checked and the next cooldown doubles.
            fake_time.time.return_value = 5_000_000.0 + 61
            await self._expect(_nginx_request("198.51.100.50"), 401)
            fake_time.time.return_value = 5_000_000.0 + 61 + 100
            await self._expect(_nginx_request("198.51.100.51"), 429)
            fake_time.time.return_value = 5_000_000.0 + 61 + 121
            await self._expect(_nginx_request("198.51.100.52"), 401)

    async def test_successful_login_does_not_refill_the_global_budget(self):
        with patch("app.routes.admin.time") as fake_time:
            fake_time.time.return_value = 6_000_000.0
            for i in range(10):
                await self._expect(_nginx_request(f"198.51.100.{i + 1}"), 401)
            with patch.object(admin, "verify_admin_password", new=AsyncMock(return_value=True)), \
                 patch.object(admin, "get_admin_api_key", new=AsyncMock(return_value="atk_fake")):
                result = await self._login(_nginx_request("192.0.2.1"), password="right")
            self.assertEqual(result["status"], "ok")
            # The admin's success must not hand the attacker 10 fresh free guesses.
            await self._expect(_nginx_request("198.51.100.30"), 401)
            await self._expect(_nginx_request("198.51.100.31"), 429)

    async def test_concurrent_guesses_cannot_outrun_the_budget(self):
        # Without serialization a burst of parallel requests all pass the checks
        # before any failure is recorded. Only 11 may reach the password check.
        async def slow_wrong(_password):
            await asyncio.sleep(0.01)
            return False

        with patch.object(admin, "verify_admin_password", new=AsyncMock(side_effect=slow_wrong)):
            results = await asyncio.gather(
                *(self._login(_nginx_request(f"198.51.{i // 200}.{i % 200 + 1}")) for i in range(60)),
                return_exceptions=True,
            )
            checked = admin.verify_admin_password.await_count
        codes = [r.status_code for r in results if isinstance(r, HTTPException)]
        self.assertEqual(len(codes), 60)
        self.assertEqual(checked, 11)
        self.assertEqual(codes.count(401), 11)
        self.assertEqual(codes.count(429), 49)


class AdminApiKeyDuringCooldownTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.init_database()
        async with database.get_db() as db:
            await db.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES ('admin_api_key', ?, 'x')
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                ("atk_budget_test_key_123",),
            )
            await db.commit()
        cooldown = admin._SharedIdentityCooldown(max_free_failures=0, initial_cooldown=900, max_cooldown=900)
        p = patch.object(admin, "_global_login_cooldown", new=cooldown)
        p.start()
        self.addCleanup(p.stop)

    async def test_stored_api_key_still_authenticates_while_password_login_cools_down(self):
        admin._global_login_cooldown.record_failure(time.time())
        with patch.object(admin, "log_operation", new=AsyncMock()):
            with self.assertRaises(HTTPException) as cm:
                await admin.login(admin.AdminLoginRequest(password="x"), _nginx_request("192.0.2.9"))
        self.assertEqual(cm.exception.status_code, 429)

        # The panel's stored key goes through require_admin, which the login budget
        # never touches.
        await security.require_admin(authorization="Bearer atk_budget_test_key_123", x_api_key=None)
        await security.require_admin(authorization=None, x_api_key="atk_budget_test_key_123")
        account = await admin.get_admin_account()
        self.assertEqual(account["api_key"], "atk_budget_test_key_123")


if __name__ == "__main__":
    unittest.main()
