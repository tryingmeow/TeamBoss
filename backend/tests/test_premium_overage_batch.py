"""批量自动分配（POST /api/gpt-members/invite，不指定 Team）按每个 Team 的超员策略超员。

* 填空位：和以前一样（缓存挑 Team，锁内现拉确认）；缓存空位也按「取小」规则算。
* 没空位的邮箱：「超员自动」的 Team 直接加、不问；只剩「超员需确认」时问一次，
  409 里带 overage_plan（加购几个、加在哪个 Team）；「禁止超员」永远不超员。
* 哪里都去不了：failed 里写「没位置，未邀请」，并列在 no_place_emails。
"""

from test_premium_overage_support import (  # noqa: I001  (_isolation first)
    FakeTeamClient,
    TempDbMixin,
    direct_call,
)

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.routes import gpt_members
from app.services import gpt_invites, seat_capacity


ABSENT = {"members": [], "pending_invites": []}
NO_PLACE = "没位置，未邀请"


def _members(n, prefix):
    return [{"email": f"{prefix}{i}@example.com", "seat_type": "default", "status": "active"} for i in range(n)]


class _BatchHarness(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._start_db()
        self.clients: dict[str, FakeTeamClient] = {}

    def team(self, team_id, *, policy, seats=1, used=1, created_at, live_used=None, **kwargs):
        """缓存里 ``used`` 个 ChatGPT 成员；现拉时 ``live_used``（默认同缓存）个。"""
        self.insert_team(
            team_id, policy=policy, seats_entitled=seats, members=_members(used, team_id),
            created_at=created_at, **kwargs,
        )
        live = used if live_used is None else live_used
        self.clients[team_id] = FakeTeamClient(
            seats_entitled=seats, counts={"default": live, "usage_based": 0}
        )

    def submit(self, emails, *, allow_overage=False):
        for team_id in self.clients:
            for email in emails:
                self.track_reservation(team_id, email)
        patches = [
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(side_effect=lambda t: self.clients[t])),
            patch.object(gpt_invites, "fetch_and_cache_members", new=AsyncMock(return_value=ABSENT)),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(seat_capacity, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "notify_member_event", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(
                gpt_members.invite_gpt_members(
                    gpt_members.InviteGptMembersRequest(
                        emails=emails, expires_in="30d", allow_overage=allow_overage
                    )
                )
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

    def invited(self, team_id):
        return [m[1] for m in self.clients[team_id].mutations if m[0] == "invite_member"]

    def all_mutations(self):
        return {t: c.mutations for t, c in self.clients.items() if c.mutations}


class AutoTeamTest(_BatchHarness):
    def test_auto_team_is_overfilled_without_asking(self):
        self.team("t-auto", policy="auto", created_at="2026-10-02")
        self.team("t-forbid", policy="forbid", created_at="2026-10-01")

        result, exc = self.submit(["a@example.com", "b@example.com"])

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual([item["team_id"] for item in result["added"]], ["t-auto", "t-auto"])
        self.assertTrue(all(item["overage"] for item in result["added"]))
        self.assertEqual(self.invited("t-auto"), ["a@example.com", "b@example.com"])
        self.assertEqual(self.invited("t-forbid"), [], "禁止超员的 Team 永远不超员")
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["no_place_emails"], [])
        log = [row for row in self.logs("invite_gpt_member") if row["result"] == "success"][0]
        self.assertIn("policy=auto", log["detail"])
        self.assertIn("overage=True", log["detail"])

    def test_free_seats_are_used_before_any_overfill(self):
        self.team("t-auto", policy="auto", created_at="2026-10-01")
        self.team("t-free", policy="forbid", seats=2, used=1, created_at="2026-10-02")

        result, exc = self.submit(["a@example.com"])

        self.assertIsNone(exc)
        self.assertEqual(result["added"][0]["team_id"], "t-free")
        self.assertFalse(result["added"][0]["overage"])
        self.assertEqual(self.invited("t-auto"), [])


class ConfirmTeamTest(_BatchHarness):
    def setUp(self):
        super().setUp()
        self.team("t-confirm-new", policy="confirm", created_at="2026-10-03")
        self.team("t-confirm-old", policy="confirm", created_at="2026-10-02")
        self.team("t-forbid", policy="forbid", created_at="2026-10-01")

    def test_asks_once_with_a_plan_and_invites_nobody(self):
        _result, exc = self.submit(["a@example.com", "b@example.com"])

        self.assertEqual(exc.status_code, 409)
        detail = exc.detail
        self.assertEqual(detail["code"], "require_overage_confirmation")
        self.assertEqual(detail["operation"], "batch")
        self.assertEqual(detail["policy"], "confirm")
        self.assertEqual(detail["seat_type"], "default")
        self.assertEqual(
            detail["overage_plan"],
            [{"team_id": "t-confirm-old", "team_name": "t-confirm-old-name", "extra_seats": 2}],
        )
        self.assertEqual(detail["extra_seats_total"], 2)
        self.assertEqual(detail["team_id"], "t-confirm-old")
        self.assertEqual(detail["remaining_emails"], ["a@example.com", "b@example.com"])
        self.assertEqual(detail["added"], [])
        self.assertEqual(detail["failed"], [])
        for key in ("available", "free_team_count", "active_team_count"):
            self.assertIn(key, detail["capacity"])
        self.assertEqual(detail["capacity"]["available"], 0)
        self.assertIn("自动加购 2 个 ChatGPT 席位并扣费", detail["message"])
        self.assertIn("「t-confirm-old-name」2 个", detail["message"])
        self.assertEqual(self.all_mutations(), {})

    def test_after_confirmation_overfills_the_planned_team_only(self):
        result, exc = self.submit(["a@example.com", "b@example.com"], allow_overage=True)

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(self.invited("t-confirm-old"), ["a@example.com", "b@example.com"])
        self.assertEqual(self.invited("t-confirm-new"), [])
        self.assertEqual(self.invited("t-forbid"), [])
        self.assertEqual(result["no_place_emails"], [])

    def test_cache_said_free_but_live_is_full_asks_with_remaining(self):
        # 缓存说 t-stale 有 1 个空位（不问），现拉发现满了：问一次，计划加在 confirm Team 上。
        self.team("t-stale", policy="forbid", seats=2, used=1, live_used=2, created_at="2026-09-01")

        _result, exc = self.submit(["a@example.com"])

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["remaining_emails"], ["a@example.com"])
        self.assertEqual(exc.detail["overage_plan"][0]["team_id"], "t-confirm-old")
        self.assertIn("已添加 0 个，剩余 1 个", exc.detail["message"])
        self.assertEqual(self.all_mutations(), {})


class ForbidOnlyTest(_BatchHarness):
    def setUp(self):
        super().setUp()
        self.team("t-forbid-1", policy="forbid", created_at="2026-10-01")
        self.team("t-forbid-2", policy="forbid", created_at="2026-10-02")

    def test_leftovers_are_reported_without_asking(self):
        for allow_overage in (False, True):
            with self.subTest(allow_overage=allow_overage):
                result, exc = self.submit(["a@example.com", "b@example.com"], allow_overage=allow_overage)

                self.assertIsNone(exc, getattr(exc, "detail", None))
                self.assertEqual(result["added"], [])
                self.assertEqual(result["no_place_emails"], ["a@example.com", "b@example.com"])
                self.assertEqual(
                    result["failed"],
                    [{"email": "a@example.com", "error": NO_PLACE}, {"email": "b@example.com", "error": NO_PLACE}],
                )
                self.assertEqual(self.all_mutations(), {})


class OverfillRereadsPolicyUnderLockTest(_BatchHarness):
    def test_policy_changed_to_forbid_after_candidates_were_loaded_is_respected(self):
        self.team("t-flip", policy="forbid", created_at="2026-10-01")
        stale_candidate = {"id": "t-flip", "name": "t-flip-name", "overage_policy": "confirm"}
        self.track_reservation("t-flip", "a@example.com")

        with (
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=self.clients["t-flip"])),
            patch.object(gpt_invites, "fetch_and_cache_members", new=AsyncMock(return_value=ABSENT)),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
        ):
            added, error = asyncio.run(
                gpt_invites._invite_to_team(
                    stale_candidate, "a@example.com", None,
                    check_capacity=False, action="invite_gpt_member", allow_overage=True,
                )
            )

        self.assertIsNone(added)
        self.assertTrue(error.startswith("no_gpt_seat"))
        self.assertEqual(self.all_mutations(), {})
        log = self.logs("invite_gpt_member")[-1]
        self.assertEqual(log["result"], "skipped")
        self.assertIn("policy=forbid", log["detail"])
        self.assertIn("reason=overage_forbidden", log["detail"])


class CachedMinRuleTest(_BatchHarness):
    def test_cached_per_type_capacity_wins_when_smaller(self):
        # 旧公式 2 − 1 = 1 个空位；缓存的分类型容量说 default 已经 0 个：不算空位。
        self.team("t-min", policy="forbid", seats=2, used=1, created_at="2026-10-01",
                  seat_capacity={"default": {"paid": 2, "available": 0}})
        self.team("t-plain", policy="forbid", seats=2, used=1, created_at="2026-10-02")

        candidates = asyncio.run(gpt_invites.load_gpt_invite_candidates(include_full=True))
        by_id = {item["id"]: item for item in candidates}

        self.assertEqual(by_id["t-min"]["cached_available"], 0)
        self.assertEqual(by_id["t-plain"]["cached_available"], 1)
        self.assertEqual(asyncio.run(gpt_invites.cached_gpt_capacity_summary())["available"], 1)


if __name__ == "__main__":
    unittest.main()
