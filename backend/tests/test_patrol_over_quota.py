"""Over-quota kicks on production-shaped Teams.

- The outsider batch guard (outsider_batch_guard) applies to Premium kicks only. Production Teams
  have 1-4 members and 1-2 seats, and the Owner's member entry has no seat type (it counts as a
  ChatGPT seat), so the guard threshold min(3, size // 2) would block every legitimate over-quota
  kick. Over-quota kicks are bounded by over_by and the per-round cap instead.
- Selection takes the newest over_by candidates first and only then drops the members TeamBoss
  protects. A protected member's slot is not handed to an older outsider.

The baseline over-quota rules (newest over_by, system never chosen, gates, classify_team) are in
test_patrol; protection from TeamBoss history is in test_patrol_authorization_protection;
invalid or unconfirmed seats_entitled is in test_patrol_baseline_and_entitlement.
Every upstream call goes to a recording fake client.
"""

import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _patrol_fixtures import (  # noqa: I001  (_isolation first)
    PROD_OWNER,
    PatrolHistoryCase,
    RecordingClient,
    _member,
    _outsider,
)

from app.services import patrol


# ═══ 超员踢人不套外部成员过多护栏（生产形状的小 Team） ═══════════════════════════

class OverQuotaSmallTeamTest(PatrolHistoryCase):
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
        over_quota_card = [t for t in self.notify_calls if "巡逻发现超员" in t]
        self.assertNotIn("外部成员数量异常", over_quota_card[0])

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


# ═══ 受保护者的名额不往后补 ═══════════════════════════════════════════════════

class OverQuotaVetoSlotTest(PatrolHistoryCase):
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


if __name__ == "__main__":
    unittest.main()
