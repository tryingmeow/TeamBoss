"""What an invite's outcome does to the code and to the redemption row.

Once the invite request may have reached OpenAI the code stays spent; only an
explicit rejection returns it (and moves on to the next Team). An uncertain
result that the live snapshot confirms counts as success. An invite whose
result never got recorded becomes 'uncertain' together with its patrol barrier,
in one transaction, for the admin to settle.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _redemption_fixtures import CREATED, EMAIL, RedeemFlowCase

from app import database as app_database
from app.routes import access_tokens
from app.utils.durations import utc_now


class _StubClient:
    async def invite_member(self, *a, **k):  # pragma: no cover - run_chatgpt_call 已被替换
        raise AssertionError("real invite must not be called in tests")


# ── 请求发出即算消耗，只有明确拒绝才退码 ─────────────────────────────────────

class ConsumptionMarkedBeforeMutationTest(unittest.IsolatedAsyncioTestCase):
    def _patches(self, invite_result):
        @asynccontextmanager
        async def _lock(_team_id):
            yield

        return (
            patch.object(access_tokens, "team_invite_lock", _lock),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: _StubClient()),
            patch.object(
                access_tokens, "_chatgpt_available", new=AsyncMock(return_value=(True, "ok"))
            ),
            patch.object(access_tokens, "_set_token_use_phase", new=AsyncMock()),
            patch.object(access_tokens, "run_chatgpt_call", new=AsyncMock(side_effect=invite_result)),
        )

    async def _run(self, invite_result, expected_exc):
        consumption = access_tokens._TokenConsumption()
        teams = [{"id": "team-1", "name": "T1", "access_token": "t", "device_id": "d", "proxy_id": None}]
        patches = self._patches(invite_result)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(expected_exc):
                await access_tokens._invite_to_available_team(
                    "user@example.com",
                    "30d",
                    teams,
                    token_use_id=1,
                    on_invite_confirmed=consumption.confirm,
                    on_invite_rejected=consumption.revert_for_rejected,
                )
        finally:
            for p in patches:
                p.stop()
        return consumption

    async def test_network_failure_after_request_keeps_the_code_consumed(self):
        consumption = await self._run(RuntimeError("connection reset"), RuntimeError)
        self.assertTrue(consumption.confirmed)

    async def test_cancellation_after_request_keeps_the_code_consumed(self):
        consumption = await self._run(asyncio.CancelledError(), asyncio.CancelledError)
        self.assertTrue(consumption.confirmed)

    async def test_explicit_rejection_returns_the_code(self):
        consumption = access_tokens._TokenConsumption()
        teams = [{"id": "team-1", "name": "T1", "access_token": "t", "device_id": "d", "proxy_id": None}]
        patches = self._patches([{"error": "seat limit", "_mutation_status": "rejected"}])
        for p in patches:
            p.start()
        try:
            with (
                patch.object(access_tokens, "log_operation", new=AsyncMock()),
                self.assertRaises(HTTPException),
            ):
                await access_tokens._invite_to_available_team(
                    "user@example.com",
                    "30d",
                    teams,
                    token_use_id=1,
                    on_invite_confirmed=consumption.confirm,
                    on_invite_rejected=consumption.revert_for_rejected,
                )
        finally:
            for p in patches:
                p.stop()
        self.assertFalse(consumption.confirmed)


# ── 不确定的邀请不再被当成明确拒绝 ──────────────────────────────────────────

class UncertainIsNotRejectedTest(unittest.IsolatedAsyncioTestCase):
    """``ChatGPTClient._error()`` 给每一种结果都塞了 ``error`` 键。

    原先的 ``if mutation_status == "rejected" or "error" in result`` 对所有不确定
    结果恒为真：超时后"重新拉快照、看到人就升级成 confirmed"的补救被架空，一次
    超时但其实成功的邀请会被退码并换下一个 Team 再发一次（一码两座）。
    """

    def _patches(self, invite_result, snapshot):
        @asynccontextmanager
        async def _lock(_team_id):
            yield

        return [
            patch.object(access_tokens, "team_invite_lock", _lock),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: _StubClient()),
            patch.object(
                access_tokens, "_chatgpt_available", new=AsyncMock(return_value=(True, "ok"))
            ),
            patch.object(access_tokens, "_set_token_use_phase", new=AsyncMock()),
            patch.object(
                access_tokens, "_lock_uncertain_with_barrier", new=AsyncMock(return_value=True)
            ),
            patch.object(
                access_tokens, "run_chatgpt_call", new=AsyncMock(return_value=invite_result)
            ),
            patch.object(
                access_tokens, "fetch_and_cache_members", new=AsyncMock(return_value=snapshot)
            ),
            patch.object(
                access_tokens,
                "record_confirmed_invite_extension",
                new=AsyncMock(return_value="2026-10-11T00:00:00+00:00"),
            ),
            patch.object(access_tokens, "log_operation", new=AsyncMock()),
            patch.object(access_tokens, "add_member_watch", new=AsyncMock()),
            patch.object(access_tokens, "reserve_default_seat", new=AsyncMock()),
            patch.object(access_tokens, "notify_member_event", new=AsyncMock()),
        ]

    async def _run(self, invite_result, snapshot, teams=None):
        consumption = access_tokens._TokenConsumption()
        teams = teams or [
            {"id": "team-1", "name": "T1", "access_token": "t", "device_id": "d", "proxy_id": None},
        ]
        patches = self._patches(invite_result, snapshot)
        for p in patches:
            p.start()
        try:
            result = await access_tokens._invite_to_available_team(
                "user@example.com",
                "30d",
                teams,
                token_use_id=1,
                on_invite_confirmed=consumption.confirm,
                on_invite_rejected=consumption.revert_for_rejected,
            )
        finally:
            for p in patches:
                p.stop()
        return result, consumption

    async def test_uncertain_but_visible_in_snapshot_completes_and_keeps_code_spent(self):
        result, consumption = await self._run(
            {"error": "read timeout", "_mutation_status": "uncertain"},
            {"members": [{"email": "user@example.com"}], "pending_invites": []},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["team"]["id"], "team-1")
        self.assertTrue(consumption.confirmed)

    async def test_confirmed_body_carrying_an_error_field_is_not_a_rejection(self):
        """2xx 响应体里恰好带 error 字段，但分类是 confirmed：不是拒绝。"""
        result, consumption = await self._run(
            {"ok": True, "error": None, "_mutation_status": "confirmed"},
            {"members": [{"email": "user@example.com"}], "pending_invites": []},
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(consumption.confirmed)

    async def test_explicit_rejection_still_reverts_and_moves_on(self):
        teams = [
            {"id": "team-1", "name": "T1", "access_token": "t", "device_id": "d", "proxy_id": None},
            {"id": "team-2", "name": "T2", "access_token": "t", "device_id": "d", "proxy_id": None},
        ]
        calls = []

        consumption = access_tokens._TokenConsumption()
        reverts = []
        original_revert = consumption.revert_for_rejected

        def _revert():
            reverts.append(True)
            original_revert()

        async def _invite(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                return {"error": "seat limit", "_mutation_status": "rejected"}
            return {"ok": True, "_mutation_status": "confirmed"}

        @asynccontextmanager
        async def _lock(_team_id):
            yield

        patches = [
            patch.object(access_tokens, "team_invite_lock", _lock),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: _StubClient()),
            patch.object(
                access_tokens, "_chatgpt_available", new=AsyncMock(return_value=(True, "ok"))
            ),
            patch.object(access_tokens, "_set_token_use_phase", new=AsyncMock()),
            patch.object(access_tokens, "run_chatgpt_call", new=AsyncMock(side_effect=_invite)),
            patch.object(
                access_tokens,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [{"email": "user@example.com"}],
                                            "pending_invites": []}),
            ),
            patch.object(
                access_tokens,
                "record_confirmed_invite_extension",
                new=AsyncMock(return_value="2026-10-11T00:00:00+00:00"),
            ),
            patch.object(access_tokens, "log_operation", new=AsyncMock()),
            patch.object(access_tokens, "add_member_watch", new=AsyncMock()),
            patch.object(access_tokens, "reserve_default_seat", new=AsyncMock()),
            patch.object(access_tokens, "notify_member_event", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            result = await access_tokens._invite_to_available_team(
                "user@example.com",
                "30d",
                teams,
                token_use_id=1,
                on_invite_confirmed=consumption.confirm,
                on_invite_rejected=_revert,
            )
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(result["team"]["id"], "team-2")
        self.assertEqual(len(reverts), 1)
        self.assertTrue(consumption.confirmed)  # 第二个 team 重新标记为已消耗


# ── 停服打断的邀请不能永远卡在 pending ───────────────────────────────────────

class InterruptedInviteBecomesUncertainTest(RedeemFlowCase):
    """进程在邀请请求途中被杀，留下 action='invite_pending', result='pending'。

    对账任务看不见这个人时原先永远 waiting：管理员列表只列 uncertain，码和邮箱占用
    永远锁死，巡逻也没有屏障挡着。超过安全时限后应转成 uncertain 并立屏障。
    """

    async def _stuck_use(self, *, age_minutes, team_id="team-a"):
        from datetime import timedelta

        from app.utils.durations import utc_now

        token_id = await self._exec(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at, last_used_at)
               VALUES (?, 'atm_stuck', '30d', 1, 1, 0, ?, ?)""",
            (access_tokens._hash_token(f"atm_stuck_{age_minutes}_{team_id}"), CREATED, CREATED),
        )
        created = (utc_now() - timedelta(minutes=age_minutes)).isoformat()
        use_id = int(await self._exec(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                error_message, created_at)
               VALUES (?, ?, 'invite_pending', ?, NULL, NULL, 'pending', NULL, ?)""",
            (token_id, EMAIL, team_id, created),
        ))
        await self._exec(
            "INSERT INTO redemption_email_claims (email, token_use_id, created_at) VALUES (?, ?, ?)",
            (EMAIL, use_id, created),
        )
        return token_id, use_id

    async def _use(self, use_id):
        return (await self._rows("SELECT * FROM access_token_uses WHERE id = ?", (use_id,)))[0]

    async def _barriers(self, use_id):
        return await self._rows(
            "SELECT * FROM pending_invite_reconciliations WHERE token_use_id = ?", (use_id,)
        )

    async def _assert_locked_uncertain(self, token_id, use_id, team_id="team-a"):
        use = await self._use(use_id)
        self.assertEqual(
            (use["action"], use["result"], use["team_id"]),
            ("invite_pending", "uncertain", team_id),
        )
        barriers = await self._barriers(use_id)
        self.assertEqual(len(barriers), 1)
        barrier = barriers[0]
        self.assertEqual(
            (barrier["team_id"], barrier["email"], barrier["kind"], barrier["source"],
             barrier["expires_at"], barrier["resolved"]),
            (team_id, EMAIL, "barrier", "self_service", None, 0),
        )
        # 不退码、不放邮箱、不给时长。
        self.assertEqual(await self._used_count(token_id), 1)
        claims = await self._rows("SELECT token_use_id FROM redemption_email_claims")
        self.assertEqual([c["token_use_id"] for c in claims], [use_id])
        self.assertEqual(await self._rows("SELECT * FROM member_expiry"), [])
        # 管理员收尾列表里看得到它。
        listed = await access_tokens.list_pending_confirmations()
        self.assertEqual([row["id"] for row in listed], [use_id])

    async def test_stale_interrupted_invite_is_locked_as_uncertain_for_the_admin(self):
        await self._team("team-a")
        token_id, use_id = await self._stuck_use(age_minutes=20)

        counts = await access_tokens.reconcile_pending_redemptions()

        await self._assert_locked_uncertain(token_id, use_id)
        self.assertEqual(counts["uncertain"], 1)
        logged = [c.args[1] for c in access_tokens.log_operation.await_args_list]
        self.assertIn("self_service_invite_interrupted", logged)

    async def test_conversion_is_idempotent(self):
        await self._team("team-a")
        token_id, use_id = await self._stuck_use(age_minutes=20)

        await access_tokens.reconcile_pending_redemptions()
        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(counts["uncertain"], 0)
        await self._assert_locked_uncertain(token_id, use_id)

    async def test_original_team_unreachable_still_reaches_the_admin_list(self):
        await self._team("team-a", status="token_expired")
        self.broken.add("team-a")
        token_id, use_id = await self._stuck_use(age_minutes=20)

        await access_tokens.reconcile_pending_redemptions()

        await self._assert_locked_uncertain(token_id, use_id)

    async def test_recent_invite_is_left_alone(self):
        await self._team("team-a")
        _, use_id = await self._stuck_use(age_minutes=3)

        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual((counts["waiting"], counts["uncertain"]), (1, 0))
        self.assertEqual((await self._use(use_id))["result"], "pending")
        self.assertEqual(await self._barriers(use_id), [])

    async def test_visible_member_is_confirmed_first_not_locked(self):
        await self._team("team-a")
        _, use_id = await self._stuck_use(age_minutes=20)
        self.live["team-a"] = {"members": [], "pending_invites": [{"email": EMAIL}]}

        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual((counts["confirmed"], counts["uncertain"]), (1, 0))
        self.assertEqual((await self._use(use_id))["result"], "success")
        self.assertEqual(await self._barriers(use_id), [])

    async def test_redemption_still_running_in_this_process_is_not_touched(self):
        await self._team("team-a")
        _, use_id = await self._stuck_use(age_minutes=20)
        access_tokens._inflight_token_uses.add(use_id)
        self.addCleanup(access_tokens._inflight_token_uses.discard, use_id)

        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual((counts["waiting"], counts["uncertain"]), (1, 0))
        self.assertEqual((await self._use(use_id))["result"], "pending")
        self.assertEqual(await self._barriers(use_id), [])

    async def test_a_redemption_finishing_concurrently_wins(self):
        # 对账读完 pending 之后、转换之前，原兑换刚好落了终态：不能被改写，也不能
        # 留下一道永远不会撤的屏障（那会让巡逻和自动踢人永远跳过这个人）。
        await self._team("team-a")
        _, use_id = await self._stuck_use(age_minutes=20)

        async def finish_then_absent(team_id, client):
            await self._exec(
                "UPDATE access_token_uses SET result = 'success' WHERE id = ?", (use_id,)
            )
            return {"members": [], "pending_invites": []}

        with patch.object(access_tokens, "fetch_and_cache_members", new=finish_then_absent):
            counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(counts["uncertain"], 0)
        self.assertEqual((await self._use(use_id))["result"], "success")
        self.assertEqual(await self._barriers(use_id), [])

    async def test_redeem_flow_registers_and_clears_its_attempt(self):
        await self._team("team-a")
        await self._token("atm_inflight_seen")
        seen = []

        async def fake_run(func, *args, **kwargs):
            seen.append(set(access_tokens._inflight_token_uses))
            return {}

        with patch.object(access_tokens, "run_chatgpt_call", new=fake_run):
            result = await self._redeem("atm_inflight_seen")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(access_tokens._inflight_token_uses, set())


# ── "Uncertain" and its patrol barrier land together or not at all ────────

class _CommitHookConnection:
    """Delegates to a real connection and runs ``after_commit`` after each commit."""

    def __init__(self, db, after_commit):
        self._db = db
        self._after_commit = after_commit

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def commit(self):
        await self._db.commit()
        await self._after_commit()


class UncertainLockIsAtomicTest(RedeemFlowCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._team("team-a")
        self.invite_result = {"error": "read timeout", "_mutation_status": "uncertain"}

    async def _stuck_use(self, *, age_minutes=20):
        token_id = await self._exec(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at, last_used_at)
               VALUES (?, 'atm_stuck', '30d', 1, 1, 0, ?, ?)""",
            (access_tokens._hash_token("atm_g1_stuck"), CREATED, CREATED),
        )
        created = (utc_now() - timedelta(minutes=age_minutes)).isoformat()
        use_id = int(await self._exec(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                error_message, created_at)
               VALUES (?, ?, 'invite_pending', 'team-a', NULL, NULL, 'pending', NULL, ?)""",
            (token_id, EMAIL, created),
        ))
        await self._exec(
            "INSERT INTO redemption_email_claims (email, token_use_id, created_at) VALUES (?, ?, ?)",
            (EMAIL, use_id, created),
        )
        return token_id, use_id

    async def _use(self, use_id):
        return (await self._rows("SELECT * FROM access_token_uses WHERE id = ?", (use_id,)))[0]

    async def _barriers(self, use_id, *, unresolved_only=False):
        sql = "SELECT * FROM pending_invite_reconciliations WHERE token_use_id = ?"
        if unresolved_only:
            sql += " AND resolved = 0"
        return await self._rows(sql, (use_id,))

    async def _fail_barrier_writes(self):
        await self._exec(
            """CREATE TRIGGER g1_fail_barrier BEFORE INSERT ON pending_invite_reconciliations
               BEGIN SELECT RAISE(ABORT, 'barrier write failed'); END"""
        )

    async def _allow_barrier_writes(self):
        await self._exec("DROP TRIGGER g1_fail_barrier")

    def _admin_settles_as_soon_as_uncertain_is_visible(self):
        """Interleave an admin 'confirm success' right after the first commit that
        makes an uncertain use visible to other connections."""
        fired: list[int] = []
        real_get_db = app_database.get_db

        async def after_commit():
            if fired:
                return
            async with real_get_db() as db:
                cursor = await db.execute(
                    "SELECT id FROM access_token_uses WHERE result = 'uncertain'"
                )
                row = await cursor.fetchone()
            if row is None:
                return
            fired.append(int(row["id"]))
            await access_tokens.resolve_pending_confirmation(
                int(row["id"]),
                access_tokens.ResolvePendingConfirmationRequest(outcome="success"),
            )

        @asynccontextmanager
        async def hooked_get_db():
            async with real_get_db() as db:
                yield _CommitHookConnection(db, after_commit)

        p = patch.object(access_tokens, "get_db", hooked_get_db)
        p.start()
        self.addCleanup(p.stop)
        return fired

    async def test_interrupted_invite_failed_barrier_write_leaves_the_use_pending(self):
        token_id, use_id = await self._stuck_use()
        await self._fail_barrier_writes()

        with self.assertLogs(access_tokens.logger, "ERROR"):
            counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual((counts["uncertain"], counts["waiting"]), (0, 1))
        self.assertEqual((await self._use(use_id))["result"], "pending")
        self.assertEqual(await self._barriers(use_id), [])
        self.assertEqual(await self._used_count(token_id), 1)

        # The next pass converts it, with both writes present.
        await self._allow_barrier_writes()
        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual((await self._use(use_id))["result"], "uncertain")
        [barrier] = await self._barriers(use_id)
        self.assertEqual(
            (barrier["kind"], barrier["resolved"], barrier["expires_at"], barrier["team_id"]),
            ("barrier", 0, None, "team-a"),
        )

    async def test_interrupted_invite_settled_in_between_leaves_no_open_barrier(self):
        _, use_id = await self._stuck_use()
        fired = self._admin_settles_as_soon_as_uncertain_is_visible()

        await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(fired, [use_id])
        self.assertEqual((await self._use(use_id))["result"], "success")
        self.assertEqual(await self._barriers(use_id, unresolved_only=True), [])

    async def test_live_uncertain_invite_failed_barrier_write_leaves_the_use_pending(self):
        token_id = await self._token("atm_g1_live_fail")
        await self._fail_barrier_writes()

        with self.assertLogs(access_tokens.logger, "ERROR"):
            result = await self._redeem("atm_g1_live_fail")

        self.assertEqual(result["status"], "pending_confirmation")
        [use] = await self._rows("SELECT * FROM access_token_uses WHERE token_id = ?", (token_id,))
        self.assertEqual((use["action"], use["result"]), ("invite_pending", "pending"))
        self.assertEqual(await self._barriers(use["id"]), [])
        # Still consumed and locked to its Team: never released or refunded.
        self.assertEqual(await self._used_count(token_id), 1)
        claims = await self._rows("SELECT token_use_id FROM redemption_email_claims")
        self.assertEqual([c["token_use_id"] for c in claims], [use["id"]])

        # Once it counts as interrupted, the reconciler converts it with its barrier.
        await self._allow_barrier_writes()
        await self._exec(
            "UPDATE access_token_uses SET created_at = ? WHERE id = ?",
            ((utc_now() - timedelta(minutes=20)).isoformat(), use["id"]),
        )
        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual((await self._use(use["id"]))["result"], "uncertain")
        self.assertEqual(len(await self._barriers(use["id"], unresolved_only=True)), 1)

    async def test_live_uncertain_invite_settled_in_between_leaves_no_open_barrier(self):
        token_id = await self._token("atm_g1_live_gap")
        fired = self._admin_settles_as_soon_as_uncertain_is_visible()

        await self._redeem("atm_g1_live_gap")

        [use] = await self._rows("SELECT * FROM access_token_uses WHERE token_id = ?", (token_id,))
        self.assertEqual(fired, [use["id"]])
        self.assertEqual(use["result"], "success")
        self.assertEqual(await self._barriers(use["id"], unresolved_only=True), [])


if __name__ == "__main__":
    unittest.main()
