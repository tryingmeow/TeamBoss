"""定时同步里资金路径缺陷的回归测试。

兑换兜底行（kind='extend'）的时长从行的创建时刻起算，兜底拖了多久，买家就
少了多久。必须和正常续期同一条规则：max(现在, 现有到期) + 时长。

全部跑在 init_database() 建出的临时库上，不发任何网络请求。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.scheduler import _reconcile_pending_invites_sync
from app.services.member_expiry import extend_member_expiry

TEAM = "team-1"
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
        self.assertTrue(self.db_path.startswith(self.tmpdir.name))

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _logs(self, action, team_id=None):
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM operation_logs WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows if team_id is None or r["team_id"] == team_id]


# ── 兜底行补记购买时长：max(现在, 现有到期) + 时长 ─────────────────────────

THIRTY_DAYS = timedelta(days=30)
DELAY = timedelta(days=10)


class FallbackCreditTest(_TempDbTest):
    """自助兑换的邀请已在上游成功，本地写到期失败，留下 kind='extend' 兜底行；
    10 天后数据同步才看到这个人并补记。"""

    def setUp(self):
        super().setUp()
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, access_token, device_id,
                                  created_at, updated_at)
               VALUES (?, 'Team 1', 'active', 'stub-token', 'stub-device',
                       '2026-07-01', '2026-07-01')""",
            (TEAM,),
        )
        conn.commit()
        conn.close()

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

    def test_duplicate_fallback_rows_for_one_redemption_credit_once(self):
        token_use_id = self._fallback_row()
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

        self._reconcile()

        expiry, use, unresolved = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))
        self.assertEqual(use["expires_at"], expiry["expires_at"])
        self.assertEqual(unresolved, 0)


if __name__ == "__main__":
    unittest.main()
