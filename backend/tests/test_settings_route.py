"""Regression coverage: GET /api/settings must not leak non-allowlisted secrets."""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app.routes import settings as settings_route


class SettingsRouteTest(unittest.TestCase):
    def setUp(self):
        self.db_path = start_temp_db(self)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def test_get_settings_only_returns_allowlisted_keys(self):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                ("admin_api_key", "super-secret-key", "2026-09-17"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                ("admin_password_hash", "hashed-password", "2026-09-17"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                ("tg_bot_token", "123456:ABCDEF", "2026-09-17"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                ("sync_interval_minutes", "15", "2026-09-17"),
            )

        result = asyncio.run(settings_route.get_settings())

        for leaked_key in ("admin_api_key", "admin_password_hash", "tg_bot_token"):
            self.assertNotIn(leaked_key, result)

        for allowed_key in settings_route.PUBLIC_SETTINGS_KEYS:
            self.assertIn(allowed_key, result)

        self.assertEqual(result["sync_interval_minutes"]["value"], "15")

    def test_skip_overage_confirmation_key_in_allowlist(self):
        self.assertIn("skip_overage_confirmation", settings_route.PUBLIC_SETTINGS_KEYS)

    def test_patch_skip_overage_confirmation_false(self):
        """已退役的全局开关：PATCH false 照收，存成字符串 "false"（不是 str(False) 的 "False"）。"""
        from app.models import SettingsUpdate

        response = asyncio.run(settings_route.update_settings(SettingsUpdate(skip_overage_confirmation=False)))
        self.assertEqual(response["updated"], {"skip_overage_confirmation": "false"})
        with self._conn() as conn:
            stored = conn.execute(
                "SELECT value FROM settings WHERE key = 'skip_overage_confirmation'"
            ).fetchone()["value"]
        self.assertEqual(stored, "false")
        result = asyncio.run(settings_route.get_settings())
        self.assertEqual(result["skip_overage_confirmation"]["value"], "false")


if __name__ == "__main__":
    unittest.main()
