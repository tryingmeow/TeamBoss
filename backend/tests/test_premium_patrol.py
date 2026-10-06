"""巡逻 / 到期踢人遇到 Premium（prolite）和注册表外席位类型（automation 等）时的回归测试。

规则（契约 §3.9，所有者裁决）：
- 已建基线、未豁免、巡逻已武装的 Team 上，source=detected 的非 Owner Premium 成员直接踢
  （不看超员 / Codex / 严格模式），仍走 _patrol_kick 的全部闸门，单轮同样封顶。
- default 超员踢人逐字不变；Codex 不变。
- 注册表外的类型任何模式都不踢、不撤；到期踢人遇到它们跳过并记一条。
- TeamBoss 管理的成员被切到 Premium（不是 TeamBoss 切的）只发限频提醒。

所有上游调用都是记录调用的假客户端，绝不触网。
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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import scheduler as app_scheduler
from app.services import patrol, team_health_alerts

OLD = "2020-01-01T00:00:00+00:00"
BASELINE = "2026-07-01T00:00:00+00:00"


def _member(email, user_id, *, seat_type="default", is_owner=False, source="detected",
            first_seen_at="2026-07-10T00:00:00+00:00", expires_at=None):
    return {
        "id": user_id,
        "email": email,
        "seat_type": seat_type,
        "is_owner": is_owner,
        "source": source,
        "first_seen_at": first_seen_at,
        "expires_at": expires_at,
        "created_time": None,
        "status": "active",
    }


def _pending(email, *, seat_type="default", source="detected",
             first_seen_at="2026-07-10T00:00:00+00:00"):
    return {
        "id": f"invite-{email}",
        "email": email,
        "seat_type": seat_type,
        "is_owner": False,
        "source": source,
        "first_seen_at": first_seen_at,
        "expires_at": None,
        "created_time": None,
        "status": "pending",
    }


class RecordingClient:
    """假 ChatGPT 客户端：只记录调用，绝不触网。"""

    calls: list = []
    live_members: list = []

    def __init__(self, *args, **kwargs):
        pass

    def remove_member(self, user_id):
        RecordingClient.calls.append(("remove_member", user_id))
        return {"status": "ok"}

    def revoke_invite(self, email):
        RecordingClient.calls.append(("revoke_invite", email))
        return {"status": "ok"}

    def get_members(self, offset=0, limit=100):
        RecordingClient.calls.append(("get_members", offset))
        items = list(RecordingClient.live_members) if offset == 0 else []
        return {"items": items, "total": len(RecordingClient.live_members)}

    def get_pending_invites(self, offset=0, limit=100):
        RecordingClient.calls.append(("get_pending_invites", offset))
        return {"items": [], "total": 0}


def _live(email, user_id, seat_type, *, owner=False):
    return {
        "id": user_id,
        "email": email,
        "role": "account-owner" if owner else "standard-user",
        "seat_type": seat_type,
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        db_patch = patch.object(app_database, "get_db_dir", return_value=self._tmpdir.name)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()

        self.notify_calls: list[str] = []

        def fake_notify(text, **kwargs):
            self.notify_calls.append(text)
            return 1

        def forbidden_notify(text, **kwargs):  # pragma: no cover - 走到这里就是绕过了巡逻出口
            raise AssertionError("patrol alerts must go through patrol.notify_admins_sync")

        RecordingClient.calls = []
        RecordingClient.live_members = []
        for target, attr, value in (
            (patrol, "notify_admins_sync", fake_notify),
            (patrol, "ChatGPTClient", RecordingClient),
            (patrol, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)),
            (team_health_alerts, "notify_admins_sync", forbidden_notify),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    # ── helpers ──────────────────────────────────────────────────────────
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _team(self, team_id, *, seats_entitled=5, codex=0, name=None):
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, access_token, device_id, seats_entitled,
                                  is_codex_enabled, status, created_at, updated_at)
               VALUES (?, ?, 'fake-token', 'fake-device', ?, ?, 'active', '2026-01-01', '2026-01-01')""",
            (team_id, name or team_id, seats_entitled, codex),
        )
        conn.commit()
        conn.close()

    def _cache(self, team_id, members, pending=(), *, updated_at=None):
        # updated_at 同时当作这份快照的 fetch_started_at（巡逻的 Premium 否决比的是它）。
        snapshot_at = updated_at or datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at,
                                         fetch_started_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(team_id) DO UPDATE SET members_json = excluded.members_json,
                                                  pending_json = excluded.pending_json,
                                                  updated_at = excluded.updated_at,
                                                  fetch_started_at = excluded.fetch_started_at""",
            (team_id, json.dumps(members), json.dumps(list(pending)), snapshot_at, snapshot_at),
        )
        conn.commit()
        conn.close()

    def _expiry(self, team_id, email, user_id="", *, source="detected",
                first_seen_at="2026-07-10T00:00:00+00:00", expires_at=None, auto_kick=0):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, first_seen_at, source, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, '2026-01-01')""",
            (team_id, user_id, email, expires_at, auto_kick, first_seen_at, source),
        )
        conn.commit()
        conn.close()

    def _add(self, team_id, member):
        """成员快照之外再补一条对应来源的 member_expiry 行（和真实同步的结果一致）。"""
        if member.get("source"):
            self._expiry(team_id, member["email"], member.get("id") or "",
                         source=member["source"], first_seen_at=member.get("first_seen_at"))
        return member

    def _setting(self, key, value):
        conn = self._conn()
        conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, '2026-01-01')
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        conn.commit()
        conn.close()

    def _arm(self, *, kick_enabled="1"):
        self._setting("patrol_kick_enabled", kick_enabled)
        self._setting("patrol_baseline_at", BASELINE)

    def _baseline(self, team_id):
        conn = self._conn()
        conn.execute(
            "INSERT INTO patrol_team_baselines (team_id, baseline_at) VALUES (?, ?)",
            (team_id, BASELINE),
        )
        conn.commit()
        conn.close()

    def _patrol(self, *, dry_run=False, allow=None):
        if allow is None:
            conn = self._conn()
            allow = [r["id"] for r in conn.execute("SELECT id FROM teams WHERE status = 'active'")]
            conn.close()
        return patrol.run_patrol(dry_run=dry_run, allow_team_ids=allow)

    def _logs(self, action):
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM operation_logs WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _calls(self, name):
        return [c for c in RecordingClient.calls if c[0] == name]

    def _alerts(self):
        return [t for t in self.notify_calls if "席位提醒" in t or "席位类型提醒" in t]

    def _kicked(self, team_id, user_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT kicked, kick_source FROM member_expiry WHERE team_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (team_id, user_id),
        ).fetchone()
        conn.close()
        return (row["kicked"], row["kick_source"]) if row else None

    def _armed_team(self, team_id, members, pending=(), **team_kwargs):
        self._team(team_id, **team_kwargs)
        for m in list(members) + list(pending):
            self._add(team_id, m)
        self._cache(team_id, members, pending)
        self._arm()
        self._baseline(team_id)


OWNER = _member("owner@example.com", "u-owner", seat_type="usage_based", is_owner=True, source=None)


# ═══ 一、Premium 外部成员自动踢 ═══════════════════════════════════════════════

class PremiumOutsiderKickTest(_Base):
    def test_detected_premium_outsider_is_kicked_when_patrol_is_on(self):
        # 没超员（5 个席位只用了 1 个 ChatGPT），也照样踢：每人都是一个自动加购的 Premium 席位。
        self._armed_team("team-p", [
            OWNER,
            _member("keeper@example.com", "u-keep", source="system"),
            _member("premiumguy@example.com", "u-p", seat_type="prolite"),
        ])

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-p")])
        self.assertEqual(self._calls("revoke_invite"), [])
        self.assertEqual(result["kicked"], 1)
        self.assertEqual(self._kicked("team-p", "u-p"), (1, "patrol_premium"))
        logs = self._logs("patrol_kick")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["result"], "success")
        self.assertEqual(logs[0]["detail"], "user_id=u-p, seat_type=prolite, reason=premium_outsider")
        self.assertTrue(any("巡逻移除 Premium 外部成员" in t and "premiumguy@example.com" in t
                            for t in self.notify_calls))
        # 已经移除的人不再进"没处理"的提醒。
        self.assertEqual(self._alerts(), [])

    def test_codex_enabled_team_is_exempt_from_premium_kicks(self):
        # 开了 Codex 的 Team 和以前一样巡逻不踢人：只提醒。
        member = _member("premiumguy@example.com", "u-p", seat_type="prolite")
        self._armed_team("team-codex", [OWNER, member], codex=1, seats_entitled=99)

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("开了 Codex", alerts[0])

        conn = self._conn()
        ok, reason = patrol._patrol_kick(conn, RecordingClient(), "team-codex", member,
                                         rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        conn.close()
        self.assertFalse(ok)
        self.assertIn("codex is enabled", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_same_outsider_is_not_kicked_when_patrol_is_off(self):
        # 曾经武装过（基线在），后来关了自动踢人：只空跑 + 提醒。
        self._armed_team("team-off", [
            OWNER, _member("premiumguy@example.com", "u-p", seat_type="prolite"),
        ])
        self._setting("patrol_kick_enabled", "0")

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["would_kick"], 1)
        would = self._logs("patrol_would_kick")
        self.assertEqual(len(would), 1)
        self.assertIn("reason=premium_outsider", would[0]["detail"])
        self.assertIn("seat_type=prolite", would[0]["detail"])
        self.assertEqual(self._kicked("team-off", "u-p"), (0, None))
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("巡逻自动踢人没开", alerts[0])
        self.assertIn("premi…@example", alerts[0])
        self.assertNotIn("premiumguy@example.com", alerts[0])

    def test_explicit_dry_run_on_armed_patrol_does_not_kick(self):
        self._armed_team("team-dry", [
            OWNER, _member("premiumguy@example.com", "u-p", seat_type="prolite"),
        ])

        result = self._patrol(dry_run=True)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["would_kick"], 1)

    def test_exempt_team_is_not_kicked(self):
        self._armed_team("team-ex", [
            OWNER, _member("premiumguy@example.com", "u-p", seat_type="prolite"),
        ])
        self._setting("patrol_exempt_team_ids", json.dumps(["team-ex"]))

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertTrue(any("已豁免" in t for t in self._alerts()))

    def test_team_without_baseline_is_not_kicked(self):
        self._team("team-nb")
        member = _member("premiumguy@example.com", "u-p", seat_type="prolite")
        self._add("team-nb", member)
        self._cache("team-nb", [OWNER, member])
        self._arm()
        # 没有 patrol_team_baselines 行：本轮只建基线（巡逻自动保护现有成员），不处理候选。

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])

    def test_system_premium_member_is_not_kicked(self):
        self._armed_team("team-sys", [
            OWNER, _member("managedone@example.com", "u-s", seat_type="prolite", source="system"),
        ])

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("不是 TeamBoss 切的", alerts[0])
        self.assertIn("manag…@example", alerts[0])

    def test_owner_is_not_kicked(self):
        owner = _member("owner@example.com", "u-owner", seat_type="prolite", is_owner=True)
        self._armed_team("team-own", [owner, _member("a@example.com", "u-a", source="system")])

        self._patrol(dry_run=False)
        self.assertEqual(self._calls("remove_member"), [])

        conn = self._conn()
        ok, reason = patrol._patrol_kick(conn, RecordingClient(), "team-own", owner,
                                         rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        conn.close()
        self.assertFalse(ok)
        self.assertIn("is_owner", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_per_round_cap_applies_to_premium_kicks(self):
        # 批量护栏的阈值（最多 3）比单轮封顶（10）严，正常数据下封顶碰不到；把封顶调成 1，
        # 证明 Premium 踢人同样受它约束。
        members = [OWNER] + [
            _member(f"keeper{i}@example.com", f"u-k{i}", source="system") for i in range(3)
        ] + [
            _member("older@example.com", "u-old", seat_type="prolite",
                    first_seen_at="2026-07-10T00:00:00+00:00"),
            _member("newer@example.com", "u-new", seat_type="prolite",
                    first_seen_at="2026-07-20T00:00:00+00:00"),
        ]
        self._armed_team("team-cap", members)

        with patch.object(patrol, "NON_STRICT_KICK_ABS_CAP", 1):
            result = self._patrol(dry_run=False)

        # 新→旧：最新的先处理，老的留到下一轮。
        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-new")])
        self.assertEqual(result["kicked"], 1)
        capped = self._logs("patrol_kick_batch_capped")
        self.assertEqual(len(capped), 1)
        self.assertIn("reason=premium_outsider", capped[0]["detail"])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("older@example", alerts[0])
        self.assertIn("本轮没能自动移除", alerts[0])

    def test_mass_premium_outsiders_trip_the_batch_guard(self):
        # 13 人队阈值 = min(3, 6) = 3；突然冒出 12 个外部 Premium 成员 = 数据异常，一个都不踢。
        outsiders = [
            _member(f"outsider{i:02d}@example.com", f"u-{i:02d}", seat_type="prolite",
                    first_seen_at=f"2026-07-{10 + i:02d}T00:00:00+00:00")
            for i in range(12)
        ]
        self._armed_team("team-mass", [OWNER] + outsiders)

        result = self._patrol(dry_run=False)
        self._patrol(dry_run=False)

        self.assertEqual(RecordingClient.calls, [])
        self.assertEqual(result["kicked"], 0)
        guard = [e for e in result["events"] if e.get("action") == "premium_batch_guard"]
        self.assertEqual(guard[0]["guard"], "outsiders")
        self.assertEqual((guard[0]["count"], guard[0]["team_size"]), (12, 13))
        capped = self._logs("patrol_kick_batch_capped")
        self.assertIn("batch_guard=outsiders", capped[0]["detail"])
        self.assertIn("capped_to=0", capped[0]["detail"])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)  # 第二轮同一份名单：限频
        self.assertIn("外部成员数量异常（12 / 团队共 13 人）", alerts[0])
        self.assertIn("等 12 人", alerts[0])

    def test_batch_guard_threshold_boundary(self):
        # 4 人队阈值 = 2：两个外部 Premium 成员正好不超，照常踢。
        members = [
            OWNER, _member("keeper@example.com", "u-k", source="system"),
            _member("p1@example.com", "u-p1", seat_type="prolite"),
            _member("p2@example.com", "u-p2", seat_type="prolite"),
        ]
        self._armed_team("team-edge", members)

        self._patrol(dry_run=False)

        self.assertEqual(sorted(self._calls("remove_member")),
                         [("remove_member", "u-p1"), ("remove_member", "u-p2")])

    def test_outsider_guard_stops_premium_kicks_with_or_without_strict_mode(self):
        # 6 人队阈值 = 3：只有 1 个 Premium 外部成员，但外部成员一共 4 个，已判异常。
        members = [
            OWNER, _member("keeper@example.com", "u-k", source="system"),
            _member("premiumguy@example.com", "u-p", seat_type="prolite", first_seen_at=OLD),
            _member("plain1@example.com", "u-d1", first_seen_at=OLD),
            _member("plain2@example.com", "u-d2", first_seen_at=OLD),
            _member("codexguy@example.com", "u-c", seat_type="usage_based", first_seen_at=OLD),
        ]
        self._armed_team("team-sguard", members, seats_entitled=99)
        self._setting("patrol_strict_mode_enabled", "1")

        result = self._patrol(dry_run=False)

        self.assertEqual(RecordingClient.calls, [])
        actions = {e.get("action") for e in result["events"]}
        self.assertIn("strict_batch_guard", actions)
        guard = [e for e in result["events"] if e.get("action") == "premium_batch_guard"]
        self.assertEqual(guard[0]["guard"], "outsiders")
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("外部成员数量异常（4 / 团队共 6 人）", alerts[0])

        # 严格模式关掉，同一份快照：护栏照样数全部外部成员，照样不踢。
        self._setting("patrol_strict_mode_enabled", "0")
        result = self._patrol(dry_run=False)
        self.assertEqual(self._calls("remove_member"), [])
        guard = [e for e in result["events"] if e.get("action") == "premium_batch_guard"]
        self.assertEqual(guard[0]["count"], 4)

    def test_timed_out_switch_to_premium_blocks_the_kick(self):
        # 场景 A：管理员把一个 detected 成员切到 Premium，上游生效了但响应超时，TeamBoss 记了
        # failed 并返回 502。下一轮快照里他是 detected + prolite，没有成功记录，也不能踢。
        member = _member("upgraded@example.com", "u-up", seat_type="prolite")
        self._armed_team("team-a", [OWNER, _member("keeper@example.com", "u-k", source="system"), member])
        conn = self._conn()
        conn.execute(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           error_message, trigger_type, created_at)
               VALUES ('team-a', 'change_seat', 'upgraded@example.com',
                       'user_id=u-up, seat_type=prolite, from_seat_type=default, policy=auto',
                       'failed', 'Read timed out', 'manual', '2026-09-01T00:00:00+00:00')"""
        )
        conn.commit()
        conn.close()

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("TeamBoss 给他开过 Premium", alerts[0])

        conn = self._conn()
        ok, reason = patrol._patrol_kick(conn, RecordingClient(), "team-a", member,
                                         rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        conn.close()
        self.assertFalse(ok)
        self.assertIn("seat or invite record", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_policy_refused_premium_invite_still_blocks_the_kick(self):
        # 被超员策略拒绝的 Premium 邀请上游什么都没发生，但管理员动过这个人：任何改席位 / 邀请记录
        # （任何结果）都挡 Premium 踢人，交给席位提醒。
        self._armed_team("team-ref", [
            OWNER, _member("keeper@example.com", "u-k", source="system"),
            _member("refused@example.com", "u-r", seat_type="prolite"),
        ])
        conn = self._conn()
        conn.executemany(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           trigger_type, created_at)
               VALUES ('team-ref', 'invite_member', 'refused@example.com', ?, ?, 'manual',
                       '2026-09-01T00:00:00+00:00')""",
            [
                ("seat_type=prolite, policy=forbid, reason=overage_forbidden", "skipped"),
                ("seat_type=prolite, policy=confirm, reason=overage_needs_confirmation", "failed"),
            ],
        )
        conn.commit()
        conn.close()

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        logged = self._logs("patrol_premium_alert")
        self.assertEqual([l["target_email"] for l in logged], ["refused@example.com"])
        self.assertIn("kind=premium_detected_with_record", logged[0]["detail"])

    def test_seat_change_after_the_snapshot_blocks_the_kick(self):
        # 场景 B：管理员把 detected 的 Premium 成员切回 ChatGPT，切换后的刷新失败，快照还写着
        # prolite。TeamBoss 有他的改席位记录：不踢，交给席位提醒。
        snapshot_at = "2026-09-01T00:00:00+00:00"
        member = _member("downgraded@example.com", "u-down", seat_type="prolite")
        members = [OWNER, _member("keeper@example.com", "u-k", source="system"), member]
        self._armed_team("team-b", members)
        self._cache("team-b", members, updated_at=snapshot_at)
        conn = self._conn()
        conn.execute(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           trigger_type, created_at)
               VALUES ('team-b', 'change_seat', NULL,
                       'user_id=u-down, seat_type=default, from_seat_type=prolite, policy=confirm',
                       'success', 'manual', '2026-09-01T00:05:00+00:00')"""
        )
        conn.commit()
        conn.close()

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        # 候选筛选就挡住了：没有走到踢人入口，不推"处理失败"。
        self.assertEqual(self._logs("patrol_kick"), [])
        self.assertFalse(any("处理失败" in t for t in self.notify_calls))
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("改过他的席位或邀请过他", alerts[0])

        # 下一轮刷新成功，快照里他已经是 ChatGPT：不再是 Premium 候选。
        refreshed = [OWNER, _member("keeper@example.com", "u-k", source="system"),
                     _member("downgraded@example.com", "u-down")]
        self._cache("team-b", refreshed, updated_at="2026-09-01T00:20:00+00:00")
        self._patrol(dry_run=False)
        self.assertEqual(self._calls("remove_member"), [])

    def test_unreadable_snapshot_time_defers_the_kick(self):
        member = _member("premiumguy@example.com", "u-p", seat_type="prolite")
        members = [OWNER, _member("keeper@example.com", "u-k", source="system"), member]
        self._armed_team("team-nots", members)
        self._cache("team-nots", members, updated_at="not-a-timestamp")

        conn = self._conn()
        ok, reason = patrol._patrol_kick(conn, RecordingClient(), "team-nots", member,
                                         rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        conn.close()
        self.assertFalse(ok)
        self.assertEqual(reason, patrol.PREMIUM_KICK_DEFERRED)
        self.assertEqual(self._calls("remove_member"), [])

    def test_stale_snapshot_leads_to_no_kick(self):
        member = _member("premiumguy@example.com", "u-p", seat_type="prolite")
        self._armed_team("team-stale", [OWNER, member])

        # 本轮没刷新成功的 Team 不在白名单里：一个都不碰。
        result = self._patrol(dry_run=False, allow=[])
        self.assertEqual(RecordingClient.calls, [])
        self.assertEqual(result["events"], [])
        self.assertEqual(self.notify_calls, [])

        # 调用方拿着旧快照里的人，但当前快照里已经没有他：闸门拒绝。
        self._cache("team-stale", [OWNER, _member("keeper@example.com", "u-k", source="system")])
        conn = self._conn()
        ok, reason = patrol._patrol_kick(conn, RecordingClient(), "team-stale", member,
                                         rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        conn.close()
        self.assertFalse(ok)
        self.assertIn("absent from current member cache", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_teamboss_premium_record_blocks_the_kick(self):
        # 来源是 detected，但 TeamBoss 用 Premium 码给他兑换过：数据对不上，不踢，只提醒。
        member = _member("redeemer@example.com", "u-r", seat_type="prolite")
        self._armed_team("team-rec", [OWNER, member])
        conn = self._conn()
        token_id = conn.execute(
            """INSERT INTO access_tokens (token_hash, token_prefix, grant_expires_in, max_uses,
                                          used_count, disabled, created_at, seat_type)
               VALUES ('h1', 'p', '30d', 1, 1, 0, '2026-07-01', 'prolite')"""
        ).lastrowid
        conn.execute(
            """INSERT INTO access_token_uses (token_id, email, action, team_id, user_id, result, created_at)
               VALUES (?, 'redeemer@example.com', 'invite', 'team-rec', 'u-r', 'success', '2026-07-09')""",
            (token_id,),
        )
        conn.commit()

        ok, reason = patrol._patrol_kick(conn, RecordingClient(), "team-rec", member,
                                         rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        conn.close()
        self.assertFalse(ok)
        self.assertIn("seat or invite record", reason)

        self._patrol(dry_run=False)
        self.assertEqual(self._calls("remove_member"), [])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("TeamBoss 给他开过 Premium", alerts[0])

    def test_gate_keeps_each_rule_on_its_own_seat_type(self):
        default_outsider = _member("plain@example.com", "u-d")
        premium_outsider = _member("premiumguy@example.com", "u-p", seat_type="prolite")
        automation = _member("robot@example.com", "u-x", seat_type="automation")
        # seats_entitled=1 + 两个 ChatGPT 席位 = 超员 1，超员规则本身是放行的。
        self._armed_team("team-gate", [
            OWNER, _member("keeper@example.com", "u-k", source="system"),
            default_outsider, premium_outsider, automation,
        ], seats_entitled=1)

        conn = self._conn()
        client = RecordingClient()
        cases = [
            (default_outsider, patrol.KICK_RULE_PREMIUM_OUTSIDER, "!= 'prolite'"),
            (premium_outsider, patrol.KICK_RULE_OVER_QUOTA, "!= 'default'"),
            (automation, patrol.KICK_RULE_PREMIUM_OUTSIDER, "!= 'prolite'"),
            (automation, patrol.KICK_RULE_OVER_QUOTA, "!= 'default'"),
            (premium_outsider, "strict", "unknown kick rule"),
        ]
        for member, rule, expected in cases:
            with self.subTest(email=member["email"], rule=rule):
                ok, reason = patrol._patrol_kick(conn, client, "team-gate", member, rule=rule)
                self.assertFalse(ok)
                self.assertIn(expected, reason)
        conn.close()
        self.assertEqual(self._calls("remove_member"), [])


# ═══ 二、default 超员不变；Codex / automation 不踢 ════════════════════════════

class OverQuotaUnchangedTest(_Base):
    def test_default_over_quota_is_unchanged_with_premium_seats_present(self):
        base = [
            OWNER,
            _member("a@example.com", "u-a", source="system"),
            _member("b@example.com", "u-b", source="system"),
            _member("newcomer@example.com", "u-d", first_seen_at="2026-07-20T00:00:00+00:00"),
            _member("codexguy@example.com", "u-c", seat_type="usage_based"),
        ]
        premium = _member("premiumguy@example.com", "u-p", seat_type="prolite",
                          first_seen_at="2026-07-25T00:00:00+00:00")

        without = patrol.classify_team(team_id="t", name="t", codex_enabled=False,
                                       seats_entitled=2, members=base)
        with_premium = patrol.classify_team(team_id="t", name="t", codex_enabled=False,
                                            seats_entitled=2, members=base + [premium])
        self.assertEqual(with_premium, without)
        self.assertEqual(with_premium["over_by"], 1)
        self.assertEqual([c["email"] for c in with_premium["detected_over"]], ["newcomer@example.com"])

        self._armed_team("team-q", base + [premium], seats_entitled=2)
        self._patrol(dry_run=False)

        removed = self._calls("remove_member")
        self.assertEqual(sorted(removed), [("remove_member", "u-d"), ("remove_member", "u-p")])
        self.assertNotIn(("remove_member", "u-c"), removed)  # Codex 外部成员：普通模式照旧不踢
        details = {log["target_email"]: log["detail"] for log in self._logs("patrol_kick")}
        self.assertEqual(details["newcomer@example.com"], "user_id=u-d")  # 超员踢人日志逐字不变
        self.assertTrue(any("巡逻发现超员" in t and "（超 1）" in t for t in self.notify_calls))

    def test_automation_is_never_kicked_in_any_mode(self):
        robot = _member("robotowner@example.com", "u-x", seat_type="automation", first_seen_at=OLD)
        self._armed_team("team-auto", [
            OWNER, _member("a@example.com", "u-a", source="system"), robot,
        ], seats_entitled=1)
        self._setting("patrol_strict_mode_enabled", "1")
        RecordingClient.live_members = [
            _live("owner@example.com", "u-owner", "usage_based", owner=True),
            _live("a@example.com", "u-a", "default"),
            _live("robotowner@example.com", "u-x", "automation"),
        ]

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(self._logs("patrol_strict_flagged"), [])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("其他（automation）", alerts[0])
        self.assertIn("包括到期踢人", alerts[0])


# ═══ 三、严格模式 ═══════════════════════════════════════════════════════════

class StrictModeTest(_Base):
    def _strict_team(self, team_id, members):
        self._armed_team(team_id, members, seats_entitled=99)
        self._setting("patrol_strict_mode_enabled", "1")
        RecordingClient.live_members = [
            _live(m["email"], m["id"], m["seat_type"], owner=bool(m["is_owner"])) for m in members
        ]

    def test_strict_mode_kicks_default_and_never_automation_and_premium_only_once(self):
        members = [
            OWNER,
            _member("a@example.com", "u-a", source="system"),
            _member("b@example.com", "u-b", source="system"),
            _member("premiumguy@example.com", "u-p", seat_type="prolite", first_seen_at=OLD),
            _member("robot@example.com", "u-x", seat_type="automation", first_seen_at=OLD),
            _member("plain@example.com", "u-d", first_seen_at=OLD),
        ]
        self._strict_team("team-strict", members)

        result = self._patrol(dry_run=False)

        removed = self._calls("remove_member")
        self.assertEqual(sorted(removed), [("remove_member", "u-d"), ("remove_member", "u-p")])
        self.assertEqual(result["strict_kicked"], 1)
        strict_logs = [l for l in self._logs("patrol_strict_kick") if l["result"] == "success"]
        self.assertEqual([l["target_email"] for l in strict_logs], ["plain@example.com"])
        premium_logs = [l for l in self._logs("patrol_kick") if l["result"] == "success"]
        self.assertEqual([l["target_email"] for l in premium_logs], ["premiumguy@example.com"])

    def test_strict_mode_kicks_codex_but_not_premium_through_strict(self):
        members = [
            OWNER,
            _member("a@example.com", "u-a", source="system"),
            _member("codexguy@example.com", "u-c", seat_type="usage_based", first_seen_at=OLD),
        ]
        self._strict_team("team-strict-c", members)

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-c")])

    def test_strict_gate_rejects_premium_and_unknown_seat_types(self):
        premium = _member("premiumguy@example.com", "u-p", seat_type="prolite", first_seen_at=OLD)
        robot = _member("robot@example.com", "u-x", seat_type="automation", first_seen_at=OLD)
        self._strict_team("team-sg", [OWNER, premium, robot])

        conn = self._conn()
        for member in (premium, robot):
            with self.subTest(email=member["email"]):
                ok, reason = patrol._patrol_strict_kick(conn, RecordingClient(), "team-sg", member)
                self.assertFalse(ok)
                self.assertIn("strict mode never acts on seat_type", reason)
        conn.close()
        self.assertEqual(self._calls("remove_member"), [])

    def test_batch_guard_still_counts_every_outsider(self):
        # 4 人队阈值 = min(3, 2) = 2；3 个疑似陌生成员（含 automation）照旧触发护栏，一个都不踢。
        # 护栏若只数可动手的 2 人，这一队反而会被放行。
        members = [
            OWNER,
            _member("plain@example.com", "u-d", first_seen_at=OLD),
            _member("codexguy@example.com", "u-c", seat_type="usage_based", first_seen_at=OLD),
            _member("robot@example.com", "u-x", seat_type="automation", first_seen_at=OLD),
        ]
        self._strict_team("team-guard", members)

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        guard = [e for e in result["events"] if e.get("action") == "strict_batch_guard"]
        self.assertEqual(len(guard), 1)
        self.assertEqual(guard[0]["count"], 3)
        self.assertEqual(guard[0]["team_size"], 4)


# ═══ 四、陌生邀请撤销 ═════════════════════════════════════════════════════════

class PendingRevocationTest(_Base):
    def test_registry_types_revoked_automation_never(self):
        pending = [
            _pending("inv-default@example.com"),
            _pending("inv-codex@example.com", seat_type="usage_based"),
            _pending("inv-premium@example.com", seat_type="prolite"),
            _pending("inv-robot@example.com", seat_type="automation"),
        ]
        self._armed_team("team-inv", [OWNER, _member("a@example.com", "u-a", source="system")], pending)

        result = self._patrol(dry_run=False)

        self.assertEqual(
            sorted(self._calls("revoke_invite")),
            [
                ("revoke_invite", "inv-codex@example.com"),
                ("revoke_invite", "inv-default@example.com"),
                ("revoke_invite", "inv-premium@example.com"),
            ],
        )
        self.assertEqual(result["invites_revoked"], 3)
        self.assertEqual(self._calls("remove_member"), [])
        # automation 邀请进提醒；本轮已撤的 Premium 外部邀请不进（撤销有自己的通知）。
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("inv-r…@example（其他（automation），邀请未接受）", alerts[0])
        self.assertNotIn("外部 Premium 邀请", alerts[0])

        conn = self._conn()
        ok, reason = patrol._patrol_revoke_invite(
            conn, RecordingClient(), "team-inv", _pending("inv-robot@example.com", seat_type="automation")
        )
        conn.close()
        self.assertFalse(ok)
        self.assertIn("not a TeamBoss seat type", reason)
        self.assertNotIn(("revoke_invite", "inv-robot@example.com"), RecordingClient.calls)


# ═══ 五、TeamBoss 成员被切到 Premium：只提醒 + 限频 ═══════════════════════════

class ManagedPremiumAlertTest(_Base):
    def _log(self, team_id, action, target_email, detail, created_at, result="success"):
        conn = self._conn()
        conn.execute(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, 'manual', ?)""",
            (team_id, action, target_email, detail, result, created_at),
        )
        conn.commit()
        conn.close()

    def test_switched_outside_teamboss_alerts_switched_by_teamboss_does_not(self):
        managed = {
            "outside1": _member("outside1@example.com", "u-m1", seat_type="prolite", source="system"),
            "viaswitch": _member("viaswitch@example.com", "u-m2", seat_type="prolite", source="system"),
            "viacode": _member("viacode@example.com", "u-m3", seat_type="prolite", source="self_service"),
            "switchedback": _member("switchedback@example.com", "u-m4", seat_type="prolite", source="system"),
            "viainvite": _member("viainvite@example.com", "u-m5", seat_type="prolite", source="system"),
            "fromprem": _member("fromprem@example.com", "u-m6", seat_type="prolite", source="system"),
            "timedout": _member("timedout@example.com", "u-m7", seat_type="prolite", source="system"),
        }
        self._team("team-m")
        for m in managed.values():
            self._add("team-m", m)
        self._cache("team-m", [OWNER] + list(managed.values()))

        # TeamBoss 切的（change_seat 的 target_email 可能为空，按 detail 里的 user_id 对上）。
        self._log("team-m", "change_seat", None,
                  "user_id=u-m2, seat_type=prolite, from_seat_type=default", "2026-08-01T00:00:00+00:00")
        # TeamBoss 切过去又切回 ChatGPT，之后在 ChatGPT 后台被切到 Premium：最新一条说了算。
        self._log("team-m", "change_seat", "switchedback@example.com",
                  "user_id=u-m4, seat_type=prolite", "2026-08-01T00:00:00+00:00")
        self._log("team-m", "change_seat", "switchedback@example.com",
                  "user_id=u-m4, seat_type=default, from_seat_type=prolite", "2026-08-02T00:00:00+00:00")
        # TeamBoss 后台直接按 Premium 邀请（邮箱大小写不同也要对上）。
        self._log("team-m", "invite_member", "ViaInvite@Example.com",
                  "seat_type=prolite, expires_in=30d, allow_overage=False", "2026-08-01T00:00:00+00:00")
        # from_seat_type=prolite 不能被当成 seat_type=prolite。
        self._log("team-m", "change_seat", "fromprem@example.com",
                  "user_id=u-m6, seat_type=default, from_seat_type=prolite", "2026-08-01T00:00:00+00:00")
        # 被超员策略拒绝的切换不算 TeamBoss 切的。
        self._log("team-m", "change_seat", "outside1@example.com",
                  "user_id=u-m1, seat_type=prolite, policy=forbid, reason=overage_forbidden",
                  "2026-08-01T00:00:00+00:00", result="skipped")
        # 超时（记成 failed）的切换上游可能已生效：算 TeamBoss 切的。
        self._log("team-m", "change_seat", "timedout@example.com",
                  "user_id=u-m7, seat_type=prolite, from_seat_type=default",
                  "2026-08-01T00:00:00+00:00", result="failed")
        # Premium 兑换码。
        conn = self._conn()
        token_id = conn.execute(
            """INSERT INTO access_tokens (token_hash, token_prefix, grant_expires_in, max_uses,
                                          used_count, disabled, created_at, seat_type)
               VALUES ('h-code', 'p', '30d', 1, 1, 0, '2026-07-01', 'prolite')"""
        ).lastrowid
        conn.execute(
            """INSERT INTO access_token_uses (token_id, email, action, team_id, user_id, result, created_at)
               VALUES (?, 'viacode@example.com', 'invite', 'team-m', 'u-m3', 'success', '2026-08-01')""",
            (token_id,),
        )
        conn.commit()
        conn.close()

        self._patrol(dry_run=True)

        self.assertEqual(RecordingClient.calls, [])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        text = alerts[0]
        for masked in ("outsi…@example", "switc…@example", "fromp…@example"):
            self.assertIn(masked, text)
        for masked in ("viasw…@example", "viaco…@example", "viain…@example", "timed…@example"):
            self.assertNotIn(masked, text)
        for m in managed.values():
            self.assertNotIn(m["email"], text)
        self.assertIn("TeamBoss 没有移除", text)
        logged = {l["target_email"]: l["detail"] for l in self._logs("patrol_premium_alert")}
        self.assertEqual(
            set(logged),
            {"outside1@example.com", "switchedback@example.com", "fromprem@example.com"},
        )
        self.assertTrue(all("kind=premium_unswitched" in d for d in logged.values()))

    def test_alert_is_throttled_and_renotified_only_on_change(self):
        first = _member("outside1@example.com", "u-m1", seat_type="prolite", source="system")
        self._team("team-t")
        self._add("team-t", first)
        self._cache("team-t", [OWNER, first])

        self._patrol(dry_run=True)
        self.assertEqual(len(self._alerts()), 1)
        self.assertEqual(len(self._logs("patrol_premium_alert")), 1)

        # 同一份名单：下一轮不再推、不再记。
        self._patrol(dry_run=True)
        self.assertEqual(len(self._alerts()), 1)
        self.assertEqual(len(self._logs("patrol_premium_alert")), 1)

        # 多了一个人：名单变了，立即提醒一次。
        second = _member("outside2@example.com", "u-m2", seat_type="prolite", source="system")
        self._add("team-t", second)
        self._cache("team-t", [OWNER, first, second])
        self._patrol(dry_run=True)
        self.assertEqual(len(self._alerts()), 2)
        self.assertIn("outsi…@example", self._alerts()[-1])

        # 都处理掉了：静默关闭，不发"恢复"。
        self._cache("team-t", [OWNER])
        self._patrol(dry_run=True)
        self.assertEqual(len(self.notify_calls), 2)
        conn = self._conn()
        open_rows = conn.execute(
            "SELECT COUNT(*) FROM team_health_incidents WHERE team_id = 'team-t' AND status = 'open'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(open_rows, 0)

        # 又回到最早那份名单：间隔内提醒过，不重复。
        self._cache("team-t", [OWNER, first])
        self._patrol(dry_run=True)
        self.assertEqual(len(self.notify_calls), 2)

        # 过了间隔仍在：再提醒一次。
        conn = self._conn()
        stale = (datetime.now(timezone.utc) - patrol.PREMIUM_ALERT_INTERVAL - timedelta(minutes=1)).isoformat()
        conn.execute("UPDATE team_health_incidents SET last_notified_at = ? WHERE team_id = 'team-t'", (stale,))
        conn.commit()
        conn.close()
        self._patrol(dry_run=True)
        self.assertEqual(len(self.notify_calls), 3)

    def test_alert_failure_never_stops_the_rest_of_patrol(self):
        self._armed_team("team-err", [
            OWNER,
            _member("a@example.com", "u-a", source="system"),
            _member("newcomer@example.com", "u-d"),
        ], seats_entitled=1)

        def boom(*args, **kwargs):
            raise RuntimeError("findings exploded")

        with patch.object(patrol, "premium_seat_findings_sync", boom):
            self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-d")])
        failed = self._logs("patrol_premium_alert")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["result"], "failed")


# ═══ 六、到期踢人：未知席位类型跳过 ═══════════════════════════════════════════

class ExpiryKickSeatTypeTest(_Base):
    def test_expired_premium_kicked_unknown_types_skipped_and_logged_once(self):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self._team("team-exp")
        self._cache(
            "team-exp",
            [
                _member("premiumpaid@example.com", "u-prem", seat_type="prolite", source="system"),
                _member("robot@example.com", "u-auto", seat_type="automation", source="system"),
            ],
            [_pending("robotinvite@example.com", seat_type="automation", source="system")],
        )
        for email, user_id in (
            ("premiumpaid@example.com", "u-prem"),
            ("robot@example.com", "u-auto"),
            ("robotinvite@example.com", ""),
        ):
            self._expiry("team-exp", email, user_id, source="system", expires_at=past, auto_kick=1)

        with patch.object(app_scheduler, "ChatGPTClient", RecordingClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None):
            app_scheduler.auto_kick_job()
            app_scheduler.auto_kick_job()

        # Premium 到期照旧踢（到期踢人不看席位类型）；automation 成员 / 邀请一次上游调用都没有。
        self.assertEqual(RecordingClient.calls, [("remove_member", "u-prem")])

        conn = self._conn()
        rows = {
            r["email"]: r["kicked"]
            for r in conn.execute("SELECT email, kicked FROM member_expiry WHERE team_id = 'team-exp'")
        }
        conn.close()
        self.assertEqual(rows, {
            "premiumpaid@example.com": 1,
            "robot@example.com": 0,
            "robotinvite@example.com": 0,
        })

        member_skips = [l for l in self._logs("auto_kick") if l["result"] == "skipped"]
        invite_skips = [l for l in self._logs("auto_revoke_invite") if l["result"] == "skipped"]
        # 跑两轮只记一次，不会每分钟刷一条。
        self.assertEqual([l["target_email"] for l in member_skips], ["robot@example.com"])
        self.assertEqual([l["target_email"] for l in invite_skips], ["robotinvite@example.com"])
        self.assertIn("seat_type=automation", member_skips[0]["detail"])


# ═══ 七、定时同步写分类型计数 ═════════════════════════════════════════════════

class SyncWriterTest(_Base):
    def test_data_sync_writes_seat_type_counts_and_capacity(self):
        from app.services import patrol as patrol_service
        from app.services import tg_notify, tg_summary

        self._team("team-sync")
        conn = self._conn()
        conn.execute(
            "UPDATE teams SET display_synced_at = ? WHERE id = 'team-sync'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        conn.close()

        class SyncClient(RecordingClient):
            def get_subscription(self):
                return {
                    "seats_entitled": 3,
                    "seats_in_use": 3,
                    "seat_capacity": [
                        {"type": "default", "paid": 2, "available": 1},
                        {"type": "prolite", "paid": 1, "available": 0},
                    ],
                }

            def get_seat_type_counts(self):
                return {"seat_type_counts": {"default": 2, "usage_based": 0,
                                             "automation": 0, "prolite": 1}}

        with patch.object(app_scheduler, "ChatGPTClient", SyncClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_recovery_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_failure_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None), \
             patch.object(patrol_service, "run_patrol", lambda *a, **kw: {}), \
             patch.object(tg_notify, "notify_admins_sync", lambda *a, **kw: 0), \
             patch.object(tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None):
            app_scheduler.data_sync_job()

        conn = self._conn()
        row = conn.execute(
            "SELECT seat_type_counts_json, seat_capacity_json, chatgpt_count, codex_count "
            "FROM teams WHERE id = 'team-sync'"
        ).fetchone()
        conn.close()
        self.assertEqual(
            json.loads(row["seat_type_counts_json"]),
            {"automation": 0, "default": 2, "prolite": 1, "usage_based": 0},
        )
        self.assertEqual(
            json.loads(row["seat_capacity_json"]),
            {"default": {"paid": 2, "available": 1}, "prolite": {"paid": 1, "available": 0}},
        )
        self.assertEqual((row["chatgpt_count"], row["codex_count"]), (2, 0))
        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(self._calls("revoke_invite"), [])


if __name__ == "__main__":
    unittest.main()
