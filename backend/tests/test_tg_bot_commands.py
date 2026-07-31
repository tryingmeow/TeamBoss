import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tg_bot
from app.services import tg_commands


class TelegramBotCancelCommandTest(unittest.TestCase):
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

    def test_command_menu_registers_q_instead_of_cancel(self):
        admin_commands = [item["command"] for item in tg_commands.ADMIN_COMMANDS]
        member_commands = [item["command"] for item in tg_commands.MEMBER_COMMANDS]
        public_commands = [item["command"] for item in tg_commands.PUBLIC_COMMANDS]

        self.assertIn("q", admin_commands)
        self.assertNotIn("cancel", admin_commands)
        self.assertNotIn("q", member_commands)
        self.assertNotIn("q", public_commands)

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

        self.assertIn("⚠️ 风险车队", text)
        self.assertIn("│ 🔴 超员：1", text)
        self.assertIn("│ 🟡 观察：1", text)
        self.assertIn("🔴 workspace", text)
        self.assertIn("└ 👤 待处理：new@example.com · default", text)
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


if __name__ == "__main__":
    unittest.main()
