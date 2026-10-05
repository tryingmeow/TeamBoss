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
                admin._GlobalLoginBudget(max_free_failures=10, initial_cooldown=60, max_cooldown=900),
            ),
        ):
            p = patch.object(admin, name, new=value)
            p.start()
            self.addCleanup(p.stop)
        await database.init_database()
        async with database.get_db() as db:
            await db.execute("DELETE FROM admin_login_trusted_sources")
            await db.commit()

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


    async def _succeed(self, request):
        with patch.object(admin, "verify_admin_password", new=AsyncMock(return_value=True)), \
             patch.object(admin, "get_admin_api_key", new=AsyncMock(return_value="atk_fake")):
            return await self._login(request, password="right")

    async def test_one_source_cannot_keep_password_login_closed(self):
        # Reported reproduction: one address sending a wrong password every time its
        # lock expired kept the site-wide cooldown armed for hours. A source now
        # charges at most 5 failures to the site-wide budget, which takes more than 10.
        clock = [7_000_000.0]
        with patch("app.routes.admin.time") as admin_time, patch("app.client_ip.time") as limiter_time:
            admin_time.time.side_effect = limiter_time.time.side_effect = lambda: clock[0]
            for _ in range(6 * 4):
                checked = admin.verify_admin_password.await_count
                with self.assertRaises(HTTPException):
                    await self._login(_nginx_request("203.0.113.7"))
                self.assertEqual(admin.verify_admin_password.await_count, checked + 1)
                clock[0] += 901  # just past the per-address lock
                result = await self._succeed(_nginx_request(f"198.51.100.{int(clock[0]) % 200 + 1}"))
                self.assertEqual(result["status"], "ok")
            self.assertFalse(admin._global_login_cooldown.is_active(clock[0]))

    async def test_two_sources_cannot_trigger_the_cooldown_but_three_can(self):
        with patch("app.routes.admin.time") as fake_time:
            fake_time.time.return_value = 7_100_000.0
            for ip in ("203.0.113.1", "203.0.113.2"):
                for _ in range(4):
                    await self._expect(_nginx_request(ip), 401)
                await self._expect(_nginx_request(ip), 429)  # per-address lock on the 5th
            self.assertFalse(admin._global_login_cooldown.is_active(7_100_000.0))
            await self._expect(_nginx_request("203.0.113.3"), 401)
            # 11 charged failures: a never-seen device must wait.
            await self._expect(_nginx_request("192.0.2.77"), 429, password="right")

    async def test_known_source_bypasses_the_cooldown_but_not_its_own_lock(self):
        with patch("app.routes.admin.time") as fake_time:
            fake_time.time.return_value = 7_200_000.0
            await self._succeed(_nginx_request("2001:db8:aa:1::5"))
            for i in range(11):
                await self._expect(_nginx_request(f"198.51.100.{i + 1}"), 401)
            self.assertTrue(admin._global_login_cooldown.is_active(7_200_000.0))

            # Same /64, different address: still the owner's known source.
            result = await self._succeed(_nginx_request("2001:db8:aa:1::9"))
            self.assertEqual(result["status"], "ok")
            # A stranger is still refused without a password check.
            await self._expect(_nginx_request("192.0.2.5"), 429, password="right")

            # The known source keeps its own per-source lock.
            for _ in range(4):
                await self._expect(_nginx_request("2001:db8:aa:1::9"), 401)
            await self._expect(_nginx_request("2001:db8:aa:1::9"), 429)
            with patch.object(admin, "verify_admin_password", new=AsyncMock(return_value=True)):
                await self._expect(_nginx_request("2001:db8:aa:1::9"), 429, password="right")

    async def test_known_source_expires_after_90_days(self):
        with patch("app.routes.admin.time") as fake_time:
            start = 8_000_000.0
            fake_time.time.return_value = start
            await self._succeed(_nginx_request("192.0.2.10"))
            later = start + admin.TRUSTED_LOGIN_SOURCE_TTL_SECONDS + 1
            fake_time.time.return_value = later
            for i in range(11):
                await self._expect(_nginx_request(f"198.51.100.{i + 1}"), 401)
            await self._expect(_nginx_request("192.0.2.10"), 429, password="right")

    async def test_known_source_list_is_bounded(self):
        with patch("app.routes.admin.time") as fake_time:
            for i in range(admin.TRUSTED_LOGIN_SOURCE_MAX + 5):
                fake_time.time.return_value = 9_000_000.0 + i
                await self._succeed(_nginx_request(f"192.0.2.{i + 1}"))
        async with database.get_db() as db:
            cursor = await db.execute("SELECT source FROM admin_login_trusted_sources")
            sources = {row["source"] for row in await cursor.fetchall()}
        self.assertEqual(len(sources), admin.TRUSTED_LOGIN_SOURCE_MAX)
        self.assertNotIn("192.0.2.1", sources)
        self.assertIn(f"192.0.2.{admin.TRUSTED_LOGIN_SOURCE_MAX + 5}", sources)

    async def test_shared_proxy_identity_is_never_remembered(self):
        # Collapsed deployments see every visitor as the proxy address; trusting it
        # would exempt everyone from the site-wide budget.
        await self._succeed(_FakeRequest("172.18.0.9", {"X-Real-IP": "172.18.0.9"}))
        async with database.get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM admin_login_trusted_sources")
            self.assertEqual((await cursor.fetchone())["n"], 0)

    async def test_sources_past_their_charge_still_rearm_the_cooldown(self):
        # Many sources that have used up their 5 charges must not each get free
        # guesses: every failure during the cooldown regime re-arms the cooldown.
        budget = admin._global_login_cooldown
        clock = [7_300_000.0]
        with patch("app.routes.admin.time") as admin_time, patch("app.client_ip.time") as limiter_time:
            admin_time.time.side_effect = limiter_time.time.side_effect = lambda: clock[0]
            for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
                for attempt in range(5):
                    clock[0] = max(clock[0], budget.cooldown_until) + 1
                    await self._expect(_nginx_request(ip), 429 if attempt == 4 else 401)
            self.assertEqual(budget.count, 15)
            self.assertEqual(admin.verify_admin_password.await_count, 15)

            clock[0] = budget.cooldown_until + 1
            # A source with no charges left (its per-source lock has expired too).
            budget.charged_by_source["203.0.113.50"] = budget.per_source_charge
            await self._expect(_nginx_request("203.0.113.50"), 401)
            self.assertEqual(budget.count, 15)
            await self._expect(_nginx_request("192.0.2.88"), 429, password="right")
            self.assertEqual(admin.verify_admin_password.await_count, 16)

    async def test_budget_resets_an_hour_after_the_last_charged_failure(self):
        budget = admin._global_login_cooldown
        with patch("app.routes.admin.time") as fake_time:
            now = 7_400_000.0
            fake_time.time.return_value = now
            for i in range(11):
                await self._expect(_nginx_request(f"198.51.100.{i + 1}"), 401)
            # One already-charged source keeps failing inside the hour: each failure
            # re-arms the cooldown but does not extend the round.
            budget.charged_by_source["203.0.113.60"] = budget.per_source_charge
            for offset in (901, 1802, 2703, 3500):
                fake_time.time.return_value = now + offset
                await self._expect(_nginx_request("203.0.113.60"), 401)
                self.assertTrue(budget.is_active(now + offset + 1))
            self.assertEqual(budget.count, 11)

            # An hour after the last charged failure the round is over: a new failure
            # starts from zero instead of re-arming the cooldown.
            fake_time.time.return_value = now + 3700
            await self._expect(_nginx_request("198.51.100.200"), 401)
            self.assertEqual(budget.count, 1)
            self.assertFalse(budget.is_active(now + 3700))
            result = await self._succeed(_nginx_request("192.0.2.99"))
            self.assertEqual(result["status"], "ok")


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
        cooldown = admin._GlobalLoginBudget(max_free_failures=0, initial_cooldown=900, max_cooldown=900)
        p = patch.object(admin, "_global_login_cooldown", new=cooldown)
        p.start()
        self.addCleanup(p.stop)

    async def test_stored_api_key_still_authenticates_while_password_login_cools_down(self):
        admin._global_login_cooldown.record_failure("198.51.100.1", time.time())
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
