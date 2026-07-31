import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tg_bot
from app.services import tg_member_bindings


def _create_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE teams (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE member_expiry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT,
                email TEXT,
                expires_at TEXT,
                kicked INTEGER DEFAULT 0
            );
            CREATE TABLE tg_member_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                chat_id TEXT NOT NULL,
                username TEXT,
                disabled INTEGER DEFAULT 0,
                paired_at TEXT,
                disabled_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE tg_member_pairing_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL COLLATE NOCASE,
                expires_at TEXT,
                used_by_chat_id TEXT,
                used_at TEXT,
                disabled INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE tg_member_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE,
                team_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                reminder_key TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(email, team_id, expires_at, reminder_key)
            );
            CREATE TABLE tg_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                disabled INTEGER DEFAULT 0,
                paired_at TEXT,
                created_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


class TelegramMemberBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "app.db")
        _create_schema(self.db_path)
        self.db_patch = patch.object(tg_member_bindings, "DB_PATH", self.db_path)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _insert_code(self, code: str, email: str) -> None:
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO tg_member_pairing_codes
                   (code, email, expires_at, disabled, created_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (code, email, expires_at, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def test_copy_template_contains_bot_url_and_pair_command(self):
        self.assertEqual(
            tg_member_bindings.build_member_copy_text("@team_bot", "abc234"),
            "TG 机器人：https://t.me/team_bot\n绑定指令：/pair ABC234",
        )

    def test_one_chat_can_bind_multiple_emails_and_email_can_be_reassigned(self):
        self._insert_code("MEMBERA", "a@example.com")
        self._insert_code("MEMBERB", "b@example.com")
        self._insert_code("MEMBERC", "a@example.com")

        self.assertIn("绑定成功", tg_member_bindings.claim_member_pairing_code_sync("100", "first", "MEMBERA"))
        self.assertIn("绑定成功", tg_member_bindings.claim_member_pairing_code_sync("100", "first", "MEMBERB"))
        self.assertEqual(
            tg_member_bindings.member_emails_for_chat_sync("100"),
            ["a@example.com", "b@example.com"],
        )

        self.assertIn("绑定成功", tg_member_bindings.claim_member_pairing_code_sync("200", "second", "MEMBERC"))
        self.assertEqual(tg_member_bindings.member_emails_for_chat_sync("100"), ["b@example.com"])
        self.assertEqual(tg_member_bindings.member_emails_for_chat_sync("200"), ["a@example.com"])

    def test_binding_disables_only_after_last_active_membership_is_gone(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """INSERT INTO tg_member_bindings
                   (email, chat_id, disabled, paired_at, created_at, updated_at)
                   VALUES ('same@example.com', '100', 0, 'now', 'now', 'now')"""
            )
            conn.execute(
                "INSERT INTO member_expiry (team_id, email, kicked) VALUES ('a', 'same@example.com', 0)"
            )
            conn.execute(
                "INSERT INTO member_expiry (team_id, email, kicked) VALUES ('b', 'same@example.com', 0)"
            )
            conn.commit()

            conn.execute("UPDATE member_expiry SET kicked = 1 WHERE team_id = 'a'")
            self.assertFalse(
                tg_member_bindings.deactivate_member_binding_if_inactive_sync(conn, "same@example.com")
            )

            conn.execute("UPDATE member_expiry SET kicked = 1 WHERE team_id = 'b'")
            self.assertTrue(
                tg_member_bindings.deactivate_member_binding_if_inactive_sync(conn, "same@example.com")
            )
            conn.commit()
            row = conn.execute(
                "SELECT disabled FROM tg_member_bindings WHERE email = 'same@example.com'"
            ).fetchone()
            self.assertEqual(row["disabled"], 1)
        finally:
            conn.close()

    def test_reminders_send_each_locked_stage_once(self):
        expires_at = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (("expiry_kick_mode", "delay_hours"), ("expiry_kick_delay_hours", "2")),
            )
            conn.execute("INSERT INTO teams (id, name) VALUES ('team-1', 'Bravo')")
            conn.execute(
                """INSERT INTO tg_member_bindings
                   (email, chat_id, disabled, paired_at, created_at, updated_at)
                   VALUES ('member@example.com', '100', 0, 'now', 'now', 'now')"""
            )
            conn.execute(
                """INSERT INTO member_expiry (team_id, email, expires_at, kicked)
                   VALUES ('team-1', 'member@example.com', ?, 0)""",
                (expires_at.isoformat(),),
            )
            conn.executemany(
                """INSERT INTO tg_users (username, disabled, paired_at, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    ("disabled_admin", 1, "2026-08-02T00:00:00+00:00", "now"),
                    ("renew_admin", 0, "2026-08-03T00:00:00+00:00", "now"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        sent: list[tuple[str, str]] = []

        def send(chat_id: str, text: str) -> bool:
            sent.append((chat_id, text))
            return True

        checkpoints = (
            expires_at - timedelta(days=6),
            expires_at - timedelta(days=2),
            expires_at - timedelta(hours=20),
            expires_at - timedelta(hours=4),
            expires_at + timedelta(hours=1),
        )
        for checkpoint in checkpoints:
            result = tg_member_bindings.run_member_expiry_reminders_sync(now=checkpoint, send_func=send)
            self.assertEqual(result["sent"], 1)
            duplicate = tg_member_bindings.run_member_expiry_reminders_sync(now=checkpoint, send_func=send)
            self.assertEqual(duplicate["sent"], 0)

        self.assertEqual(len(sent), 5)
        self.assertIn("7 天内到期", sent[0][1])
        self.assertIn("3 天内到期", sent[1][1])
        self.assertIn("1 天内到期", sent[2][1])
        self.assertIn("5 小时内到期", sent[3][1])
        self.assertIn("系统宽限期", sent[4][1])
        self.assertTrue(all("不计入购买时长" in text for _, text in sent))
        self.assertTrue(all("💬 管理员：@renew_admin" in text for _, text in sent))
        self.assertTrue(all("disabled_admin" not in text for _, text in sent))

    def test_reminder_omits_admin_line_without_public_username(self):
        expires_at = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        text = tg_member_bindings._reminder_text(
            email="member@example.com",
            team_name="Bravo",
            expires_at=expires_at,
            effective_kick_at=expires_at + timedelta(hours=2),
            reminder_key="before_1d",
            stage_label="1 天",
            admin_contact=None,
        )
        self.assertNotIn("管理员联系方式", text)


class TelegramMemberCommandScopeTest(unittest.TestCase):
    def test_member_only_identity_routes_bare_info_to_own_emails(self):
        with (
            patch.object(tg_bot, "_find_user", return_value=None),
            patch.object(tg_bot, "_find_member_emails", return_value=["me@example.com"]),
            patch.object(tg_bot, "_handle_info") as handle_info,
            patch.object(tg_bot, "_send"),
        ):
            tg_bot._handle_message({"chat": {"id": 123, "type": "private"}, "text": "/info"})

        handle_info.assert_called_once_with(None, "123", "", ["me@example.com"])

    def test_non_admin_cannot_query_another_email(self):
        with (
            patch.object(tg_bot, "_api_get") as api_get,
            patch.object(tg_bot, "_send") as send,
        ):
            tg_bot._handle_info(
                None,
                "123",
                "other@example.com",
                ["me@example.com"],
            )

        api_get.assert_not_called()
        self.assertIn("不能查询其他邮箱", send.call_args.args[1])

    def test_info_expiry_separates_service_time_from_grace(self):
        expires_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        lines = tg_bot._member_expiry_lines(
            {
                "expires_at": expires_at.isoformat(),
                "effective_kick_at": (expires_at + timedelta(hours=2)).isoformat(),
            },
            now=datetime.now(timezone.utc),
        )
        rendered = "\n".join(lines)
        self.assertIn("服务到期", rendered)
        self.assertIn("处于系统宽限期", rendered)
        self.assertIn("不计入购买时长", rendered)


if __name__ == "__main__":
    unittest.main()
