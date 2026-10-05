"""资金路径与巡逻的回归测试（2026-09-11 修）。

每个用例钉住的是"修好之后的行为"，不是实现细节：

1. 邀请结果不确定但快照里看得见人 → 算成功，码保持已用（原先恒被当成 rejected）。
2. 调度器结清一次兑换时，必须在同一事务里撤掉这次兑换的巡逻屏障。
3. 累加式写入的兜底行必须真的"加时长"，不能被更远的现有到期时间吃掉。
4. 快照拍完之后才写进来的成员行，不参与"缺席→踢"的判定。
5. 巡逻白名单：只巡逻本轮刷新成功的 team。
6. 纯展示接口失败不参与挂起判定。
7. 匿名查询的兑换历史需要出示这个邮箱自己的兑换码。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import scheduler as app_scheduler
from app.routes import access_tokens
from app.scheduler import (
    _classify_overview_failures,
    _reactivate_or_insert_detected_member,
    _reconcile_pending_invites_sync,
)
from app.services.member_expiry import record_uncertain_invite


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
               VALUES ('team-1', 'Team 1', 'active', '2026-09-11', '2026-09-11')"""
        )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _new_token_use(self, email="user@example.com", result="pending",
                       grant="30d", action="invite_pending"):
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'p', ?, 1, 1, 0, '2026-09-11')""",
            (f"h{datetime.now(timezone.utc).timestamp()}", grant),
        )
        token_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                created_at)
               VALUES (?, ?, ?, 'team-1', NULL, NULL, ?, '2026-09-11')""",
            (token_id, email, action, result),
        )
        token_use_id = cur.lastrowid
        conn.commit()
        conn.close()
        return token_use_id


# ── 1. 不确定的邀请不再被当成明确拒绝 ────────────────────────────────────

class _StubClient:
    async def invite_member(self, *a, **k):  # pragma: no cover
        raise AssertionError("real invite must not be called in tests")


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


# ── 2. 结清兑换的路径必须同时撤掉这次兑换的屏障 ──────────────────────────

class BackfillResolvesOrphanedBarrierTest(_TempDbTest):
    def test_backfill_path_leaves_no_barrier_behind(self):
        """同一次兑换可能同时留下 backfill 行和 barrier 行。

        调度器结清 backfill 行时会把这次兑换置成 success，
        ``reconcile_pending_redemptions`` 只扫 pending/uncertain，从此再也够不到
        它，撤屏障的那一步也就永远不会触发。屏障留在
        resolved=0：auto_kick_job 对这个 (team,email) 永远 defer，patrol 又因为
        source='self_service' 不碰它——一份无限期免费的会员。
        """
        token_use_id = self._new_token_use()
        # uncertain 分支立下的屏障
        asyncio.run(
            record_uncertain_invite(
                "team-1", "", "user@example.com", None,
                source="self_service", reason="timeout",
                token_use_id=token_use_id, kind="barrier",
            )
        )
        conn = self._conn()
        # 本地落库失败留下的兜底行，带同一个 token_use_id
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               VALUES ('team-1', 'u1', 'user@example.com', '2026-10-11T00:00:00+00:00',
                       'self_service', 'write failed', 0, '2026-09-11T00:00:00+00:00',
                       ?, 'backfill')""",
            (token_use_id,),
        )
        conn.commit()

        reconciled = _reconcile_pending_invites_sync(
            conn, "team-1",
            [{"id": "u1", "email": "user@example.com"}],
            [],
            "2026-09-11T00:00:00+00:00",
        )
        conn.commit()
        self.assertEqual(reconciled, 1)

        unresolved = conn.execute(
            "SELECT kind FROM pending_invite_reconciliations WHERE resolved = 0"
        ).fetchall()
        use_result = conn.execute(
            "SELECT result FROM access_token_uses WHERE id = ?", (token_use_id,)
        ).fetchone()["result"]
        conn.close()
        self.assertEqual(use_result, "success")
        self.assertEqual([dict(r) for r in unresolved], [])


# ── 3. 兜底回填必须累加，不能被更远的现有到期吃掉 ────────────────────────

class FallbackBackfillAddsDurationTest(_TempDbTest):
    def test_further_out_existing_expiry_still_gains_the_purchased_duration(self):
        """原先取 max(行内到期, 现有到期)：现有到期更远时，这次买的时长凭空蒸发，
        而兑换还被置成 success，用户和管理员都看不到任何异常。
        """
        token_use_id = self._new_token_use()
        created = "2026-09-11T00:00:00+00:00"
        nominal = "2026-10-11T00:00:00+00:00"  # created + 30d
        far_future = "2027-01-01T00:00:00+00:00"

        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'u1', 'user@example.com', ?, 1, 0, ?, 'self_service', ?)""",
            (far_future, created, created),
        )
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               VALUES ('team-1', 'u1', 'user@example.com', ?, 'self_service',
                       'write failed', 0, ?, ?, 'extend')""",
            (nominal, created, token_use_id),
        )
        conn.commit()

        _reconcile_pending_invites_sync(
            conn, "team-1",
            [{"id": "u1", "email": "user@example.com"}],
            [],
            "2026-09-12T00:00:00+00:00",
        )
        conn.commit()
        row = conn.execute(
            "SELECT expires_at FROM member_expiry WHERE team_id='team-1'"
        ).fetchone()
        conn.close()

        resolved = datetime.fromisoformat(row["expires_at"])
        expected = datetime.fromisoformat(far_future) + timedelta(days=30)
        self.assertEqual(resolved, expected)

    def test_authorized_permanent_membership_is_never_downgraded(self):
        token_use_id = self._new_token_use()
        created = "2026-09-11T00:00:00+00:00"
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'u1', 'user@example.com', NULL, 0, 0, ?, 'system', ?)""",
            (created, created),
        )
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               VALUES ('team-1', 'u1', 'user@example.com', '2026-10-11T00:00:00+00:00',
                       'self_service', 'write failed', 0, ?, ?, 'extend')""",
            (created, token_use_id),
        )
        conn.commit()
        _reconcile_pending_invites_sync(
            conn, "team-1",
            [{"id": "u1", "email": "user@example.com"}],
            [],
            "2026-09-12T00:00:00+00:00",
        )
        conn.commit()
        row = conn.execute(
            "SELECT expires_at, auto_kick FROM member_expiry WHERE team_id='team-1'"
        ).fetchone()
        conn.close()
        self.assertIsNone(row["expires_at"])
        self.assertEqual(row["auto_kick"], 0)


# ── 4. 快照之后才写进来的行不参与缺席判定 / 复用行不被降级成 detected ─────

class DetectedSourceIsNotDowngradedTest(_TempDbTest):
    def test_reusing_a_paid_row_keeps_its_source(self):
        """上游 user_id 漂移时会按邮箱复用已有行。无条件写 'detected' 会把付费
        成员降级成外人，出现在管理端"检测到的成员"列表里等着被手工清掉。
        """
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'old-uid', 'paid@example.com',
                       '2027-01-01T00:00:00+00:00', 1, 0,
                       '2026-09-01T00:00:00+00:00', 'self_service',
                       '2026-09-01T00:00:00+00:00')"""
        )
        conn.commit()
        created = _reactivate_or_insert_detected_member(
            conn, "team-1", "new-uid", "paid@example.com", "2026-09-11T00:00:00+00:00"
        )
        conn.commit()
        row = conn.execute(
            "SELECT user_id, source, expires_at FROM member_expiry WHERE team_id='team-1'"
        ).fetchone()
        conn.close()
        self.assertFalse(created)
        self.assertEqual(row["user_id"], "new-uid")
        self.assertEqual(row["source"], "self_service")
        self.assertEqual(row["expires_at"], "2027-01-01T00:00:00+00:00")

    def test_a_genuinely_detected_row_is_still_labelled_detected(self):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'old-uid', 'stranger@example.com', NULL, 0, 0,
                       '2026-09-01T00:00:00+00:00', 'detected',
                       '2026-09-01T00:00:00+00:00')"""
        )
        conn.commit()
        _reactivate_or_insert_detected_member(
            conn, "team-1", "new-uid", "stranger@example.com", "2026-09-11T00:00:00+00:00"
        )
        conn.commit()
        source = conn.execute(
            "SELECT source FROM member_expiry WHERE team_id='team-1'"
        ).fetchone()["source"]
        conn.close()
        self.assertEqual(source, "detected")


class AbsenceJudgementSkipsRowsNewerThanSnapshotTest(unittest.TestCase):
    """反向缺席判定的时间界限。

    成员名单在 ``snapshot_taken_at`` 那一刻拍下，``expiry_rows`` 在那之后才读。
    夹在中间落地的一次兑换写出的行当然不在名单里，判它缺席会 kicked=1，下一轮再
    以 source='detected'、expires_at=NULL、auto_kick=0 重新插一条——付过钱的到期
    时间没了，人正好长成 patrol 的踢人目标。
    """

    def _judge(self, row_created_at, snapshot_taken_at):
        return app_scheduler._too_new_to_judge_absent(row_created_at, snapshot_taken_at)

    def test_row_created_after_the_snapshot_is_too_new_to_judge(self):
        snapshot = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(self._judge("2026-09-11T12:00:01+00:00", snapshot))

    def test_row_created_before_the_snapshot_is_judged_normally(self):
        snapshot = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(self._judge("2026-09-11T11:59:59+00:00", snapshot))

    def test_legacy_row_without_timestamps_is_still_judged(self):
        snapshot = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(self._judge(None, snapshot))

    def test_the_absence_branch_is_the_only_caller_and_is_wired_up(self):
        source = Path(app_scheduler.__file__).read_text(encoding="utf-8")
        self.assertIn("COALESCE(created_at, first_seen_at) AS row_created_at", source)
        self.assertIn("if _too_new_to_judge_absent(", source)
        # 快照时刻必须在拉名单之前取，否则这个界限本身就是错的。
        snapshot_line = source.index("snapshot_taken_at = datetime.now(timezone.utc)")
        members_line = source.index('_fetch_all_api_items_sync(client.get_members, "users")')
        self.assertLess(snapshot_line, members_line)


# ── 6. 纯展示接口失败不参与挂起判定 ──────────────────────────────────────

class OverviewFailureClassificationTest(unittest.TestCase):
    def test_display_only_failures_are_not_enforcement_failures(self):
        overview, display, enforcement = _classify_overview_failures(
            subscription={"seats_entitled": 5},
            seat_counts={"default": 3},
            balance_info={"error": "boom"},
            payment_methods={"error": "boom"},
            account_info={"error": "boom"},
        )
        self.assertEqual(overview, ["balance", "payment_methods", "account_info"])
        self.assertEqual(display, ["balance", "payment_methods", "account_info"])
        self.assertEqual(enforcement, [])

    def test_subscription_and_seat_counts_are_enforcement_inputs(self):
        _overview, display, enforcement = _classify_overview_failures(
            subscription={"error": "401"},
            seat_counts={"error": "401"},
            balance_info=None,
            payment_methods=None,
            account_info=None,
        )
        self.assertEqual(enforcement, ["subscription", "seat_counts"])
        self.assertEqual(display, [])

    def test_throttled_calls_are_not_failures(self):
        overview, display, enforcement = _classify_overview_failures(
            subscription={"seats_entitled": 5},
            seat_counts={"default": 3},
            balance_info=None,
            payment_methods=None,
            account_info=None,
        )
        self.assertEqual((overview, display, enforcement), ([], [], []))


# ── 7. 匿名查询的兑换历史需要凭据 ────────────────────────────────────────

class RedemptionHistoryNeedsProofTest(_TempDbTest):
    def setUp(self):
        super().setUp()
        self.raw_token = "atm_secret_code"
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'atm_sec', '30d', 1, 1, 0, '2026-09-11')""",
            (access_tokens._hash_token(self.raw_token),),
        )
        token_id = cur.lastrowid
        conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result, created_at)
               VALUES (?, 'owner@example.com', 'invited', 'team-1', 'u1',
                       '2026-10-11T00:00:00+00:00', 'success', '2026-09-11')""",
            (token_id,),
        )
        conn.commit()
        conn.close()

    def _status(self, email, token):
        with patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[])
        ):
            return asyncio.run(access_tokens._query_membership_status(email, token))

    def test_bare_email_gets_membership_but_no_history(self):
        result = self._status("owner@example.com", None)
        self.assertEqual(result["status"], "absent")
        self.assertEqual(result["redemption_history"], [])

    def test_own_code_unlocks_the_history(self):
        result = self._status("owner@example.com", self.raw_token)
        self.assertEqual(len(result["redemption_history"]), 1)

    def test_someone_elses_email_with_that_code_stays_locked(self):
        result = self._status("victim@example.com", self.raw_token)
        self.assertEqual(result["redemption_history"], [])

    def test_unknown_code_stays_locked(self):
        result = self._status("owner@example.com", "atm_not_a_real_code")
        self.assertEqual(result["redemption_history"], [])


if __name__ == "__main__":
    unittest.main()
