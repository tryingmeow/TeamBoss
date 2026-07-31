import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
