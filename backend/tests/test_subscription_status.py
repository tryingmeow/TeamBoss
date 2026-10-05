import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.subscription_status import (
    subscription_status,
    subscription_status_display,
)


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


class SubscriptionStatusDisplayTest(unittest.TestCase):
    """陈旧快照不判到期（2026-08-20 用户拍板）。

    同步连续 48h 没有一次全量成功时，active_until 是冻结的旧值，
    展示层要报 stale（数据未同步）而不是拿旧值说「已到期」误导。
    """

    def setUp(self):
        self.now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def test_stale_snapshot_suppresses_expired(self):
        self.assertEqual(
            subscription_status_display(
                "2026-08-18T10:00:00Z", True, "2026-08-18T10:00:00Z", now=self.now
            ),
            "stale",
        )

    def test_never_synced_snapshot_is_stale_when_past_due(self):
        self.assertEqual(
            subscription_status_display("2026-08-18T10:00:00Z", True, None, now=self.now),
            "stale",
        )

    def test_fresh_snapshot_keeps_genuine_expiry(self):
        self.assertEqual(
            subscription_status_display(
                "2026-08-20T10:00:00Z", True, "2026-08-20T11:00:00Z", now=self.now
            ),
            "expired",
        )

    def test_stale_snapshot_with_future_expiry_stays_normal(self):
        # 旧数据但订阅明显还没到期：照常显示续费状态，不额外报 stale。
        self.assertEqual(
            subscription_status_display(
                "2026-09-18T10:00:00Z", False, "2026-08-01T10:00:00Z", now=self.now
            ),
            "nonrenewing",
        )


if __name__ == "__main__":
    unittest.main()
