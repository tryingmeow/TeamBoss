"""Regression coverage: GET /api/settings must not leak non-allowlisted secrets."""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes import settings as settings_route


class SettingsRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()

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

    def test_patch_skip_overage_confirmation_true(self):
        """已退役的全局开关：照收不报错，但存的、回的都是 false（旧页面读到 true 会自动确认超员）。"""
        from app.models import SettingsUpdate

        asyncio.run(settings_route.update_settings(SettingsUpdate(skip_overage_confirmation=True)))
        result = asyncio.run(settings_route.get_settings())
        self.assertEqual(result["skip_overage_confirmation"]["value"], "false")

    def test_patch_skip_overage_confirmation_false(self):
        from app.models import SettingsUpdate

        asyncio.run(settings_route.update_settings(SettingsUpdate(skip_overage_confirmation=False)))
        result = asyncio.run(settings_route.get_settings())
        self.assertEqual(result["skip_overage_confirmation"]["value"], "false")


if __name__ == "__main__":
    unittest.main()
