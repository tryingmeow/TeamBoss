"""Secrets must not come back out: masked in error text and in stored logs, never echoed by an import error."""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app import database as app_database
from app.chatgpt_client import mask_secrets
from app.routes import sessions as sessions_route


class MaskSecretsTest(unittest.TestCase):
    def test_masks_url_credentials_keeping_scheme_and_host(self):
        text = "proxy error for http://proxyuser:s3cr3t-pass@10.0.0.1:8080/foo"
        masked = mask_secrets(text)
        self.assertNotIn("proxyuser", masked)
        self.assertNotIn("s3cr3t-pass", masked)
        self.assertIn("http://***@10.0.0.1:8080/foo", masked)

    def test_masks_url_credentials_with_https_and_no_password(self):
        text = "LocationParseError: https://onlyuser@example.com:443/"
        masked = mask_secrets(text)
        self.assertNotIn("onlyuser", masked)
        self.assertIn("https://***@example.com:443/", masked)

    def test_leaves_plain_urls_without_credentials_untouched(self):
        text = "GET https://chatgpt.com/backend-api/teams failed"
        self.assertEqual(mask_secrets(text), text)


class LogOperationMaskingTest(unittest.TestCase):
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


class SessionsImportTest(unittest.TestCase):
    def setUp(self):
        start_temp_db(self)

    def test_malformed_session_does_not_echo_submitted_tokens(self):
        secret_access_token = "sk-super-secret-access-token"
        secret_session_token = "super-secret-session-token"
        malformed_payload = {
            "accessToken": secret_access_token,
            "sessionToken": secret_session_token,
            # missing required fields (e.g. team id) makes validation fail
        }

        result = asyncio.run(sessions_route.import_sessions(malformed_payload))

        body_text = str(result)
        self.assertNotIn(secret_access_token, body_text)
        self.assertNotIn(secret_session_token, body_text)

        self.assertEqual(len(result["errors"]), 1)
        error_detail = result["errors"][0]["detail"]
        self.assertTrue(len(error_detail) > 0)
        # The field location must survive so the operator can tell which field failed.
        for field_error in error_detail:
            self.assertIn("loc", field_error)
            self.assertIn("msg", field_error)
            self.assertNotIn("input", field_error)


if __name__ == "__main__":
    unittest.main()
