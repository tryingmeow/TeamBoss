"""Premium 超员策略复审的加购修复（C1、C2、C3、S3 和结果不明时的席位占用）。

* C1：单个 Team 的确认带着管理员看到的加购个数（``overage_confirmation.seat_limit``），服务端
  按 ``confirmation_id`` 在 overage_confirmations 表里记账，最多加购这么多个；有空位的邀请
  不动确认；只有上游明确拒绝才把扣掉的那 1 个还回去。
* C2：批量超员在发邀请之前先扣确认额度，结果不明也算用掉，下一个邮箱不能再买一个。
* C3：旧前端的 ``allow_overage=True`` 在「超员需确认」的 Team 上不算确认。
* S3：Telegram 在「禁止超员」的 Team 上不按缓存拒绝，由服务端现查决定。
* 后台邀请 / 切换席位结果不明时照样占住一个计费席位。

上游一律是记录调用的替身，不碰 ChatGPT。
"""

import _isolation  # noqa: F401  must precede any app import
from _fixtures import direct_call
from _seat_fixtures import FakeTeamClient, TempDbMixin

import asyncio
import re
import unittest
from unittest.mock import AsyncMock, Mock, patch

import requests
from fastapi import HTTPException

from app import tg_bot
from app.models import ChangeSeatRequest, InviteMemberRequest
from app.routes import gpt_members, members
from app.services import gpt_invites, seat_capacity
from app.services.team_locks import reserved_seats


TEAM = "rf-prem-team"
OTHER_TEAM = "rf-other-team"
ABSENT = {"members": [], "pending_invites": []}
EMAILS = [f"redeemer{i}@example.com" for i in range(4)]


def _confirmation(seat_limit, *, seat_type="default", cid="admin-confirm-0001"):
    return {"confirmation_id": cid, "seat_type": seat_type, "seat_limit": seat_limit}


def _full_client(**kwargs):
    """ChatGPT 席位已满：2 个已付、2 个在用。"""
    return FakeTeamClient(seats_entitled=2, counts={"default": 2, "usage_based": 0}, **kwargs)


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


class _SwitchResultClient(FakeTeamClient):
    def __init__(self, switch_result, **kwargs):
        super().__init__(**kwargs)
        self.switch_result = switch_result

    def change_seat_type(self, user_id, seat_type):
        self.mutations.append(("change_seat_type", user_id, seat_type))
        return dict(self.switch_result)


class _Harness(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._start_db()
        for team_id in (TEAM, OTHER_TEAM):
            for email in EMAILS + ["seat.member@example.com", "tg.member@example.com"]:
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


# ---- C1：确认带个数，服务端按账本卡住 ----

class ConfirmationSeatLimitTest(_Harness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="confirm")

    def test_one_confirmation_buys_at_most_its_seat_limit(self):
        """确认的是 2 个；对话框（或旧前端）给 4 个邮箱都带上它，也只加购 2 个。"""
        client = _full_client()
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

    def test_cached_free_seats_vanished_server_still_stops_at_confirmed_count(self):
        """缓存说还有 1 个空位：3 个邮箱只确认加购 2 个。现拉其实已满，第 3 个必须停下再问。"""
        client = _full_client()
        outcomes = [self.invite(client, email, confirmation=_confirmation(2)) for email in EMAILS[:3]]

        self.assertEqual(self.invited(client), EMAILS[:2])
        self.assertEqual(outcomes[2][1].status_code, 409)
        self.assertEqual(outcomes[2][1].detail["code"], "require_overage_confirmation")

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
        client = _full_client()
        self.invite(client, EMAILS[0], confirmation=_confirmation(1))

        _response, exc = self.invite(client, EMAILS[1], confirmation=_confirmation(5))

        self.assertEqual(exc.detail["confirmation_status"], "used_up")
        self.assertEqual(self.invited(client), EMAILS[:1])
        self.assertEqual(self.ledger()["seat_limit"], 1)

    def test_confirmation_is_bound_to_team_and_seat_type(self):
        self.insert_team(OTHER_TEAM, policy="confirm")
        client = _full_client()
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
        client = _full_client()
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


# ---- C1 / C3：切换席位 ----

class SeatSwitchConfirmationTest(_Harness):
    USER = "user-77"
    EMAIL = "seat.member@example.com"

    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="confirm")

    def switch(self, client, *, confirmation=None, allow_overage=False, target="default"):
        member = {"id": self.USER, "email": self.EMAIL, "seat_type": "usage_based", "status": "active"}
        snapshot = {"members": [member], "pending_invites": []}
        patches = [
            patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(members, "fetch_and_cache_members", new=AsyncMock(return_value=snapshot)),
            patch.object(members, "run_chatgpt_call", new=direct_call),
            patch.object(seat_capacity, "run_chatgpt_call", new=direct_call),
            patch.object(members, "add_member_watch", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(
                members.change_seat(
                    TEAM,
                    self.USER,
                    ChangeSeatRequest(
                        seat_type=target, overage_confirmation=confirmation, allow_overage=allow_overage
                    ),
                )
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

    def test_bare_allow_overage_is_not_a_confirmation(self):
        client = FakeTeamClient(seats_entitled=2, counts={"default": 2, "usage_based": 1})

        _result, exc = self.switch(client, allow_overage=True)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "require_overage_confirmation")
        self.assertEqual(exc.detail["confirmation_status"], "missing")
        self.assertEqual(client.mutations, [])

    def test_seat_limit_one_buys_one(self):
        client = FakeTeamClient(seats_entitled=2, counts={"default": 2, "usage_based": 1})

        result, exc = self.switch(client, confirmation=_confirmation(1))
        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual((result["overage"], result["policy"]), (True, "confirm"))

        _result, exc = self.switch(client, confirmation=_confirmation(1))
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["confirmation_status"], "used_up")
        self.assertEqual(len(client.mutations), 1)

    def test_explicit_rejection_gives_the_unit_back(self):
        client = _SwitchResultClient(
            {"error": "400 Bad Request", "status_code": 400},
            seats_entitled=2, counts={"default": 2, "usage_based": 1},
        )

        _result, exc = self.switch(client, confirmation=_confirmation(1))

        self.assertEqual(exc.status_code, 502)
        self.assertEqual(self.ledger()["used"], 0)
        self.assertEqual(self.logs("change_seat")[-1]["result"], "failed")
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "default")), 0)

    def test_uncertain_switch_keeps_the_unit_and_holds_the_target_seat(self):
        for status_code in (None, 502, 429):
            with self.subTest(status_code=status_code):
                result = {"error": "upstream timed out"}
                if status_code is not None:
                    result["status_code"] = status_code
                client = _SwitchResultClient(result, seats_entitled=2, counts={"default": 2, "usage_based": 1})
                cid = f"admin-confirm-switch-{status_code}"

                _result, exc = self.switch(client, confirmation=_confirmation(1, cid=cid))

                self.assertEqual(exc.status_code, 502)
                self.assertIn("切换结果不明确", exc.detail)
                self.assertEqual(self.ledger(cid)["used"], 1)
                self.assertEqual(self.logs("change_seat")[-1]["result"], "uncertain")
                self.assertEqual(asyncio.run(reserved_seats(TEAM, "default")), 1)


# ---- C3：旧前端的 allow_overage ----

class LegacyAllowOverageTest(_Harness):
    def test_bare_allow_overage_on_confirm_team_asks_for_confirmation(self):
        self.insert_team(TEAM, policy="confirm")
        client = _full_client()

        _response, exc = self.invite(client, EMAILS[0], allow_overage=True)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "require_overage_confirmation")
        self.assertEqual(exc.detail["confirmation_status"], "missing")
        self.assertEqual(client.mutations, [])

    def test_auto_team_is_unaffected(self):
        self.insert_team(TEAM, policy="auto")
        client = _full_client()

        response, exc = self.invite(client, EMAILS[0], allow_overage=True)

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual((response["overage"], response["policy"]), (True, "auto"))
        self.assertEqual(self.invited(client), EMAILS[:1])


# ---- C2：批量超员先扣额度 ----

class BatchAllowanceReservedBeforeInviteTest(_Harness):
    def setUp(self):
        super().setUp()
        # 缓存：1 个已付、0 个在用（1 个空位）；现拉：已经满了。
        self.insert_team(TEAM, policy="confirm", seats_entitled=1, created_at="2026-10-01T00:00:00+00:00")

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


# ---- C3 / S3：Telegram ----

class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _bridge_post(path, body=None, **_kwargs):
    match = re.fullmatch(r"/api/teams/([^/]+)/members/invite", path)
    assert match, path
    try:
        return asyncio.run(members.invite_member(match.group(1), InviteMemberRequest(**body)))
    except HTTPException as exc:
        raise requests.HTTPError(
            f"{exc.status_code}", response=_FakeResponse(exc.status_code, {"detail": exc.detail})
        ) from exc


class _ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **_ignored):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class TelegramInviteTest(_Harness):
    CHAT = "chat-rf"
    EMAIL = "tg.member@example.com"

    def setUp(self):
        super().setUp()
        tg_bot.reset_conversation_state()
        self.addCleanup(tg_bot.reset_conversation_state)
        self.post = Mock(side_effect=_bridge_post)
        self.edits = Mock(return_value=True)
        self.client = None
        patches = [
            patch.object(tg_bot, "_api_post", new=self.post),
            patch.object(tg_bot, "_send_returning_id", new=Mock(return_value=42)),
            patch.object(tg_bot, "_edit_message", new=self.edits),
            patch.object(tg_bot, "_update_watch_tg_info", new=Mock()),
            patch.object(tg_bot.threading, "Thread", new=_ImmediateThread),
            patch.object(members, "get_team_client", new=AsyncMock(side_effect=lambda _t: self.client)),
            patch.object(members, "fetch_and_cache_members", new=AsyncMock(return_value=ABSENT)),
            patch.object(members, "run_chatgpt_call", new=direct_call),
            patch.object(seat_capacity, "run_chatgpt_call", new=direct_call),
            patch.object(members, "add_member_watch", new=AsyncMock()),
            patch.object(members, "notify_member_event", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_wizard(self, *, policy, cached_active, live_used):
        self.insert_team(TEAM, policy=policy, seats_entitled=2)
        self.client = FakeTeamClient(seats_entitled=2, counts={"default": live_used, "usage_based": 0})
        status = {"teams": [{"team_id": TEAM, "name": "Team RF", "active_chatgpt": cached_active,
                             "seats_entitled": 2}]}
        with patch.object(tg_bot, "_api_get", new=Mock(return_value=status)):
            tg_bot._start_invite({"chat_id": self.CHAT}, self.CHAT, self.EMAIL)
        tg_bot._step_invite(self.CHAT, tg_bot._wizards[self.CHAT], "1")
        card = tg_bot._step_invite(self.CHAT, tg_bot._wizards[self.CHAT], "30d")
        tg_bot._step_invite(self.CHAT, tg_bot._wizards[self.CHAT], "1")
        return card

    def test_forbid_team_full_in_cache_but_free_live_is_invited(self):
        card = self.run_wizard(policy="forbid", cached_active=2, live_used=1)

        self.assertIn("提交后会现查空位", card)
        self.assertEqual(self.client.mutations, [("invite_member", self.EMAIL, "default")])
        self.assertIn("✅ 邀请已发送", self.edits.call_args.args[2])

    def test_forbid_team_full_live_gets_the_forbid_refusal(self):
        self.run_wizard(policy="forbid", cached_active=2, live_used=2)

        self.assertEqual(self.client.mutations, [])
        text = self.edits.call_args.args[2]
        self.assertIn("❌ 邀请失败", text)
        self.assertIn("设为禁止超员", text)

    def test_confirm_step_sends_a_one_seat_confirmation_not_allow_overage(self):
        self.run_wizard(policy="confirm", cached_active=2, live_used=2)

        body = self.post.call_args.args[1]
        self.assertNotIn("allow_overage", body)
        self.assertEqual(body["overage_confirmation"]["seat_limit"], 1)
        self.assertEqual(body["overage_confirmation"]["seat_type"], "default")
        self.assertRegex(body["overage_confirmation"]["confirmation_id"], r"^[A-Za-z0-9_-]{16,64}$")
        self.assertEqual(self.client.mutations, [("invite_member", self.EMAIL, "default")])


if __name__ == "__main__":
    unittest.main()
