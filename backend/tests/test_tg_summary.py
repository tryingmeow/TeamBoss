import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.services import tg_summary


def _init_temp_db(db_file_path: str) -> None:
    # 使用 monkeypatch get_db_dir 返回数据库文件所在目录
    # 这样 init_database 会创建所有必要的表
    db_dir = str(Path(db_file_path).parent)
    with patch.object(app_database, "get_db_dir", return_value=db_dir):
        asyncio.run(app_database.init_database())


class TelegramSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_dir = self.tmpdir.name
        self.db_path = str(Path(self.db_dir) / "app.db")
        _init_temp_db(self.db_path)

        # Patch get_db_dir to return test database directory
        # This makes get_db_path() return the test database path automatically
        self.db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.db_dir)
        self.db_dir_patch.start()

        self.original_notify = tg_summary.notify_admins_sync
        self.sent_texts: list[str] = []
        tg_summary.notify_admins_sync = lambda text: self.sent_texts.append(text) or 1

        conn = sqlite3.connect(self.db_path)
        now = "2026-07-21T12:00:00+00:00"
        for key, value in (
            ("tg_bot_enabled", "1"),
            ("tg_summary_enabled", "1"),
            ("tg_summary_interval_minutes", "15"),
            ("tg_summary_last_sent_at", ""),
            ("sync_interval_minutes", "15"),
        ):
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value, now),
            )
        teams = (
            ("over", "Over Team", "active", 0, 1),
            ("codex", "Codex Team", "active", 1, 2),
            ("expired", "Expired Team", "token_expired", 0, 2),
        )
        for team_id, name, status, codex, entitled in teams:
            conn.execute(
                """INSERT INTO teams
                   (id, name, status, is_codex_enabled, seats_entitled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (team_id, name, status, codex, entitled, now, now),
            )
        default_member = lambda email: {
            "id": email,
            "email": email,
            "seat_type": "default",
            "is_owner": False,
            "source": "system",
        }
        for team_id, members in (
            ("over", [default_member("a@x.com"), default_member("b@x.com")]),
            ("codex", [default_member("c@x.com")]),
        ):
            conn.execute(
                "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, ?, '[]', ?)",
                (team_id, json.dumps(members), now),
            )
        conn.commit()
        conn.close()
        self.now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.db_dir_patch.stop()
        tg_summary.notify_admins_sync = self.original_notify
        self.tmpdir.cleanup()

    def test_build_summary_counts_online_and_overage(self):
        text = tg_summary.build_summary_sync(now=self.now)
        self.assertIn("│ 🏢 Team 总数　　　 3", text)
        self.assertIn("│ 💺 GPT 席位　　　  3 / 3", text)
        self.assertIn("│ ⚠️ 超员 Team　　　1", text)
        self.assertIn("│ 👤 超员人数　　　　1", text)
        self.assertIn("│ 🛡️ 巡逻自动踢人：关闭 ⏸️", text)
        self.assertIn("🚨 超员 Team：Over Team", text)

    def test_send_is_throttled_after_successful_delivery(self):
        first = tg_summary.maybe_send_summary_sync(now=self.now)
        second = tg_summary.maybe_send_summary_sync(now=self.now)
        self.assertEqual(first["reason"], "sent")
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["reason"], "not_due")
        self.assertEqual(len(self.sent_texts), 1)

    def test_disabled_summary_does_not_send(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE settings SET value='0' WHERE key='tg_summary_enabled'")
        conn.commit()
        conn.close()
        result = tg_summary.maybe_send_summary_sync(now=self.now)
        self.assertEqual(result["reason"], "summary_disabled")
        self.assertEqual(self.sent_texts, [])


if __name__ == "__main__":
    unittest.main()
