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


class SnapshotRowIdentityTests(unittest.TestCase):
    """每行要认得出是谁；同一份名单里 id / 邮箱不能重复（跨页也算）。"""

    def assert_incomplete(self, pages, *, limit=100):
        acc = SnapshotPageAccumulator("users", limit=limit)
        with self.assertRaises(SnapshotPageError):
            for page in pages:
                acc.add(page)

    def test_row_without_identity_is_incomplete(self):
        for row in (
            {},
            {"id": "", "email": ""},
            {"id": "   ", "user_id": " ", "email": "  ", "email_address": ""},
            {"id": None, "email": None, "role": "standard-user"},
        ):
            with self.subTest(row=row):
                self.assert_incomplete([{"items": _members(1) + [row], "total": 2}])

    def test_owner_twice_is_incomplete(self):
        owner = {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}
        self.assert_incomplete([{"items": [owner, dict(owner)], "total": 2}])

    def test_same_id_with_different_emails_is_incomplete(self):
        self.assert_incomplete([{
            "items": [{"id": "u1", "email": "a@example.com"}, {"id": "u1", "email": "b@example.com"}],
            "total": 2,
        }])

    def test_same_email_in_different_case_is_incomplete(self):
        self.assert_incomplete([{
            "items": [
                {"id": "u1", "email": "Same@Example.com"},
                {"id": "u2", "email": " same@example.COM "},
            ],
            "total": 2,
        }])

    def test_duplicate_split_across_pages_is_incomplete(self):
        first = _members(2)
        self.assert_incomplete(
            [
                {"items": first, "total": 3},
                {"items": [{"id": "u-new", "email": first[0]["email"].upper()}], "total": 3},
            ],
            limit=2,
        )

    def test_fallback_identity_fields_count_for_duplicates(self):
        # id 落空时取 user_id，email 落空时取 email_address。
        self.assert_incomplete([{
            "items": [{"user_id": "u1", "email": "a@example.com"}, {"id": "u1", "email": "b@example.com"}],
            "total": 2,
        }])
        acc = SnapshotPageAccumulator("invites", limit=100)
        with self.assertRaises(SnapshotPageError):
            acc.add({
                "invites": [{"email_address": "x@example.com"}, {"email": "X@example.com"}],
                "total": 2,
            })

    def test_rows_with_only_one_identity_field_are_fine(self):
        acc = SnapshotPageAccumulator("invites", limit=100)
        self.assertTrue(acc.add({
            "items": [{"email_address": "a@example.com"}, {"id": "inv-2"}, {"user_id": "u-3"}],
            "total": 3,
        }))

    def test_realistic_multi_page_list_is_complete(self):
        owner = {"id": "user-owner", "email": "Owner@Example.com", "role": "account-owner"}
        rest = _members(204)
        rows = [owner] + rest
        acc = SnapshotPageAccumulator("users", limit=100)
        self.assertFalse(acc.add({"items": rows[:100], "total": 205, "limit": 100, "offset": 0}))
        self.assertFalse(acc.add({"items": rows[100:200], "total": 205, "limit": 100, "offset": 100}))
        self.assertTrue(acc.add({"items": rows[200:], "total": 205, "limit": 100, "offset": 200}))
        self.assertEqual(len(acc.items), 205)


class RequiredItemsTests(unittest.TestCase):
    """成员名单（require_items=True）拉完不能是空的；邀请名单可以。"""

    def test_member_list_that_completes_empty_is_incomplete(self):
        for pages in (
            [{"items": [], "total": 0}],
            [{"items": []}],
            [{"users": [], "total": None}],
        ):
            with self.subTest(pages=pages):
                acc = SnapshotPageAccumulator("users", limit=100, require_items=True)
                with self.assertRaises(SnapshotPageError) as ctx:
                    for page in pages:
                        acc.add(page)
                self.assertEqual(ctx.exception.reason, "empty member list")

    def test_member_list_with_the_owner_is_complete(self):
        acc = SnapshotPageAccumulator("users", limit=100, require_items=True)
        owner = {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}
        self.assertTrue(acc.add({"items": [owner], "total": 1}))

    def test_empty_trailing_page_without_total_is_fine_once_rows_were_seen(self):
        acc = SnapshotPageAccumulator("users", limit=2, require_items=True)
        self.assertFalse(acc.add({"items": _members(2)}))
        self.assertTrue(acc.add({"items": []}))

    def test_invite_list_may_be_empty(self):
        self.assertTrue(SnapshotPageAccumulator("invites", limit=100).add({"items": [], "total": 0}))
        self.assertTrue(SnapshotPageAccumulator("invites", limit=100).add({"items": []}))


if __name__ == "__main__":
    unittest.main()
