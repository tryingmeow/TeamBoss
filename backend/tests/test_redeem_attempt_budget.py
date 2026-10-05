"""公开兑换的尝试预算：不消耗的兑换不能被无限重放去打上游。"""

import _isolation  # noqa: F401  must precede any app import
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app import database as app_database
from app.routes import access_tokens

EMAIL = "owner@example.com"
TEAM_A = {"id": "team-a", "name": "Team A", "access_token": "tok-a", "device_id": "dev-a", "proxy_id": None}
TEAM_B = {"id": "team-b", "name": "Team B", "access_token": "tok-b", "device_id": "dev-b", "proxy_id": None}


class RedeemLookupBudgetUnitTest(unittest.TestCase):
    def test_per_code_limit_and_window(self):
        budget = access_tokens._RedeemLookupBudget(per_code=3, per_code_window=3600, global_limit=100, global_window=600)
        now = 1_000.0
        for _ in range(3):
            self.assertIsNone(budget.try_take(1, now))
        self.assertEqual(budget.try_take(1, now), "per_code")
        # Another code is unaffected.
        self.assertIsNone(budget.try_take(2, now))
        # The window slides.
        self.assertIsNone(budget.try_take(1, now + 3601))

    def test_only_two_pick_a_team_prompts_per_code_per_hour_are_refunded(self):
        budget = access_tokens._RedeemLookupBudget(per_code=3, per_code_window=3600, global_limit=1000, global_window=600)
        now = 1_000.0
        outcomes = []
        for i in range(10):
            t = now + i
            if budget.try_take(1, t) is not None:
                outcomes.append("blocked")
                continue
            outcomes.append("refunded" if budget.refund_prompt(1, t, t) else "charged")
        self.assertEqual(outcomes[:5], ["refunded", "refunded", "charged", "charged", "charged"])
        self.assertEqual(outcomes[5:], ["blocked"] * 5)
        # The refund allowance is per code and slides with the window.
        self.assertIsNone(budget.try_take(2, now))
        self.assertTrue(budget.refund_prompt(2, now, now))
        later = now + 3700
        self.assertIsNone(budget.try_take(1, later))
        self.assertTrue(budget.refund_prompt(1, later, later))

    def test_global_limit_across_codes(self):
        budget = access_tokens._RedeemLookupBudget(per_code=10, per_code_window=3600, global_limit=4, global_window=600)
        now = 1_000.0
        for code in range(4):
            self.assertIsNone(budget.try_take(code, now))
        self.assertEqual(budget.try_take(99, now), "global")
        self.assertIsNone(budget.try_take(99, now + 601))

    def test_a_code_stopped_by_its_own_limit_does_not_drain_the_global_budget(self):
        # One code hammered past its limit must not be able to block everyone else.
        budget = access_tokens._RedeemLookupBudget(per_code=2, per_code_window=3600, global_limit=5, global_window=600)
        now = 1_000.0
        self.assertIsNone(budget.try_take(1, now))
        self.assertIsNone(budget.try_take(1, now))
        for _ in range(50):
            self.assertEqual(budget.try_take(1, now), "per_code")
        for code in (2, 3, 4):
            self.assertIsNone(budget.try_take(code, now))

    def test_rejected_by_global_does_not_spend_the_codes_own_budget(self):
        budget = access_tokens._RedeemLookupBudget(per_code=2, per_code_window=3600, global_limit=1, global_window=600)
        now = 1_000.0
        self.assertIsNone(budget.try_take(1, now))
        for _ in range(5):
            self.assertEqual(budget.try_take(2, now), "global")
        # Code 2 never got through, so after the global window it has its full budget.
        self.assertIsNone(budget.try_take(2, now + 601))
        self.assertIsNone(budget.try_take(2, now + 1202))

    def test_refund_returns_the_codes_attempt_but_not_the_global_one(self):
        budget = access_tokens._RedeemLookupBudget(per_code=2, per_code_window=3600, global_limit=3, global_window=600)
        for i in range(3):
            self.assertIsNone(budget.try_take(1, 1_000.0 + i))
            if i < 2:
                budget.refund_code(1, 1_000.0 + i)
        # The code itself still has room, the site-wide cap does not.
        self.assertEqual(budget.try_take(1, 1_010.0), "global")
        self.assertEqual(len(budget._per_code_hits[1]), 1)
        # Refunding an unknown attempt is a no-op.
        budget.refund_code(1, 5.0)
        budget.refund_code(42, 5.0)
        self.assertEqual(len(budget._per_code_hits[1]), 1)

    def test_production_limits(self):
        budget = access_tokens._redeem_lookup_budget
        self.assertEqual((budget.per_code, budget.per_code_window), (10, 3600))
        self.assertEqual((budget.global_limit, budget.global_window), (60, 600))


class RedeemAttemptBudgetRouteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()
        for target, value in (
            ("_check_rate_limit", AsyncMock()),
            ("load_active_teams", AsyncMock(return_value=[TEAM_A])),
            ("log_operation", AsyncMock()),
            (
                "_redeem_lookup_budget",
                access_tokens._RedeemLookupBudget(per_code=6, per_code_window=3600, global_limit=30, global_window=600),
            ),
        ):
            p = patch.object(access_tokens, target, new=value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _make_token(self, raw_token: str) -> int:
        async with app_database.get_db() as db:
            cursor = await db.execute(
                """INSERT INTO access_tokens
                   (token_hash, token_prefix, grant_expires_in, max_uses,
                    used_count, disabled, created_at)
                   VALUES (?, 'atm_bud', '30d', 1, 0, 0, '2026-10-01T00:00:00+00:00')""",
                (access_tokens._hash_token(raw_token),),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def _used_count(self, token_id: int) -> int:
        async with app_database.get_db() as db:
            row = await (
                await db.execute("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,))
            ).fetchone()
        return int(row["used_count"])

    async def _redeem(self, raw_token: str):
        return await access_tokens.redeem_access_token(
            access_tokens.RedeemAccessTokenRequest(email=EMAIL, token=raw_token), Mock()
        )

    async def test_owner_replay_is_capped_per_code_without_spending_the_code(self):
        raw = "atm_budget_owner_replay"
        token_id = await self._make_token(raw)
        owner_hit = [{
            "kind": "member", "team": TEAM_A, "user_id": "u-owner", "is_owner": True,
            "expires_at": None, "cache_updated_at": None,
        }]
        lookup = AsyncMock(return_value=owner_hit)
        with patch.object(access_tokens, "_find_all_memberships", new=lookup):
            for _ in range(6):
                with self.assertRaises(HTTPException) as cm:
                    await self._redeem(raw)
                self.assertEqual(cm.exception.status_code, 409)
            with self.assertRaises(HTTPException) as cm:
                await self._redeem(raw)

        self.assertEqual(cm.exception.status_code, 429)
        self.assertIn("稍后再试", cm.exception.detail)
        self.assertIn("兑换码未使用", cm.exception.detail)
        # The 7th attempt never reached the live upstream lookup ...
        self.assertEqual(lookup.await_count, 6)
        # ... and the code is still unspent and still redeemable later.
        self.assertEqual(await self._used_count(token_id), 0)

    async def test_global_cap_stops_live_lookups_across_codes(self):
        access_tokens._redeem_lookup_budget.global_limit = 2
        tokens = [f"atm_budget_global_{i}" for i in range(3)]
        ids = [await self._make_token(raw) for raw in tokens]
        lookup = AsyncMock(return_value=[])
        no_seat = AsyncMock(side_effect=HTTPException(status_code=409, detail="没有可用 ChatGPT 席位，请联系管理员"))
        with patch.object(access_tokens, "_find_all_memberships", new=lookup), \
             patch.object(access_tokens, "_invite_to_available_team", new=no_seat):
            for raw in tokens[:2]:
                with self.assertRaises(HTTPException) as cm:
                    await self._redeem(raw)
                self.assertEqual(cm.exception.status_code, 409)
            with self.assertRaises(HTTPException) as cm:
                await self._redeem(tokens[2])

        self.assertEqual(cm.exception.status_code, 429)
        self.assertIn("稍后再试", cm.exception.detail)
        self.assertEqual(lookup.await_count, 2)
        for token_id in ids:
            self.assertEqual(await self._used_count(token_id), 0)

    async def test_upstream_failures_do_not_spend_the_codes_attempts(self):
        # A customer retrying through an upstream outage must not lock their own code.
        raw = "atm_budget_upstream_down"
        token_id = await self._make_token(raw)
        down = AsyncMock(side_effect=HTTPException(status_code=503, detail="暂时无法确认全部 Team 的成员状态，请稍后重试"))
        with patch.object(access_tokens, "_find_all_memberships", new=down):
            for _ in range(15):
                with self.assertRaises(HTTPException) as cm:
                    await self._redeem(raw)
                self.assertEqual(cm.exception.status_code, 503)
        self.assertEqual(down.await_count, 15)
        self.assertEqual(await self._used_count(token_id), 0)
        self.assertEqual(access_tokens._redeem_lookup_budget._per_code_hits[token_id], [])
        # Every one of them still hit the upstream, so the site-wide cap counts them.
        self.assertEqual(len(access_tokens._redeem_lookup_budget._global_hits), 15)

    async def test_unexpected_server_errors_do_not_spend_the_codes_attempts(self):
        raw = "atm_budget_server_error"
        token_id = await self._make_token(raw)
        broken = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(access_tokens, "_find_all_memberships", new=broken):
            for _ in range(8):
                with self.assertRaises(RuntimeError):
                    await self._redeem(raw)
        self.assertEqual(access_tokens._redeem_lookup_budget._per_code_hits[token_id], [])
        self.assertEqual(await self._used_count(token_id), 0)

    async def test_only_two_team_prompts_per_hour_are_free_then_the_code_limit_stops_replay(self):
        raw = "atm_budget_pick_team"
        token_id = await self._make_token(raw)
        hits = [
            {"kind": "member", "team": team, "user_id": "u-1", "is_owner": False,
             "expires_at": "2026-12-01T00:00:00+00:00", "cache_updated_at": None}
            for team in (TEAM_A, TEAM_B)
        ]
        lookup = AsyncMock(return_value=hits)
        with patch.object(access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])), \
             patch.object(access_tokens, "_find_all_memberships", new=lookup):
            # per_code=6: two free prompts, then six charged ones.
            for _ in range(8):
                result = await self._redeem(raw)
                self.assertEqual(result["status"], "team_selection_required")
            with self.assertRaises(HTTPException) as cm:
                await self._redeem(raw)
        self.assertEqual(cm.exception.status_code, 429)
        self.assertIn("兑换码未使用", cm.exception.detail)
        self.assertEqual(lookup.await_count, 8)
        self.assertEqual(len(access_tokens._redeem_lookup_budget._per_code_hits[token_id]), 6)
        self.assertEqual(await self._used_count(token_id), 0)

    async def test_refused_attempts_still_count_against_the_code(self):
        # Outcomes the customer cannot fix by retrying (here: no free seat) keep
        # counting, so a valid code still cannot be replayed without limit.
        raw = "atm_budget_no_seat"
        token_id = await self._make_token(raw)
        access_tokens._redeem_lookup_budget.per_code = 10
        no_seat = AsyncMock(side_effect=HTTPException(status_code=409, detail="没有可用 ChatGPT 席位，请联系管理员"))
        with patch.object(access_tokens, "_find_all_memberships", new=AsyncMock(return_value=[])), \
             patch.object(access_tokens, "_invite_to_available_team", new=no_seat):
            for _ in range(10):
                with self.assertRaises(HTTPException) as cm:
                    await self._redeem(raw)
                self.assertEqual(cm.exception.status_code, 409)
            with self.assertRaises(HTTPException) as cm:
                await self._redeem(raw)
        self.assertEqual(cm.exception.status_code, 429)
        self.assertEqual(no_seat.await_count, 10)
        self.assertEqual(await self._used_count(token_id), 0)

    async def test_invalid_codes_do_not_touch_the_budget(self):
        for _ in range(40):
            with self.assertRaises(HTTPException) as cm:
                await self._redeem("atm_does_not_exist")
            self.assertEqual(cm.exception.status_code, 401)
        self.assertEqual(access_tokens._redeem_lookup_budget._global_hits, [])


if __name__ == "__main__":
    unittest.main()
