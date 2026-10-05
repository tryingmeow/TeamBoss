import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.services import patrol


def _init_temp_db() -> None:
    """用真正的 init_database() 建表，保证 schema 和生产完全一致。

    Assumes get_db_dir() has already been patched by the caller.
    """
    asyncio.run(app_database.init_database())


def _member(email, user_id, *, seat_type="default", is_owner=False,
            source="detected", first_seen_at=None, created_time=None):
    return {
        "id": user_id,
        "email": email,
        "seat_type": seat_type,
        "is_owner": is_owner,
        "source": source,
        "first_seen_at": first_seen_at,
        "created_time": created_time,
        "status": "active",
    }


def _insert_team(conn, team_id, *, name=None, is_codex_enabled=0, seats_entitled=2,
                  status="active", proxy_id=None):
    conn.execute(
        """INSERT INTO teams (id, name, access_token, device_id, proxy_id,
                               seats_entitled, is_codex_enabled, status, created_at, updated_at)
           VALUES (?, ?, 'fake-token', 'fake-device', ?, ?, ?, ?, '2026-01-01', '2026-01-01')""",
        (team_id, name or team_id, proxy_id, seats_entitled, is_codex_enabled, status),
    )
    conn.commit()


def _insert_member_cache(conn, team_id, members):
    conn.execute(
        """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
           VALUES (?, ?, '[]', '2026-01-01')
           ON CONFLICT(team_id) DO UPDATE SET members_json = excluded.members_json""",
        (team_id, json.dumps(members)),
    )
    conn.commit()


def _set_setting(conn, key, value):
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, '2026-01-01')
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )
    conn.commit()


def _insert_team_baseline(conn, team_id, baseline_at="2026-07-01T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO patrol_team_baselines (team_id, baseline_at) VALUES (?, ?)",
        (team_id, baseline_at),
    )
    conn.commit()


class RaisingChatGPTClient:
    """站岗用的假客户端：一旦被真的调用 remove_member 就直接让测试失败。

    专门用来断言"这条路径绝不应该真的去踢人"。
    """

    def __init__(self, *args, **kwargs):
        pass

    def remove_member(self, user_id):  # pragma: no cover - 不应该被调用到
        raise AssertionError(f"remove_member should never be called in this scenario (user_id={user_id})")


class PatrolTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_dir = self._tmpdir.name

        # monkeypatch get_db_dir() to return test database directory
        self.get_db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.db_dir)
        self.get_db_dir_patch.start()

        # Initialize temp database with patched get_db_dir
        _init_temp_db()
        self.db_path = app_database.get_db_path()

        # 屏蔽真实 Telegram 推送，只记录调用过的文本
        self.notify_calls = []
        self._orig_notify = patrol.notify_admins_sync
        patrol.notify_admins_sync = lambda text, **kw: self.notify_calls.append(text)

        # 默认拒绝任何真实网络调用；单个测试可以自行覆盖
        self._orig_client = patrol.ChatGPTClient
        self._orig_run_call = patrol.run_chatgpt_call_sync

    def tearDown(self):
        self.get_db_dir_patch.stop()
        patrol.notify_admins_sync = self._orig_notify
        patrol.ChatGPTClient = self._orig_client
        patrol.run_chatgpt_call_sync = self._orig_run_call
        self._tmpdir.cleanup()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _patrol(self, **kwargs):
        """跑一轮巡逻，白名单 = 当前所有 active team。

        run_patrol 现在收的是白名单（只巡逻本轮刚刷新成功的 team），没有默认值：
        忘记传 = 什么都不做。这些用例关心的是巡逻自身的判定，所以统一把全部
        active team 放进白名单；白名单本身的行为由 PatrolAllowListTest 覆盖。
        """
        conn = self._conn()
        team_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM teams WHERE status = 'active'"
            ).fetchall()
        ]
        conn.close()
        return patrol.run_patrol(allow_team_ids=team_ids, **kwargs)

    def _operation_logs(self, action=None):
        conn = self._conn()
        if action:
            rows = conn.execute(
                "SELECT * FROM operation_logs WHERE action = ?", (action,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM operation_logs").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── (a) system / self_service / owner 永不入选 ──────────────────────

    def test_select_candidates_excludes_system_self_service_and_owner(self):
        members = [
            _member("system@x.com", "u1", source="system", first_seen_at="2026-07-01T00:00:00Z"),
            _member("selfservice@x.com", "u2", source="self_service", first_seen_at="2026-07-01T00:00:00Z"),
            _member("owner@x.com", "u3", source="detected", is_owner=True, first_seen_at="2026-07-01T00:00:00Z"),
            _member("nosource@x.com", "u4", source=None, first_seen_at="2026-07-01T00:00:00Z"),
            _member("detected@x.com", "u5", source="detected", first_seen_at="2026-07-01T00:00:00Z"),
        ]
        candidates = patrol.select_kick_candidates(members)
        emails = [c["email"] for c in candidates]
        self.assertEqual(emails, ["detected@x.com"])

    # ── (b) member_cache 缺失/空 → 整队跳过,绝不动手 ─────────────────────

    def test_run_patrol_skips_team_with_missing_or_empty_cache(self):
        conn = self._conn()
        _insert_team(conn, "team-missing-cache", seats_entitled=1)
        # 完全没有 member_cache 行

        _insert_team(conn, "team-empty-cache", seats_entitled=1)
        _insert_member_cache(conn, "team-empty-cache", [])  # 空列表

        _set_setting(conn, "patrol_kick_enabled", "1")
        conn.close()

        result = self._patrol(dry_run=True)

        self.assertEqual(result["events"], [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["would_kick"], 0)
        self.assertEqual(self._operation_logs(), [])
        self.assertEqual(self.notify_calls, [])

    # ── (c) codex 开 → 无论超不超都不踢 ─────────────────────────────────

    def test_run_patrol_skips_kicking_when_codex_enabled(self):
        conn = self._conn()
        _insert_team(conn, "team-codex-on", is_codex_enabled=1, seats_entitled=1)
        _insert_member_cache(conn, "team-codex-on", [
            _member("a@x.com", "u1", first_seen_at="2026-07-01T00:00:00Z"),
            _member("b@x.com", "u2", first_seen_at="2026-07-02T00:00:00Z"),
            _member("owner@x.com", "u3", is_owner=True, source=None, seat_type="usage_based"),
        ])
        _set_setting(conn, "patrol_kick_enabled", "1")
        conn.close()

        result = self._patrol(dry_run=False)

        self.assertEqual(result["events"], [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["would_kick"], 0)
        self.assertEqual(self._operation_logs(), [])

    # ── (d) 只选最新的 over_by 个 ────────────────────────────────────────

    def test_run_patrol_selects_only_newest_over_by_candidates(self):
        conn = self._conn()
        _insert_team(conn, "team-overage", seats_entitled=1)  # over_by 用 active_chatgpt - 1 算
        _insert_member_cache(conn, "team-overage", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("oldest@x.com", "u-old", first_seen_at="2026-07-01T00:00:00Z"),
            _member("middle@x.com", "u-mid", first_seen_at="2026-07-05T00:00:00Z"),
            _member("newest@x.com", "u-new", first_seen_at="2026-07-10T00:00:00Z"),
        ])
        # active_chatgpt = 3 (三个 default 席位), seats_entitled = 1 => over_by = 2
        _set_setting(conn, "patrol_kick_enabled", "1")
        conn.close()

        result = self._patrol(dry_run=True)  # dry_run 强制空跑，安全地看会选中谁

        would_kick_events = [e for e in result["events"] if e.get("action") == "would_kick"]
        emails = [e["email"] for e in would_kick_events]
        self.assertEqual(len(emails), 2)
        self.assertEqual(set(emails), {"newest@x.com", "middle@x.com"})
        self.assertNotIn("oldest@x.com", emails)
        self.assertEqual(result["would_kick"], 2)
        self.assertEqual(result["kicked"], 0)

    # ── (e) source='system' 无论多"新"都绝不踢（取代旧的 baseline 时间戳门槛）──

    def test_run_patrol_never_selects_system_sourced_members_regardless_of_recency(self):
        conn = self._conn()
        _insert_team(conn, "team-grandfathered", seats_entitled=1)
        _insert_member_cache(conn, "team-grandfathered", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            # system 来源，即便 first_seen_at 是"刚刚"，也绝不能被当成候选
            _member("protected@x.com", "u-sys", source="system", first_seen_at="2026-07-20T23:59:00Z"),
            _member("real-candidate@x.com", "u-det", source="detected", first_seen_at="2026-07-10T00:00:00Z"),
        ])
        # active_chatgpt = 2, seats_entitled = 1 => over_by = 1
        _set_setting(conn, "patrol_kick_enabled", "1")
        conn.close()

        result = self._patrol(dry_run=True)

        would_kick_events = [e for e in result["events"] if e.get("action") == "would_kick"]
        emails = [e["email"] for e in would_kick_events]
        self.assertEqual(emails, ["real-candidate@x.com"])
        self.assertNotIn("protected@x.com", emails)

    # ── (f) patrol_kick_enabled != '1' → 即便调用方传 dry_run=False 也强制空跑 ──

    def test_run_patrol_forced_dry_run_when_kick_disabled_in_settings(self):
        conn = self._conn()
        _insert_team(conn, "team-kick-off", seats_entitled=0)
        _insert_member_cache(conn, "team-kick-off", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("candidate@x.com", "u-det", source="detected", first_seen_at="2026-07-10T00:00:00Z"),
        ])
        _set_setting(conn, "patrol_kick_enabled", "0")  # 关键：踢人总开关是关的
        conn.close()

        # 站岗：如果代码试图真的去踢人，RaisingChatGPTClient.remove_member 会让测试失败
        patrol.ChatGPTClient = RaisingChatGPTClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)  # 调用方明确要求"别空跑"

        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["would_kick"], 1)
        would_kick_events = [e for e in result["events"] if e.get("action") == "would_kick"]
        self.assertEqual([e["email"] for e in would_kick_events], ["candidate@x.com"])

        logs = self._operation_logs("patrol_kick")
        self.assertEqual(logs, [])  # 绝不应该出现真踢的日志
        dryrun_logs = self._operation_logs("patrol_would_kick")
        self.assertEqual(len(dryrun_logs), 1)
        self.assertEqual(dryrun_logs[0]["result"], "dryrun")

    # ── 额外：豁免车队即便超额也绝不自动处理 ─────────────────────────────

    def test_run_patrol_never_touches_exempt_team(self):
        conn = self._conn()
        _insert_team(conn, "team-exempt", seats_entitled=0)
        _insert_member_cache(conn, "team-exempt", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("candidate@x.com", "u-det", source="detected", first_seen_at="2026-07-10T00:00:00Z"),
        ])
        _set_setting(conn, "patrol_kick_enabled", "1")
        _set_setting(conn, "patrol_exempt_team_ids", json.dumps(["team-exempt"]))
        conn.close()

        patrol.ChatGPTClient = RaisingChatGPTClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)

        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["would_kick"], 0)
        exempt_events = [e for e in result["events"] if e.get("action") == "exempt_skip"]
        self.assertEqual(len(exempt_events), 1)
        self.assertEqual(exempt_events[0]["team_id"], "team-exempt")
        # 仍然应该通知管理员有超额但被豁免了
        self.assertTrue(any("豁免" in text for text in self.notify_calls))

    def test_activate_patrol_protects_complete_snapshot_and_enables_atomically(self):
        conn = self._conn()
        _insert_team(conn, "team-activate", seats_entitled=2)
        members = [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("detected@x.com", "u-detected", source="detected"),
            _member("untracked@x.com", "u-untracked", source=None),
        ]
        pending = [{
            "id": "invite-1",
            "email": "pending@x.com",
            "seat_type": "default",
            "is_owner": False,
            "source": None,
            "status": "pending",
        }]
        conn.execute(
            """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
               VALUES (?, ?, ?, '2026-07-21')""",
            ("team-activate", json.dumps(members), json.dumps(pending)),
        )
        conn.execute(
            """INSERT INTO member_expiry (team_id, user_id, email, kicked, source, created_at)
               VALUES ('team-activate', 'u-detected', 'detected@x.com', 0, 'detected', '2026-07-20')"""
        )
        conn.commit()
        conn.close()

        result = patrol.activate_patrol_sync(["team-activate"])

        self.assertTrue(result["kick_enabled"])
        self.assertEqual(result["grandfathered"], 1)
        self.assertEqual(result["backfilled"], 2)
        conn = self._conn()
        settings = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('patrol_kick_enabled', 'patrol_baseline_at')"
            ).fetchall()
        }
        sources = {
            row["email"]: row["source"]
            for row in conn.execute(
                "SELECT email, source FROM member_expiry WHERE team_id = 'team-activate' AND kicked = 0"
            ).fetchall()
        }
        cache = conn.execute(
            "SELECT members_json, pending_json FROM member_cache WHERE team_id = 'team-activate'"
        ).fetchone()
        conn.close()
        self.assertEqual(settings["patrol_kick_enabled"], "1")
        self.assertTrue(settings["patrol_baseline_at"])
        self.assertEqual(
            sources,
            {
                "detected@x.com": "system",
                "untracked@x.com": "system",
                "pending@x.com": "system",
            },
        )
        cached_people = json.loads(cache["members_json"]) + json.loads(cache["pending_json"])
        self.assertTrue(all(
            item.get("source") == "system"
            for item in cached_people
            if not item.get("is_owner")
        ))
        conn = self._conn()
        team_baseline = conn.execute(
            "SELECT baseline_at FROM patrol_team_baselines WHERE team_id = 'team-activate'"
        ).fetchone()
        conn.close()
        self.assertTrue(team_baseline["baseline_at"])

    def test_new_active_team_is_protected_before_live_patrol(self):
        conn = self._conn()
        _insert_team(conn, "existing", seats_entitled=2)
        _insert_member_cache(conn, "existing", [
            _member("existing@x.com", "u-existing", source="system"),
        ])
        conn.close()
        patrol.activate_patrol_sync(["existing"])

        conn = self._conn()
        _insert_team(conn, "new-team", seats_entitled=0)
        _insert_member_cache(conn, "new-team", [
            _member("original@x.com", "u-original", source="detected"),
        ])
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, kicked, source, created_at)
               VALUES ('new-team', 'u-original', 'original@x.com', 0, 'detected', '2026-07-22')"""
        )
        conn.commit()
        conn.close()

        patrol.ChatGPTClient = RaisingChatGPTClient
        result = self._patrol(dry_run=False)

        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["would_kick"], 0)
        conn = self._conn()
        source = conn.execute(
            "SELECT source FROM member_expiry WHERE team_id='new-team' AND user_id='u-original'"
        ).fetchone()["source"]
        baseline = conn.execute(
            "SELECT baseline_at FROM patrol_team_baselines WHERE team_id='new-team'"
        ).fetchone()
        conn.close()
        self.assertEqual(source, "system")
        self.assertIsNotNone(baseline)
        self.assertEqual(len(self._operation_logs("patrol_team_initialize")), 1)

    def test_activate_patrol_rolls_back_when_any_snapshot_is_empty(self):
        conn = self._conn()
        _insert_team(conn, "team-empty-activation")
        _insert_member_cache(conn, "team-empty-activation", [])
        conn.execute(
            """INSERT INTO member_expiry (team_id, user_id, email, kicked, source, created_at)
               VALUES ('team-empty-activation', 'u1', 'candidate@x.com', 0, 'detected', '2026-07-20')"""
        )
        conn.commit()
        conn.close()

        with self.assertRaises(patrol.PatrolActivationError):
            patrol.activate_patrol_sync(["team-empty-activation"])

        conn = self._conn()
        kick_enabled = conn.execute(
            "SELECT value FROM settings WHERE key = 'patrol_kick_enabled'"
        ).fetchone()["value"]
        source = conn.execute(
            "SELECT source FROM member_expiry WHERE email = 'candidate@x.com'"
        ).fetchone()["source"]
        conn.close()
        self.assertEqual(kick_enabled, "0")
        self.assertEqual(source, "detected")

    # ── 额外：候选不够 over_by 时只踢检测到的,绝不动系统成员凑数 ──────────

    def test_run_patrol_kicks_only_available_detected_when_insufficient(self):
        conn = self._conn()
        _insert_team(conn, "team-insufficient", seats_entitled=1)
        _insert_member_cache(conn, "team-insufficient", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("system-a@x.com", "u-sys-a", source="system", first_seen_at="2026-07-01T00:00:00Z"),
            _member("system-b@x.com", "u-sys-b", source="system", first_seen_at="2026-07-02T00:00:00Z"),
            _member("only-candidate@x.com", "u-det", source="detected", first_seen_at="2026-07-10T00:00:00Z"),
        ])
        # 4 个 active 成员，owner 是 usage_based 不占 chatgpt 席位 => active_chatgpt = 3
        # seats_entitled = 1 => over_by = 2，但只有 1 个 detected 候选可踢
        _set_setting(conn, "patrol_kick_enabled", "1")
        conn.close()

        result = self._patrol(dry_run=True)

        would_kick_events = [e for e in result["events"] if e.get("action") == "would_kick"]
        self.assertEqual([e["email"] for e in would_kick_events], ["only-candidate@x.com"])
        self.assertTrue(any("仅 1 个外部成员可移除" in text for text in self.notify_calls))

    # ── 额外：真踢通路 — 开关打开时真正走 _patrol_kick，成功后写 kicked=1 ──

    def test_run_patrol_live_kick_marks_member_expiry_kicked(self):
        conn = self._conn()
        _insert_team(conn, "team-live", seats_entitled=0)
        _insert_member_cache(conn, "team-live", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("candidate@x.com", "u-det", source="detected", first_seen_at="2026-07-10T00:00:00Z"),
        ])
        conn.execute(
            """INSERT INTO member_expiry (team_id, user_id, email, kicked, source, created_at)
               VALUES ('team-live', 'u-det', 'candidate@x.com', 0, 'detected', '2026-07-10')"""
        )
        _set_setting(conn, "patrol_kick_enabled", "1")
        _set_setting(conn, "patrol_baseline_at", "2026-07-01T00:00:00+00:00")
        _insert_team_baseline(conn, "team-live")
        conn.commit()
        conn.close()

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            def remove_member(self, user_id):
                self.removed = user_id
                return {"status": "ok"}

        patrol.ChatGPTClient = FakeClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)

        self.assertEqual(result["kicked"], 1)
        self.assertEqual(result["would_kick"], 0)

        conn = self._conn()
        row = conn.execute(
            "SELECT kicked, kick_source FROM member_expiry WHERE team_id='team-live' AND user_id='u-det'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["kicked"], 1)
        self.assertEqual(row["kick_source"], "patrol")

        logs = self._operation_logs("patrol_kick")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["result"], "success")

    def test_central_kick_gate_rejects_persisted_system_even_if_cache_says_detected(self):
        """保护基线更新 DB 后即使缓存尚未刷新，system 成员也绝不能被踢。"""
        conn = self._conn()
        _insert_team(conn, "team-system-gate", seats_entitled=0)
        candidate = _member("protected@x.com", "u-system", source="detected")
        _insert_member_cache(conn, "team-system-gate", [candidate])
        conn.execute(
            """INSERT INTO member_expiry (team_id, user_id, email, kicked, source, created_at)
               VALUES ('team-system-gate', 'u-system', 'protected@x.com', 0, 'system', '2026-07-10')"""
        )
        _set_setting(conn, "patrol_kick_enabled", "1")
        _set_setting(conn, "patrol_baseline_at", "2026-07-01T00:00:00+00:00")
        _insert_team_baseline(conn, "team-system-gate")

        ok, reason = patrol._patrol_kick(
            conn, RaisingChatGPTClient(), "team-system-gate", candidate
        )
        conn.close()

        self.assertFalse(ok)
        self.assertIn("persisted source is not detected", reason)

    def test_central_kick_gate_rejects_codex_on(self):
        conn = self._conn()
        _insert_team(conn, "team-codex-gate", seats_entitled=0, is_codex_enabled=1)
        candidate = _member("candidate@x.com", "u-det", source="detected")
        _insert_member_cache(conn, "team-codex-gate", [candidate])
        conn.execute(
            """INSERT INTO member_expiry (team_id, user_id, email, kicked, source, created_at)
               VALUES ('team-codex-gate', 'u-det', 'candidate@x.com', 0, 'detected', '2026-07-10')"""
        )
        _set_setting(conn, "patrol_kick_enabled", "1")
        _set_setting(conn, "patrol_baseline_at", "2026-07-01T00:00:00+00:00")
        _insert_team_baseline(conn, "team-codex-gate")

        ok, reason = patrol._patrol_kick(
            conn, RaisingChatGPTClient(), "team-codex-gate", candidate
        )
        conn.close()

        self.assertFalse(ok)
        self.assertIn("codex is enabled", reason)

    def test_central_kick_gate_rejects_team_not_over_quota(self):
        conn = self._conn()
        _insert_team(conn, "team-not-over", seats_entitled=1)
        candidate = _member("candidate@x.com", "u-det", source="detected")
        _insert_member_cache(conn, "team-not-over", [candidate])
        conn.execute(
            """INSERT INTO member_expiry (team_id, user_id, email, kicked, source, created_at)
               VALUES ('team-not-over', 'u-det', 'candidate@x.com', 0, 'detected', '2026-07-10')"""
        )
        _set_setting(conn, "patrol_kick_enabled", "1")
        _set_setting(conn, "patrol_baseline_at", "2026-07-01T00:00:00+00:00")
        _insert_team_baseline(conn, "team-not-over")

        ok, reason = patrol._patrol_kick(
            conn, RaisingChatGPTClient(), "team-not-over", candidate
        )
        conn.close()

        self.assertFalse(ok)
        self.assertIn("not currently over quota", reason)

    # ── classify_team：纯函数风险分类，供 GET /api/patrol/status 复用 ────

    def test_classify_team_risk_levels(self):
        ok_status = patrol.classify_team(
            team_id="t1", name="T1", codex_enabled=True, seats_entitled=1,
            members=[_member("a@x.com", "u1", source="detected")] * 5,
        )
        self.assertEqual(ok_status["risk"], "ok")

        watch_status = patrol.classify_team(
            team_id="t2", name="T2", codex_enabled=False, seats_entitled=5,
            members=[_member("a@x.com", "u1", source="detected")],
        )
        self.assertEqual(watch_status["risk"], "watch")

        over_status = patrol.classify_team(
            team_id="t3", name="T3", codex_enabled=False, seats_entitled=1,
            members=[
                _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
                _member("a@x.com", "u1", source="detected", first_seen_at="2026-07-01T00:00:00Z"),
                _member("b@x.com", "u2", source="detected", first_seen_at="2026-07-05T00:00:00Z"),
            ],
        )
        self.assertEqual(over_status["risk"], "over")
        self.assertEqual(over_status["over_by"], 1)
        self.assertEqual(len(over_status["detected_over"]), 1)
        self.assertEqual(over_status["detected_over"][0]["email"], "b@x.com")


# ═══════════════════════════════════════════════════════════════════════════
# 新增：陌生 pending invite 自动撤销 + 严格模式 + 每个 team 独立判断（不再一票否决）
#
# 独立的测试基类（不复用/不修改上面的 PatrolTest），避免任何风险影响已有 16 个用例。
# ═══════════════════════════════════════════════════════════════════════════

def _pending(email, *, source="detected", first_seen_at=None, created_time=None, invite_id=None):
    return {
        "id": invite_id or f"invite-{email}",
        "email": email,
        "seat_type": "default",
        "is_owner": False,
        "source": source,
        "first_seen_at": first_seen_at,
        "created_time": created_time,
        "status": "pending",
    }


def _insert_expiry_row(conn, team_id, *, user_id="", email="", source="detected",
                        first_seen_at=None, expires_at=None, kicked=0, created_at="2026-01-01"):
    conn.execute(
        """INSERT INTO member_expiry
           (team_id, user_id, email, expires_at, auto_kick, kicked, first_seen_at, source, created_at)
           VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)""",
        (team_id, user_id, email, expires_at, kicked, first_seen_at, source, created_at),
    )
    conn.commit()


def _set_pending_cache(conn, team_id, pending):
    """member_cache 里的 pending_json 单独更新（_insert_member_cache 只覆盖 members_json）。"""
    conn.execute(
        "UPDATE member_cache SET pending_json = ? WHERE team_id = ?",
        (json.dumps(pending), team_id),
    )
    conn.commit()


class RaisingInviteClient:
    """站岗：一旦真的调用 revoke_invite 就让测试失败。"""

    def __init__(self, *args, **kwargs):
        pass

    def revoke_invite(self, email):  # pragma: no cover
        raise AssertionError(f"revoke_invite should never be called (email={email})")

    def remove_member(self, user_id):  # pragma: no cover
        raise AssertionError(f"remove_member should never be called (user_id={user_id})")

    def get_members(self, offset=0, limit=100):  # pragma: no cover
        raise AssertionError("get_members should never be called")

    def get_pending_invites(self, offset=0, limit=100):  # pragma: no cover
        raise AssertionError("get_pending_invites should never be called")


class _PatrolNewFeaturesTestBase(unittest.TestCase):
    """与 PatrolTest 完全独立的 setUp/tearDown/helper 拷贝，绝不touch 已有测试类。"""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_dir = self._tmpdir.name

        # Patch get_db_dir() to return test database directory
        self.get_db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.db_dir)
        self.get_db_dir_patch.start()

        # Initialize temp database with patched get_db_dir
        _init_temp_db()
        self.db_path = app_database.get_db_path()

        self.notify_calls = []
        self._orig_notify = patrol.notify_admins_sync
        patrol.notify_admins_sync = lambda text, **kw: self.notify_calls.append(text)

        self._orig_client = patrol.ChatGPTClient
        self._orig_run_call = patrol.run_chatgpt_call_sync

    def tearDown(self):
        self.get_db_dir_patch.stop()
        patrol.notify_admins_sync = self._orig_notify
        patrol.ChatGPTClient = self._orig_client
        patrol.run_chatgpt_call_sync = self._orig_run_call
        self._tmpdir.cleanup()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _patrol(self, **kwargs):
        """跑一轮巡逻，白名单 = 当前所有 active team。

        run_patrol 现在收的是白名单（只巡逻本轮刚刷新成功的 team），没有默认值：
        忘记传 = 什么都不做。这些用例关心的是巡逻自身的判定，所以统一把全部
        active team 放进白名单；白名单本身的行为由 PatrolAllowListTest 覆盖。
        """
        conn = self._conn()
        team_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM teams WHERE status = 'active'"
            ).fetchall()
        ]
        conn.close()
        return patrol.run_patrol(allow_team_ids=team_ids, **kwargs)

    def _operation_logs(self, action=None):
        conn = self._conn()
        if action:
            rows = conn.execute(
                "SELECT * FROM operation_logs WHERE action = ?", (action,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM operation_logs").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _arm_globally(self, conn, *, baseline_at="2026-07-01T00:00:00+00:00", kick_enabled="1"):
        _set_setting(conn, "patrol_kick_enabled", kick_enabled)
        _set_setting(conn, "patrol_baseline_at", baseline_at)


# ── 一、陌生 pending invite 自动撤销 ──────────────────────────────────────────

class PatrolInviteRevokeTest(_PatrolNewFeaturesTestBase):

    def test_run_patrol_revokes_detected_invite_when_armed(self):
        conn = self._conn()
        _insert_team(conn, "team-invite", seats_entitled=5)
        _insert_member_cache(conn, "team-invite", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
        ])
        _set_pending_cache(conn, "team-invite", [
            _pending("stray@x.com", source="detected", first_seen_at="2026-07-10T00:00:00Z"),
        ])
        _insert_expiry_row(conn, "team-invite", email="stray@x.com", source="detected",
                            first_seen_at="2026-07-10T00:00:00Z")
        self._arm_globally(conn)
        _insert_team_baseline(conn, "team-invite")
        conn.close()

        class FakeInviteClient:
            def __init__(self, *a, **kw):
                pass

            def revoke_invite(self, email):
                self.revoked = email
                return {"status": "ok"}

        patrol.ChatGPTClient = FakeInviteClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)

        self.assertEqual(result["invites_revoked"], 1)
        self.assertEqual(result["invites_would_revoke"], 0)

        conn = self._conn()
        row = conn.execute(
            "SELECT kicked, kick_source FROM member_expiry WHERE team_id='team-invite' AND email='stray@x.com'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["kicked"], 1)
        self.assertEqual(row["kick_source"], "patrol_invite_revoke")

        logs = self._operation_logs("patrol_revoke_invite")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["result"], "success")
        self.assertTrue(any("已撤销" in t and "stray@x.com" in t for t in self.notify_calls))

    def test_run_patrol_never_revokes_system_or_self_service_invite(self):
        conn = self._conn()
        _insert_team(conn, "team-invite-safe", seats_entitled=5)
        _insert_member_cache(conn, "team-invite-safe", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
        ])
        _set_pending_cache(conn, "team-invite-safe", [
            _pending("grandfathered@x.com", source="system"),
            _pending("selfservice@x.com", source="self_service"),
        ])
        _insert_expiry_row(conn, "team-invite-safe", email="grandfathered@x.com", source="system")
        _insert_expiry_row(conn, "team-invite-safe", email="selfservice@x.com", source="self_service")
        self._arm_globally(conn)
        _insert_team_baseline(conn, "team-invite-safe")
        conn.close()

        patrol.ChatGPTClient = RaisingInviteClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)  # 若代码试图真的撤邀请，站岗类会让测试失败

        self.assertEqual(result["invites_revoked"], 0)
        self.assertEqual(self._operation_logs("patrol_revoke_invite"), [])

    def test_central_invite_gate_rejects_when_persisted_source_not_detected(self):
        """缓存说是 detected，但本地记录（更权威）已经是 system——绝不能撤。"""
        conn = self._conn()
        _insert_team(conn, "team-invite-race", seats_entitled=5)
        cached_invite = _pending("racer@x.com", source="detected")
        _insert_member_cache(conn, "team-invite-race", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
        ])
        _set_pending_cache(conn, "team-invite-race", [cached_invite])
        _insert_expiry_row(conn, "team-invite-race", email="racer@x.com", source="system")
        self._arm_globally(conn)
        _insert_team_baseline(conn, "team-invite-race")

        ok, reason = patrol._patrol_revoke_invite(conn, RaisingInviteClient(), "team-invite-race", cached_invite)
        conn.close()

        self.assertFalse(ok)
        self.assertIn("persisted source is not detected", reason)

    def test_pending_reconciliation_blocks_all_destructive_patrol_gates(self):
        """一条未补完的系统邀请记录必须同时拦住撤邀请、普通踢人和严格踢人。"""
        conn = self._conn()
        self._arm_globally(conn)
        _set_setting(conn, "patrol_strict_mode_enabled", "1")

        invite = _pending("pending-invite@x.com", source="detected")
        _insert_team(conn, "team-pending-invite", seats_entitled=5)
        _insert_member_cache(conn, "team-pending-invite", [])
        _set_pending_cache(conn, "team-pending-invite", [invite])
        _insert_expiry_row(
            conn, "team-pending-invite", email="pending-invite@x.com", source="detected"
        )
        _insert_team_baseline(conn, "team-pending-invite")

        normal_member = _member(
            "pending-member@x.com",
            "u-pending-member",
            source="detected",
            first_seen_at="2020-01-01T00:00:00Z",
        )
        _insert_team(conn, "team-pending-member", seats_entitled=0)
        _insert_member_cache(conn, "team-pending-member", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            normal_member,
        ])
        _insert_expiry_row(
            conn,
            "team-pending-member",
            user_id="u-pending-member",
            email="pending-member@x.com",
            source="detected",
            first_seen_at="2020-01-01T00:00:00Z",
        )
        _insert_team_baseline(conn, "team-pending-member")

        strict_member = _member(
            "pending-strict@x.com",
            "u-pending-strict",
            source="detected",
            first_seen_at="2020-01-01T00:00:00Z",
        )
        _insert_team(conn, "team-pending-strict", seats_entitled=99)
        _insert_member_cache(conn, "team-pending-strict", [strict_member])
        _insert_expiry_row(
            conn,
            "team-pending-strict",
            user_id="u-pending-strict",
            email="pending-strict@x.com",
            source="detected",
            first_seen_at="2020-01-01T00:00:00Z",
        )
        _insert_team_baseline(conn, "team-pending-strict")

        conn.executemany(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, source, reason, resolved, created_at)
               VALUES (?, ?, ?, 'system', 'database locked', 0, '2026-07-22')""",
            [
                ("team-pending-invite", "", "pending-invite@x.com"),
                ("team-pending-member", "u-pending-member", "pending-member@x.com"),
                ("team-pending-strict", "u-pending-strict", "pending-strict@x.com"),
            ],
        )
        conn.commit()

        invite_ok, invite_reason = patrol._patrol_revoke_invite(
            conn, RaisingInviteClient(), "team-pending-invite", invite
        )
        member_ok, member_reason = patrol._patrol_kick(
            conn, RaisingInviteClient(), "team-pending-member", normal_member
        )
        strict_ok, strict_reason = patrol._patrol_strict_kick(
            conn, RaisingInviteClient(), "team-pending-strict", strict_member
        )
        conn.close()

        self.assertEqual(
            (invite_ok, member_ok, strict_ok),
            (False, False, False),
        )
        for reason in (invite_reason, member_reason, strict_reason):
            self.assertIn("reconciliation is still pending", reason)


# ── 二、严格模式 ──────────────────────────────────────────────────────────────

class PatrolStrictModeTest(_PatrolNewFeaturesTestBase):

    def test_run_patrol_strict_mode_disabled_by_default_no_behavior_change(self):
        """patrol_strict_mode_enabled 默认是 '0'——即便有天然会被严格模式抓到的候选，
        且团队开了 Codex（普通模式完全跳过），也绝不能有任何严格模式相关的动作/日志/通知。
        """
        conn = self._conn()
        _insert_team(conn, "team-strict-off", seats_entitled=99, is_codex_enabled=1)
        _insert_member_cache(conn, "team-strict-off", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("stray@x.com", "u-stray", source="detected",
                     first_seen_at="2020-01-01T00:00:00Z"),
        ])
        _insert_expiry_row(conn, "team-strict-off", email="stray@x.com", user_id="u-stray",
                            source="detected", first_seen_at="2020-01-01T00:00:00Z")
        self._arm_globally(conn)
        _insert_team_baseline(conn, "team-strict-off")
        # 确认默认值确实是 '0'（数据库初始化写入的默认值），不手动设置
        settings_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'patrol_strict_mode_enabled'"
        ).fetchone()
        conn.close()
        self.assertEqual(settings_row["value"], "0")

        patrol.ChatGPTClient = RaisingInviteClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)

        self.assertEqual(result["strict_kicked"], 0)
        self.assertEqual(result["strict_would_kick"], 0)
        strict_logs = [
            log for log in self._operation_logs()
            if str(log.get("action", "")).startswith("patrol_strict")
        ]
        self.assertEqual(strict_logs, [])
        self.assertFalse(any("严格模式" in t for t in self.notify_calls))

    def test_run_patrol_strict_mode_kicks_codex_team_member_ignoring_quota(self):
        """核心场景：严格模式必须能处理 Codex 队、且不看超没超员。"""
        conn = self._conn()
        _insert_team(conn, "team-strict-codex", seats_entitled=99, is_codex_enabled=1)
        _insert_member_cache(conn, "team-strict-codex", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("stray@x.com", "u-stray", source="detected",
                     first_seen_at="2020-01-01T00:00:00Z"),
        ])
        _insert_expiry_row(conn, "team-strict-codex", email="stray@x.com", user_id="u-stray",
                            source="detected", first_seen_at="2020-01-01T00:00:00Z")
        self._arm_globally(conn)
        _set_setting(conn, "patrol_strict_mode_enabled", "1")
        _insert_team_baseline(conn, "team-strict-codex")
        conn.close()

        class FakeStrictClient:
            def __init__(self, *a, **kw):
                pass

            def get_members(self, offset=0, limit=100):
                if offset > 0:
                    return {"items": [], "total": 2}
                return {
                    "items": [
                        {"id": "u-owner", "email": "owner@x.com", "role": "account-owner",
                         "seat_type": "usage_based"},
                        {"id": "u-stray", "email": "stray@x.com", "role": "standard-user",
                         "seat_type": "default"},
                    ],
                    "total": 2,
                }

            def get_pending_invites(self, offset=0, limit=100):
                return {"items": [], "total": 0}

            def remove_member(self, user_id):
                self.removed = user_id
                return {"status": "ok"}

        patrol.ChatGPTClient = FakeStrictClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)

        self.assertEqual(result["strict_kicked"], 1)
        strict_events = [e for e in result["events"] if e.get("action") == "strict_kick"]
        self.assertEqual(len(strict_events), 1)
        self.assertEqual(strict_events[0]["result"], "success")

        conn = self._conn()
        row = conn.execute(
            "SELECT kicked, kick_source FROM member_expiry WHERE team_id='team-strict-codex' AND user_id='u-stray'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["kicked"], 1)
        self.assertEqual(row["kick_source"], "patrol_strict")

        logs = self._operation_logs("patrol_strict_kick")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["result"], "success")
        self.assertTrue(any("严格模式已处理" in t and "stray@x.com" in t for t in self.notify_calls))

    def test_run_patrol_strict_mode_waits_for_delay_and_does_not_renotify(self):
        conn = self._conn()
        _insert_team(conn, "team-strict-delay", seats_entitled=99)
        now_iso = datetime.now(timezone.utc).isoformat()
        _insert_member_cache(conn, "team-strict-delay", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("stray@x.com", "u-stray", source="detected", first_seen_at=now_iso),
        ])
        _insert_expiry_row(conn, "team-strict-delay", email="stray@x.com", user_id="u-stray",
                            source="detected", first_seen_at=now_iso)
        self._arm_globally(conn)
        _set_setting(conn, "patrol_strict_mode_enabled", "1")
        _set_setting(conn, "expiry_kick_delay_hours", "48")  # 48 小时等待期，管理员有时间反悔
        _insert_team_baseline(conn, "team-strict-delay")
        conn.close()

        patrol.ChatGPTClient = RaisingInviteClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result1 = self._patrol(dry_run=False)
        self.assertEqual(result1["strict_kicked"], 0)
        self.assertEqual(result1["strict_would_kick"], 0)
        flagged_logs = self._operation_logs("patrol_strict_flagged")
        self.assertEqual(len(flagged_logs), 1)
        self.assertTrue(any("检测到疑似陌生成员" in t and "stray@x.com" in t for t in self.notify_calls))

        notify_count_after_first_run = len(self.notify_calls)

        result2 = self._patrol(dry_run=False)  # 第二轮：仍在等待期内，不应重复通知/仍不动手
        self.assertEqual(result2["strict_kicked"], 0)
        self.assertEqual(len(self._operation_logs("patrol_strict_flagged")), 1)  # 没有新增
        self.assertEqual(len(self.notify_calls), notify_count_after_first_run)  # 没有重复推送

    def test_run_patrol_strict_mode_never_touches_members_with_expiry_record(self):
        """"有到期记录的人永远不进候选"——即便 source 是 detected 也不行。"""
        conn = self._conn()
        _insert_team(conn, "team-strict-expiry", seats_entitled=99)
        _insert_member_cache(conn, "team-strict-expiry", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("tracked@x.com", "u-tracked", source="detected",
                     first_seen_at="2020-01-01T00:00:00Z"),
        ])
        # cache 里手动打上 expires_at（正常流程里这是从 member_expiry 带过去的）
        conn.execute(
            "SELECT members_json FROM member_cache WHERE team_id='team-strict-expiry'"
        )
        cache_row = conn.execute(
            "SELECT members_json FROM member_cache WHERE team_id='team-strict-expiry'"
        ).fetchone()
        members = json.loads(cache_row["members_json"])
        for m in members:
            if m["email"] == "tracked@x.com":
                m["expires_at"] = "2026-12-31T00:00:00Z"
        conn.execute(
            "UPDATE member_cache SET members_json = ? WHERE team_id = 'team-strict-expiry'",
            (json.dumps(members),),
        )
        _insert_expiry_row(conn, "team-strict-expiry", email="tracked@x.com", user_id="u-tracked",
                            source="detected", first_seen_at="2020-01-01T00:00:00Z",
                            expires_at="2026-12-31T00:00:00Z")
        self._arm_globally(conn)
        _set_setting(conn, "patrol_strict_mode_enabled", "1")
        _insert_team_baseline(conn, "team-strict-expiry")
        conn.commit()
        conn.close()

        patrol.ChatGPTClient = RaisingInviteClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)

        self.assertEqual(result["strict_kicked"], 0)
        self.assertEqual(result["strict_would_kick"], 0)
        self.assertEqual(self._operation_logs("patrol_strict_flagged"), [])

    def test_run_patrol_strict_mode_batch_guard_blocks_mass_kick_no_api_call(self):
        """"别一次踢一片"：陌生人数超阈值时只报警，绝不自动处理，站岗类证明零 API 调用。"""
        conn = self._conn()
        _insert_team(conn, "team-strict-batch", seats_entitled=99)
        members = [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
        ]
        for i in range(3):  # team_size = 4 (owner + 3)；阈值 min(3, 4//2)=2，3 > 2 触发护栏
            email = f"stray{i}@x.com"
            members.append(_member(email, f"u-stray{i}", source="detected",
                                    first_seen_at="2020-01-01T00:00:00Z"))
            _insert_expiry_row(conn, "team-strict-batch", email=email, user_id=f"u-stray{i}",
                                source="detected", first_seen_at="2020-01-01T00:00:00Z")
        _insert_member_cache(conn, "team-strict-batch", members)
        self._arm_globally(conn)
        _set_setting(conn, "patrol_strict_mode_enabled", "1")
        _insert_team_baseline(conn, "team-strict-batch")
        conn.close()

        patrol.ChatGPTClient = RaisingInviteClient  # get_members/remove_member 任一被调用都会让测试失败
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = self._patrol(dry_run=False)

        self.assertEqual(result["strict_kicked"], 0)
        self.assertEqual(result["strict_would_kick"], 0)
        guard_events = [e for e in result["events"] if e.get("action") == "strict_batch_guard"]
        self.assertEqual(len(guard_events), 1)
        self.assertEqual(guard_events[0]["count"], 3)
        self.assertEqual(guard_events[0]["team_size"], 4)
        self.assertTrue(any("数量过多" in t and "3 / 团队共 4 人" in t for t in self.notify_calls))
        self.assertEqual(len(self._operation_logs("patrol_strict_batch_guard")), 1)

# ── 三、每个 team 独立判断（run_patrol 的 allow_team_ids 白名单） ────────────

class PatrolFailureIsolationTest(_PatrolNewFeaturesTestBase):

    def test_run_patrol_allow_list_isolates_unrefreshed_team_only(self):
        conn = self._conn()
        _insert_team(conn, "team-healthy", seats_entitled=0)
        _insert_member_cache(conn, "team-healthy", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("candidate@x.com", "u-healthy", source="detected",
                     first_seen_at="2026-07-10T00:00:00Z"),
        ])
        _insert_expiry_row(conn, "team-healthy", email="candidate@x.com", user_id="u-healthy",
                            source="detected", first_seen_at="2026-07-10T00:00:00Z")
        _insert_team_baseline(conn, "team-healthy")

        _insert_team(conn, "team-failed", seats_entitled=0)
        _insert_member_cache(conn, "team-failed", [
            _member("owner2@x.com", "u-owner2", is_owner=True, source=None, seat_type="usage_based"),
            _member("candidate2@x.com", "u-failed", source="detected",
                     first_seen_at="2026-07-10T00:00:00Z"),
        ])
        _insert_expiry_row(conn, "team-failed", email="candidate2@x.com", user_id="u-failed",
                            source="detected", first_seen_at="2026-07-10T00:00:00Z")
        _insert_team_baseline(conn, "team-failed")

        self._arm_globally(conn)
        conn.close()

        class SelectiveClient:
            def __init__(self, *a, **kw):
                pass

            def remove_member(self, user_id):
                if user_id == "u-failed":
                    raise AssertionError("team-failed should never be contacted this round")
                return {"status": "ok"}

        patrol.ChatGPTClient = SelectiveClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = patrol.run_patrol(dry_run=False, allow_team_ids={"team-healthy"})

        self.assertEqual(result["kicked"], 1)
        healthy_events = [e for e in result["events"] if e.get("team_id") == "team-healthy"]
        failed_events = [e for e in result["events"] if e.get("team_id") == "team-failed"]
        self.assertTrue(len(healthy_events) >= 1)
        self.assertEqual(failed_events, [])

        conn = self._conn()
        healthy_row = conn.execute(
            "SELECT kicked FROM member_expiry WHERE team_id='team-healthy' AND user_id='u-healthy'"
        ).fetchone()
        failed_row = conn.execute(
            "SELECT kicked FROM member_expiry WHERE team_id='team-failed' AND user_id='u-failed'"
        ).fetchone()
        conn.close()
        self.assertEqual(healthy_row["kicked"], 1)
        self.assertEqual(failed_row["kicked"], 0)  # 完全没被碰过

        failed_logs = [
            log for log in self._operation_logs()
            if log.get("team_id") == "team-failed"
        ]
        self.assertEqual(failed_logs, [])

    def test_empty_allow_list_patrols_nothing(self):
        """白名单是故意选的方向：失败关闭。

        黑名单在调用方提前 return / 抛异常 / 忘记传参时会交出一个空集合，巡逻就
        拿着每个 team 的陈旧缓存全量开工——按冻住的席位数判超员、按冻住的名单挑
        人。白名单的空集合意味着什么都不做。
        """
        conn = self._conn()
        _insert_team(conn, "team-a", seats_entitled=0)
        _insert_member_cache(conn, "team-a", [
            _member("owner@x.com", "u-owner", is_owner=True, source=None, seat_type="usage_based"),
            _member("candidate@x.com", "u-a", source="detected",
                     first_seen_at="2026-07-10T00:00:00Z"),
        ])
        _insert_expiry_row(conn, "team-a", email="candidate@x.com", user_id="u-a",
                            source="detected", first_seen_at="2026-07-10T00:00:00Z")
        _insert_team_baseline(conn, "team-a")
        self._arm_globally(conn)
        conn.close()

        patrol.ChatGPTClient = RaisingInviteClient
        patrol.run_chatgpt_call_sync = lambda func, *a, **kw: func(*a, **kw)

        result = patrol.run_patrol(dry_run=False, allow_team_ids=set())

        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["events"], [])


if __name__ == "__main__":
    unittest.main()
