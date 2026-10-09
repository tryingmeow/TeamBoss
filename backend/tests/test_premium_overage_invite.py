"""单个 Team 邀请（POST /api/teams/{team_id}/members/invite）按每个 Team 的超员策略把关。

跑真实的 invite_member 路由和真实的策略检查（services/overage_policy.py），只把上游换成
记录调用的替身。被拒时必须：409、没有任何上游写、记一条 skipped 日志。

超员确认（``overage_confirmation``）带着管理员看到的加购个数 ``seat_limit``：服务端按
``confirmation_id`` 在 overage_confirmations 表里记账，最多加购这么多个；有空位的邀请不动确认；
只有上游明确拒绝才把扣掉的那 1 个还回去，结果不明按已经加购算并占住一个计费席位。
旧前端只带 ``allow_overage=True``，在「超员需确认」的 Team 上不算确认。
"""

import _isolation  # noqa: F401  must precede any app import
from _fixtures import direct_call
from _seat_fixtures import (
    ABSENT,
    INVITE_EMAIL as EMAIL,
    INVITE_TEAM as TEAM,
    FakeTeamClient,
    InviteHarness,
    TempDbMixin,
    _full_default_client,
    capacity_entries,
)

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.models import InviteMemberRequest
from app.routes import members
from app.services import seat_capacity
from app.services.team_locks import reserve_default_seat, reserved_seats


OTHER_TEAM = "rf-other-team"
EMAILS = [f"redeemer{i}@example.com" for i in range(4)]


def _free_default_client(**kwargs):
    return FakeTeamClient(seats_entitled=3, counts={"default": 2, "usage_based": 0}, **kwargs)


def _confirmation(seat_limit, *, seat_type="default", cid="admin-confirm-0001"):
    return {"confirmation_id": cid, "seat_type": seat_type, "seat_limit": seat_limit}


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


class ConfirmPolicyTest(InviteHarness):
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

    def test_confirmation_proceeds_after_the_live_read(self):
        confirmation = {"confirmation_id": "confirm-invite-0001", "seat_type": "default", "seat_limit": 1}
        response, exc = self.invite(_full_default_client(), confirmation=confirmation)

        log = self.assert_invited(response, exc)
        self.assertEqual(self.client.capacity_reads, 1, "带了确认也先现拉：有空位就不动确认")
        self.assertIn("policy=confirm", log["detail"])
        self.assertIn("overage=True", log["detail"])
        self.assertIn("overage_confirmed=1/1", log["detail"])
        self.assertEqual((response["overage"], response["policy"]), (True, "confirm"))

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


class UnknownPendingSeatTypeTest(InviteHarness):
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


class ReservationAfterInviteTest(InviteHarness):
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


class AutoPolicyTest(InviteHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="auto")

    def test_full_team_proceeds_like_old_skip_overage(self):
        response, exc = self.invite(_full_default_client())

        log = self.assert_invited(response, exc)
        self.assertEqual(self.client.capacity_reads, 0)
        self.assertIn("policy=auto", log["detail"])
        self.assertIn("overage=True", log["detail"])


class ForbidPolicyTest(InviteHarness):
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


class CodexInviteTest(InviteHarness):
    def test_codex_invite_never_checks_capacity(self):
        self.insert_team(TEAM, policy="forbid")

        response, exc = self.invite(_full_default_client(), policy="forbid", seat_type="usage_based")

        log = self.assert_invited(response, exc, seat_type="usage_based")
        self.assertEqual(self.client.reads, [], "Codex 邀请和以前一样不读任何容量")
        self.assertNotIn("policy=", log["detail"])
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "usage_based")), 0)


class PremiumInviteTest(InviteHarness):
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


class _LedgerHarness(TempDbMixin, unittest.TestCase):
    """Invites any of EMAILS into TEAM / OTHER_TEAM through the real route, one call per email,
    and reads the overage_confirmations ledger. Every live member list is ``snapshot``."""

    def setUp(self):
        self._start_db()
        for team_id in (TEAM, OTHER_TEAM):
            for email in EMAILS:
                self.track_reservation(team_id, email)

    def ledger(self, confirmation_id="admin-confirm-0001"):
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM overage_confirmations WHERE confirmation_id = ?", (confirmation_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def invite(self, client, email, *, team_id=TEAM, seat_type="default", confirmation=None,
               allow_overage=False, snapshot=ABSENT):
        patches = [
            patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(members, "fetch_and_cache_members", new=AsyncMock(return_value=snapshot)),
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
                    team_id,
                    InviteMemberRequest(
                        email=email, expires_in="30d", seat_type=seat_type,
                        overage_confirmation=confirmation, allow_overage=allow_overage,
                    ),
                )
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

    def invited(self, client):
        return [m[1] for m in client.mutations if m[0] == "invite_member"]


class ConfirmationSeatLimitTest(_LedgerHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="confirm")

    def test_one_confirmation_buys_at_most_its_seat_limit(self):
        """确认的是 2 个；对话框（或旧前端）给 4 个邮箱都带上它，也只加购 2 个。"""
        client = _full_default_client()
        outcomes = [self.invite(client, email, confirmation=_confirmation(2)) for email in EMAILS]

        self.assertEqual(self.invited(client), EMAILS[:2])
        for response, exc in outcomes[:2]:
            self.assertIsNone(exc, getattr(exc, "detail", None))
            self.assertEqual((response["overage"], response["policy"]), (True, "confirm"))
        for _response, exc in outcomes[2:]:
            self.assertEqual(exc.status_code, 409)
            self.assertEqual(exc.detail["code"], "require_overage_confirmation")
            self.assertEqual(exc.detail["confirmation_status"], "used_up")
            self.assertTrue(exc.detail["message"].startswith("你确认过的加购个数已经用完，需要重新确认。"))
        self.assertEqual(self.ledger()["used"], 2)
        details = [row["detail"] for row in self.logs("invite_member") if row["result"] == "success"]
        self.assertIn("overage_confirmed=1/2", details[0])
        self.assertIn("overage_confirmed=2/2", details[1])

    def test_free_live_seat_does_not_touch_the_confirmation(self):
        client = FakeTeamClient(seats_entitled=3, counts={"default": 2, "usage_based": 0})

        first, exc = self.invite(client, EMAILS[0], confirmation=_confirmation(1))
        self.assertIsNone(exc)
        self.assertFalse(first["overage"])
        self.assertIsNone(self.ledger(), "有空位时不登记、不扣确认")

        # 第一个人占住了最后一个空位（预留），第二个才真要加购，用掉确认的 1 个。
        second, exc = self.invite(client, EMAILS[1], confirmation=_confirmation(1))
        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertTrue(second["overage"])
        self.assertEqual(self.ledger()["used"], 1)

    def test_seat_limit_is_fixed_when_first_used(self):
        client = _full_default_client()
        self.invite(client, EMAILS[0], confirmation=_confirmation(1))

        _response, exc = self.invite(client, EMAILS[1], confirmation=_confirmation(5))

        self.assertEqual(exc.detail["confirmation_status"], "used_up")
        self.assertEqual(self.invited(client), EMAILS[:1])
        self.assertEqual(self.ledger()["seat_limit"], 1)

    def test_confirmation_is_bound_to_team_and_seat_type(self):
        self.insert_team(OTHER_TEAM, policy="confirm")
        client = _full_default_client()
        self.invite(client, EMAILS[0], confirmation=_confirmation(3))

        _response, exc = self.invite(client, EMAILS[1], team_id=OTHER_TEAM, confirmation=_confirmation(3))
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["confirmation_status"], "mismatch")

        _response, exc = self.invite(
            client, EMAILS[2], confirmation=_confirmation(3, seat_type="prolite", cid="admin-confirm-0002")
        )
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["confirmation_status"], "mismatch")
        self.assertEqual(self.invited(client), EMAILS[:1])

    def test_expired_confirmation_is_not_a_confirmation(self):
        client = _full_default_client()
        self.invite(client, EMAILS[0], confirmation=_confirmation(3))
        conn = self._conn()
        conn.execute(
            "UPDATE overage_confirmations SET expires_at = '2000-01-01T00:00:00.000000+00:00'"
        )
        conn.commit()
        conn.close()

        _response, exc = self.invite(client, EMAILS[1], confirmation=_confirmation(3))

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["confirmation_status"], "expired")
        self.assertEqual(self.invited(client), EMAILS[:1])

    def test_explicit_rejection_gives_the_unit_back(self):
        rejected = {"error": "OpenAI 拒绝邀请", "_mutation_status": "rejected"}
        client = _QueuedInviteClient([rejected], seats_entitled=2, counts={"default": 2, "usage_based": 0})

        _response, exc = self.invite(client, EMAILS[0], confirmation=_confirmation(1))
        self.assertEqual(exc.status_code, 502)
        self.assertEqual(self.ledger()["used"], 0)

        response, exc = self.invite(client, EMAILS[1], confirmation=_confirmation(1))
        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertTrue(response["overage"])
        self.assertEqual(self.ledger()["used"], 1)

    def test_uncertain_invite_keeps_the_unit_used_and_holds_a_seat(self):
        uncertain = {"error": "timed out", "_mutation_status": "uncertain"}
        client = _QueuedInviteClient([uncertain], seats_entitled=2, counts={"default": 2, "usage_based": 0})

        _response, exc = self.invite(client, EMAILS[0], confirmation=_confirmation(1))
        self.assertEqual(exc.status_code, 409)
        self.assertIn("邀请结果确认中", exc.detail)
        self.assertEqual(self.ledger()["used"], 1, "结果不明按已经加购算")
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "default")), 1)
        uncertain_log = [row for row in self.logs("invite_member") if row["result"] == "uncertain"][-1]
        self.assertIn("seat_type=default", uncertain_log["detail"])

        _response, exc = self.invite(client, EMAILS[1], confirmation=_confirmation(1))
        self.assertEqual(exc.detail["confirmation_status"], "used_up")
        self.assertEqual(self.invited(client), EMAILS[:1])

    def test_uncertain_premium_invite_holds_a_premium_seat(self):
        self.set_policy(TEAM, "auto")
        uncertain = {"error": "timed out", "_mutation_status": "uncertain"}
        client = _QueuedInviteClient([uncertain], seats_entitled=2, counts={"default": 2, "usage_based": 0})

        _response, exc = self.invite(client, EMAILS[0], seat_type="prolite")

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "prolite")), 1)


class LegacyAllowOverageTest(_LedgerHarness):
    def test_bare_allow_overage_on_confirm_team_asks_for_confirmation(self):
        self.insert_team(TEAM, policy="confirm")
        client = _full_default_client()

        _response, exc = self.invite(client, EMAILS[0], allow_overage=True)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "require_overage_confirmation")
        self.assertEqual(exc.detail["confirmation_status"], "missing")
        self.assertEqual(client.mutations, [])

    def test_auto_team_is_unaffected(self):
        self.insert_team(TEAM, policy="auto")
        client = _full_default_client()

        response, exc = self.invite(client, EMAILS[0], allow_overage=True)

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual((response["overage"], response["policy"]), (True, "auto"))
        self.assertEqual(self.invited(client), EMAILS[:1])


if __name__ == "__main__":
    unittest.main()
