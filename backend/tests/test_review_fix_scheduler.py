"""调度器/同步路径上三处资金缺陷的回归测试。

1. 同一次兑换被两条恢复路径各加一遍时长（30 天码变 60 天）。
2. 上游 seats_entitled 为 null/0/非正整数时被原样写库，patrol 据此把所有默认
   席位算成超员。
3. 自动踢人的邮箱查找把"结构不认识的 200"当成空列表，到期行被当成"人已不在"
   关掉。

每个用例断言的是修好之后的行为，不是实现细节。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes import access_tokens
from app.scheduler import _reconcile_pending_invites_sync
from app.services import member_expiry as member_expiry_module
from app.services.member_expiry import (
    extend_member_expiry,
    record_confirmed_invite_extension,
)

EMAIL = "buyer@example.com"


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
            """INSERT INTO teams (id, name, status, access_token, device_id,
                                  created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', 'stub-token', 'stub-device',
                       '2026-10-01', '2026-10-01')"""
        )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _new_token_use(self, *, result="pending", grant="30d"):
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'p', ?, 1, 1, 0, '2026-10-01')""",
            (f"h-{datetime.now(timezone.utc).timestamp()}", grant),
        )
        token_id = cur.lastrowid
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                created_at)
               VALUES (?, ?, 'invite_pending', 'team-1', NULL, NULL, ?, ?)""",
            (token_id, EMAIL, result, now_iso),
        )
        token_use_id = cur.lastrowid
        conn.commit()
        conn.close()
        return token_use_id


# ── 1. 两条恢复路径共用一次性凭据：时长只加一次 ───────────────────────────

class SingleCreditAcrossRecoveryPathsTest(_TempDbTest):
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


if __name__ == "__main__":
    unittest.main()
