import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.subscription_status import subscription_status


class SubscriptionStatusTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

    def test_renewing_before_end(self):
        self.assertEqual(
            subscription_status("2026-08-01T00:00:00Z", True, now=self.now),
            "renewing",
        )

    def test_nonrenewing_remains_active_until_end(self):
        self.assertEqual(
            subscription_status("2026-08-01T00:00:00Z", False, now=self.now),
            "nonrenewing",
        )

    def test_expired_is_separate_from_renewal_flag(self):
        self.assertEqual(
            subscription_status("2026-07-22T11:59:59Z", True, now=self.now),
            "expired",
        )


if __name__ == "__main__":
    unittest.main()
