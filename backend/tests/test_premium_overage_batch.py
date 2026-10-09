"""批量自动分配（POST /api/gpt-members/invite，不指定 Team）按每个 Team 的超员策略超员。

* 填空位：和以前一样（缓存挑 Team，锁内现拉确认）；缓存空位也按「取小」规则算。
* 没空位的邮箱：「超员自动」的 Team 直接加、不问；只剩「超员需确认」时问一次，
  409 里带 overage_plan（加购几个、加在哪个 Team）；「禁止超员」永远不超员。
* 哪里都去不了：failed 里写「没位置，未邀请」，并列在 no_place_emails。
* 确认过的加购额度在发邀请之前先扣：结果不明也算用掉，下一个邮箱不能再买一个；
  上游明确拒绝才把额度还回去。
"""

import _isolation  # noqa: F401  must precede any app import
from _fixtures import direct_call
from _seat_fixtures import ABSENT, BatchHarness, FakeTeamClient, TempDbMixin

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.routes import gpt_members
from app.services import gpt_invites, seat_capacity


NO_PLACE = "没位置，未邀请"
TEAM = "rf-prem-team"
EMAILS = [f"redeemer{i}@example.com" for i in range(4)]


class _QueuedInviteClient(FakeTeamClient):
    """邀请结果按顺序取 ``results``，取完后照常成功。"""

    def __init__(self, results, **kwargs):
        super().__init__(**kwargs)
        self._results = list(results)

    def invite_member(self, email, seat_type="default"):
        if self._results:
            self.mutations.append(("invite_member", email, seat_type))
            return dict(self._results.pop(0))
        return super().invite_member(email, seat_type)


class AutoTeamTest(BatchHarness):
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


class ConfirmTeamTest(BatchHarness):
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
            [{"team_id": "t-confirm-old", "team_name": "t-confirm-old-name", "extra_seats": 2, "seat_price": None}],
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

    def _confirm_plan(self, emails):
        """先拿 409 的计划，返回确认重发要带的参数：计划里的 team_id 和 extra_seats_total。"""
        _result, exc = self.submit(emails)
        self.assertEqual(exc.status_code, 409)
        return {
            "overage_team_ids": [item["team_id"] for item in exc.detail["overage_plan"]],
            "overage_seat_limit": exc.detail["extra_seats_total"],
        }

    def test_after_confirmation_overfills_the_planned_team_only(self):
        emails = ["a@example.com", "b@example.com"]
        planned = self._confirm_plan(emails)

        result, exc = self.submit(emails, allow_overage=True, **planned)

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(planned, {"overage_team_ids": ["t-confirm-old"], "overage_seat_limit": 2})
        self.assertEqual(self.invited("t-confirm-old"), ["a@example.com", "b@example.com"])
        self.assertEqual(self.invited("t-confirm-new"), [])
        self.assertEqual(self.invited("t-forbid"), [])
        self.assertEqual(result["no_place_emails"], [])

    def test_confirmation_without_team_ids_charges_nobody(self):
        for ids in (None, []):
            with self.subTest(overage_team_ids=ids):
                _result, exc = self.submit(
                    ["a@example.com"], allow_overage=True, overage_team_ids=ids, overage_seat_limit=1
                )

                self.assertEqual(exc.status_code, 409)
                self.assertEqual(exc.detail["overage_plan"][0]["team_id"], "t-confirm-old")
                self.assertIn("你确认过的超员计划已经不成立", exc.detail["message"])
                self.assertEqual(self.all_mutations(), {})

    def test_planned_team_turned_forbid_asks_again_and_charges_no_other_team(self):
        emails = ["a@example.com", "b@example.com"]
        planned = self._confirm_plan(emails)
        self.set_policy("t-confirm-old", "forbid")

        _result, exc = self.submit(emails, allow_overage=True, **planned)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(
            exc.detail["overage_plan"],
            [{"team_id": "t-confirm-new", "team_name": "t-confirm-new-name", "extra_seats": 2, "seat_price": None}],
        )
        self.assertEqual(exc.detail["remaining_emails"], emails)
        self.assertEqual(self.all_mutations(), {}, "不能把加购挪到管理员没看到的 Team 上")

    def test_planned_team_turned_forbid_mid_request_asks_again_with_remaining(self):
        emails = ["a@example.com", "b@example.com"]
        planned = self._confirm_plan(emails)
        original = gpt_invites.load_team_policy
        calls = {"n": 0}

        async def _flip_after_first(team_id):
            calls["n"] += 1
            if calls["n"] == 2:  # 第一个邮箱超员成功之后，管理员把它改成了禁止超员
                self.set_policy("t-confirm-old", "forbid")
            return await original(team_id)

        with patch.object(gpt_invites, "load_team_policy", new=_flip_after_first):
            _result, exc = self.submit(emails, allow_overage=True, **planned)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual([item["email"] for item in exc.detail["added"]], ["a@example.com"])
        self.assertEqual(exc.detail["remaining_emails"], ["b@example.com"])
        self.assertEqual(exc.detail["overage_plan"][0]["team_id"], "t-confirm-new")
        self.assertEqual(exc.detail["overage_plan"][0]["extra_seats"], 1)
        self.assertEqual(self.invited("t-confirm-old"), ["a@example.com"])
        self.assertEqual(self.invited("t-confirm-new"), [])

    def test_confirmation_without_seat_limit_charges_nobody(self):
        planned = self._confirm_plan(["a@example.com"])

        _result, exc = self.submit(
            ["a@example.com"], allow_overage=True, overage_team_ids=planned["overage_team_ids"]
        )

        self.assertEqual(exc.status_code, 409, "旧前端只带 allow_overage：重新问，不加购")
        self.assertEqual(exc.detail["extra_seats_total"], 1)
        self.assertIn("没有带上要加购几个席位", exc.detail["message"])
        self.assertEqual(self.all_mutations(), {})

    def test_more_extra_seats_than_confirmed_upfront_asks_again_before_inviting(self):
        planned = self._confirm_plan(["a@example.com"])  # 计划：加购 1 个

        _result, exc = self.submit(["a@example.com", "b@example.com"], allow_overage=True, **planned)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["extra_seats_total"], 2)
        self.assertEqual(exc.detail["overage_plan"][0]["team_id"], "t-confirm-old")
        self.assertIn("比你确认的 1 个多", exc.detail["message"])
        self.assertEqual(self.all_mutations(), {})

    def test_upstream_rejection_on_planned_team_does_not_overfill_an_unlisted_team(self):
        planned = self._confirm_plan(["a@example.com"])
        self.clients["t-confirm-old"].invite_result = {
            "error": "invalid email", "_mutation_status": "rejected",
        }

        result, exc = self.submit(["a@example.com"], allow_overage=True, **planned)

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(self.invited("t-confirm-old"), ["a@example.com"])
        self.assertEqual(self.invited("t-confirm-new"), [], "被拒后不能换到没列出的 confirm Team")
        self.assertEqual(self.invited("t-forbid"), [])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["failed"], [{"email": "a@example.com", "error": "invalid email"}])

    def test_cache_said_free_but_live_is_full_asks_with_remaining(self):
        # 缓存说 t-stale 有 1 个空位（不问），现拉发现满了：问一次，计划加在 confirm Team 上。
        self.team("t-stale", policy="forbid", seats=2, used=1, live_used=2, created_at="2026-09-01")

        _result, exc = self.submit(["a@example.com"])

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["remaining_emails"], ["a@example.com"])
        self.assertEqual(exc.detail["overage_plan"][0]["team_id"], "t-confirm-old")
        self.assertIn("已添加 0 个，剩余 1 个", exc.detail["message"])
        self.assertEqual(self.all_mutations(), {})


class SeatCountBindingTest(BatchHarness):
    """确认绑定个数：问完之后空位被占了，确认后的请求最多加购确认过的个数，剩下的重新问。"""

    def setUp(self):
        super().setUp()
        # t-free：缓存和现拉都有 1 个空位；t-confirm：已满、超员需确认。
        self.team("t-free", policy="forbid", seats=2, used=1, created_at="2026-10-01")
        self.team("t-confirm", policy="confirm", created_at="2026-10-02")
        self.emails = ["a@example.com", "b@example.com", "c@example.com"]

    def _confirm(self):
        _result, exc = self.submit(self.emails)
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(self.all_mutations(), {})
        return {
            "overage_team_ids": [item["team_id"] for item in exc.detail["overage_plan"]],
            "overage_seat_limit": exc.detail["extra_seats_total"],
        }

    def _take_free_seat_live_only(self):
        """别人占了 t-free 的空位，但缓存还没更新：只有现拉看得到。"""
        self.clients["t-free"].counts["default"] = 2

    def test_vanished_free_seat_overfills_at_most_the_confirmed_count_then_asks(self):
        planned = self._confirm()
        self.assertEqual(planned, {"overage_team_ids": ["t-confirm"], "overage_seat_limit": 2})
        self._take_free_seat_live_only()

        _result, exc = self.submit(self.emails, allow_overage=True, **planned)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(self.invited("t-confirm"), ["a@example.com", "b@example.com"],
                         "确认了 2 个就最多加购 2 个")
        self.assertEqual(self.invited("t-free"), [])
        detail = exc.detail
        self.assertEqual([item["email"] for item in detail["added"]], ["a@example.com", "b@example.com"])
        self.assertEqual(detail["remaining_emails"], ["c@example.com"])
        self.assertEqual(
            detail["overage_plan"], [{"team_id": "t-confirm", "team_name": "t-confirm-name", "extra_seats": 1, "seat_price": None}]
        )
        self.assertEqual(detail["extra_seats_total"], 1)
        self.assertIn("比你确认的 2 个多", detail["message"])

        # 管理员再确认这 1 个：剩下的邮箱加在同一个 Team 上，不再问。
        result, exc = self.submit(
            detail["remaining_emails"], allow_overage=True,
            overage_team_ids=["t-confirm"], overage_seat_limit=detail["extra_seats_total"],
        )
        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(self.invited("t-confirm"), self.emails)

    def test_matching_count_needs_no_second_ask(self):
        planned = self._confirm()

        result, exc = self.submit(self.emails, allow_overage=True, **planned)

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(self.invited("t-free"), ["a@example.com"])
        self.assertEqual(self.invited("t-confirm"), ["b@example.com", "c@example.com"])
        self.assertEqual(result["failed"], [])
        self.assertEqual([item["policy"] for item in result["added"]], ["forbid", "confirm", "confirm"])

    def test_auto_overfills_do_not_count_against_the_limit(self):
        self.team("t-auto", policy="auto", created_at="2026-10-03")
        self._take_free_seat_live_only()

        result, exc = self.submit(
            self.emails, allow_overage=True, overage_team_ids=["t-confirm"], overage_seat_limit=0
        )

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(self.invited("t-auto"), self.emails)
        self.assertEqual(self.invited("t-confirm"), [])


class ForbidOnlyTest(BatchHarness):
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


class OverfillRereadsPolicyUnderLockTest(BatchHarness):
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


class CachedMinRuleTest(BatchHarness):
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


class BatchAllowanceReservedBeforeInviteTest(TempDbMixin, unittest.TestCase):
    """One confirm Team; each submit passes its own client, so a test can swap in a queued result."""

    def setUp(self):
        self._start_db()
        for email in EMAILS:
            self.track_reservation(TEAM, email)
        # 缓存：1 个已付、0 个在用（1 个空位）；现拉：已经满了。
        self.insert_team(TEAM, policy="confirm", seats_entitled=1, created_at="2026-10-01T00:00:00+00:00")

    def invited(self, client):
        return [m[1] for m in client.mutations if m[0] == "invite_member"]

    def submit(self, client, emails, **kwargs):
        patches = [
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=client)),
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
                    gpt_members.InviteGptMembersRequest(emails=emails, expires_in="30d", **kwargs)
                )
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

    def confirmed_plan(self, emails):
        _result, exc = self.submit(FakeTeamClient(seats_entitled=1, counts={"default": 1, "usage_based": 0}), emails)
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["extra_seats_total"], 1, "按缓存空位，管理员确认加购 1 个")
        return {
            "allow_overage": True,
            "overage_team_ids": [item["team_id"] for item in exc.detail["overage_plan"]],
            "overage_seat_limit": exc.detail["extra_seats_total"],
        }

    def test_uncertain_overfill_uses_up_the_confirmed_seat(self):
        emails = EMAILS[:2]
        planned = self.confirmed_plan(emails)
        uncertain = {"error": "timed out", "_mutation_status": "uncertain"}
        client = _QueuedInviteClient([uncertain], seats_entitled=1, counts={"default": 1, "usage_based": 0})

        _result, exc = self.submit(client, emails, **planned)

        self.assertEqual(self.invited(client), emails[:1], "第二个邮箱不能再买一个没确认的席位")
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["remaining_emails"], emails[1:])
        self.assertEqual([item["email"] for item in exc.detail["failed"]], emails[:1])

    def test_explicit_rejection_returns_the_confirmed_seat(self):
        emails = EMAILS[:2]
        planned = self.confirmed_plan(emails)
        rejected = {"error": "OpenAI 拒绝邀请", "_mutation_status": "rejected"}
        client = _QueuedInviteClient([rejected], seats_entitled=1, counts={"default": 1, "usage_based": 0})

        result, exc = self.submit(client, emails, **planned)

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(self.invited(client), emails)
        self.assertEqual([item["email"] for item in result["added"]], emails[1:])
        self.assertTrue(result["added"][0]["overage"])
        self.assertEqual([item["email"] for item in result["failed"]], emails[:1])

    def test_allowance_is_taken_before_the_upstream_call(self):
        allowance = gpt_invites.ConfirmedOverfillAllowance(1)
        seen = []
        client = FakeTeamClient(seats_entitled=1, counts={"default": 1, "usage_based": 0})
        original = client.invite_member

        def _invite(email, seat_type="default"):
            seen.append(allowance.remaining)
            return original(email, seat_type)

        client.invite_member = _invite
        team = {"id": TEAM, "name": f"{TEAM}-name", "overage_policy": "confirm"}
        with (
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(gpt_invites, "fetch_and_cache_members", new=AsyncMock(return_value=ABSENT)),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "notify_member_event", new=AsyncMock()),
        ):
            added, error = asyncio.run(gpt_invites._invite_to_team(
                team, EMAILS[0], None, check_capacity=False, action="invite_gpt_member",
                allow_overage=True, allowance=allowance,
            ))
            self.assertIsNone(error)
            self.assertTrue(added["overage"])
            again, error = asyncio.run(gpt_invites._invite_to_team(
                team, EMAILS[1], None, check_capacity=False, action="invite_gpt_member",
                allow_overage=True, allowance=allowance,
            ))

        self.assertEqual(seen, [0], "发邀请时额度已经扣掉")
        self.assertIsNone(again)
        self.assertTrue(error.startswith("no_gpt_seat"))
        self.assertEqual(self.invited(client), EMAILS[:1])


if __name__ == "__main__":
    unittest.main()
