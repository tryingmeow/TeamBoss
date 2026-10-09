"""app.tg_bot: the polling loop, chat commands, admin pairing and how replies are rendered."""

import _isolation  # noqa: F401  must precede any app import
import copy
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app import tg_bot


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


class TelegramPollLoopTest(unittest.TestCase):
    def test_polling_resets_offset_when_token_changes(self):
        calls = []

        class StopAfterTwoPolls:
            def is_set(self):
                return len(calls) >= 2

            def wait(self, seconds):
                return None

        tokens = iter(("bot-a", "bot-b"))

        def fake_setting(key):
            return "1" if key == "tg_bot_enabled" else next(tokens)

        def fake_updates(token, offset):
            calls.append((token, offset))
            return [{"update_id": 100}] if token == "bot-a" else []

        with (
            patch.object(tg_bot, "_get_setting", side_effect=fake_setting),
            patch.object(tg_bot, "_get_updates", side_effect=fake_updates),
            patch.object(tg_bot, "sync_all_commands_sync"),
        ):
            tg_bot._poll_loop(StopAfterTwoPolls())

        self.assertEqual(calls, [("bot-a", None), ("bot-b", None)])


class EditedMessageIgnoredTest(unittest.TestCase):
    def setUp(self):
        tg_bot._wizards.clear()
        self.addCleanup(tg_bot._wizards.clear)

    def _poll(self, updates):
        stop = _OneShotStop()

        def fake_updates(token, offset):
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
        seen = []

        def fake_updates(token, offset):
            seen.append(offset)
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


class TelegramBotCommandTest(unittest.TestCase):
    def test_slash_q_cancels_current_wizard(self):
        with (
            patch.object(tg_bot, "_find_user", return_value={"id": 1}),
            patch.object(tg_bot, "_reset_wizard", return_value=True) as reset,
            patch.object(tg_bot, "_send") as send,
        ):
            tg_bot._handle_message({"chat": {"id": 123, "type": "private"}, "text": "/q"})

        reset.assert_called_once_with("123")
        send.assert_called_once_with("123", "已取消当前操作。")

    def test_plain_q_cancels_current_wizard(self):
        with (
            patch.object(tg_bot, "_find_user", return_value={"id": 1}),
            patch.object(tg_bot, "_reset_wizard", return_value=False) as reset,
            patch.object(tg_bot, "_send") as send,
        ):
            tg_bot._handle_message({"chat": {"id": 123, "type": "private"}, "text": "q"})

        reset.assert_called_once_with("123")
        send.assert_called_once_with("123", "当前没有进行中的操作。")

    def test_group_chat_cannot_pair_or_run_commands(self):
        with (
            patch.object(tg_bot, "_pair") as pair,
            patch.object(tg_bot, "_find_user") as find_user,
            patch.object(tg_bot, "_send") as send,
        ):
            tg_bot._handle_message({
                "chat": {"id": -100123, "type": "supergroup"},
                "from": {"id": 456},
                "text": "/pair ABC12345",
            })

        pair.assert_not_called()
        find_user.assert_not_called()
        send.assert_called_once_with(
            "-100123",
            "为保护管理权限，本机器人仅支持个人私聊。请私聊机器人完成配对和操作。",
        )

    def test_member_logs_command_uses_server_member_scope(self):
        with patch.object(tg_bot, "_api_get", return_value={"logs": []}) as api_get:
            text = tg_bot.cmd_member_logs({}, "alice")

        api_get.assert_called_once_with(
            "/api/logs",
            params={"per_page": 10, "page": 1, "scope": "members", "q": "alice"},
        )
        self.assertEqual(text, "没有匹配的日志。")
        self.assertIn("/m_logs [关键词] · 人员日志", tg_bot._HELP_ADMIN)


class TelegramBotMessageStyleTest(unittest.TestCase):
    def test_watch_uses_summary_and_separate_risk_cards(self):
        payload = {
            "teams": [
                {
                    "team_id": "watch-1",
                    "name": "stillthinkingmeow",
                    "risk": "watch",
                    "active_chatgpt": 1,
                    "seats_entitled": 2,
                    "codex_enabled": False,
                },
                {
                    "team_id": "over-1",
                    "name": "workspace",
                    "risk": "over",
                    "active_chatgpt": 3,
                    "seats_entitled": 2,
                    "codex_enabled": False,
                    "detected_over": [
                        {"email": "new@example.com", "seat_type": "default"}
                    ],
                },
            ]
        }
        with patch.object(tg_bot, "_api_get", return_value=payload):
            text = tg_bot.cmd_watch({}, "")

        self.assertIn("⚠️ 风险 Team", text)
        self.assertIn("│ 🔴 超员：1", text)
        self.assertIn("│ 🟡 观察：1", text)
        self.assertIn("🔴 workspace", text)
        self.assertIn("└ 👤 待处理：new@example.com · ChatGPT", text)
        self.assertIn("🟡 stillthinkingmeow", text)
        self.assertNotIn("risk:watch", text)
        self.assertLess(text.index("🔴 workspace"), text.index("🟡 stillthinkingmeow"))

    def test_status_separates_team_cards_with_blank_lines(self):
        text = tg_bot._render_status(
            {
                "kick_enabled": True,
                "teams": [
                    {
                        "name": "Alpha",
                        "risk": "ok",
                        "active_chatgpt": 1,
                        "seats_entitled": 2,
                        "codex_enabled": True,
                    },
                    {
                        "name": "Beta",
                        "risk": "watch",
                        "active_chatgpt": 2,
                        "seats_entitled": 2,
                        "codex_enabled": False,
                    },
                ],
            },
            "all",
        )

        self.assertIn("│ 🛡️ 自动巡逻：开启 ✅", text)
        self.assertIn("🟢 Alpha", text)
        self.assertIn("🟡 Beta", text)
        self.assertIn("└ 🧭 风险状态：正常\n\n🟡 Beta", text)

    def test_billing_translates_alert_type_and_keeps_hierarchy(self):
        payload = {
            "base_currency": "USD",
            "monthly_total_base": 42.5,
            "teams": [
                {"team_id": "t1", "name": "Alpha", "monthly_total_base": 42.5}
            ],
            "alerts": [
                {"type": "low_balance", "team_name": "Alpha", "detail": "余额 0.00"}
            ],
        }
        with patch.object(tg_bot, "_api_get", return_value=payload):
            text = tg_bot.cmd_billing({}, "")

        # 文案已统一：系统显示的是本地估算的预计成本，不是 OpenAI 的真实账单；
        # "余额"特指 OpenAI Credit，避免被读成银行卡余额。
        self.assertIn("💳 预计月支出", text)
        self.assertIn("│ 💰 月度总支出：42.50 USD", text)
        self.assertIn("1. Alpha  ·  42.50 USD", text)
        self.assertIn("🏷️ 类型：Credit 不足", text)

    def test_team_detail_labels_converted_monthly_total_with_base_currency(self):
        patrol_payload = {
            "teams": [{
                "team_id": "t1",
                "name": "Alpha",
                "risk": "ok",
                "active_chatgpt": 1,
                "seats_entitled": 2,
                "codex_enabled": False,
            }]
        }
        finance_payload = {
            "base_currency": "USD",
            "teams": [{
                "team_id": "t1",
                "billing_currency": "THB",
                "balance": "100",
                "active_until": "2026-08-01T00:00:00Z",
                "subscription_status": "renewing",
                "monthly_total_base": 42.5,
            }],
        }

        def fake_get(path, **kwargs):
            return patrol_payload if path == "/api/patrol/status" else finance_payload

        with patch.object(tg_bot, "_api_get", side_effect=fake_get):
            text = tg_bot.cmd_team({}, "Alpha")

        self.assertIn("💰 月费：42.5 USD", text)
        self.assertNotIn("💰 月费：42.5 THB", text)
        self.assertIn("🔁 订阅：正常续费", text)


class TelegramAdminPairingTest(unittest.TestCase):
    def setUp(self):
        self.db_path = start_temp_db(self)
        self.path_patch = patch.object(tg_bot, "get_db_path", return_value=self.db_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        tg_bot._pair_failures.clear()
        self.addCleanup(tg_bot._pair_failures.clear)
        self.sent: list[tuple[str, str]] = []
        send_patch = patch.object(
            tg_bot, "_send", side_effect=lambda chat_id, text: self.sent.append((chat_id, text)) or True
        )
        send_patch.start()
        self.addCleanup(send_patch.stop)
        sync_patch = patch.object(tg_bot, "sync_chat_commands_sync")
        sync_patch.start()
        self.addCleanup(sync_patch.stop)

    def _add_code(self, code: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO tg_pairing_codes (code, expires_at, disabled, created_at)
               VALUES (?, '2099-01-01T00:00:00+00:00', 0, '2026-01-01')""",
            (code,),
        )
        conn.commit()
        conn.close()

    def _add_admin(self, chat_id: str, disabled: int = 0):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO tg_users (chat_id, username, paired_at, created_at, disabled)
               VALUES (?, 'old', '2026-01-01', '2026-01-01', ?)""",
            (chat_id, disabled),
        )
        conn.commit()
        conn.close()

    def _is_admin(self, chat_id: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM tg_users WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_five_bad_codes_lock_the_chat_even_against_a_valid_code(self):
        self._add_code("GOODCODE")
        replies = [tg_bot._pair("guesser", "g", f"BAD{i}") for i in range(5)]
        self.assertTrue(all("无效" in r for r in replies[:4]))
        self.assertIn("错误次数过多", replies[4])

        # Locked: even the right code is refused and nothing is granted.
        self.assertIn("错误次数过多", tg_bot._pair("guesser", "g", "GOODCODE"))
        self.assertFalse(self._is_admin("guesser"))

        # Other chats are not affected.
        self.assertIn("注册成功", tg_bot._pair("someone-else", "s", "GOODCODE"))

    def test_lockout_expires(self):
        with patch.object(tg_bot.time, "time", return_value=1_000_000.0):
            for i in range(5):
                tg_bot._pair("guesser", "g", f"BAD{i}")
        self._add_code("GOODCODE")
        with patch.object(tg_bot.time, "time", return_value=1_000_000.0 + tg_bot._PAIR_LOCKOUT_SECONDS + 1):
            self.assertIn("注册成功", tg_bot._pair("guesser", "g", "GOODCODE"))

    def test_successful_admin_pairing_notifies_every_existing_admin(self):
        self._add_admin("admin-1")
        self._add_admin("admin-2")
        self._add_admin("admin-off", disabled=1)
        self._add_code("NEWADMIN")

        reply = tg_bot._pair("newcomer", "new_guy", "NEWADMIN")

        self.assertIn("注册成功", reply)
        recipients = sorted(chat for chat, _ in self.sent)
        self.assertEqual(recipients, ["admin-1", "admin-2"])
        for _, text in self.sent:
            self.assertIn("new_guy", text)
            self.assertIn("newcomer", text)

    def test_first_admin_pairing_has_nobody_to_notify(self):
        self._add_code("FIRSTONE")
        self.assertIn("注册成功", tg_bot._pair("first", "f", "FIRSTONE"))
        self.assertEqual(self.sent, [])

    def test_member_info_failure_does_not_leak_the_internal_backend_url(self):
        leak = requests.ConnectionError(
            "HTTPConnectionPool(host='127.0.0.1', port=18087): Max retries exceeded "
            "with url: /api/users/members?q=a%40b.c"
        )
        with patch.object(tg_bot, "_member_info_items", side_effect=leak):
            tg_bot._handle_info(None, "member-chat", "", ["a@b.c"])
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0][1]
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("/api/", text)
        self.assertNotIn("18087", text)

        self.sent.clear()
        timeout = requests.ReadTimeout("HTTPConnectionPool(host='127.0.0.1', port=18087): Read timed out.")
        message = tg_bot._extract_api_error(timeout)
        self.assertNotIn("127.0.0.1", message)

    def test_failed_atomic_claim_does_not_grant_admin(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO tg_pairing_codes
               (code, expires_at, disabled, created_at)
               VALUES ('PAIR1234', '2099-01-01T00:00:00+00:00', 0, '2026-01-01')"""
        )
        conn.execute(
            """CREATE TRIGGER ignore_pairing_claim
               BEFORE UPDATE OF used_by_chat_id ON tg_pairing_codes
               BEGIN
                 SELECT RAISE(IGNORE);
               END"""
        )
        conn.commit()
        conn.close()

        result = tg_bot._pair("loser-chat", "loser", "PAIR1234")

        conn = sqlite3.connect(self.db_path)
        user_count = conn.execute(
            "SELECT COUNT(*) FROM tg_users WHERE chat_id = 'loser-chat'"
        ).fetchone()[0]
        conn.close()
        self.assertIn("同时使用", result)
        self.assertEqual(user_count, 0)


if __name__ == "__main__":
    unittest.main()
