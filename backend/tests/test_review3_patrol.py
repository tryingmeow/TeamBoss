"""巡逻踢人第三轮复核修复（F4–F6、N1、N2）的回归测试。

- F4：邮箱等于 teams.owner_email（不分大小写）的人是 Owner，即使上游角色不是 account-owner。
  候选筛选和 _patrol_kick 的闸门都认这一条，和 scheduler 同步时认 Owner 的规则相同。
- F5：批量邀请发现"邮箱已在这个 Team"时写的 invite_gpt_member_existing 也是 TeamBoss 的邀请记录，
  单独一条就挡住 Premium 踢人；它是 skipped，"TeamBoss 最近给他定的席位"照旧不看它。
- F6：成功 / 待定 / 处理中的兑换只在之后没有被到期（auto_expire）、管理员（admin）、巡逻（patrol*）
  关掉过这个 Team + 邮箱的行时才保护；按解析后的时间比，读不出的时间按"还保护"。两条踢人路径都是，
  Premium 兑换码的兑换（_teamboss_seat_record_sync）也是。改席位 / 邀请记录仍然永久挡 Premium 踢人。
- N1：GET /api/patrol/status（Telegram /watch、/team）列的"待处理"和真正的超员踢人同一份选人；
  受保护的人单列为"不自动移除"，不算待处理。
- N2：Premium 外部成员移除失败（上游 403 这类每轮都会重复）的卡片走 team-health 的限频
  （REPEAT_ALERT_INTERVAL）；失败日志每轮照写，移除成功的卡片照常每次发。

所有上游调用都是记录调用的假客户端，绝不触网。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_premium_patrol import (  # noqa: I001  (_isolation first)
    RecordingClient,
    _member,
)
from test_review2_patrol import PROD_OWNER, _Fixture, _outsider

from app import tg_bot
from app.routes import patrol as patrol_routes
from app.services import patrol, team_health_alerts
from app.services.gpt_invites import EMAIL_ALREADY_IN_TEAM


class _R3Fixture(_Fixture):
    _token_seq = 0

    def _owner_email(self, team_id, owner_email):
        conn = self._conn()
        conn.execute("UPDATE teams SET owner_email = ? WHERE id = ?", (owner_email, team_id))
        conn.commit()
        conn.close()

    def _redeem(self, team_id, email, created_at, *, result="success", seat_type="default"):
        """这个 Team + 邮箱的一次兑换（access_token_uses 一行）。"""
        _R3Fixture._token_seq += 1
        conn = self._conn()
        token_id = conn.execute(
            """INSERT INTO access_tokens (token_hash, token_prefix, grant_expires_in, max_uses,
                                          used_count, disabled, created_at, seat_type)
               VALUES (?, 'p', '30d', 1, 1, 0, '2026-07-01', ?)""",
            (f"hash-r3-{_R3Fixture._token_seq}", seat_type),
        ).lastrowid
        conn.execute(
            """INSERT INTO access_token_uses (token_id, email, action, team_id, user_id, result, created_at)
               VALUES (?, ?, 'invite', ?, NULL, ?, ?)""",
            (token_id, email, team_id, result, created_at),
        )
        conn.commit()
        conn.close()

    def _closed(self, team_id, email, user_id, *, kick_source, kicked_at, source="self_service"):
        """这个 Team + 邮箱一条已关闭的 member_expiry 行（kicked=1）。"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at, kick_source,
                first_seen_at, source, created_at)
               VALUES (?, ?, ?, '2026-08-01T00:00:00+00:00', 1, 1, ?, ?,
                       '2026-07-01T00:00:00+00:00', ?, '2026-07-01T00:00:00+00:00')""",
            (team_id, user_id, email, kicked_at, kick_source, source),
        )
        conn.commit()
        conn.close()


# ═══ F4：teams.owner_email 也是 Owner ═════════════════════════════════════════

class OwnerEmailIsOwnerTest(_R3Fixture):
    def _owner_row(self, seat_type):
        # Owner 的成员条目角色不是 account-owner（is_owner=False），来源记录是 detected。
        return _member("Boss@Example.com", "u-boss", seat_type=seat_type,
                       first_seen_at="2026-08-01T00:00:00+00:00")

    def test_premium_owner_by_email_is_not_kicked(self):
        team_id = "team-f4-premium"
        boss = self._owner_row("prolite")
        self._armed_team(team_id, [PROD_OWNER, boss], seats_entitled=2)
        self._owner_email(team_id, "boss@EXAMPLE.com ")

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(self._logs("patrol_kick"), [])
        self.assertEqual(self._logs("patrol_premium_alert"), [])
        self.assertEqual(
            patrol.select_premium_kick_candidates([PROD_OWNER, boss], "boss@example.com"), []
        )

    def test_premium_kick_gate_rejects_the_owner_email(self):
        team_id = "team-f4-gate"
        boss = self._owner_row("prolite")
        self._armed_team(team_id, [PROD_OWNER, boss], seats_entitled=2)
        self._owner_email(team_id, "boss@example.com")

        ok, reason = self._kick(team_id, boss, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)

        self.assertFalse(ok)
        self.assertIn("owner_email", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_over_quota_skips_the_owner_email_and_takes_the_next_outsider(self):
        # 1 个席位：Owner 条目（按邮箱认）+ 一个更老的外部成员，超 1 个。Owner 不算候选，
        # 和 is_owner=True 的 Owner 一样；超员的那一个是外部成员。
        team_id = "team-f4-oq"
        boss = self._owner_row("default")
        out = _outsider(1, day=10)
        self._armed_team(team_id, [boss, out], seats_entitled=1)
        self._owner_email(team_id, "boss@example.com")

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-out1")])
        RecordingClient.calls = []
        ok, reason = self._kick(team_id, boss)
        self.assertFalse(ok)
        self.assertIn("owner_email", reason)
        self.assertEqual(self._calls("remove_member"), [])


# ═══ F5：批量邀请的"已在这个 Team"记录也挡 Premium 踢人 ═════════════════════════

class ExistingMemberInviteLogTest(_R3Fixture):
    def test_batch_invite_existing_member_log_alone_blocks_the_premium_kick(self):
        team_id = "team-f5"
        member = _member("x@example.com", "u-x", seat_type="prolite")
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)
        self._seat_log(team_id, "invite_gpt_member_existing", "X@Example.com",
                       EMAIL_ALREADY_IN_TEAM, "skipped", "2026-07-01T00:00:00+00:00")

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(self._logs("patrol_kick"), [])
        logged = self._logs("patrol_premium_alert")
        self.assertEqual([l["target_email"] for l in logged], ["x@example.com"])
        self.assertIn("kind=premium_detected_with_record", logged[0]["detail"])

        ok, reason = self._kick(team_id, member, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        self.assertFalse(ok)
        self.assertIn("seat or invite record", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_existing_member_log_is_not_a_seat_assignment(self):
        # TeamBoss 邀请他进 Premium，后来批量邀请时发现他已在 Team、跳过：最近一次定席位仍是 Premium，
        # 不发"不是 TeamBoss 切的"提醒。
        team_id = "team-f5-managed"
        managed = _member("m@example.com", "u-m", seat_type="prolite", source="system")
        self._armed_team(team_id, [PROD_OWNER, managed], seats_entitled=2)
        self._seat_log(team_id, "invite_member", "m@example.com",
                       "seat_type=prolite, expires_in=30d", "success", "2026-07-01T00:00:00+00:00")
        self._seat_log(team_id, "invite_gpt_member_existing", "m@example.com",
                       EMAIL_ALREADY_IN_TEAM, "skipped", "2026-07-05T00:00:00+00:00")

        conn = self._conn()
        try:
            self.assertTrue(patrol._teamboss_set_premium_sync(conn, team_id, "m@example.com", "u-m"))
        finally:
            conn.close()
        self._patrol(dry_run=False)
        self.assertEqual(self._logs("patrol_premium_alert"), [])


# ═══ F6：兑换的保护在之后的到期 / 管理员 / 巡逻关闭时结束 ═══════════════════════════

REDEEMED = "2026-07-09T00:00:00+00:00"
AFTER = "2026-08-02T00:00:00+00:00"
BEFORE = "2026-07-01T00:00:00+00:00"


class RedemptionProtectionEndsTest(_R3Fixture):
    EMAIL = "ex@example.com"
    USER_ID = "u-ex"

    def _close(self, team_id, kick_source, kicked_at, *, source="self_service", email=None):
        self._closed(team_id, email or self.EMAIL, self.USER_ID,
                     kick_source=kick_source, kicked_at=kicked_at, source=source)

    def _cases(self):
        """名字 → (怎么造历史, 是否保护)。兑换都在这个 Team、这个邮箱上。"""
        e = self.EMAIL
        return {
            # 到期踢掉 / 管理员移出 / 巡逻移出发生在兑换之后：服务已经结束，回来就是外部成员。
            "expired_after": (lambda t: (self._redeem(t, e, REDEEMED),
                                         self._close(t, "auto_expire", AFTER)), False),
            "pending_redemption_expired_after": (lambda t: (
                self._redeem(t, e, REDEEMED, result="pending"),
                self._close(t, "auto_expire", AFTER)), False),
            "uncertain_redemption_expired_after": (lambda t: (
                self._redeem(t, e, REDEEMED, result="uncertain"),
                self._close(t, "auto_expire", AFTER)), False),
            "admin_after": (lambda t: (self._redeem(t, e, REDEEMED),
                                       self._close(t, "admin", AFTER)), False),
            "patrol_after": (lambda t: (self._redeem(t, e, REDEEMED),
                                        self._close(t, "patrol", AFTER, source="detected")), False),
            "patrol_premium_after": (lambda t: (
                self._redeem(t, e, REDEEMED),
                self._close(t, "patrol_premium", AFTER, source="detected")), False),
            "expired_after_z_suffix": (lambda t: (self._redeem(t, e, REDEEMED),
                                                  self._close(t, "auto_expire", "2026-08-02T00:00:00Z")),
                                       False),
            # 字符串顺序和时间顺序相反：+08:00 的兑换时间（UTC 00:00）字符串更"大"，关闭其实晚 1 小时。
            "closure_after_in_other_timezone": (lambda t: (
                self._redeem(t, e, "2026-08-02T08:00:00+08:00"),
                self._close(t, "auto_expire", "2026-08-02T01:00:00+00:00")), False),
            # 反过来：关闭时间字符串更"大"，其实比兑换早 30 分钟。
            "closure_before_in_other_timezone": (lambda t: (
                self._redeem(t, e, "2026-08-02T01:00:00+00:00"),
                self._close(t, "auto_expire", "2026-08-02T08:30:00+08:00")), True),
            # 仍然保护的：关闭在兑换之前、同步因缺席关掉、同一时刻、别的 Team / 别的邮箱、时间读不出、
            # 到期之后又兑换了。
            "expired_before": (lambda t: (self._redeem(t, e, REDEEMED),
                                          self._close(t, "auto_expire", BEFORE)), True),
            "sync_absence_after": (lambda t: (
                self._redeem(t, e, REDEEMED),
                self._close(t, "detected", AFTER, source="detected")), True),
            "closed_at_the_same_instant": (lambda t: (self._redeem(t, e, REDEEMED),
                                                      self._close(t, "auto_expire", REDEEMED)), True),
            "closure_on_another_team": (lambda t: (self._redeem(t, e, REDEEMED),
                                                   self._close("team-elsewhere", "auto_expire", AFTER)),
                                        True),
            "closure_for_another_email": (lambda t: (
                self._redeem(t, e, REDEEMED),
                self._close(t, "auto_expire", AFTER, email="someone@example.com")), True),
            "unparsable_closure_time": (lambda t: (self._redeem(t, e, REDEEMED),
                                                   self._close(t, "auto_expire", "not a time")), True),
            "unparsable_redemption_time": (lambda t: (self._redeem(t, e, "not a time"),
                                                      self._close(t, "auto_expire", AFTER)), True),
            "redeemed_again_after_expiry": (lambda t: (
                self._redeem(t, e, REDEEMED),
                self._close(t, "auto_expire", AFTER),
                self._redeem(t, e, "2026-08-10T00:00:00+00:00")), True),
        }

    def _run_cases(self, *, seat_type, seats_entitled, prefix):
        for name, (add_history, protects) in self._cases().items():
            with self.subTest(case=name):
                RecordingClient.calls = []
                team_id = f"team-f6-{prefix}-{name}"
                member = _member(self.EMAIL, self.USER_ID, seat_type=seat_type)
                add_history(team_id)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=seats_entitled)

                self._patrol(dry_run=False, allow=[team_id])

                expected = [] if protects else [("remove_member", self.USER_ID)]
                self.assertEqual(self._calls("remove_member"), expected)

    def test_over_quota_kick(self):
        # 1 个席位：Owner + 这个外部成员，超 1 个。
        self._run_cases(seat_type="default", seats_entitled=1, prefix="oq")

    def test_premium_kick(self):
        self._run_cases(seat_type="prolite", seats_entitled=2, prefix="pr")

    def test_expired_self_service_customer_back_is_kickable_through_the_gate(self):
        for seat_type, seats, rule in (
            ("default", 1, patrol.KICK_RULE_OVER_QUOTA),
            ("prolite", 2, patrol.KICK_RULE_PREMIUM_OUTSIDER),
        ):
            with self.subTest(rule=rule):
                RecordingClient.calls = []
                team_id = f"team-f6-gate-{rule}"
                member = _member(self.EMAIL, self.USER_ID, seat_type=seat_type)
                self._redeem(team_id, self.EMAIL, REDEEMED)
                self._close(team_id, "auto_expire", AFTER)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=seats)

                conn = self._conn()
                try:
                    self.assertFalse(patrol._teamboss_managed_history_sync(
                        conn, team_id, self.EMAIL, self.USER_ID))
                finally:
                    conn.close()
                ok, reason = self._kick(team_id, member, rule=rule)
                self.assertTrue(ok, reason)
                self.assertEqual(self._calls("remove_member"), [("remove_member", self.USER_ID)])

    def test_premium_code_use_ends_with_the_service(self):
        # Premium 兑换码卖出的席位：到期之后从外面回来占 Premium，是普通外部成员。
        team_id = "team-f6-code-expired"
        member = _member(self.EMAIL, self.USER_ID, seat_type="prolite")
        self._redeem(team_id, self.EMAIL, REDEEMED, seat_type="prolite")
        self._close(team_id, "auto_expire", AFTER)
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

        conn = self._conn()
        try:
            self.assertFalse(patrol._teamboss_seat_record_sync(conn, team_id, self.EMAIL, self.USER_ID))
            self.assertIsNone(patrol._premium_kick_veto_sync(conn, team_id, self.EMAIL, self.USER_ID))
        finally:
            conn.close()
        self._patrol(dry_run=False)
        self.assertEqual(self._calls("remove_member"), [("remove_member", self.USER_ID)])

    def test_premium_code_use_still_counts_while_the_service_runs(self):
        team_id = "team-f6-code-live"
        member = _member(self.EMAIL, self.USER_ID, seat_type="prolite")
        self._redeem(team_id, self.EMAIL, REDEEMED, seat_type="prolite")
        self._close(team_id, "detected", AFTER, source="detected")
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        ok, reason = self._kick(team_id, member, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        self.assertFalse(ok)
        self.assertIn("seat or invite record", reason)

    def test_seat_and_invite_records_still_block_premium_after_a_closure(self):
        # P4 不变：改席位 / 邀请记录永久有效，之后被到期 / 管理员移出也照样挡 Premium 踢人。
        for action, detail in (
            ("change_seat", "user_id=u-ex, seat_type=default, from_seat_type=prolite"),
            ("invite_member", "seat_type=prolite, expires_in=30d"),
            ("invite_gpt_member", "seat_type=default"),
        ):
            with self.subTest(action=action):
                RecordingClient.calls = []
                team_id = f"team-f6-p4-{action}"
                member = _member(self.EMAIL, self.USER_ID, seat_type="prolite")
                self._seat_log(team_id, action, self.EMAIL, detail, "success", REDEEMED)
                self._close(team_id, "admin", AFTER)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

                self._patrol(dry_run=False, allow=[team_id])

                self.assertEqual(self._calls("remove_member"), [])


# ═══ N1：Telegram 风险视图的"待处理"和真踢同一份选人 ═══════════════════════════════

class RiskViewSelectionTest(_R3Fixture):
    def _over_team(self, team_id):
        # 2 个席位：Owner + 老外部成员 + 最新进来的付费用户（兑换还在保护期），超 1 个。
        payer = _member("payer@example.com", "u-pay", first_seen_at="2026-08-02T00:00:00+00:00")
        older = _outsider(1, day=10)
        self._armed_team(team_id, [PROD_OWNER, older, payer], seats_entitled=2)
        return payer

    def _status_for(self, team_id):
        status = asyncio.run(patrol_routes.get_patrol_status(refresh=False))
        return status, {t["team_id"]: t for t in status["teams"]}[team_id]

    @staticmethod
    def _tg_views(status, team_id):
        def fake_get(path, params=None, timeout=None):
            if path == "/api/patrol/status":
                return status
            raise RuntimeError("finance overview is not part of this test")

        with patch.object(tg_bot, "_api_get", side_effect=fake_get):
            return tg_bot.cmd_watch({}, ""), tg_bot.cmd_team({}, team_id)

    def test_protected_member_is_not_listed_as_pending(self):
        team_id = "team-n1-kept"
        self._redeem(team_id, "payer@example.com", REDEEMED)
        self._over_team(team_id)

        status, team = self._status_for(team_id)

        self.assertEqual(team["risk"], "over")
        self.assertEqual(team["over_by"], 1)
        # 最新的那个受保护，名额不往后补：真踢一个都不踢，视图也没有待处理。
        self.assertEqual(team["detected_over"], [])
        self.assertEqual([c["email"] for c in team["detected_over_kept"]], ["payer@example.com"])
        self._patrol(dry_run=True, allow=[team_id])
        self.assertEqual(self._logs("patrol_would_kick"), [])

        for text in self._tg_views(status, team_id):
            self.assertNotIn("待处理", text)
            self.assertIn("🛡️ 不自动移除：payer@example.com", text)

    def test_kickable_member_is_listed_as_pending(self):
        # 同一个人，兑换之后已经到期踢掉过：真踢会踢他，视图把他列为待处理。
        team_id = "team-n1-pending"
        self._redeem(team_id, "payer@example.com", REDEEMED)
        self._closed(team_id, "payer@example.com", "u-pay", kick_source="auto_expire", kicked_at=AFTER)
        self._over_team(team_id)

        status, team = self._status_for(team_id)

        self.assertEqual([c["email"] for c in team["detected_over"]], ["payer@example.com"])
        self.assertEqual(team["detected_over_kept"], [])
        self._patrol(dry_run=True, allow=[team_id])
        self.assertEqual([l["target_email"] for l in self._logs("patrol_would_kick")],
                         ["payer@example.com"])

        watch, card = self._tg_views(status, team_id)
        self.assertIn("👤 待处理：payer@example.com · ChatGPT", watch)
        self.assertIn("👤 待处理：payer@example.com", card)
        self.assertNotIn("不自动移除", watch + card)

    def test_owner_email_is_never_pending(self):
        team_id = "team-n1-owner"
        boss = _member("boss@example.com", "u-boss", first_seen_at="2026-08-02T00:00:00+00:00")
        self._armed_team(team_id, [boss, _outsider(1, day=10)], seats_entitled=1)
        self._owner_email(team_id, "BOSS@example.com")

        _status, team = self._status_for(team_id)

        self.assertEqual([c["email"] for c in team["detected_over"]], ["out1@example.com"])


# ═══ N2：Premium 移除失败的卡片限频 ═══════════════════════════════════════════════

class ForbiddenClient(RecordingClient):
    """remove_member 每次都被上游 403 拒绝。"""

    def remove_member(self, user_id):
        RecordingClient.calls.append(("remove_member", user_id))
        return {"error": "403 Forbidden: insufficient permissions"}


class PremiumKickFailureThrottleTest(_R3Fixture):
    def _premium_team(self, team_id):
        member = _member("p@example.com", "u-p", seat_type="prolite")
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

    def _kick_cards(self):
        return [t for t in self.notify_calls if "巡逻移除 Premium 外部成员" in t]

    def _failed_kick_logs(self):
        return [l for l in self._logs("patrol_kick") if l["result"] == "failed"]

    def test_repeated_403_sends_one_card_and_logs_every_round(self):
        self._premium_team("team-n2")

        with patch.object(patrol, "ChatGPTClient", ForbiddenClient):
            first = self._patrol(dry_run=False)
            second = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-p")] * 2)
        cards = self._kick_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("p@example.com（403 Forbidden", cards[0])
        failed = self._failed_kick_logs()
        self.assertEqual(len(failed), 2)
        self.assertTrue(all("403 Forbidden" in (l["error_message"] or "") for l in failed))
        for result in (first, second):
            self.assertEqual(
                [e["result"] for e in result["events"] if e.get("action") == "kick"], ["failed"]
            )

    def test_reminder_after_the_team_health_interval(self):
        self._premium_team("team-n2-remind")
        with patch.object(patrol, "ChatGPTClient", ForbiddenClient):
            self._patrol(dry_run=False)
            # 上一次提醒已经过了 REPEAT_ALERT_INTERVAL：再提醒一次。
            stale = (datetime.now(timezone.utc) - team_health_alerts.REPEAT_ALERT_INTERVAL
                     - timedelta(minutes=1)).isoformat()
            conn = self._conn()
            conn.execute(
                "UPDATE team_health_incidents SET last_notified_at = ? WHERE team_id = ? "
                "AND alert_key LIKE 'premium_kick_failed:%'",
                (stale, "team-n2-remind"),
            )
            conn.commit()
            conn.close()
            self._patrol(dry_run=False)

        cards = self._kick_cards()
        self.assertEqual(len(cards), 2)
        self.assertIn("仍未成功", cards[1])
        self.assertEqual(len(self._failed_kick_logs()), 2)

    def test_success_card_is_not_throttled_and_closes_the_failure(self):
        self._premium_team("team-n2-ok")
        with patch.object(patrol, "ChatGPTClient", ForbiddenClient):
            self._patrol(dry_run=False)

        self._patrol(dry_run=False)

        cards = self._kick_cards()
        self.assertEqual(len(cards), 2)
        self.assertIn("✅ 已移除：p@example.com", cards[1])
        conn = self._conn()
        try:
            open_rows = conn.execute(
                "SELECT COUNT(*) FROM team_health_incidents WHERE team_id = ? AND status = 'open' "
                "AND alert_key LIKE 'premium_kick_failed:%'",
                ("team-n2-ok",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(open_rows, 0)


if __name__ == "__main__":
    unittest.main()
