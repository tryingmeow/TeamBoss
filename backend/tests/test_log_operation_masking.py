"""Regression coverage: log_operation must mask secrets in free-text fields."""

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


class LogOperationMaskingTest(unittest.TestCase):
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

    def test_error_message_with_credential_url_is_masked_in_storage(self):
        secret_error = "connection failed for http://user:secretpw@host:8080/path"

        asyncio.run(
            app_database.log_operation(
                "team-1", "check_proxy", None, None, "failed", secret_error
            )
        )

        with self._conn() as conn:
            row = conn.execute(
                "SELECT error_message FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()

        stored = row["error_message"]
        self.assertNotIn("user:secretpw@", stored)
        self.assertIn("http://***@host:8080/path", stored)


if __name__ == "__main__":
    unittest.main()
