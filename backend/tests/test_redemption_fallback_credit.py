"""The scheduler's fallback credit for a redemption whose local write failed.

The invite succeeded upstream but writing the expiry did not, so the redemption
left a pending_invite_reconciliations row ('extend', or legacy 'backfill').
When a sync sees the person, the row is credited: once per redemption, whichever
recovery path (data sync or the redemption reconciler) gets there first, as
max(now, current expiry) + the purchased duration, never over a permanent
authorization, and never for a code the admin already refunded. Upstream calls
are faked; nothing touches the network.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _redemption_fixtures import EMAIL, ConnectedTeamCase, RedemptionLedgerCase

from app.routes import access_tokens
from app.scheduler import _reconcile_pending_invites_sync
from app.services import member_expiry as member_expiry_module
from app.services.member_expiry import (
    extend_member_expiry,
    record_confirmed_invite_extension,
)

TEAM = "team-1"
THIRTY_DAYS = timedelta(days=30)
DELAY = timedelta(days=10)


# ── 两条恢复路径共用一次性凭据：时长只加一次 ─────────────────────────────────

class SingleCreditAcrossRecoveryPathsTest(ConnectedTeamCase):
    """兑换邀请已在上游成功、本地 extend 写入连续失败 → 留下 kind='extend' 兜底行
    且兑换仍是 pending。之后 60 秒一轮的 reconcile_pending_redemptions 和数据同步里
    的 _reconcile_pending_invites_sync 都会去结算它，谁先谁后都只能加一次时长。
    """

    def _fail_primary_write(self):
        """跑真实的 record_confirmed_invite_extension，但让 extend 写入每次都失败。"""
        token_use_id = self._new_token_use()
        failing = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))
        with patch.object(member_expiry_module, "extend_member_expiry", failing), \
             patch.object(member_expiry_module, "_CONFIRM_WRITE_BACKOFF_SECONDS", 0):
            asyncio.run(
                record_confirmed_invite_extension(
                    "team-1",
                    "",
                    EMAIL,
                    "30d",
                    source="self_service",
                    token_use_id=token_use_id,
                    token_action="invited",
                )
            )
        conn = self._conn()
        rows = conn.execute(
            "SELECT kind, resolved FROM pending_invite_reconciliations WHERE token_use_id = ?",
            (token_use_id,),
        ).fetchall()
        use = conn.execute(
            "SELECT result FROM access_token_uses WHERE id = ?", (token_use_id,)
        ).fetchone()
        conn.close()
        self.assertEqual([(r["kind"], r["resolved"]) for r in rows], [("extend", 0)])
        self.assertEqual(use["result"], "pending")
        return token_use_id

    def _run_redemption_reconciler(self):
        snapshot = {"members": [{"id": "u1", "email": EMAIL}], "pending_invites": []}
        with patch.object(access_tokens, "fetch_and_cache_members",
                          AsyncMock(return_value=snapshot)), \
             patch.object(access_tokens, "_get_proxy_url", AsyncMock(return_value=None)), \
             patch.object(access_tokens, "ChatGPTClient", MagicMock()):
            return asyncio.run(access_tokens.reconcile_pending_redemptions())

    def _run_scheduler_sync(self):
        conn = self._conn()
        try:
            reconciled = _reconcile_pending_invites_sync(
                conn,
                "team-1",
                [{"id": "u1", "email": EMAIL}],
                [],
                datetime.now(timezone.utc).isoformat(),
            )
            conn.commit()
        finally:
            conn.close()
        return reconciled

    def _state(self, token_use_id):
        conn = self._conn()
        expiry_rows = conn.execute(
            "SELECT expires_at FROM member_expiry WHERE team_id = 'team-1' AND kicked = 0"
        ).fetchall()
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM pending_invite_reconciliations WHERE resolved = 0"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(len(expiry_rows), 1)
        return expiry_rows[0]["expires_at"], dict(use), unresolved

    def _assert_about_thirty_days(self, expires_iso):
        remaining = datetime.fromisoformat(expires_iso) - datetime.now(timezone.utc)
        self.assertGreater(remaining, timedelta(days=29, hours=23))
        self.assertLessEqual(remaining, timedelta(days=30))

    def test_reconciler_first_then_scheduler_does_not_add_again(self):
        token_use_id = self._fail_primary_write()

        counts = self._run_redemption_reconciler()
        self.assertEqual(counts["confirmed"], 1)
        credited, use, _ = self._state(token_use_id)
        self._assert_about_thirty_days(credited)
        self.assertEqual(use["result"], "success")

        self._run_scheduler_sync()
        after, use, unresolved = self._state(token_use_id)
        # 原先这里会变成 credited + 30 天：兜底行把同一笔购买又追加了一遍。
        self.assertEqual(after, credited)
        self.assertEqual(use["expires_at"], credited)
        self.assertEqual(unresolved, 0)

    def test_scheduler_first_then_reconciler_does_not_add_again(self):
        token_use_id = self._fail_primary_write()

        self._run_scheduler_sync()
        credited, use, unresolved = self._state(token_use_id)
        self._assert_about_thirty_days(credited)
        self.assertEqual(use["result"], "success")
        # 收据上记的到期时间必须就是写进 member_expiry 的那一个。
        self.assertEqual(use["expires_at"], credited)
        self.assertEqual(unresolved, 0)

        # 结清后的兑换不再出现在对账扫描里……
        counts = self._run_redemption_reconciler()
        # 只比这几个计数：对账任务还会报告别的计数（例如 uncertain），与本用例无关。
        self.assertEqual(
            {key: counts.get(key) for key in ("confirmed", "released", "waiting")},
            {"confirmed": 0, "released": 0, "waiting": 0},
        )
        # ……就算对账在调度器结清之前已经读到了它（并发），extend 也只会原样返回
        # 已结清的到期时间，不会再加。
        returned = asyncio.run(
            extend_member_expiry(
                "team-1", "", EMAIL, "30d",
                source="self_service", keep_permanent=True,
                token_use_id=token_use_id, token_action="invited",
            )
        )
        self.assertEqual(returned, credited)
        after, _, _ = self._state(token_use_id)
        self.assertEqual(after, credited)

    def test_receipt_records_the_expiry_actually_written_on_top_of_existing_time(self):
        token_use_id = self._fail_primary_write()
        far_future = (datetime.now(timezone.utc) + timedelta(days=200)).isoformat()
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'u1', ?, ?, 1, 0, '2026-01-01', 'self_service', '2026-01-01')""",
            (EMAIL, far_future),
        )
        conn.commit()
        conn.close()

        self._run_scheduler_sync()
        credited, use, _ = self._state(token_use_id)
        gained = datetime.fromisoformat(credited) - datetime.fromisoformat(far_future)
        self.assertGreater(gained, timedelta(days=29, hours=23))
        self.assertLessEqual(gained, timedelta(days=30))
        self.assertEqual(use["expires_at"], credited)

    def test_duplicate_fallback_rows_for_one_redemption_credit_once(self):
        """对账路径自己的 extend 也可能再失败一次，于是同一次兑换留下两行 'extend'。"""
        token_use_id = self._fail_primary_write()
        conn = self._conn()
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               SELECT team_id, user_id, email, expires_at, source, reason, 0,
                      created_at, token_use_id, kind
               FROM pending_invite_reconciliations WHERE token_use_id = ?""",
            (token_use_id,),
        )
        conn.commit()
        conn.close()

        self._run_scheduler_sync()
        credited, use, unresolved = self._state(token_use_id)
        self._assert_about_thirty_days(credited)
        self.assertEqual(use["expires_at"], credited)
        self.assertEqual(unresolved, 0)

    def test_redemption_released_by_admin_is_never_credited(self):
        """管理员已核实退码（result='failed'）：之后人再出现在名单里，兜底行也不能
        把已经退掉的时长写回去。"""
        token_use_id = self._fail_primary_write()
        conn = self._conn()
        conn.execute(
            """UPDATE access_token_uses
               SET action = 'redeem_admin_released', result = 'failed', expires_at = NULL
               WHERE id = ?""",
            (token_use_id,),
        )
        conn.commit()
        conn.close()

        self._run_scheduler_sync()
        conn = self._conn()
        expiry_count = conn.execute("SELECT COUNT(*) FROM member_expiry").fetchone()[0]
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM pending_invite_reconciliations WHERE resolved = 0"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(expiry_count, 0)
        self.assertEqual(use["result"], "failed")
        self.assertIsNone(use["expires_at"])
        self.assertEqual(unresolved, 0)


# ── 兜底行补记购买时长：max(现在, 现有到期) + 时长 ───────────────────────────

class FallbackCreditTest(ConnectedTeamCase):
    """自助兑换的邀请已在上游成功，本地写到期失败，留下 kind='extend' 兜底行；
    10 天后数据同步才看到这个人并补记。"""

    def _fallback_row(self, *, duration=THIRTY_DAYS, delay=DELAY):
        """按 member_expiry._persist_confirmed_membership 的形状落一行兜底 + 一次兑换。"""
        created = datetime.now(timezone.utc) - delay
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'p', ?, 1, 1, 0, '2026-07-01')""",
            (f"h-{created.timestamp()}", "30d" if duration else "never"),
        )
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result, created_at)
               VALUES (?, ?, 'invite_pending', ?, NULL, NULL, 'pending', ?)""",
            (cur.lastrowid, EMAIL, TEAM, created.isoformat()),
        )
        token_use_id = cur.lastrowid
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               VALUES (?, '', ?, ?, 'self_service', 'database is locked', 0, ?, ?, 'extend')""",
            (
                TEAM,
                EMAIL,
                (created + duration).isoformat() if duration else None,
                created.isoformat(),
                token_use_id,
            ),
        )
        conn.commit()
        conn.close()
        return token_use_id

    def _existing_expiry(self, expires_at, *, source="self_service", kicked=0):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES (?, 'u1', ?, ?, ?, ?, '2026-07-01', ?, '2026-07-01')""",
            (TEAM, EMAIL, expires_at, 1 if expires_at else 0, kicked, source),
        )
        conn.commit()
        conn.close()

    def _reconcile(self):
        conn = self._conn()
        try:
            count = _reconcile_pending_invites_sync(
                conn,
                TEAM,
                [{"id": "u1", "email": EMAIL}],
                [],
                datetime.now(timezone.utc).isoformat(),
            )
            conn.commit()
        finally:
            conn.close()
        return count

    def _state(self, token_use_id):
        conn = self._conn()
        rows = conn.execute(
            """SELECT expires_at, auto_kick FROM member_expiry
               WHERE team_id = ? AND kicked = 0""",
            (TEAM,),
        ).fetchall()
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM pending_invite_reconciliations WHERE resolved = 0"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(len(rows), 1)
        return dict(rows[0]), dict(use), unresolved

    def _assert_full_duration_from(self, expires_iso, base):
        gained = datetime.fromisoformat(expires_iso) - base
        # 兜底行的到期和 created_at 在落盘时先后取时间，差几毫秒属正常。
        self.assertGreater(gained, THIRTY_DAYS - timedelta(minutes=1))
        self.assertLessEqual(gained, THIRTY_DAYS + timedelta(minutes=1))

    def test_delayed_fallback_credits_full_duration_from_now(self):
        token_use_id = self._fallback_row()

        self.assertEqual(self._reconcile(), 1)

        expiry, use, unresolved = self._state(token_use_id)
        # 原先是 created_at + 30 天 = 只剩 20 天。
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))
        self.assertEqual(expiry["auto_kick"], 1)
        self.assertEqual(use, {"result": "success", "expires_at": expiry["expires_at"]})
        self.assertEqual(unresolved, 0)

    def test_delayed_fallback_over_detected_row_credits_from_now(self):
        # 同步先把这个人当成"外部发现"建了档（detected + NULL 不是永久授权）。
        self._existing_expiry(None, source="detected")
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, use, _ = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))
        self.assertEqual(expiry["auto_kick"], 1)
        self.assertEqual(use["expires_at"], expiry["expires_at"])

    def test_delayed_fallback_after_archived_membership_credits_from_now(self):
        # 上一段成员身份已 kicked=1 归档，没有任何已购时长需要保护。
        self._existing_expiry(
            (datetime.now(timezone.utc) + timedelta(days=90)).isoformat(), kicked=1,
        )
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, _, _ = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))

    def test_existing_future_expiry_is_extended_from_that_expiry(self):
        future = datetime.now(timezone.utc) + timedelta(days=100)
        self._existing_expiry(future.isoformat())
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, use, _ = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], future)
        self.assertEqual(use["expires_at"], expiry["expires_at"])

    def test_existing_past_expiry_credits_from_now(self):
        self._existing_expiry(
            (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        )
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, _, _ = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))

    def test_permanent_authorization_is_never_made_finite(self):
        self._existing_expiry(None, source="system")
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, use, unresolved = self._state(token_use_id)
        self.assertIsNone(expiry["expires_at"])
        self.assertEqual(expiry["auto_kick"], 0)
        self.assertEqual(use, {"result": "success", "expires_at": None})
        self.assertEqual(unresolved, 0)

    def test_permanent_purchase_grants_permanent(self):
        self._existing_expiry(
            (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        )
        token_use_id = self._fallback_row(duration=None)

        self._reconcile()

        expiry, use, _ = self._state(token_use_id)
        self.assertIsNone(expiry["expires_at"])
        self.assertIsNone(use["expires_at"])

    def test_second_pass_and_other_recovery_path_do_not_credit_again(self):
        token_use_id = self._fallback_row()
        self._reconcile()
        credited, _, _ = self._state(token_use_id)

        # 同一行再被扫一次（已 resolved），以及另一条恢复路径晚到：都不能再加。
        self.assertEqual(self._reconcile(), 0)
        returned = asyncio.run(
            extend_member_expiry(
                TEAM, "u1", EMAIL, "30d",
                source="self_service", keep_permanent=True,
                token_use_id=token_use_id, token_action="invited",
            )
        )
        after, use, unresolved = self._state(token_use_id)
        self.assertEqual(returned, credited["expires_at"])
        self.assertEqual(after, credited)
        self.assertEqual(use["expires_at"], credited["expires_at"])
        self.assertEqual(unresolved, 0)


# ── 兜底回填必须累加，不能被更远的现有到期吃掉 ───────────────────────────────

class FallbackBackfillAddsDurationTest(RedemptionLedgerCase):
    def test_further_out_existing_expiry_still_gains_the_purchased_duration(self):
        """原先取 max(行内到期, 现有到期)：现有到期更远时，这次授予的时长凭空蒸发，
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


# ── 旧式 backfill 行：按行内到期落库并结清兑换 ──────────────────────────────

class SingleRecoveryCreditTest(RedemptionLedgerCase):
    def test_scheduler_backfill_settles_the_redemption(self):
        token_use_id = self._new_token_use(result="uncertain")
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


if __name__ == "__main__":
    unittest.main()
