"""单个 Team 邀请（POST /api/teams/{team_id}/members/invite）按每个 Team 的超员策略把关。

跑真实的 invite_member 路由和真实的策略检查（services/overage_policy.py），只把上游换成
记录调用的替身。被拒时必须：409、没有任何上游写、记一条 skipped 日志。
"""

from test_premium_overage_support import (  # noqa: I001  (_isolation first)
    FakeTeamClient,
    TempDbMixin,
    capacity_entries,
    direct_call,
)

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.models import InviteMemberRequest
from app.routes import members
from app.services import seat_capacity
from app.services.team_locks import reserve_default_seat, reserved_seats


TEAM = "prem-inv-team"
EMAIL = "new.member@example.com"
ABSENT = {"members": [], "pending_invites": []}


def _full_default_client(**kwargs):
    """ChatGPT 席位已满：2 个已付、2 个在用。"""
    return FakeTeamClient(seats_entitled=2, counts={"default": 2, "usage_based": 0}, **kwargs)


def _free_default_client(**kwargs):
    return FakeTeamClient(seats_entitled=3, counts={"default": 2, "usage_based": 0}, **kwargs)


class _InviteHarness(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._start_db()
        self.track_reservation(TEAM, EMAIL)

    def invite(self, client, *, policy=None, seat_type="default", allow_overage=False, snapshot=ABSENT,
               refreshed=None):
        """``snapshot``：邀请前现拉的名单；``refreshed``：邀请成功后刷新拿到的名单（默认同前）。"""
        if policy is not None:
            self.set_policy(TEAM, policy)
        self.client = client
        patches = [
            patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(
                members,
                "fetch_and_cache_members",
                new=AsyncMock(side_effect=[snapshot, snapshot if refreshed is None else refreshed]),
            ),
            patch.object(members, "run_chatgpt_call", new=direct_call),
            patch.object(seat_capacity, "run_chatgpt_call", new=direct_call),
            patch.object(members, "add_member_watch", new=AsyncMock()),
            patch.object(members, "notify_member_event", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(
                members.invite_member(
                    TEAM,
                    InviteMemberRequest(
                        email=EMAIL, expires_in="30d", seat_type=seat_type, allow_overage=allow_overage
                    ),
                )
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

    def assert_refused(self, exc, code, *, seat_type="default", unknown=False):
        self.assertIsNotNone(exc, "应当被超员策略挡下")
        self.assertEqual(exc.status_code, 409)
        detail = exc.detail
        self.assertEqual(detail["code"], code)
        self.assertEqual(detail["team_id"], TEAM)
        self.assertEqual(detail["team_name"], f"{TEAM}-name")
        self.assertEqual(detail["seat_type"], seat_type)
        self.assertEqual(detail["operation"], "invite")
        self.assertEqual(detail["capacity"]["seat_type"], seat_type)
        self.assertEqual(detail["capacity"]["available"], 0)
        self.assertIs(detail["capacity"]["capacity_unknown"], unknown)
        self.assertEqual(self.client.mutations, [], "被拒时绝不能发上游写请求")
        log = self.logs("invite_member")[-1]
        self.assertEqual(log["result"], "skipped")
        reason = "overage_forbidden" if code == "overage_forbidden" else "overage_needs_confirmation"
        self.assertIn(f"seat_type={seat_type}", log["detail"])
        self.assertIn(f"reason={reason}", log["detail"])
        self.assertEqual(log["error_message"], detail["message"])
        return detail

    def assert_invited(self, response, exc, seat_type="default"):
        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(response["status"], "ok")
        self.assertEqual(self.client.mutations, [("invite_member", EMAIL, seat_type)])
        return self.logs("invite_member")[-1]


class ConfirmPolicyTest(_InviteHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="confirm")

    def test_full_team_without_flag_asks_for_confirmation(self):
        _response, exc = self.invite(_full_default_client())

        detail = self.assert_refused(exc, "require_overage_confirmation")
        self.assertEqual(detail["policy"], "confirm")
        self.assertIn("自动加购 1 个 ChatGPT 席位并扣费", detail["message"])
        # 旧前端读的容量字段还在。
        self.assertEqual(detail["capacity"]["seats_entitled"], 2)
        self.assertEqual(detail["capacity"]["active_chatgpt"], 2)

    def test_flag_proceeds_without_reading_capacity(self):
        response, exc = self.invite(_full_default_client(), allow_overage=True)

        log = self.assert_invited(response, exc)
        self.assertEqual(self.client.capacity_reads, 0, "确认过就和旧的 allow_overage=True 一样，不再读容量")
        self.assertIn("policy=confirm", log["detail"])
        self.assertIn("overage=True", log["detail"])

    def test_free_seat_proceeds_and_reserves_it(self):
        response, exc = self.invite(_free_default_client())

        log = self.assert_invited(response, exc)
        self.assertEqual(self.client.capacity_reads, 1)
        self.assertIn("overage=False", log["detail"])
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "default")), 1)

    def test_live_read_failure_asks_with_capacity_unknown(self):
        _response, exc = self.invite(_free_default_client(fail_reads=True))

        detail = self.assert_refused(exc, "require_overage_confirmation", unknown=True)
        self.assertTrue(detail["message"].startswith("暂时读不到空位，按已满处理："))

    def test_min_rule_refuses_when_per_type_value_is_smaller(self):
        # 旧公式 3 − 2 = 1 个空位；分类型容量说 default 只剩 0 个：按小的算，满了。
        client = _free_default_client(seat_capacity=capacity_entries(default=(3, 0)))

        _response, exc = self.invite(client)

        detail = self.assert_refused(exc, "require_overage_confirmation")
        self.assertEqual(detail["capacity"]["legacy_available"], 1)
        self.assertEqual(detail["capacity"]["per_type_available"], 0)

    def test_other_email_reservation_counts_against_free_seats(self):
        other = "someone.else@example.com"
        self.track_reservation(TEAM, other)
        asyncio.run(reserve_default_seat(TEAM, other))

        _response, exc = self.invite(_free_default_client())

        detail = self.assert_refused(exc, "require_overage_confirmation")
        self.assertEqual(detail["capacity"]["reserved"], 1)

    def test_same_seat_resend_needs_no_check(self):
        snapshot = {"members": [], "pending_invites": [{"email": EMAIL, "seat_type": "default"}]}
        self.set_policy(TEAM, "forbid")

        response, exc = self.invite(_full_default_client(), policy="forbid", snapshot=snapshot)

        self.assert_invited(response, exc)
        self.assertTrue(response["resent"])
        self.assertEqual(self.client.capacity_reads, 0)


class UnknownPendingSeatTypeTest(_InviteHarness):
    def test_resend_of_unknown_type_pending_invite_is_refused_before_anything(self):
        self.insert_team(TEAM, policy="auto")
        snapshot = {"members": [], "pending_invites": [{"email": EMAIL, "seat_type": "automation"}]}

        _response, exc = self.invite(_free_default_client(), snapshot=snapshot)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "seat_type_unknown")
        self.assertEqual(exc.detail["seat_type"], "automation")
        self.assertIn("其他（automation）", exc.detail["message"])
        self.assertEqual(self.client.reads, [], "不做策略检查、不读容量")
        self.assertEqual(self.client.mutations, [])
        log = self.logs("invite_member")[-1]
        self.assertEqual(log["result"], "skipped")
        self.assertIn("pending_seat_type=automation", log["detail"])


class ReservationAfterInviteTest(_InviteHarness):
    """Premium 邀请成功后总是占一个 Premium 预留；ChatGPT 只在刷新名单还没反映时才占。"""

    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="auto")
        self.shown = {"members": [], "pending_invites": [{"email": EMAIL}]}  # 上游没带 seat_type

    def test_premium_is_reserved_even_when_the_refresh_already_shows_the_invite(self):
        response, exc = self.invite(_full_default_client(), seat_type="prolite", refreshed=self.shown)

        self.assert_invited(response, exc, seat_type="prolite")
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "prolite")), 1)

    def test_chatgpt_is_not_reserved_when_the_refresh_shows_the_invite(self):
        response, exc = self.invite(_full_default_client(), refreshed=self.shown)

        self.assert_invited(response, exc)
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "default")), 0)


class AutoPolicyTest(_InviteHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="auto")

    def test_full_team_proceeds_like_old_skip_overage(self):
        response, exc = self.invite(_full_default_client())

        log = self.assert_invited(response, exc)
        self.assertEqual(self.client.capacity_reads, 0)
        self.assertIn("policy=auto", log["detail"])
        self.assertIn("overage=True", log["detail"])


class ForbidPolicyTest(_InviteHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="forbid")

    def test_full_team_is_refused(self):
        _response, exc = self.invite(_full_default_client(), policy="forbid")

        detail = self.assert_refused(exc, "overage_forbidden")
        self.assertEqual(detail["policy"], "forbid")
        self.assertEqual(
            detail["message"],
            f"「{TEAM}-name」设为禁止超员：ChatGPT 席位已满，不会自动加购。要加人请先在 Team 设置里修改超员策略。",
        )

    def test_confirm_flag_does_not_bypass_forbid(self):
        _response, exc = self.invite(_full_default_client(), policy="forbid", allow_overage=True)

        self.assert_refused(exc, "overage_forbidden")
        self.assertEqual(self.client.capacity_reads, 1, "forbid 时确认标记无效，照样现拉容量")

    def test_free_seat_still_proceeds(self):
        response, exc = self.invite(_free_default_client(), policy="forbid")

        self.assert_invited(response, exc)

    def test_live_read_failure_refuses(self):
        _response, exc = self.invite(_free_default_client(fail_reads=True), policy="forbid")

        detail = self.assert_refused(exc, "overage_forbidden", unknown=True)
        self.assertTrue(detail["message"].startswith("暂时读不到空位，按已满处理："))

    def test_garbage_policy_value_is_treated_as_forbid(self):
        _response, exc = self.invite(_full_default_client(), policy="whatever")

        self.assert_refused(exc, "overage_forbidden")


class CodexInviteTest(_InviteHarness):
    def test_codex_invite_never_checks_capacity(self):
        self.insert_team(TEAM, policy="forbid")

        response, exc = self.invite(_full_default_client(), policy="forbid", seat_type="usage_based")

        log = self.assert_invited(response, exc, seat_type="usage_based")
        self.assertEqual(self.client.reads, [], "Codex 邀请和以前一样不读任何容量")
        self.assertNotIn("policy=", log["detail"])
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "usage_based")), 0)


class PremiumInviteTest(_InviteHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="confirm")

    def test_free_premium_seat_proceeds_and_reserves_premium(self):
        client = _full_default_client(seat_capacity=capacity_entries(default=(2, 0), prolite=(1, 1)))

        response, exc = self.invite(client, seat_type="prolite")

        log = self.assert_invited(response, exc, seat_type="prolite")
        self.assertIn("policy=confirm", log["detail"])
        self.assertIn("overage=False", log["detail"])
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "prolite")), 1)
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "default")), 0)

    def test_missing_premium_entry_is_treated_as_full(self):
        client = _free_default_client(seat_capacity=capacity_entries(default=(3, 1)))

        _response, exc = self.invite(client, seat_type="prolite")

        detail = self.assert_refused(exc, "require_overage_confirmation", seat_type="prolite")
        self.assertIn("自动加购 1 个 Premium 席位并扣费", detail["message"])
        self.assertIsNone(detail["capacity"]["paid"])

    def test_pending_premium_invites_use_up_the_free_seat(self):
        client = _full_default_client(
            seat_capacity=capacity_entries(prolite=(1, 1)),
            pending=[{"email": "waiting@example.com", "seat_type": "prolite"}],
        )

        _response, exc = self.invite(client, seat_type="prolite")

        detail = self.assert_refused(exc, "require_overage_confirmation", seat_type="prolite")
        self.assertEqual(detail["capacity"]["pending"], 1)

    def test_forbid_premium_full_is_refused(self):
        client = _full_default_client(seat_capacity=capacity_entries(prolite=(1, 0)))

        _response, exc = self.invite(client, policy="forbid", seat_type="prolite")

        detail = self.assert_refused(exc, "overage_forbidden", seat_type="prolite")
        self.assertIn("Premium 席位已满", detail["message"])


if __name__ == "__main__":
    unittest.main()
