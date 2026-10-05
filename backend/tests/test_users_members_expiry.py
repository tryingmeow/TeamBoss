"""Regression coverage for current-member expiry selection in the users API."""

import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import users


class UsersMembersExpiryTest(unittest.TestCase):
    def test_reinvited_member_uses_live_email_expiry_and_keeps_kicked_history(self):
        historical_kicked = {
            "id": 1,
            "team_id": "team-1",
            "user_id": "live-user",
            "email": "rejoined@example.com",
            "expires_at": "2026-09-18T12:34:00+00:00",
            "kicked": 1,
            "kicked_at": "2026-09-01T12:34:00+00:00",
        }
        active_reinvite = {
            "id": 2,
            "team_id": "team-1",
            "user_id": "",
            "email": "rejoined@example.com",
            "expires_at": "2026-10-17T12:34:00+00:00",
            "kicked": 0,
        }
        expiry_rows = [active_reinvite, historical_kicked]

        by_user, by_email = users._expiry_maps(expiry_rows)
        policy = {"mode": "delay_hours", "delay_hours": 0, "label": "到期"}

        current_member = users._member_expiry_payload(
            "team-1",
            "live-user",
            "rejoined@example.com",
            None,
            by_user,
            by_email,
            policy,
        )
        pending_invite = users._member_expiry_payload(
            "team-1",
            "",
            "rejoined@example.com",
            None,
            by_user,
            by_email,
            policy,
        )

        self.assertNotIn(("team-1", "live-user"), by_user)
        self.assertEqual(by_email[("team-1", "rejoined@example.com")], active_reinvite)
        self.assertEqual(current_member["expiry_id"], 2)
        self.assertEqual(current_member["expires_at"], "2026-10-17T12:34:00+00:00")
        self.assertFalse(current_member["kicked"])
        self.assertEqual(pending_invite["expiry_id"], 2)
        self.assertEqual(pending_invite["expires_at"], "2026-10-17T12:34:00+00:00")

        # _expiry_maps must not filter the source rows: list_members consumes
        # kicked rows separately when constructing history output.
        self.assertEqual(
            [row for row in expiry_rows if row["kicked"] == 1],
            [historical_kicked],
        )


if __name__ == "__main__":
    unittest.main()
