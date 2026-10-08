"""Regression coverage: session import errors must not echo submitted tokens back."""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app.routes import sessions as sessions_route


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
