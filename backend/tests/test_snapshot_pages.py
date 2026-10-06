"""成员 / 邀请名单分页完整性判定（services/snapshot_pages.py）的单元测试。纯函数，不发任何请求。"""

import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.snapshot_pages import SnapshotPageAccumulator, SnapshotPageError


def _members(n, start=0):
    return [{"id": f"u{i}", "email": f"u{i}@example.com"} for i in range(start, start + n)]


class SnapshotPageAccumulatorTests(unittest.TestCase):
    def test_empty_page_with_positive_total_is_incomplete(self):
        pages = SnapshotPageAccumulator("users", limit=100)
        with self.assertRaises(SnapshotPageError):
            pages.add({"items": [], "total": 1})

    def test_exact_total_on_one_page_is_complete(self):
        pages = SnapshotPageAccumulator("users", limit=100)
        self.assertTrue(pages.add({"items": _members(3), "total": 3}))
        self.assertEqual(len(pages.items), 3)

    def test_empty_team_with_zero_total_is_complete(self):
        pages = SnapshotPageAccumulator(limit=100)
        self.assertTrue(pages.add({"items": [], "total": 0}))

    def test_more_entries_than_total_is_incomplete(self):
        pages = SnapshotPageAccumulator(limit=100)
        with self.assertRaises(SnapshotPageError):
            pages.add({"items": _members(3), "total": 2})

    def test_multi_page_total_must_match_exactly(self):
        pages = SnapshotPageAccumulator(limit=2)
        self.assertFalse(pages.add({"items": _members(2), "total": 3}))
        self.assertEqual(pages.next_offset, 2)
        self.assertTrue(pages.add({"items": _members(1, 2), "total": 3}))

    def test_short_page_before_total_is_incomplete(self):
        pages = SnapshotPageAccumulator(limit=2)
        self.assertFalse(pages.add({"items": _members(2), "total": 5}))
        with self.assertRaises(SnapshotPageError):
            pages.add({"items": _members(1, 2), "total": 5})

    def test_total_changing_between_pages_is_incomplete(self):
        pages = SnapshotPageAccumulator(limit=2)
        pages.add({"items": _members(2), "total": 4})
        with self.assertRaises(SnapshotPageError):
            pages.add({"items": _members(1, 2), "total": 3})

    def test_total_appearing_mid_sequence_is_incomplete(self):
        pages = SnapshotPageAccumulator(limit=2)
        pages.add({"items": _members(2)})
        with self.assertRaises(SnapshotPageError):
            pages.add({"items": _members(1, 2), "total": 3})

    def test_no_total_short_page_ends_the_list(self):
        pages = SnapshotPageAccumulator(limit=2)
        self.assertFalse(pages.add({"items": _members(2)}))
        self.assertTrue(pages.add({"items": []}))
        self.assertEqual(len(pages.items), 2)

    def test_null_total_counts_as_not_reported(self):
        pages = SnapshotPageAccumulator(limit=100)
        self.assertTrue(pages.add({"items": _members(1), "total": None}))

    def test_malformed_pages_are_rejected(self):
        for data in (
            None,
            [],
            "x",
            {},
            {"items": None},
            {"items": ["a@example.com"]},
            {"items": _members(1), "total": "1"},
            {"items": _members(1), "total": True},
            {"items": _members(1), "total": -1},
        ):
            with self.subTest(data=data):
                with self.assertRaises(SnapshotPageError):
                    SnapshotPageAccumulator(limit=100).add(data)

    def test_page_larger_than_limit_is_rejected(self):
        with self.assertRaises(SnapshotPageError):
            SnapshotPageAccumulator(limit=2).add({"items": _members(3)})

    def test_error_page_carries_upstream_error(self):
        with self.assertRaises(SnapshotPageError) as ctx:
            SnapshotPageAccumulator(limit=100).add({"error": "HTTP 401"})
        self.assertEqual(ctx.exception.upstream_error, "HTTP 401")

    def test_fallback_key_is_read(self):
        pages = SnapshotPageAccumulator("invites", limit=100)
        self.assertTrue(pages.add({"invites": [{"email_address": "a@example.com"}], "total": 1}))


if __name__ == "__main__":
    unittest.main()
