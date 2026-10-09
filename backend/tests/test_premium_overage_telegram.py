"""Telegram /invite 按 Team 的超员策略走（仍然只邀请 ChatGPT 席位）。

* 禁止超员：向导不按缓存拒绝，提交后服务端现查；现拉也满了才显示拒绝原因。
* 超员需确认且已满：确认卡片说清会加购扣费，回 1 才带确认（只管 1 个席位，不带旧的 allow_overage）调接口。
* 超员自动：照常邀请。
* 缓存说没满、服务端现拉发现满了：禁止超员 → 显示拒绝原因；超员需确认 → 转成同样的
  确认步骤，回 1 后带确认重发。

机器人经本地 HTTP 调邀请接口；这里把 ``_api_post`` 接到真实的 invite_member 路由上
（上游仍是记录调用的替身），所以服务端的策略检查也一起跑。
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
from app.models import InviteMemberRequest
from app.routes import members
from app.services import seat_capacity


TEAM = "prem-tg-team"
CHAT = "chat-77"
EMAIL = "tg.member@example.com"
ABSENT = {"members": [], "pending_invites": []}


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _bridge_post(path, body=None, **_kwargs):
    """tg_bot._api_post 的替身：同进程调用真实的邀请路由，HTTPException 转成 requests 的错误。"""
    match = re.fullmatch(r"/api/teams/([^/]+)/members/invite", path)
    assert match, path
    try:
        return asyncio.run(members.invite_member(match.group(1), InviteMemberRequest(**body)))
    except HTTPException as exc:
        raise requests.HTTPError(
            f"{exc.status_code}", response=_FakeResponse(exc.status_code, {"detail": exc.detail})
        ) from exc


class _ImmediateThread:
    """threading.Thread 的替身：start() 时同步跑 target，让用例能直接看结果。"""

    def __init__(self, target, args=(), kwargs=None, **_ignored):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class TelegramInviteOverageTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._start_db()
        self.track_reservation(TEAM, EMAIL)
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

    def start_wizard(self, *, policy, cached_active, live_used):
        """建 Team、起 /invite 向导、选第 1 个 Team、填有效期，返回确认卡片文本。"""
        self.insert_team(TEAM, policy=policy, seats_entitled=2)
        self.client = FakeTeamClient(seats_entitled=2, counts={"default": live_used, "usage_based": 0})
        status = {"teams": [{"team_id": TEAM, "name": "Team TG", "active_chatgpt": cached_active,
                             "seats_entitled": 2}]}
        with patch.object(tg_bot, "_api_get", new=Mock(return_value=status)):
            tg_bot._start_invite({"chat_id": CHAT}, CHAT, EMAIL)
        reply = tg_bot._step_invite(CHAT, tg_bot._wizards[CHAT], "1")
        if CHAT not in tg_bot._wizards:
            return reply
        return tg_bot._step_invite(CHAT, tg_bot._wizards[CHAT], "30d")

    def confirm(self):
        return tg_bot._step_invite(CHAT, tg_bot._wizards[CHAT], "1")

    def last_edit(self):
        return self.edits.call_args.args[2]

    # ---- 禁止超员 ----

    def test_forbid_cached_full_team_is_decided_by_the_server(self):
        card = self.start_wizard(policy="forbid", cached_active=2, live_used=2)
        self.assertIn("提交后会现查空位", card)
        self.assertNotIn("扣费", card)

        self.assertIsNone(self.confirm())

        self.assertIsNone(self.post.call_args.args[1]["overage_confirmation"])
        text = self.last_edit()
        self.assertIn("❌ 邀请失败", text)
        self.assertIn("设为禁止超员", text)
        self.assertIn("Team 设置里修改超员策略", text)
        self.assertEqual(self.client.mutations, [])

    def test_forbid_team_full_in_cache_but_free_live_is_invited(self):
        card = self.start_wizard(policy="forbid", cached_active=2, live_used=1)
        self.assertIn("提交后会现查空位", card)

        self.confirm()

        self.assertEqual(self.client.mutations, [("invite_member", EMAIL, "default")])
        self.assertIn("✅ 邀请已发送", self.last_edit())

    def test_forbid_live_full_gets_the_server_refusal(self):
        card = self.start_wizard(policy="forbid", cached_active=1, live_used=2)
        self.assertNotIn("加购", card)

        self.assertIsNone(self.confirm())

        body = self.post.call_args.args[1]
        self.assertEqual(body["seat_type"], "default")
        self.assertIsNone(body["overage_confirmation"])
        text = self.last_edit()
        self.assertIn("❌ 邀请失败", text)
        self.assertIn("设为禁止超员", text)
        self.assertNotIn("{'code'", text, "回给人的是 message，不是 detail 对象原样")
        self.assertNotIn(CHAT, tg_bot._wizards)
        self.assertEqual(self.client.mutations, [])

    # ---- 超员需确认 ----

    def test_confirm_full_team_warns_then_invites_after_reply_1(self):
        card = self.start_wizard(policy="confirm", cached_active=2, live_used=2)
        self.assertIn("自动加购 1 个 ChatGPT 席位并扣费", card)
        self.assertIn("超员需确认", card)

        self.confirm()

        body = self.post.call_args.args[1]
        self.assertNotIn("allow_overage", body)
        confirmation = body["overage_confirmation"]
        self.assertEqual((confirmation["seat_type"], confirmation["seat_limit"]), ("default", 1))
        self.assertRegex(confirmation["confirmation_id"], r"^[A-Za-z0-9_-]{16,64}$")
        self.assertEqual(self.client.mutations, [("invite_member", EMAIL, "default")])
        self.assertEqual(self.client.capacity_reads, 1)
        self.assertIn("✅ 邀请已发送", self.last_edit())

    def test_confirm_live_full_turns_into_a_confirm_step(self):
        card = self.start_wizard(policy="confirm", cached_active=1, live_used=2)
        self.assertNotIn("加购", card)

        self.confirm()

        self.assertIsNone(self.post.call_args.args[1]["overage_confirmation"])
        prompt = self.last_edit()
        self.assertIn("需要确认超员", prompt)
        self.assertIn("自动加购 1 个 ChatGPT 席位并扣费", prompt)
        self.assertIn("回复 1 确认加购", prompt)
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(tg_bot._wizards[CHAT]["step"], "confirm")

        self.confirm()

        self.assertEqual(self.post.call_args.args[1]["overage_confirmation"]["seat_limit"], 1)
        self.assertEqual(self.client.mutations, [("invite_member", EMAIL, "default")])
        self.assertNotIn(CHAT, tg_bot._wizards)

    def test_confirm_step_cancel_sends_nothing(self):
        self.start_wizard(policy="confirm", cached_active=2, live_used=2)

        self.assertEqual(tg_bot._step_invite(CHAT, tg_bot._wizards[CHAT], "0"), "已取消。")

        self.post.assert_not_called()
        self.assertEqual(self.client.mutations, [])

    # ---- 超员自动 ----

    def test_auto_full_team_invites(self):
        card = self.start_wizard(policy="auto", cached_active=2, live_used=2)
        self.assertIn("超员自动", card)

        self.confirm()

        self.assertEqual(self.client.mutations, [("invite_member", EMAIL, "default")])
        self.assertEqual(self.client.capacity_reads, 0)


class TelegramSeatLabelTest(unittest.TestCase):
    def test_member_info_shows_registry_labels(self):
        items = [
            {"email": f"{seat}@example.com", "team_name": "T", "seat_type": seat, "status": "joined"}
            for seat in ("default", "usage_based", "prolite", "automation")
        ] + [{"email": "missing@example.com", "team_name": "T", "status": "joined"}]

        text = tg_bot._info_blocks(items)

        for label in ("席位：ChatGPT", "席位：Codex", "席位：Premium", "席位：其他（automation）"):
            self.assertIn(label, text)
        self.assertNotIn("席位：default", text)
        self.assertNotIn("席位：prolite", text)


if __name__ == "__main__":
    unittest.main()
