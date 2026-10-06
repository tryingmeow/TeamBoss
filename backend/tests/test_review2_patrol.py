"""巡逻踢人第二轮复核修复（P1–P4 + 严格模式刷新的分页判定）的回归测试。

- P1：外部成员过多护栏（outsider_batch_guard）只管 Premium 踢人，超员踢人不套。生产 Team 只有
  1–4 人、1–2 个席位，Owner 的成员条目没有席位类型（按 ChatGPT 席位计），护栏阈值
  min(3, 人数 // 2) 会拦下每一次正当的超员踢人。
- P2：超员踢人先取最新的 over_by 个，再去掉受保护的人；受保护者的名额不往后补给更老的外部成员。
- P3：只有开着的 system / self_service 记录、被同步因缺席关掉的记录（kick_source='detected'）、
  成功 / 待定 / 处理中的兑换保护一个 detected 成员；到期踢掉、管理员移出的记录不保护。两条踢人路径都是。
- P4：TeamBoss 对这个人在这个 Team 有任何改席位 / 邀请记录（任何结果、任何目标席位）就不踢
  Premium，改走席位提醒。
- H2：严格模式动手前的强制刷新按 SnapshotPageAccumulator 判定名单是否完整；不完整就不写缓存、不踢。

所有上游调用都是记录调用的假客户端，绝不触网。
"""

import _isolation  # noqa: F401  must precede any app import
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_premium_patrol import (  # noqa: I001  (_isolation first)
    OLD,
    RecordingClient,
    _Base,
    _live,
    _member,
)

from app.services import patrol

# 生产形状：Owner 的成员条目没有席位类型，没有来源记录。
PROD_OWNER = _member("owner@example.com", "u-owner", seat_type=None, is_owner=True, source=None)
LIVE_PROD_OWNER = {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}


def _iso(delta=timedelta()):
    return (datetime.now(timezone.utc) + delta).isoformat()


def _outsider(n, *, seat_type="default", day=10):
    return _member(f"out{n}@example.com", f"u-out{n}", seat_type=seat_type,
                   first_seen_at=f"2026-07-{day:02d}T00:00:00+00:00")


class _Fixture(_Base):
    def _closed_row(self, team_id, email, user_id, *, kick_source, source="system",
                    expires_at="2026-08-01T00:00:00+00:00"):
        """TeamBoss 以前管过这个人、后来关掉的 member_expiry 行。"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at, kick_source,
                first_seen_at, source, created_at)
               VALUES (?, ?, ?, ?, 1, 1, '2026-08-02T00:00:00+00:00', ?,
                       '2026-07-01T00:00:00+00:00', ?, '2026-07-01T00:00:00+00:00')""",
            (team_id, user_id, email, expires_at, kick_source, source),
        )
        conn.commit()
        conn.close()

    def _open_row(self, team_id, email, user_id, *, source="system"):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, first_seen_at, source, created_at)
               VALUES (?, ?, ?, '2026-12-01T00:00:00+00:00', 1, 0,
                       '2026-07-01T00:00:00+00:00', ?, '2026-07-01T00:00:00+00:00')""",
            (team_id, user_id, email, source),
        )
        conn.commit()
        conn.close()

    def _seat_log(self, team_id, action, target_email, detail, result, created_at):
        conn = self._conn()
        conn.execute(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, 'manual', ?)""",
            (team_id, action, target_email, detail, result, created_at),
        )
        conn.commit()
        conn.close()

    def _cache_row(self, team_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT members_json, pending_json, updated_at, fetch_started_at FROM member_cache "
            "WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def _kick(self, team_id, member, rule=patrol.KICK_RULE_OVER_QUOTA):
        conn = self._conn()
        try:
            return patrol._patrol_kick(conn, RecordingClient(), team_id, member, rule=rule)
        finally:
            conn.close()


# ═══ P1：超员踢人不套外部成员过多护栏（生产形状的小 Team） ═══════════════════════

class OverQuotaSmallTeamTest(_Fixture):
    def _two_seat_team(self, team_id):
        # 2 个席位；Owner（没有席位类型，按 ChatGPT 计）+ 2 个外部成员 = 3 个 ChatGPT 席位，超 1 个。
        older, newer = _outsider(1, day=10), _outsider(2, day=20)
        self._armed_team(team_id, [PROD_OWNER, older, newer], seats_entitled=2)
        return older, newer

    def test_two_seat_team_kicks_the_newest_outsider(self):
        self._two_seat_team("team-p1")

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-out2")])
        self.assertEqual(result["kicked"], 1)
        self.assertEqual(self._kicked("team-p1", "u-out2"), (1, "patrol"))
        self.assertEqual(self._logs("patrol_kick_batch_capped"), [])
        self.assertFalse(any(e.get("action") == "over_quota_batch_guard" for e in result["events"]))

    def test_two_seat_team_dry_run_names_the_newest_outsider(self):
        self._two_seat_team("team-p1-dry")

        result = self._patrol(dry_run=True)

        self.assertEqual(RecordingClient.calls, [])
        self.assertEqual(result["would_kick"], 1)
        self.assertEqual([l["target_email"] for l in self._logs("patrol_would_kick")],
                         ["out2@example.com"])

    def test_kick_gate_lets_the_two_seat_over_quota_kick_through(self):
        _older, newer = self._two_seat_team("team-p1-gate")

        ok, reason = self._kick("team-p1-gate", newer)

        self.assertTrue(ok, reason)
        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-out2")])

    def test_production_team_shapes(self):
        # (席位数, TeamBoss 管着的成员数, 外部成员数)：Team 1–4 人、1–2 个席位，Owner 都没有席位类型。
        # 超员 = 人数 - 席位数；踢最新的 min(超员, 外部成员数) 个。
        shapes = [
            (1, 0, 1),
            (1, 0, 2),
            (1, 0, 3),
            (1, 1, 1),
            (1, 1, 2),
            (2, 0, 2),
            (2, 0, 3),
            (2, 1, 2),
        ]
        for seats, keepers, outsiders in shapes:
            with self.subTest(seats=seats, keepers=keepers, outsiders=outsiders):
                RecordingClient.calls = []
                team_id = f"team-p1-{seats}-{keepers}-{outsiders}"
                members = [PROD_OWNER]
                members += [_member(f"keeper{i}@{team_id}.com", f"u-k{i}", source="system")
                            for i in range(keepers)]
                outs = [_outsider(i, day=10 + i) for i in range(outsiders)]
                members += outs
                self._armed_team(team_id, members, seats_entitled=seats)

                self._patrol(dry_run=False, allow=[team_id])

                over_by = len(members) - seats
                newest_first = sorted(outs, key=lambda m: m["first_seen_at"], reverse=True)
                expected = [("remove_member", m["id"]) for m in newest_first[:min(over_by, outsiders)]]
                self.assertEqual(self._calls("remove_member"), expected)

    def test_outsider_guard_still_stops_premium_in_the_same_team(self):
        # 1 个席位：Owner + 一个 ChatGPT 外部成员 + 一个 Premium 外部成员。3 人队护栏阈值 = 1，外部成员 2 个：
        # Premium 那个不踢、只提醒；ChatGPT 超员 1 个，照踢。
        plain = _outsider(1)
        premium = _outsider(2, seat_type="prolite")
        self._armed_team("team-p1-mixed", [PROD_OWNER, plain, premium], seats_entitled=1)

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-out1")])
        self.assertTrue(any(e.get("action") == "premium_batch_guard" for e in result["events"]))
        ok, reason = self._kick("team-p1-mixed", premium, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        self.assertFalse(ok)
        self.assertIn("abnormal number of detected outsiders", reason)
        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-out1")])


# ═══ P2：受保护者的名额不往后补 ═══════════════════════════════════════════════

class OverQuotaVetoSlotTest(_Fixture):
    def _veto_team(self, team_id, *, keepers=3):
        # Owner + 若干 TeamBoss 成员 + 一个老外部成员 + 一个最新的付费老用户（记录被同步因缺席关掉，
        # 回来后是新的 detected 行）。席位比人数少 1：超 1 个，最新的 1 个就是付费老用户。
        payer = _member("payer@example.com", "u-pay", first_seen_at="2026-08-02T00:00:00+00:00")
        older = _outsider(1, day=10)
        members = [PROD_OWNER]
        members += [_member(f"keeper{i}@example.com", f"u-k{i}", source="system") for i in range(keepers)]
        members += [older, payer]
        self._closed_row(team_id, "payer@example.com", "u-pay", kick_source="detected",
                         source="self_service", expires_at="2026-12-01T00:00:00+00:00")
        self._armed_team(team_id, members, seats_entitled=len(members) - 1)
        return older, payer

    def test_vetoed_newest_member_does_not_shift_the_kick_to_an_older_outsider(self):
        self._veto_team("team-p2")

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        self.assertIsNone(self._kicked("team-p2", "u-out1")[1])
        card = [t for t in self.notify_calls if "巡逻发现超员" in t][0]
        self.assertIn("超额 1，仅 0 个外部成员可移除", card)
        alerted = {l["target_email"]: l["detail"] for l in self._logs("patrol_premium_alert")}
        self.assertEqual(set(alerted), {"payer@example.com"})
        self.assertIn("kind=chatgpt_detected_was_managed", alerted["payer@example.com"])

    def test_dry_run_uses_the_same_selection(self):
        self._veto_team("team-p2-dry")

        result = self._patrol(dry_run=True)

        self.assertEqual(result["would_kick"], 0)
        self.assertEqual(self._logs("patrol_would_kick"), [])

    def test_kick_gate_rejects_the_older_outsider(self):
        older, _payer = self._veto_team("team-p2-gate")

        ok, reason = self._kick("team-p2-gate", older)

        self.assertFalse(ok)
        self.assertIn("not within the newest over-quota candidates", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_production_shape(self):
        # 2 个席位：Owner + 老外部成员 + 付费老用户，超 1 个，最新的是付费老用户：谁都不踢。
        self._veto_team("team-p2-prod", keepers=0)

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])

    def test_only_the_vetoed_slot_is_dropped(self):
        # 超 2 个：最新的两个是付费老用户和 out2，踢 out2；更老的 out1 不补位。
        payer = _member("payer@example.com", "u-pay", first_seen_at="2026-08-02T00:00:00+00:00")
        out1, out2 = _outsider(1, day=10), _outsider(2, day=20)
        keepers = [_member(f"keeper{i}@example.com", f"u-k{i}", source="system") for i in range(4)]
        members = [PROD_OWNER] + keepers + [out1, out2, payer]
        self._closed_row("team-p2-two", "payer@example.com", "u-pay", kick_source="detected",
                         source="self_service", expires_at="2026-12-01T00:00:00+00:00")
        self._armed_team("team-p2-two", members, seats_entitled=len(members) - 2)

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-out2")])


# ═══ P3：只有在管 / 因缺席才没在管 / 兑换过的人受保护 ════════════════════════════

class ManagedHistoryNarrowTest(_Fixture):
    def _redemption(self, team_id, email, result):
        conn = self._conn()
        token_id = conn.execute(
            """INSERT INTO access_tokens (token_hash, token_prefix, grant_expires_in, max_uses,
                                          used_count, disabled, created_at, seat_type)
               VALUES (?, 'p', '30d', 1, 1, 0, '2026-07-01', 'default')""",
            (f"hash-{team_id}-{result}",),
        ).lastrowid
        conn.execute(
            """INSERT INTO access_token_uses (token_id, email, action, team_id, user_id, result, created_at)
               VALUES (?, ?, 'invite', ?, NULL, ?, '2026-07-09')""",
            (token_id, email, team_id, result),
        )
        conn.commit()
        conn.close()

    def _history_cases(self):
        """名字 → (怎么造历史, 是否保护)。"""
        return {
            "closed_by_expiry": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="auto_expire"), False),
            "self_service_closed_by_expiry": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="auto_expire", source="self_service"), False),
            "closed_by_admin": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="admin"), False),
            "closed_by_patrol": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="patrol"), False),
            "closed_by_sync_absence": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="detected"), True),
            "closed_by_sync_absence_matched_by_user_id": (lambda t: self._closed_row(
                t, "old-address@example.com", "u-ex", kick_source="detected", source="self_service"), True),
            "open_system_row": (lambda t: self._open_row(t, "ex@example.com", "u-ex"), True),
            "successful_redemption": (lambda t: self._redemption(t, "ex@example.com", "success"), True),
            "pending_redemption": (lambda t: self._redemption(t, "ex@example.com", "pending"), True),
            "uncertain_redemption": (lambda t: self._redemption(t, "ex@example.com", "uncertain"), True),
            "failed_redemption": (lambda t: self._redemption(t, "ex@example.com", "failed"), False),
        }

    def test_over_quota_kick(self):
        # 1 个席位：Owner + 这个外部成员，超 1 个。
        for name, (add_history, protects) in self._history_cases().items():
            with self.subTest(case=name):
                RecordingClient.calls = []
                team_id = f"team-p3-oq-{name}"
                member = _member("ex@example.com", "u-ex")
                add_history(team_id)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=1)

                self._patrol(dry_run=False, allow=[team_id])

                expected = [] if protects else [("remove_member", "u-ex")]
                self.assertEqual(self._calls("remove_member"), expected)

    def test_premium_kick(self):
        for name, (add_history, protects) in self._history_cases().items():
            with self.subTest(case=name):
                RecordingClient.calls = []
                team_id = f"team-p3-pr-{name}"
                member = _member("ex@example.com", "u-ex", seat_type="prolite")
                add_history(team_id)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

                self._patrol(dry_run=False, allow=[team_id])

                expected = [] if protects else [("remove_member", "u-ex")]
                self.assertEqual(self._calls("remove_member"), expected)

    def test_expired_customer_back_as_premium_is_a_normal_outsider(self):
        team_id = "team-p3-gate"
        member = _member("ex@example.com", "u-ex", seat_type="prolite")
        self._closed_row(team_id, "ex@example.com", "u-ex", kick_source="auto_expire")
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

        conn = self._conn()
        self.assertFalse(patrol._teamboss_managed_history_sync(conn, team_id, "ex@example.com", "u-ex"))
        self.assertIsNone(patrol._premium_kick_veto_sync(conn, team_id, "ex@example.com", "u-ex"))
        conn.close()
        ok, reason = self._kick(team_id, member, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        self.assertTrue(ok, reason)
        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-ex")])


# ═══ P4：管理员动过他的席位，就不踢 Premium、改走提醒 ═══════════════════════════════

class PremiumSeatRecordTest(_Fixture):
    def test_switch_to_chatgpt_logged_before_the_snapshot_blocks_the_kick(self):
        # 管理员把 Premium 外部成员切到 ChatGPT：成功日志先落库，之后开始的一次刷新里上游还列着
        # Premium。快照开始时间晚于日志，"快照之后改过席位"挡不住；有改席位记录就不能踢，交给提醒。
        team_id = "team-p4"
        member = _member("switched@example.com", "u-sw", seat_type="prolite")
        members = [PROD_OWNER, _member("keeper@example.com", "u-k", source="system"), member]
        self._armed_team(team_id, members, seats_entitled=2)
        self._seat_log(team_id, "change_seat", "switched@example.com",
                       "user_id=u-sw, seat_type=default, from_seat_type=prolite, policy=confirm",
                       "success", _iso(-timedelta(minutes=10)))
        self._cache(team_id, members, updated_at=_iso(-timedelta(minutes=5)))

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(self._logs("patrol_kick"), [])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("改过他的席位或邀请过他", alerts[0])
        logged = self._logs("patrol_premium_alert")
        self.assertEqual([l["target_email"] for l in logged], ["switched@example.com"])
        self.assertIn("kind=premium_detected_with_record", logged[0]["detail"])

        ok, reason = self._kick(team_id, member, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        self.assertFalse(ok)
        self.assertIn("seat or invite record", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_any_seat_or_invite_record_blocks_the_kick(self):
        cases = {
            "switch_to_chatgpt_failed": ("change_seat", "x@example.com",
                                         "user_id=u-x, seat_type=default, from_seat_type=prolite",
                                         "failed"),
            "switch_matched_by_user_id_only": ("change_seat", None,
                                               "user_id=u-x, seat_type=usage_based", "success"),
            "chatgpt_invite": ("invite_member", "X@Example.com",
                               "seat_type=default, expires_in=30d", "success"),
            "invite_refused_by_policy": ("invite_member", "x@example.com",
                                         "seat_type=prolite, policy=forbid, reason=overage_forbidden",
                                         "skipped"),
            "switch_lookup_failed": ("change_seat", None, "user_id=u-x, pre_switch_lookup", "failed"),
        }
        for name, (action, target, detail, result) in cases.items():
            with self.subTest(case=name):
                RecordingClient.calls = []
                team_id = f"team-p4-{name}"
                member = _member("x@example.com", "u-x", seat_type="prolite")
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)
                self._seat_log(team_id, action, target, detail, result, "2026-07-01T00:00:00+00:00")

                self._patrol(dry_run=False, allow=[team_id])

                self.assertEqual(self._calls("remove_member"), [])

    def test_record_on_another_team_or_member_does_not_block(self):
        team_id = "team-p4-other"
        member = _member("x@example.com", "u-x", seat_type="prolite")
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)
        self._seat_log("team-elsewhere", "change_seat", "x@example.com",
                       "user_id=u-x, seat_type=default", "success", "2026-07-01T00:00:00+00:00")
        self._seat_log(team_id, "change_seat", None,
                       "user_id=u-x1, seat_type=default", "success", "2026-07-01T00:00:00+00:00")

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-x")])


# ═══ H2：严格模式动手前的强制刷新只认完整名单 ═══════════════════════════════════

class StrictRefreshPagingTest(_Fixture):
    def _strict_team(self, team_id):
        # 严格模式：一个早就过了等待期的 ChatGPT 外部成员，刷新成功就会被踢。
        plain = _member("plain@example.com", "u-d", first_seen_at=OLD)
        members = [PROD_OWNER, _member("keeper@example.com", "u-k", source="system"), plain]
        self._armed_team(team_id, members, seats_entitled=99)
        self._setting("patrol_strict_mode_enabled", "1")
        live = [LIVE_PROD_OWNER, _live("keeper@example.com", "u-k", "default"),
                _live("plain@example.com", "u-d", "default")]
        return live

    def _run_with_member_pages(self, team_id, pages):
        """get_members 依次返回 pages 里的每一页；返回 (本轮结果, 刷新前的缓存行)。"""
        served = list(pages)

        class PagedClient(RecordingClient):
            def get_members(self, offset=0, limit=100):
                RecordingClient.calls.append(("get_members", offset))
                return served.pop(0) if served else {"items": [], "total": 0}

        before = self._cache_row(team_id)
        with patch.object(patrol, "ChatGPTClient", PagedClient):
            result = self._patrol(dry_run=False)
        return result, before

    def test_empty_page_with_nonzero_total_fails_the_refresh(self):
        team_id = "team-h2-empty"
        self._strict_team(team_id)

        result, before = self._run_with_member_pages(team_id, [{"items": [], "total": 1}])

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(self._cache_row(team_id), before)
        self.assertTrue(any(e.get("action") == "strict_refresh_failed" for e in result["events"]))
        self.assertEqual(len(self._logs("patrol_strict_refresh_failed")), 1)

    def test_more_entries_than_the_reported_total_fails_the_refresh(self):
        team_id = "team-h2-over"
        live = self._strict_team(team_id)

        result, before = self._run_with_member_pages(team_id, [{"items": live, "total": 2}])

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(self._cache_row(team_id), before)
        failed = [e for e in result["events"] if e.get("action") == "strict_refresh_failed"]
        self.assertIn("more member/invite entries than the reported total", failed[0]["error"])

    def test_non_object_entry_fails_the_refresh(self):
        team_id = "team-h2-entry"
        live = self._strict_team(team_id)

        result, before = self._run_with_member_pages(
            team_id, [{"items": live[:2] + ["plain@example.com"], "total": 3}]
        )

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(self._cache_row(team_id), before)
        self.assertTrue(any(e.get("action") == "strict_refresh_failed" for e in result["events"]))

    def test_complete_snapshot_still_refreshes_and_kicks(self):
        team_id = "team-h2-ok"
        live = self._strict_team(team_id)

        self._run_with_member_pages(team_id, [{"items": live, "total": 3}])

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-d")])

    def _fetch(self, pages, **kwargs):
        served = list(pages)
        requested = []

        def method(offset=0, limit=100):
            requested.append((offset, limit))
            return served.pop(0)

        items, error = patrol._fetch_all_api_items_sync(method, "users", **kwargs)
        return items, error, requested

    def test_paginator_rules(self):
        a, b, c = ({"id": f"u-{n}"} for n in "abc")
        cases = {
            "total_changed_between_pages": ([{"items": [a, b], "total": 4}, {"items": [c], "total": 3}],
                                            "total changed"),
            "malformed_total": ([{"items": [a], "total": "1"}], "malformed total"),
            "page_larger_than_limit": ([{"items": [a, b, c], "total": 3}], "larger than the requested limit"),
            "missing_list": ([{"total": 0}], "unrecognized member/invite response structure"),
        }
        for name, (pages, expected) in cases.items():
            with self.subTest(case=name):
                items, error, _requested = self._fetch(pages, limit=2)
                self.assertIsNone(items)
                self.assertIn(expected, error)

    def test_paginator_keeps_the_upstream_error_text_and_the_page_cap(self):
        items, error, _ = self._fetch([{"error": "Unauthorized"}])
        self.assertEqual((items, error), (None, "Unauthorized"))

        full_page = {"items": [{"id": "u-a"}, {"id": "u-b"}]}
        items, error, requested = self._fetch([full_page] * 5, limit=2, max_items=6)
        self.assertIsNone(items)
        self.assertIn("exceeds the paging limit", error)
        self.assertEqual(requested, [(0, 2), (2, 2), (4, 2)])

    def test_paginator_returns_a_complete_multi_page_list(self):
        a, b, c = ({"id": f"u-{n}"} for n in "abc")
        items, error, requested = self._fetch(
            [{"users": [a, b], "total": 3}, {"users": [c], "total": 3}], limit=2
        )
        self.assertIsNone(error)
        self.assertEqual(items, [a, b, c])
        self.assertEqual(requested, [(0, 2), (2, 2)])


if __name__ == "__main__":
    unittest.main()
