"""六个兑换资金缺口的回归测试（2026-09-06 修）。

每条对应一个已确认的缺陷，断言的是"修好之后的行为"，不是实现细节。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import member_cache_service
from app.routes import access_tokens
from app.scheduler import _reconcile_pending_invites_sync
from app.services import patrol
from app.services.member_expiry import (
    PermanentMembershipError,
    extend_member_expiry,
    get_active_expiry_state,
    record_uncertain_invite,
    upsert_member_expiry,
)


class _TempDbTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(
            app_database, "get_db_dir", return_value=self.tmpdir.name
        )
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO teams (id, name, status, created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', '2026-09-06', '2026-09-06')"""
        )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _new_token_use(self, email="user@example.com", result="uncertain"):
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES ('h', 'p', '30d', 1, 1, 0, '2026-09-06')"""
        )
        token_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                created_at)
               VALUES (?, ?, 'invite_pending', 'team-1', NULL, NULL, ?, '2026-09-06')""",
            (token_id, email, result),
        )
        token_use_id = cur.lastrowid
        conn.commit()
        conn.close()
        return token_use_id


# ── 缺口 4：上游名单残缺不能归一化成"Team 为空" ─────────────────────────────

class MalformedUpstreamFailsClosedTest(unittest.TestCase):
    def test_empty_list_is_still_an_empty_team(self):
        self.assertEqual(member_cache_service._api_items({"items": []}, "users"), [])

    def test_missing_and_null_list_fields_raise(self):
        for payload in ({}, {"items": None}, {"total": 3}, []):
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPException) as ctx:
                    member_cache_service._api_items(payload, "users")
                self.assertEqual(ctx.exception.status_code, 502)


# ── 缺口 2：detected + NULL 不是永久授权 ───────────────────────────────────

class DetectedNullIsNotPermanentTest(_TempDbTest):
    def test_state_and_renewal(self):
        asyncio.run(
            upsert_member_expiry(
                "team-1", "u1", "stranger@example.com", None, source="detected"
            )
        )
        self.assertEqual(
            asyncio.run(
                get_active_expiry_state("team-1", "u1", "stranger@example.com")
            ),
            "unmanaged",
        )

        token_use_id = self._new_token_use("stranger@example.com", result="pending")
        expires = asyncio.run(
            extend_member_expiry(
                "team-1",
                "u1",
                "stranger@example.com",
                "30d",
                source="self_service",
                token_use_id=token_use_id,
                token_action="renewed_member",
            )
        )
        self.assertIsNotNone(expires)

        conn = self._conn()
        row = conn.execute(
            "SELECT expires_at, auto_kick, source FROM member_expiry"
        ).fetchone()
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row["expires_at"])
        self.assertEqual(row["auto_kick"], 1)
        self.assertEqual(row["source"], "self_service")
        self.assertEqual(use["result"], "success")

    def test_authorized_null_row_is_still_permanent(self):
        asyncio.run(
            upsert_member_expiry(
                "team-1", "u2", "vip@example.com", None, source="self_service"
            )
        )
        self.assertEqual(
            asyncio.run(get_active_expiry_state("team-1", "u2", "vip@example.com")),
            "permanent",
        )
        with self.assertRaises(PermanentMembershipError):
            asyncio.run(
                extend_member_expiry(
                    "team-1", "u2", "vip@example.com", "30d", source="self_service"
                )
            )


# ── 缺口 3：两条恢复路径共用一次性凭据 ───────────────────────────────────

class SingleRecoveryCreditTest(_TempDbTest):
    def test_scheduler_backfill_settles_the_redemption(self):
        token_use_id = self._new_token_use()
        conn = self._conn()
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               VALUES ('team-1', 'u1', 'user@example.com', '2026-10-06T00:00:00+00:00',
                       'self_service', 'primary write failed', 0, '2026-09-06', ?,
                       'backfill')""",
            (token_use_id,),
        )
        conn.commit()

        reconciled = _reconcile_pending_invites_sync(
            conn,
            "team-1",
            [{"id": "u1", "email": "user@example.com"}],
            [],
            "2026-09-06T00:00:00+00:00",
        )
        conn.commit()
        self.assertEqual(reconciled, 1)

        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        conn.close()
        # 已结清 → reconcile_pending_redemptions 的查询（result IN pending/uncertain）
        # 再也扫不到它，不会第二次累加时长。
        self.assertEqual(use["result"], "success")
        self.assertEqual(use["expires_at"], "2026-10-06T00:00:00+00:00")

    def test_access_reconcile_query_skips_settled_use(self):
        token_use_id = self._new_token_use()
        conn = self._conn()
        conn.execute(
            "UPDATE access_token_uses SET result = 'success' WHERE id = ?",
            (token_use_id,),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT id FROM access_token_uses WHERE result IN ('pending','uncertain')"
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [])


# ── 缺口 5：结果未定的自助邀请要有巡逻屏障 ───────────────────────────────

class UncertainInviteBarrierTest(_TempDbTest):
    def test_barrier_blocks_patrol_and_is_not_backfilled(self):
        token_use_id = self._new_token_use()
        asyncio.run(
            record_uncertain_invite(
                "team-1",
                "",
                "user@example.com",
                None,
                source="self_service",
                reason="timeout",
                token_use_id=token_use_id,
                kind="barrier",
            )
        )

        conn = self._conn()
        # 巡逻必须拒绝撤销
        self.assertIsNotNone(
            patrol._pending_invite_reconciliation_reject(
                conn, "team-1", "", "user@example.com"
            )
        )
        # 调度器不得据此写任何到期时间
        reconciled = _reconcile_pending_invites_sync(
            conn,
            "team-1",
            [],
            [{"email": "user@example.com"}],
            "2026-09-06T00:00:00+00:00",
        )
        conn.commit()
        self.assertEqual(reconciled, 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM member_expiry").fetchone()[0], 0
        )
        self.assertEqual(
            conn.execute(
                "SELECT resolved FROM pending_invite_reconciliations"
            ).fetchone()["resolved"],
            0,
        )
        conn.close()


# ── 缺口 1：请求发出即算消耗，只有明确拒绝才退码 ─────────────────────────

class _StubClient:
    async def invite_member(self, *a, **k):  # pragma: no cover - run_chatgpt_call 已被替换
        raise AssertionError("real invite must not be called in tests")


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


# ── 缺口 6：没指定车队时，拿锁后要重新全量扫描 ───────────────────────────

class RescanAfterClaimTest(unittest.IsolatedAsyncioTestCase):
    async def test_second_team_appearing_during_the_window_asks_the_user(self):
        @asynccontextmanager
        async def _claim(*a, **k):
            yield True

        team_a = {"id": "team-a", "name": "A"}
        team_b = {"id": "team-b", "name": "B"}
        existing = {"team": team_a, "user_id": "u1", "kind": "member", "is_owner": False}
        both = [
            {"team": team_a, "user_id": "u1", "kind": "member", "is_owner": False},
            {"team": team_b, "user_id": "u1", "kind": "member", "is_owner": False},
        ]

        with (
            patch.object(access_tokens, "member_operation_claim", _claim),
            patch.object(
                access_tokens, "_find_all_memberships", new=AsyncMock(return_value=both)
            ),
            patch.object(access_tokens, "extend_member_expiry", new=AsyncMock()) as extend,
        ):
            result = await access_tokens._renew_existing_membership(
                existing, "user@example.com", "30d", 1, rescan_teams=[team_a, team_b]
            )

        self.assertEqual(result["needs_selection"], both)
        extend.assert_not_awaited()

    async def test_chosen_team_path_does_not_rescan(self):
        @asynccontextmanager
        async def _claim(*a, **k):
            yield True

        team_a = {"id": "team-a", "name": "A"}
        existing = {"team": team_a, "user_id": "u1", "kind": "member", "is_owner": False}

        with (
            patch.object(access_tokens, "member_operation_claim", _claim),
            patch.object(access_tokens, "_find_all_memberships", new=AsyncMock()) as scan_all,
            patch.object(
                access_tokens,
                "_find_existing_membership",
                new=AsyncMock(return_value=existing),
            ),
            patch.object(access_tokens, "_set_token_use_phase", new=AsyncMock()),
            patch.object(
                access_tokens,
                "extend_member_expiry",
                new=AsyncMock(return_value="2026-10-06T00:00:00+00:00"),
            ),
        ):
            result = await access_tokens._renew_existing_membership(
                existing, "user@example.com", "30d", 1
            )

        scan_all.assert_not_awaited()
        self.assertEqual(result["action"], "renewed_member")


if __name__ == "__main__":
    unittest.main()


# ── A 方案：结果确认中的兑换只由管理员收尾，绝不按时间自动退码 ─────────────

class AdminResolvesPendingConfirmationTest(_TempDbTest):
    def setUp(self):
        super().setUp()
        self.token_use_id = self._new_token_use("user@example.com", result="uncertain")

    def test_listing_shows_the_stuck_redemption(self):
        rows = asyncio.run(access_tokens.list_pending_confirmations())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "user@example.com")
        self.assertEqual(rows[0]["team_id"], "team-1")

    def test_confirm_success_grants_the_duration_and_keeps_the_code_used(self):
        result = asyncio.run(
            access_tokens.resolve_pending_confirmation(
                self.token_use_id,
                access_tokens.ResolvePendingConfirmationRequest(outcome="success"),
            )
        )
        self.assertEqual(result["outcome"], "success")

        conn = self._conn()
        use = conn.execute(
            "SELECT result FROM access_token_uses WHERE id = ?", (self.token_use_id,)
        ).fetchone()
        token = conn.execute("SELECT used_count FROM access_tokens").fetchone()
        member = conn.execute("SELECT expires_at, source FROM member_expiry").fetchone()
        conn.close()
        self.assertEqual(use["result"], "success")
        self.assertEqual(token["used_count"], 1)
        self.assertIsNotNone(member["expires_at"])

    def test_release_refuses_while_the_member_is_still_visible(self):
        with (
            patch.object(
                access_tokens,
                "load_active_teams",
                new=AsyncMock(
                    return_value=[
                        {"id": "team-1", "name": "T1", "access_token": "t",
                         "device_id": "d", "proxy_id": None}
                    ]
                ),
            ),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(
                access_tokens,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [{"email": "user@example.com"}]}),
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    access_tokens.resolve_pending_confirmation(
                        self.token_use_id,
                        access_tokens.ResolvePendingConfirmationRequest(outcome="released"),
                    )
                )
        self.assertEqual(ctx.exception.status_code, 409)
        conn = self._conn()
        self.assertEqual(
            conn.execute("SELECT used_count FROM access_tokens").fetchone()[0], 1
        )
        conn.close()

    def test_release_refuses_when_the_live_list_cannot_be_read(self):
        with (
            patch.object(
                access_tokens,
                "load_active_teams",
                new=AsyncMock(
                    return_value=[
                        {"id": "team-1", "name": "T1", "access_token": "t",
                         "device_id": "d", "proxy_id": None}
                    ]
                ),
            ),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(
                access_tokens,
                "fetch_and_cache_members",
                new=AsyncMock(side_effect=RuntimeError("upstream down")),
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    access_tokens.resolve_pending_confirmation(
                        self.token_use_id,
                        access_tokens.ResolvePendingConfirmationRequest(outcome="released"),
                    )
                )
        self.assertEqual(ctx.exception.status_code, 503)

    def test_release_returns_the_code_once_absence_is_verified(self):
        with (
            patch.object(
                access_tokens,
                "load_active_teams",
                new=AsyncMock(
                    return_value=[
                        {"id": "team-1", "name": "T1", "access_token": "t",
                         "device_id": "d", "proxy_id": None}
                    ]
                ),
            ),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(
                access_tokens,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [], "pending_invites": []}),
            ),
        ):
            result = asyncio.run(
                access_tokens.resolve_pending_confirmation(
                    self.token_use_id,
                    access_tokens.ResolvePendingConfirmationRequest(
                        outcome="released", note="OpenAI 后台确认没有这个人"
                    ),
                )
            )
        self.assertEqual(result["outcome"], "released")

        conn = self._conn()
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (self.token_use_id,),
        ).fetchone()
        token = conn.execute("SELECT used_count FROM access_tokens").fetchone()
        claims = conn.execute("SELECT COUNT(*) FROM redemption_email_claims").fetchone()[0]
        conn.close()
        self.assertEqual(use["result"], "failed")
        self.assertIsNone(use["expires_at"])
        self.assertEqual(token["used_count"], 0)
        self.assertEqual(claims, 0)

    def test_a_settled_redemption_cannot_be_resolved_twice(self):
        conn = self._conn()
        conn.execute(
            "UPDATE access_token_uses SET result = 'success' WHERE id = ?",
            (self.token_use_id,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                access_tokens.resolve_pending_confirmation(
                    self.token_use_id,
                    access_tokens.ResolvePendingConfirmationRequest(outcome="success"),
                )
            )
        self.assertEqual(ctx.exception.status_code, 409)
