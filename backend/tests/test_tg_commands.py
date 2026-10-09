import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app import database as app_database
from app.services import tg_commands


class _Response:
    ok = True
    status_code = 200


class TelegramCommandScopeTest(unittest.TestCase):
    def setUp(self):
        self.db_path = start_temp_db(self)

        # Insert test data
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE settings SET value='test-token' WHERE key='tg_bot_token'")
        conn.executemany(
            "INSERT INTO tg_users (chat_id, disabled) VALUES (?, ?)",
            (("admin", 0), ("disabled-admin", 1), ("both", 0)),
        )
        conn.executemany(
            "INSERT INTO tg_member_bindings (email, chat_id, disabled) VALUES (?, ?, ?)",
            (
                ("member@example.com", "member", 0),
                ("disabled@example.com", "disabled-admin", 0),
                ("both@example.com", "both", 0),
            ),
        )
        conn.commit()
        conn.close()

    def test_identity_precedence_is_admin_then_member_then_public(self):
        self.assertEqual(tg_commands.command_identity_sync("admin"), "admin")
        self.assertEqual(tg_commands.command_identity_sync("both"), "admin")
        self.assertEqual(tg_commands.command_identity_sync("member"), "member")
        self.assertEqual(tg_commands.command_identity_sync("disabled-admin"), "member")
        self.assertEqual(tg_commands.command_identity_sync("unknown"), "public")

    def test_sync_all_sets_public_default_and_chat_specific_menus(self):
        sent: list[dict] = []

        def fake_post(_url, *, json, timeout):
            sent.append(json)
            return _Response()

        with patch.object(tg_commands.requests, "post", side_effect=fake_post):
            self.assertTrue(tg_commands.sync_all_commands_sync())

        default = next(payload for payload in sent if "scope" not in payload)
        by_chat = {
            payload["scope"]["chat_id"]: payload["commands"]
            for payload in sent
            if payload.get("scope", {}).get("type") == "chat"
        }
        self.assertEqual(default["commands"], tg_commands.PUBLIC_COMMANDS)
        self.assertEqual(by_chat["admin"], tg_commands.ADMIN_COMMANDS)
        self.assertEqual(by_chat["both"], tg_commands.ADMIN_COMMANDS)
        self.assertEqual(by_chat["member"], tg_commands.MEMBER_COMMANDS)
        self.assertEqual(by_chat["disabled-admin"], tg_commands.MEMBER_COMMANDS)

    def test_last_member_binding_disabled_downgrades_menu_to_public(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE tg_member_bindings SET disabled = 1 WHERE chat_id = 'member'"
        )
        conn.commit()
        conn.close()

        with patch.object(tg_commands.requests, "post", return_value=_Response()) as post:
            self.assertTrue(tg_commands.sync_chat_commands_sync("member"))

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["scope"], {"type": "chat", "chat_id": "member"})
        self.assertEqual(payload["commands"], tg_commands.PUBLIC_COMMANDS)


class TelegramCommandMenuTest(unittest.TestCase):
    def test_command_menu_registers_q_instead_of_cancel(self):
        admin_commands = [item["command"] for item in tg_commands.ADMIN_COMMANDS]
        member_commands = [item["command"] for item in tg_commands.MEMBER_COMMANDS]
        public_commands = [item["command"] for item in tg_commands.PUBLIC_COMMANDS]

        self.assertIn("q", admin_commands)
        self.assertIn("m_logs", admin_commands)
        self.assertNotIn("cancel", admin_commands)
        self.assertNotIn("q", member_commands)
        self.assertNotIn("q", public_commands)
        self.assertNotIn("m_logs", member_commands)
        self.assertNotIn("m_logs", public_commands)


class TelegramLegacyRoleMigrationTest(unittest.TestCase):
    def test_init_database_removes_legacy_viewer_rows_and_role_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create legacy database with role columns
            path = str(Path(tmpdir) / "app.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE tg_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL UNIQUE,
                    username TEXT,
                    role TEXT DEFAULT 'viewer',
                    note TEXT,
                    disabled INTEGER DEFAULT 0,
                    paired_at TEXT,
                    created_at TEXT
                );
                CREATE TABLE tg_pairing_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    role TEXT DEFAULT 'viewer',
                    note TEXT,
                    expires_at TEXT,
                    used_by_chat_id TEXT,
                    used_at TEXT,
                    disabled INTEGER DEFAULT 0,
                    created_at TEXT
                );
                INSERT INTO tg_users (chat_id, role) VALUES ('admin-chat', 'admin');
                INSERT INTO tg_users (chat_id, role) VALUES ('viewer-chat', 'viewer');
                INSERT INTO tg_pairing_codes (code, role) VALUES ('ADMINCODE', 'admin');
                INSERT INTO tg_pairing_codes (code, role) VALUES ('VIEWERCODE', 'viewer');
                """
            )
            conn.commit()
            conn.close()

            # Run init_database with patched get_db_dir
            with patch.object(app_database, "get_db_dir", return_value=tmpdir):
                asyncio.run(app_database.init_database())

            # Verify the role columns were removed
            conn = sqlite3.connect(path)
            try:
                user_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(tg_users)")
                }
                code_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(tg_pairing_codes)")
                }
                users = conn.execute("SELECT chat_id FROM tg_users").fetchall()
                codes = conn.execute("SELECT code FROM tg_pairing_codes").fetchall()
            finally:
                conn.close()

            self.assertNotIn("role", user_columns)
            self.assertNotIn("role", code_columns)
            self.assertEqual(users, [("admin-chat",)])
            self.assertEqual(codes, [("ADMINCODE",)])


if __name__ == "__main__":
    unittest.main()
