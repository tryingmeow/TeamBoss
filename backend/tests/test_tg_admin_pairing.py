import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import tg_bot


class TelegramAdminPairingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_dir_patch = patch.object(
            app_database,
            "get_db_dir",
            return_value=self.tmpdir.name,
        )
        self.db_dir_patch.start()
        self.addCleanup(self.db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
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
