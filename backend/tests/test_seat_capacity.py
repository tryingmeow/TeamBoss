import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.seat_capacity import (
    chatgpt_seat_capacity,
    chatgpt_count_from_seat_counts,
    codex_count_from_seat_counts,
    member_seat_usage_from_members_data,
)


class SeatCapacityTest(unittest.TestCase):
    def test_codex_seats_do_not_consume_chatgpt_entitlement(self):
        capacity = chatgpt_seat_capacity(
            seats_entitled=2,
            seats_in_use=2,
            codex_count=1,
        )

        self.assertEqual(capacity.active_chatgpt, 1)
        self.assertEqual(capacity.available, 1)

    def test_pending_default_invites_reserve_chatgpt_capacity(self):
        capacity = chatgpt_seat_capacity(
            seats_entitled=2,
            seats_in_use=2,
            codex_count=1,
            pending_default=1,
        )

        self.assertEqual(capacity.active_chatgpt, 1)
        self.assertEqual(capacity.available, 0)

    def test_overreported_codex_count_cannot_make_negative_gpt_usage(self):
        capacity = chatgpt_seat_capacity(
            seats_entitled=2,
            seats_in_use=1,
            codex_count=3,
        )

        self.assertEqual(capacity.active_chatgpt, 0)
        self.assertEqual(capacity.available, 2)

    def test_codex_count_from_seat_counts(self):
        self.assertEqual(
            codex_count_from_seat_counts({"seat_type_counts": {"usage_based": "2"}}),
            2,
        )

    def test_official_default_count_is_used_directly(self):
        seat_counts = {
            "seat_type_counts": {
                "default": "2",
                "usage_based": "1",
                "automation": "4",
            }
        }
        self.assertEqual(chatgpt_count_from_seat_counts(seat_counts), 2)

        capacity = chatgpt_seat_capacity(
            seats_entitled=3,
            seats_in_use=7,
            codex_count=1,
            active_chatgpt=chatgpt_count_from_seat_counts(seat_counts),
        )
        self.assertEqual(capacity.active_chatgpt, 2)
        self.assertEqual(capacity.available, 1)

    def test_member_seat_usage_is_authoritative_for_live_team_counts(self):
        usage = member_seat_usage_from_members_data({
            "members": [
                {"email": "owner@example.com", "seat_type": "usage_based", "status": "active"},
                {"email": "member@example.com", "seat_type": "default", "status": "active"},
            ],
            "pending_invites": [
                {"email": "pending@example.com", "seat_type": "default", "status": "pending"},
            ],
        })

        self.assertIsNotNone(usage)
        self.assertEqual(usage.seats_in_use_total, 2)
        self.assertEqual(usage.codex_count, 1)
        self.assertEqual(usage.active_chatgpt, 1)

    def test_member_seat_usage_treats_missing_seat_type_as_chatgpt(self):
        usage = member_seat_usage_from_members_data({
            "members": [
                {"email": "owner@example.com", "seat_type": None},
            ],
        })

        self.assertIsNotNone(usage)
        self.assertEqual(usage.seats_in_use_total, 1)
        self.assertEqual(usage.codex_count, 0)
        self.assertEqual(usage.active_chatgpt, 1)

    def test_member_seat_usage_does_not_count_automation_as_chatgpt(self):
        usage = member_seat_usage_from_members_data({
            "members": [
                {"email": "gpt@example.com", "seat_type": "default"},
                {"email": "codex@example.com", "seat_type": "usage_based"},
                {"email": "automation@example.com", "seat_type": "automation"},
            ],
        })

        self.assertIsNotNone(usage)
        self.assertEqual(usage.seats_in_use_total, 3)
        self.assertEqual(usage.codex_count, 1)
        self.assertEqual(usage.active_chatgpt, 1)


if __name__ == "__main__":
    unittest.main()
