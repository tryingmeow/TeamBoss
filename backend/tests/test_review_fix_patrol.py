"""巡逻基线 grandfather 只做一次 + seats_entitled 异常时不做超员踢人 的回归测试。

全部跑在 init_database() 建出的临时库上；ChatGPT 客户端一律替换成假对象，不发任何网络请求。
"""
import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import chatgpt_limiter
from app import database as app_database
from app import team_service
from app.models import TeamSession
from app.routes import teams as teams_routes
from app.routes import patrol as patrol_routes
from app.services import patrol


TEAM_ID = "00000000-0000-4000-8000-00000000b0b0"
NOW = "2026-07-20T00:00:00+00:00"


def _member(email, user_id, *, source="detected", seat_type="default", is_owner=False,
            first_seen_at=None):
    return {
        "id": user_id,
        "email": email,
        "seat_type": seat_type,
        "is_owner": is_owner,
        "source": source,
        "first_seen_at": first_seen_at,
        "status": "active",
    }


def _owner():
    return _member("owner@x.com", "u-owner", source=None, seat_type="usage_based", is_owner=True)


def _pending(email, *, source="detected"):
    return {
        "id": f"invite-{email}",
        "email": email,
        "seat_type": "default",
        "is_owner": False,
        "source": source,
        "status": "pending",
    }


def _insert_team(conn, team_id, *, seats_entitled=1, status="active", is_codex_enabled=0):
    conn.execute(
        """INSERT INTO teams (id, name, access_token, device_id, seats_entitled,
                               is_codex_enabled, status, created_at, updated_at)
           VALUES (?, ?, 'fake-token', 'fake-device', ?, ?, ?, '2026-01-01', '2026-01-01')""",
        (team_id, team_id, seats_entitled, is_codex_enabled, status),
    )
    conn.commit()


def _set_cache(conn, team_id, members, pending=()):
    conn.execute(
        """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(team_id) DO UPDATE SET members_json = excluded.members_json,
                                              pending_json = excluded.pending_json""",
        (team_id, json.dumps(list(members)), json.dumps(list(pending)), NOW),
    )
    conn.commit()


def _insert_expiry(conn, team_id, user_id, email, source):
    # 与 scheduler._reactivate_or_insert_detected_member 写出的"外部发现"行同形。
    conn.execute(
        """INSERT INTO member_expiry
           (team_id, user_id, email, expires_at, auto_kick, kicked, first_seen_at, source, created_at)
           VALUES (?, ?, ?, NULL, 0, 0, ?, ?, ?)""",
        (team_id, user_id, email, NOW, source, NOW),
    )
    conn.commit()


def _set_setting(conn, key, value):
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value, NOW),
    )
    conn.commit()


def _token() -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(days=10)).timestamp()),
            "https://api.openai.com/auth": {"chatgpt_account_id": TEAM_ID},
        },
        key="",
        algorithm="none",
    )


def _import_team_session(*, log_action, expected_team_id=None):
    """走真实的 upsert_team_from_session（新增 / 重新导入），上游调用全部打桩。"""

    async def fake_run(func, *args, **kwargs):
        name = getattr(func, "__name__", "")
        if name == "get_account_info":
            return {"accounts": {TEAM_ID: {"account": {"name": "Team B"}}}}
        if name == "get_subscription":
            return {"seats_entitled": 1}
        return {"error": "not needed for this test"}

    session = TeamSession(
        user={"email": "owner@x.com"},
        expires="2026-12-01T00:00:00Z",
        account={"id": TEAM_ID},
        accessToken=_token(),
        sessionToken="fresh-session",
    )
    with (
        patch.object(team_service, "run_chatgpt_call", new=fake_run),
        patch.object(team_service, "fetch_seat_pricing", new=AsyncMock(return_value={})),
        patch.object(team_service, "write_session_file"),
    ):
        asyncio.run(
            team_service.upsert_team_from_session(
                session, log_action=log_action, expected_team_id=expected_team_id
            )
        )


class RaisingClient:
    """站岗：任何上游写操作都让测试失败（基线建立的这一轮绝不能动手）。"""

    def __init__(self, *args, **kwargs):
        pass

    def remove_member(self, user_id):  # pragma: no cover
        raise AssertionError(f"remove_member must not be called (user_id={user_id})")

    def revoke_invite(self, email):  # pragma: no cover
        raise AssertionError(f"revoke_invite must not be called (email={email})")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self._tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        self.assertTrue(self.db_path.startswith(self._tmpdir.name))

        self.notify_calls = []
        for name, value in (
            ("notify_admins_sync", lambda text, **kw: self.notify_calls.append(text)),
            ("ChatGPTClient", RaisingClient),
            ("run_chatgpt_call_sync", lambda func, *a, **kw: func(*a, **kw)),
        ):
            p = patch.object(patrol, name, new=value)
            p.start()
            self.addCleanup(p.stop)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _sources(self, team_id):
        conn = self._conn()
        rows = conn.execute(
            "SELECT email, source FROM member_expiry WHERE team_id = ? AND kicked = 0",
            (team_id,),
        ).fetchall()
        conn.close()
        return {row["email"]: row["source"] for row in rows}

    def _marker(self, team_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT patrol_grandfathered_at FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        conn.close()
        return row["patrol_grandfathered_at"] if row else None

    def _baseline(self, team_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT baseline_at FROM patrol_team_baselines WHERE team_id = ?", (team_id,)
        ).fetchone()
        conn.close()
        return row["baseline_at"] if row else None

    def _logs(self, action):
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM operation_logs WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


class PatrolGrandfatherOnceTest(_Base):
    """grandfather（detected→system）只在一个 Team 第一次建立巡逻基线时发生。"""

    def _seed_team(self, team_id=TEAM_ID):
        """一个刚接入的 Team：original 是同步检测到的老成员，untracked 还没有任何记录。"""
        conn = self._conn()
        _insert_team(conn, team_id, seats_entitled=1)
        _set_cache(conn, team_id, [
            _owner(),
            _member("original@x.com", "u-original", source="detected"),
            _member("untracked@x.com", "u-untracked", source=None),
        ])
        _insert_expiry(conn, team_id, "u-original", "original@x.com", "detected")
        conn.close()

    def _stranger_joins(self, team_id=TEAM_ID):
        """巡逻基线之后有人从系统外混进来：同步按 detected 建档；另一人尚未建档。"""
        conn = self._conn()
        _insert_expiry(conn, team_id, "u-stranger", "stranger@x.com", "detected")
        cache = json.loads(conn.execute(
            "SELECT members_json FROM member_cache WHERE team_id = ?", (team_id,)
        ).fetchone()["members_json"])
        cache.append(_member("stranger@x.com", "u-stranger", source="detected"))
        cache.append(_member("rowless@x.com", "u-rowless", source=None))
        _set_cache(conn, team_id, cache)
        conn.close()

    def _dry_run_candidates(self, team_id=TEAM_ID):
        result = patrol.run_patrol(dry_run=True, allow_team_ids=[team_id])
        return sorted(e["email"] for e in result["events"] if e.get("action") == "would_kick")

    def test_first_activation_still_grandfathers_existing_members(self):
        self._seed_team()

        result = patrol.activate_patrol_sync([TEAM_ID])

        self.assertEqual(result["grandfathered"], 1)
        self.assertEqual(result["backfilled"], 1)
        self.assertEqual(result["detected_kept"], 0)
        self.assertEqual(
            self._sources(TEAM_ID),
            {"original@x.com": "system", "untracked@x.com": "system"},
        )
        self.assertTrue(self._marker(TEAM_ID))
        self.assertTrue(self._baseline(TEAM_ID))

    def test_second_activation_keeps_detected_strangers_detected(self):
        self._seed_team()
        patrol.activate_patrol_sync([TEAM_ID])
        first_marker = self._marker(TEAM_ID)
        self._stranger_joins()

        result = patrol.activate_patrol_sync([TEAM_ID])

        self.assertEqual(result["grandfathered"], 0)
        self.assertEqual(result["backfilled"], 1)
        self.assertEqual(result["detected_kept"], 1)
        self.assertEqual(
            self._sources(TEAM_ID),
            {
                "original@x.com": "system",
                "untracked@x.com": "system",
                "stranger@x.com": "detected",
                "rowless@x.com": "system",
            },
        )
        # 标记记的是第一次 grandfather 的时间，重建基线不改它。
        self.assertEqual(self._marker(TEAM_ID), first_marker)
        self.assertEqual(self._dry_run_candidates(), ["stranger@x.com"])

    def test_patrol_off_then_on_keeps_detected_strangers_as_candidates(self):
        self._seed_team()
        patrol.activate_patrol_sync([TEAM_ID])
        asyncio.run(patrol_routes.update_patrol_settings(
            patrol_routes.PatrolSettingsUpdate(kick_enabled=False)
        ))
        self._stranger_joins()

        result = patrol.activate_patrol_sync([TEAM_ID])

        self.assertEqual(result["grandfathered"], 0)
        self.assertEqual(self._sources(TEAM_ID)["stranger@x.com"], "detected")
        self.assertEqual(self._dry_run_candidates(), ["stranger@x.com"])

    def test_token_expired_recovery_keeps_detected_and_backfills_only_rowless(self):
        self._seed_team()
        patrol.activate_patrol_sync([TEAM_ID])

        conn = self._conn()
        chatgpt_limiter._mark_team_auth_expired(conn, TEAM_ID)
        conn.commit()
        conn.close()
        self.assertIsNone(self._baseline(TEAM_ID))
        self.assertTrue(self._marker(TEAM_ID))

        self._stranger_joins()
        # 管理员重新导入会话：真实的 re-import 路径只 UPDATE teams 行。
        _import_team_session(log_action="reimport_team", expected_team_id=TEAM_ID)
        self.assertTrue(self._marker(TEAM_ID))

        # 恢复后第一轮巡逻重建基线（这一轮不处理任何候选，RaisingClient 站岗）。
        result = patrol.run_patrol(dry_run=False, allow_team_ids=[TEAM_ID])

        self.assertEqual(result["kicked"], 0)
        self.assertTrue(self._baseline(TEAM_ID))
        init_logs = self._logs("patrol_team_initialize")
        self.assertEqual(len(init_logs), 1)
        self.assertIn("grandfathered=0", init_logs[0]["detail"])
        self.assertIn("backfilled=1", init_logs[0]["detail"])
        self.assertEqual(
            self._sources(TEAM_ID),
            {
                "original@x.com": "system",
                "untracked@x.com": "system",
                "stranger@x.com": "detected",
                "rowless@x.com": "system",
            },
        )
        self.assertEqual(self._dry_run_candidates(), ["stranger@x.com"])

    def test_deleted_and_readded_team_is_grandfathered_again(self):
        self._seed_team()
        patrol.activate_patrol_sync([TEAM_ID])
        self._stranger_joins()

        with patch.object(teams_routes, "sync_email_chat_commands_sync"):
            asyncio.run(teams_routes.delete_team(TEAM_ID))
        _import_team_session(log_action="add_team")
        self.assertIsNone(self._marker(TEAM_ID))

        # 删除 Team 会清掉成员缓存但保留 member_expiry；重新添加后同步重新写缓存。
        conn = self._conn()
        _set_cache(conn, TEAM_ID, [
            _owner(),
            _member("original@x.com", "u-original", source="system"),
            _member("stranger@x.com", "u-stranger", source="detected"),
        ])
        conn.close()

        # 巡逻仍武装：重新添加的 Team 在下一轮按"第一次"建立基线。
        result = patrol.run_patrol(dry_run=False, allow_team_ids=[TEAM_ID])

        self.assertEqual(result["kicked"], 0)
        self.assertEqual(self._sources(TEAM_ID)["stranger@x.com"], "system")
        self.assertTrue(self._marker(TEAM_ID))
        self.assertIn("grandfathered=1", self._logs("patrol_team_initialize")[-1]["detail"])

    def test_migration_seeds_marker_only_for_currently_baselined_teams(self):
        conn = self._conn()
        # 回到迁移前的 schema：teams 上还没有标记列。
        conn.execute("ALTER TABLE teams DROP COLUMN patrol_grandfathered_at")
        _insert_team(conn, "team-baselined")
        _insert_team(conn, "team-never-baselined")
        _insert_team(conn, "team-expired", status="token_expired")
        conn.execute(
            "INSERT INTO patrol_team_baselines (team_id, baseline_at) VALUES (?, ?)",
            ("team-baselined", "2026-07-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        asyncio.run(app_database.init_database())
        asyncio.run(app_database.init_database())  # 幂等：重复启动不报错、不改值

        self.assertEqual(self._marker("team-baselined"), "2026-07-01T00:00:00+00:00")
        self.assertIsNone(self._marker("team-never-baselined"))
        self.assertIsNone(self._marker("team-expired"))

        conn = self._conn()
        for team_id in ("team-baselined", "team-never-baselined"):
            _set_cache(conn, team_id, [_owner(), _member(f"x@{team_id}", f"u-{team_id}")])
            _insert_expiry(conn, team_id, f"u-{team_id}", f"x@{team_id}", "detected")
        conn.close()

        patrol.activate_patrol_sync(["team-baselined", "team-never-baselined"])

        self.assertEqual(self._sources("team-baselined"), {"x@team-baselined": "detected"})
        self.assertEqual(
            self._sources("team-never-baselined"), {"x@team-never-baselined": "system"}
        )


INVALID_ENTITLEMENTS = (None, 0, -3, "abc", 2.5)


class PatrolInvalidEntitlementTest(_Base):
    """seats_entitled 不是正整数时，超员判定无从谈起：不踢、不预演，只记日志。"""

    def setUp(self):
        super().setUp()
        self.calls = []
        calls = self.calls

        class RecordingClient:
            def __init__(self, *args, **kwargs):
                pass

            def remove_member(self, user_id):
                calls.append(("remove_member", user_id))
                return {"status": "ok"}

            def revoke_invite(self, email):
                calls.append(("revoke_invite", email))
                return {"status": "ok"}

        p = patch.object(patrol, "ChatGPTClient", new=RecordingClient)
        p.start()
        self.addCleanup(p.stop)
        self.client_cls = RecordingClient

        conn = self._conn()
        _set_setting(conn, "patrol_kick_enabled", "1")
        _set_setting(conn, "patrol_baseline_at", "2026-07-01T00:00:00+00:00")
        conn.close()

    def _armed_team(self, team_id, seats_entitled, *, members=None, pending=()):
        """一个已武装、已建基线的 Team：默认有两个检测到的外部成员。"""
        conn = self._conn()
        _insert_team(conn, team_id, seats_entitled=seats_entitled)
        if members is None:
            members = [
                _owner(),
                _member(f"a@{team_id}", f"ua-{team_id}", first_seen_at="2026-07-10T00:00:00Z"),
                _member(f"b@{team_id}", f"ub-{team_id}", first_seen_at="2026-07-11T00:00:00Z"),
            ]
        _set_cache(conn, team_id, members, pending)
        for item in list(members) + list(pending):
            if item.get("source") == "detected" and not item.get("is_owner"):
                _insert_expiry(conn, team_id, item["id"] if item["status"] == "active" else "",
                               item["email"], "detected")
        conn.execute(
            "INSERT INTO patrol_team_baselines (team_id, baseline_at) VALUES (?, ?)",
            (team_id, "2026-07-01T00:00:00+00:00"),
        )
        conn.execute(
            "UPDATE teams SET patrol_grandfathered_at = ? WHERE id = ?",
            ("2026-07-01T00:00:00+00:00", team_id),
        )
        conn.commit()
        conn.close()

    def test_invalid_entitlement_skips_over_quota_kicks_live_and_dry_run(self):
        for index, value in enumerate(INVALID_ENTITLEMENTS):
            team_id = f"team-invalid-{index}"
            with self.subTest(seats_entitled=value):
                self._armed_team(team_id, value)

                live = patrol.run_patrol(dry_run=False, allow_team_ids=[team_id])
                dry = patrol.run_patrol(dry_run=True, allow_team_ids=[team_id])

                self.assertEqual(self.calls, [])
                for result in (live, dry):
                    self.assertEqual(result["kicked"], 0)
                    self.assertEqual(result["would_kick"], 0)
                    self.assertFalse([
                        e for e in result["events"]
                        if e.get("action") in ("kick", "would_kick", "exempt_skip")
                    ])
                skip_logs = [
                    log for log in self._logs("patrol_skip_invalid_entitlement")
                    if log["team_id"] == team_id
                ]
                self.assertEqual(len(skip_logs), 2)
                self.assertEqual(self._logs("patrol_job_error"), [])
                self.assertEqual(
                    set(self._sources(team_id).values()), {"detected"}
                )

    def test_valid_entitlement_still_kicks_newest_over_quota_member(self):
        self._armed_team("team-valid", 1)

        result = patrol.run_patrol(dry_run=False, allow_team_ids=["team-valid"])

        self.assertEqual(result["kicked"], 1)
        self.assertEqual(self.calls, [("remove_member", "ub-team-valid")])
        self.assertEqual(self._logs("patrol_skip_invalid_entitlement"), [])

    def test_central_kick_gate_rejects_invalid_entitlement(self):
        for index, value in enumerate(INVALID_ENTITLEMENTS):
            team_id = f"team-gate-{index}"
            with self.subTest(seats_entitled=value):
                self._armed_team(team_id, value)
                candidate = _member(f"b@{team_id}", f"ub-{team_id}")
                conn = self._conn()
                ok, reason = patrol._patrol_kick(conn, self.client_cls(), team_id, candidate)
                conn.close()

                self.assertFalse(ok)
                self.assertIn("seats_entitled", reason)
                self.assertEqual(self.calls, [])

    def test_status_preview_lists_no_kick_candidates_for_invalid_entitlement(self):
        self._armed_team("team-null", None)
        self._armed_team("team-valid", 1)

        status = asyncio.run(patrol_routes.get_patrol_status(refresh=False))

        teams = {team["team_id"]: team for team in status["teams"]}
        self.assertEqual(teams["team-null"]["over_by"], 0)
        self.assertEqual(teams["team-null"]["detected_over"], [])
        self.assertEqual(teams["team-null"]["risk"], "watch")
        self.assertFalse(teams["team-null"]["entitlement_valid"])
        self.assertEqual(teams["team-valid"]["risk"], "over")
        self.assertEqual(teams["team-valid"]["over_by"], 1)
        self.assertTrue(teams["team-valid"]["entitlement_valid"])

    def test_invalid_entitlement_still_revokes_stranger_invites(self):
        self._armed_team(
            "team-null-invite",
            None,
            members=[_owner()],
            pending=[_pending("stray@x.com")],
        )

        result = patrol.run_patrol(dry_run=False, allow_team_ids=["team-null-invite"])

        self.assertEqual(result["invites_revoked"], 1)
        self.assertEqual(self.calls, [("revoke_invite", "stray@x.com")])


if __name__ == "__main__":
    unittest.main()
