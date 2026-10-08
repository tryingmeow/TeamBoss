"""巡逻自动重建基线不给"没有任何记录"的成员/邀请补 system 行 的回归测试。

一个 Team 已经 grandfather 过（teams.patrol_grandfathered_at 非空），后来丢了
patrol_team_baselines 行（token_expired / 重新导入），run_patrol 自动重建基线时，
成员快照里可能有同步还没来得及建档的人（快照由别的刷新路径写入，比如管理端手动刷新）。
这些人没人确认过：补一条 system + NULL 到期 = 永久保护。修好之后自动重建不补，
下一轮同步照常按 detected 建档，他们就是普通的巡逻候选。

管理员显式开启（activate_patrol_sync）和 Team 第一次建基线仍然补 system 行，行为不变。

全部跑在 init_database() 建出的临时库上；ChatGPT 客户端一律替换成假对象，不发任何网络请求。
"""
import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.services import patrol


TEAM_ID = "team-g5"
NOW = "2026-07-20T00:00:00+00:00"
GRANDFATHERED_AT = "2026-07-01T00:00:00+00:00"

OWNER = ("u-owner", "owner@x.com")
PAID = ("u-paid", "paid@x.com")            # 系统自己拉的人：system 行
STRANGER = ("u-stranger", "stranger@x.com")  # 同步已按 detected 建档的外部成员
ROWLESS = ("u-rowless", "rowless@x.com")     # 快照里有、member_expiry 里还没有任何记录
ROWLESS_INVITE = "rowless-invite@x.com"      # 同上，但还是 pending 邀请


def _cached_member(identity, source, *, seat_type="default", is_owner=False):
    user_id, email = identity
    return {
        "id": user_id,
        "email": email,
        "seat_type": seat_type,
        "is_owner": is_owner,
        "source": source,
        "first_seen_at": NOW,
        "status": "active",
    }


def _cached_invite(email, source):
    return {
        "id": f"invite-{email}",
        "email": email,
        "seat_type": "default",
        "is_owner": False,
        "source": source,
        "status": "pending",
    }


class RaisingClient:
    """站岗：建立基线的这一轮绝不能动手。"""

    def __init__(self, *args, **kwargs):
        pass

    def remove_member(self, user_id):  # pragma: no cover
        raise AssertionError(f"remove_member must not be called (user_id={user_id})")

    def revoke_invite(self, email):  # pragma: no cover
        raise AssertionError(f"revoke_invite must not be called (email={email})")


class _UpstreamClient:
    """data_sync_job 看到的上游：与成员快照同一批人，全部只读。"""

    def __init__(self, access_token, team_id, device_id, proxy_url=None):
        pass

    def get_subscription(self):
        return {
            "seats_entitled": 1,
            "seats_in_use": 4,
            "billing_currency": "USD",
            "active_start": "2026-10-01T00:00:00+00:00",
            "active_until": "2026-11-01T00:00:00+00:00",
            "will_renew": True,
        }

    def get_seat_type_counts(self):
        return {"seat_type_counts": {"default": 3, "usage_based": 1}}

    def get_members(self, offset=0, limit=100):
        items = [
            {"id": OWNER[0], "email": OWNER[1], "role": "account-owner",
             "seat_type": "usage_based"},
        ] + [
            {"id": user_id, "email": email, "role": "standard-user", "seat_type": "default"}
            for user_id, email in (PAID, STRANGER, ROWLESS)
        ]
        return {"items": items if offset == 0 else [], "total": len(items)}

    def get_pending_invites(self, offset=0, limit=100):
        items = [{"id": "inv-1", "email_address": ROWLESS_INVITE, "role": "standard-user",
                  "seat_type": "default"}]
        return {"items": items if offset == 0 else [], "total": len(items)}


class AutoRebaselineBackfillTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self._tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        self.assertTrue(self.db_path.startswith(self._tmpdir.name))

        for name, value in (
            ("notify_admins_sync", lambda *a, **kw: None),
            ("ChatGPTClient", RaisingClient),
            ("run_chatgpt_call_sync", lambda func, *a, **kw: func(*a, **kw)),
        ):
            p = patch.object(patrol, name, new=value)
            p.start()
            self.addCleanup(p.stop)

    # ── fixtures ──────────────────────────────────────────────────────────

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _seed(self, *, grandfathered):
        """巡逻已武装；这个 Team 没有 patrol_team_baselines 行，下一轮巡逻会给它建基线。"""
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, access_token, device_id, seats_entitled,
                                   is_codex_enabled, status, owner_email,
                                   patrol_grandfathered_at, display_synced_at,
                                   created_at, updated_at)
               VALUES (?, ?, 'fake-token', 'fake-device', 1, 0, 'active', ?, ?, ?,
                       '2026-01-01', '2026-01-01')""",
            (TEAM_ID, TEAM_ID, OWNER[1], GRANDFATHERED_AT if grandfathered else None, now_iso),
        )
        for (user_id, email), source in ((PAID, "system"), (STRANGER, "detected")):
            conn.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked,
                    first_seen_at, source, created_at)
                   VALUES (?, ?, ?, NULL, 0, 0, ?, ?, ?)""",
                (TEAM_ID, user_id, email, NOW, source, NOW),
            )
        members = [
            _cached_member(OWNER, None, seat_type="usage_based", is_owner=True),
            _cached_member(PAID, "system"),
            _cached_member(STRANGER, "detected"),
            _cached_member(ROWLESS, None),
        ]
        pending = [_cached_invite(ROWLESS_INVITE, None)]
        conn.execute(
            """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            (TEAM_ID, json.dumps(members), json.dumps(pending), NOW),
        )
        for key, value in (("patrol_kick_enabled", "1"), ("patrol_baseline_at", NOW)):
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value, NOW),
            )
        conn.commit()
        conn.close()

    def _sources(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT email, source FROM member_expiry WHERE team_id = ? AND kicked = 0",
            (TEAM_ID,),
        ).fetchall()
        conn.close()
        return {row["email"]: row["source"] for row in rows}

    def _marker(self):
        conn = self._conn()
        row = conn.execute(
            "SELECT patrol_grandfathered_at FROM teams WHERE id = ?", (TEAM_ID,)
        ).fetchone()
        conn.close()
        return row["patrol_grandfathered_at"]

    def _init_log_detail(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT detail, result FROM operation_logs WHERE action = 'patrol_team_initialize' "
            "ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "success")
        return rows[0]["detail"]

    def _run_sync(self):
        """跑一轮真实的 data_sync_job（上游打桩，巡逻本身替换掉，只看建档结果）。"""
        from app import scheduler as app_scheduler
        from app.services import tg_notify, tg_summary

        with patch.object(app_scheduler, "ChatGPTClient", _UpstreamClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync",
                          lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_recovery_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_failure_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None), \
             patch.object(patrol, "run_patrol", MagicMock(return_value={})), \
             patch.object(tg_notify, "notify_admins_sync", lambda *a, **kw: None), \
             patch.object(tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None):
            app_scheduler.data_sync_job()

    def _dry_run(self):
        result = patrol.run_patrol(dry_run=True, allow_team_ids=[TEAM_ID])
        kicks = sorted(e["email"] for e in result["events"] if e.get("action") == "would_kick")
        revokes = sorted(
            e["email"] for e in result["events"] if e.get("action") == "would_revoke_invite"
        )
        return kicks, revokes

    # ── tests ─────────────────────────────────────────────────────────────

    def test_auto_rebaseline_does_not_protect_rowless_members(self):
        self._seed(grandfathered=True)

        result = patrol.run_patrol(dry_run=False, allow_team_ids=[TEAM_ID])

        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["invites_revoked"], 0)
        # 没有给 rowless / rowless-invite 造 system 行；已有的行原样保留。
        self.assertEqual(
            self._sources(),
            {PAID[1]: "system", STRANGER[1]: "detected"},
        )
        detail = self._init_log_detail()
        self.assertIn("grandfathered=0", detail)
        self.assertIn("backfilled=0", detail)
        self.assertIn("detected_kept=1", detail)
        self.assertEqual(self._marker(), GRANDFATHERED_AT)

    def test_rowless_members_become_candidates_after_next_sync(self):
        self._seed(grandfathered=True)
        patrol.run_patrol(dry_run=False, allow_team_ids=[TEAM_ID])

        self._run_sync()

        self.assertEqual(
            self._sources(),
            {
                PAID[1]: "system",
                STRANGER[1]: "detected",
                ROWLESS[1]: "detected",
                ROWLESS_INVITE: "detected",
            },
        )
        kicks, revokes = self._dry_run()
        # seats_entitled=1，三个 default 席位 → 超 2；system 的 paid 永不进候选。
        self.assertEqual(kicks, [ROWLESS[1], STRANGER[1]])
        self.assertEqual(revokes, [ROWLESS_INVITE])

    def test_explicit_activation_still_protects_every_current_member(self):
        self._seed(grandfathered=True)

        result = patrol.activate_patrol_sync([TEAM_ID])

        self.assertEqual(result["grandfathered"], 1)
        self.assertEqual(result["backfilled"], 2)
        self.assertEqual(
            self._sources(),
            {
                PAID[1]: "system",
                STRANGER[1]: "system",
                ROWLESS[1]: "system",
                ROWLESS_INVITE: "system",
            },
        )
        self.assertEqual(self._marker(), GRANDFATHERED_AT)
        self.assertEqual(self._dry_run(), ([], []))

    def test_first_auto_baseline_still_protects_every_current_member(self):
        self._seed(grandfathered=False)

        result = patrol.run_patrol(dry_run=False, allow_team_ids=[TEAM_ID])

        self.assertEqual(result["kicked"], 0)
        self.assertEqual(
            self._sources(),
            {
                PAID[1]: "system",
                STRANGER[1]: "system",
                ROWLESS[1]: "system",
                ROWLESS_INVITE: "system",
            },
        )
        detail = self._init_log_detail()
        self.assertIn("grandfathered=1", detail)
        self.assertIn("backfilled=2", detail)
        self.assertIn("detected_kept=0", detail)
        self.assertTrue(self._marker())
        self.assertEqual(self._dry_run(), ([], []))


if __name__ == "__main__":
    unittest.main()
