"""评审杂项修复的回归测试：编辑消息不得重放、改密码错误码不得触发登出。"""

import _isolation  # noqa: F401  must precede any app import
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app import security, tg_bot
from app.routes import admin as admin_routes


class _OneShotStop:
    """让 _poll_loop 只跑一轮 getUpdates。"""

    def __init__(self):
        self.polls = 0

    def is_set(self):
        return self.polls >= 1

    def wait(self, seconds):
        return None


def _edited(text, update_id=7):
    return {
        "update_id": update_id,
        "edited_message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "username": "admin"},
            "text": text,
        },
    }


class EditedMessageIgnoredTest(unittest.TestCase):
    def setUp(self):
        tg_bot._wizards.clear()
        self.addCleanup(tg_bot._wizards.clear)

    def _poll(self, updates):
        stop = _OneShotStop()
        offsets = []

        def fake_updates(token, offset):
            offsets.append(offset)
            stop.polls += 1
            return updates

        with (
            patch.object(tg_bot, "_get_setting", side_effect=lambda k: "1" if k == "tg_bot_enabled" else "tok"),
            patch.object(tg_bot, "_get_updates", side_effect=fake_updates),
            patch.object(tg_bot, "sync_all_commands_sync"),
            patch.object(tg_bot, "_send") as send,
            patch.object(tg_bot, "_api_get") as api_get,
            patch.object(tg_bot, "_api_post") as api_post,
            patch.object(tg_bot.requests, "post") as http_post,
            patch.object(tg_bot, "_handle_message") as handle,
        ):
            tg_bot._poll_loop(stop)
        return send, api_get, api_post, http_post, handle

    def test_edited_message_does_not_reach_handler(self):
        for text in ("1", "/invite a@b.c 5", "/token"):
            with self.subTest(text=text):
                send, api_get, api_post, http_post, handle = self._poll([_edited(text)])
                handle.assert_not_called()
                send.assert_not_called()
                api_get.assert_not_called()
                api_post.assert_not_called()
                http_post.assert_not_called()

    def test_edited_message_during_kick_confirm_keeps_wizard_and_calls_nothing(self):
        target = {"email": "x@example.com", "team_name": "T", "team_id": 1, "actions": {"kick": "/api/x/kick"}}
        tg_bot._wizards["42"] = {"flow": "kick", "step": "confirm", "target": target}
        before = copy.deepcopy(tg_bot._wizards)

        # 不 mock _handle_message：走完整路径，证明编辑消息进不了向导。
        stop = _OneShotStop()

        def fake_updates(token, offset):
            stop.polls += 1
            return [_edited("1")]

        with (
            patch.object(tg_bot, "_get_setting", side_effect=lambda k: "1" if k == "tg_bot_enabled" else "tok"),
            patch.object(tg_bot, "_get_updates", side_effect=fake_updates),
            patch.object(tg_bot, "sync_all_commands_sync"),
            patch.object(tg_bot, "_find_user", return_value={"id": 1}),
            patch.object(tg_bot, "_find_member_emails", return_value=[]),
            patch.object(tg_bot, "_send") as send,
            patch.object(tg_bot, "_send_returning_id") as send_id,
            patch.object(tg_bot, "_api_get") as api_get,
            patch.object(tg_bot, "_api_post") as api_post,
            patch.object(tg_bot.requests, "post") as http_post,
        ):
            tg_bot._poll_loop(stop)

        for m in (send, send_id, api_get, api_post, http_post):
            m.assert_not_called()
        self.assertEqual(tg_bot._wizards, before)

    def test_edited_update_still_advances_offset(self):
        stop = _OneShotStop()
        seen = []

        def fake_updates(token, offset):
            seen.append(offset)
            if len(seen) >= 2:
                stop.polls = 1
            return [_edited("1", update_id=7)] if len(seen) == 1 else []

        class Stop2:
            def is_set(self_):
                return len(seen) >= 2

            def wait(self_, s):
                return None

        with (
            patch.object(tg_bot, "_get_setting", side_effect=lambda k: "1" if k == "tg_bot_enabled" else "tok"),
            patch.object(tg_bot, "_get_updates", side_effect=fake_updates),
            patch.object(tg_bot, "sync_all_commands_sync"),
        ):
            tg_bot._poll_loop(Stop2())
        self.assertEqual(seen, [None, 8])


class ChangePasswordWrongCurrentTest(unittest.IsolatedAsyncioTestCase):
    """错误的当前密码是表单校验失败，不能用 401：前端会把带管理员 key 的 401 当成 key 失效并登出。"""

    def setUp(self):
        self.store = {security.ADMIN_PASSWORD_HASH_SETTING: security._password_hash("correct-password")}

        async def read(key):
            return self.store.get(key)

        async def write(key, value):
            self.store[key] = value

        for target, new in (("_read_setting", read), ("_write_setting", write)):
            p = patch.object(security, target, side_effect=new)
            p.start()
            self.addCleanup(p.stop)
        lp = patch.object(admin_routes, "log_operation", new=AsyncMock())
        self.log = lp.start()
        self.addCleanup(lp.stop)

    def _req(self, current, new="brand-new-password"):
        return admin_routes.ChangeAdminPasswordRequest(current_password=current, new_password=new)

    async def test_wrong_current_password_is_400_and_password_unchanged(self):
        before = self.store[security.ADMIN_PASSWORD_HASH_SETTING]
        with self.assertRaises(HTTPException) as ctx:
            await admin_routes.update_admin_password(self._req("wrong-password"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "当前密码不正确")
        self.assertEqual(self.store[security.ADMIN_PASSWORD_HASH_SETTING], before)
        self.assertTrue(await security.verify_admin_password("correct-password"))

    async def test_correct_current_password_still_changes_it(self):
        result = await admin_routes.update_admin_password(self._req("correct-password"))
        self.assertEqual(result, {"status": "ok"})
        self.assertTrue(await security.verify_admin_password("brand-new-password"))
        self.assertFalse(await security.verify_admin_password("correct-password"))


if __name__ == "__main__":
    unittest.main()
