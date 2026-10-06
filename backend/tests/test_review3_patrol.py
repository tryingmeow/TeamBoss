"""巡逻踢人第三轮复核修复（F4–F6、N1、N2）的回归测试。

- F4：邮箱等于 teams.owner_email（不分大小写）的人是 Owner，即使上游角色不是 account-owner。
  候选筛选和 _patrol_kick 的闸门都认这一条，和 scheduler 同步时认 Owner 的规则相同。
- F5：批量邀请发现"邮箱已在这个 Team"时写的 invite_gpt_member_existing 也是 TeamBoss 的邀请记录，
  单独一条就挡住 Premium 踢人；它是 skipped，"TeamBoss 最近给他定的席位"照旧不看它。

所有上游调用都是记录调用的假客户端，绝不触网。
"""

import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_premium_patrol import (  # noqa: I001  (_isolation first)
    RecordingClient,
    _member,
)
from test_review2_patrol import PROD_OWNER, _Fixture, _outsider

from app.services import patrol
from app.services.gpt_invites import EMAIL_ALREADY_IN_TEAM


class _R3Fixture(_Fixture):
    def _owner_email(self, team_id, owner_email):
        conn = self._conn()
        conn.execute("UPDATE teams SET owner_email = ? WHERE id = ?", (owner_email, team_id))
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


if __name__ == "__main__":
    unittest.main()
