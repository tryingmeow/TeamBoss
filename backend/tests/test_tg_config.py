import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes import tg


class TelegramConfigSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_dir = self.tmpdir.name

        # Patch get_db_dir to return temp directory
        self.db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.db_dir)
        self.db_dir_patch.start()

        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE settings SET value='old-token' WHERE key='tg_bot_token'")
        conn.execute("UPDATE settings SET value='100' WHERE key='tg_bot_id'")
        conn.execute("UPDATE settings SET value='old_bot' WHERE key='tg_bot_username'")
        conn.execute(
            "INSERT INTO tg_users (chat_id, disabled) VALUES ('admin-chat', 0)"
        )
        conn.execute(
            """INSERT INTO tg_member_bindings
               (email, chat_id, disabled) VALUES ('member@example.com', 'member-chat', 0)"""
        )
        conn.execute(
            """INSERT INTO tg_pairing_codes
               (code, disabled, created_at) VALUES ('OLDCODE1', 0, '2026-07-22')"""
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.db_dir_patch.stop()
        self.tmpdir.cleanup()

    def _rows(self):
        conn = sqlite3.connect(self.db_path)
        users = conn.execute("SELECT disabled FROM tg_users").fetchall()
        members = conn.execute("SELECT disabled FROM tg_member_bindings").fetchall()
        codes = conn.execute(
            "SELECT code, disabled FROM tg_pairing_codes ORDER BY id"
        ).fetchall()
        conn.close()
        return users, members, codes

    def _setting(self, key):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row[0] if row else None

    def test_same_bot_token_rotation_preserves_bindings(self):
        with (
            patch.object(tg, "_fetch_bot_identity", return_value={"id": "100", "username": "old_bot"}),
            patch.object(tg, "sync_all_commands_sync"),
        ):
            result = asyncio.run(tg.update_config(tg.TgConfigUpdate(token="rotated-token")))

        users, members, codes = self._rows()
        self.assertFalse(result["bot_changed"])
        self.assertEqual(users, [(0,)])
        self.assertEqual(members, [(0,)])
        self.assertEqual(codes, [("OLDCODE1", 0)])

    def test_different_bot_resets_bindings_and_creates_admin_code(self):
        with (
            patch.object(tg, "_fetch_bot_identity", return_value={"id": "200", "username": "new_bot"}),
            patch.object(tg, "sync_all_commands_sync"),
            patch("app.tg_bot.reset_conversation_state") as reset_state,
        ):
            result = asyncio.run(tg.update_config(tg.TgConfigUpdate(token="new-bot-token")))

        users, members, codes = self._rows()
        self.assertTrue(result["bot_changed"])
        self.assertTrue(result["admin_pairing_code"])
        self.assertEqual(users, [(1,)])
        self.assertEqual(members, [(1,)])
        self.assertEqual(codes[0], ("OLDCODE1", 1))
        self.assertEqual(codes[1], (result["admin_pairing_code"], 0))
        reset_state.assert_called_once_with()

    def test_invalid_token_does_not_apply_enabled_change(self):
        self.assertEqual(self._setting("tg_bot_enabled"), "0")

        with patch.object(tg, "_fetch_bot_identity", return_value=None):
            with self.assertRaises(HTTPException):
                asyncio.run(tg.update_config(tg.TgConfigUpdate(enabled=True, token="invalid")))

        self.assertEqual(self._setting("tg_bot_enabled"), "0")

    def test_invalid_summary_interval_does_not_apply_other_fields(self):
        self.assertEqual(self._setting("tg_bot_enabled"), "0")
        self.assertEqual(self._setting("tg_summary_enabled"), "0")

        with self.assertRaises(HTTPException):
            asyncio.run(tg.update_config(tg.TgConfigUpdate(
                enabled=True,
                summary_enabled=True,
                summary_interval_minutes=1,
            )))

        self.assertEqual(self._setting("tg_bot_enabled"), "0")
        self.assertEqual(self._setting("tg_summary_enabled"), "0")


if __name__ == "__main__":
    unittest.main()
