"""Premium 席位核心（注册表、分类型容量、预留、迁移）的独立测试。

现拉容量：待接受邀请分页拉全，拉不全 / 结构不对 = 占用未知（SeatCapacityFetchError），绝不按 0 算。
Premium 空位 = min(available, 已付 − 在用 − 待接受 Premium − 没带类型的待接受) − 预留；
Premium 预留同时落库（seat_holds），ChatGPT 预留只在内存。
"""
import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import direct_call, start_temp_db, temp_db_dir
from _seat_fixtures import PagedClient, _filler, _premium_subscription

from app import database as app_database
from app import seat_types
from app.services import seat_capacity, seat_holds, team_locks
from app.services.seat_capacity import (
    SeatCapacityFetchError,
    billed_free_seats,
    chatgpt_seat_capacity,
    fetch_live_chatgpt_seat_capacity,
    fetch_live_seat_type_capacity,
    parse_seat_capacity,
)
from app.services.team_locks import reserve_default_seat, reserve_seat, reserved_seats

OTHER = "second.redeemer@example.com"


class FakeClient:
    def __init__(self, subscription, seat_counts=None, pending=None):
        self.subscription = subscription
        self.seat_counts = seat_counts if seat_counts is not None else {"seat_type_counts": {}}
        self.pending = pending if pending is not None else {"items": []}

    def get_subscription(self):
        return self.subscription

    def get_seat_type_counts(self):
        return self.seat_counts

    def get_pending_invites(self, offset=0, limit=100):
        return self.pending


def _cap(seat_type, paid, available):
    return {"type": seat_type, "paid": paid, "held": 0, "available": available}


class BilledFreeSeatsTest(unittest.TestCase):
    def test_min_rule_old_formula_lower(self):
        entries = {"default": {"paid": 5, "available": 4}}
        self.assertEqual(
            billed_free_seats("default", entries=entries, pending=0, legacy_available=1), 1
        )

    def test_min_rule_per_type_lower(self):
        entries = {"default": {"paid": 5, "available": 1}}
        self.assertEqual(
            billed_free_seats("default", entries=entries, pending=0, legacy_available=4), 1
        )

    def test_pending_is_subtracted_from_per_type_only(self):
        entries = {"default": {"paid": 5, "available": 3}}
        self.assertEqual(
            billed_free_seats("default", entries=entries, pending=2, legacy_available=9), 1
        )

    def test_only_one_source_known(self):
        entries = {"default": {"paid": 2, "available": 2}}
        self.assertEqual(billed_free_seats("default", entries=entries, pending=0), 2)
        self.assertEqual(billed_free_seats("default", entries=None, legacy_available=3), 3)
        self.assertEqual(billed_free_seats("default", entries=None), 0)

    def test_premium_without_entry_is_zero(self):
        self.assertEqual(billed_free_seats("prolite", entries={}, pending=0), 0)
        self.assertEqual(billed_free_seats("prolite", entries=None, legacy_available=9), 0)
        self.assertEqual(
            billed_free_seats(
                "prolite", entries={"prolite": {"paid": 2, "available": 2}}, pending=1
            ),
            1,
        )

    def test_unknown_and_codex_are_zero(self):
        entries = {
            "automation": {"paid": 3, "available": 3},
            "usage_based": {"paid": 3, "available": 3},
        }
        self.assertEqual(billed_free_seats("automation", entries=entries, legacy_available=5), 0)
        self.assertEqual(billed_free_seats("usage_based", entries=entries, legacy_available=5), 0)


class ParseSeatCapacityTest(unittest.TestCase):
    def test_missing_or_non_list_is_unknown(self):
        self.assertIsNone(parse_seat_capacity({}))
        self.assertIsNone(parse_seat_capacity({"seat_capacity": {"type": "default"}}))
        self.assertIsNone(parse_seat_capacity(None))

    def test_malformed_entries_are_dropped(self):
        parsed = parse_seat_capacity(
            {
                "seat_capacity": [
                    "junk",
                    {"type": "", "paid": 1, "available": 1},
                    {"type": "default", "paid": "2", "available": 1},
                    {"type": "default", "paid": 2, "available": -1},
                    {"type": "prolite", "paid": 1, "available": 0},
                ]
            }
        )
        self.assertEqual(parsed, {"prolite": {"paid": 1, "available": 0}})

    def test_bool_values_are_rejected(self):
        parsed = parse_seat_capacity(
            {"seat_capacity": [{"type": "default", "paid": True, "available": True}]}
        )
        self.assertEqual(parsed, {})

    def test_duplicate_types_keep_smaller_available(self):
        parsed = parse_seat_capacity(
            {"seat_capacity": [_cap("default", 5, 3), _cap("default", 5, 1), _cap("default", 5, 2)]}
        )
        self.assertEqual(parsed, {"default": {"paid": 5, "available": 1}})


class ChatGPTSeatCapacityTest(unittest.TestCase):
    def test_per_type_smaller_wins(self):
        cap = chatgpt_seat_capacity(
            seats_entitled=10,
            seats_in_use=2,
            codex_count=0,
            active_chatgpt=2,
            pending_default=0,
            seat_capacity={"default": {"paid": 4, "available": 1}},
        )
        self.assertEqual(cap.legacy_available, 8)
        self.assertEqual(cap.per_type_available, 1)
        self.assertEqual(cap.available, 1)

    def test_legacy_smaller_wins(self):
        cap = chatgpt_seat_capacity(
            seats_entitled=3,
            seats_in_use=2,
            codex_count=0,
            active_chatgpt=2,
            seat_capacity={"default": {"paid": 3, "available": 3}},
        )
        self.assertEqual(cap.available, 1)

    def test_without_seat_capacity_is_old_formula(self):
        cap = chatgpt_seat_capacity(
            seats_entitled=3, seats_in_use=2, codex_count=0, active_chatgpt=2
        )
        self.assertEqual(cap.available, 1)
        self.assertIsNone(cap.per_type_available)


class _LiveCapacityCase(unittest.TestCase):
    def _run(self, coro):
        with patch.object(seat_capacity, "run_chatgpt_call", new=direct_call):
            return asyncio.run(coro)


class LiveCapacityTest(_LiveCapacityCase):
    def test_live_default_uses_lower_per_type_value(self):
        client = FakeClient(
            {
                "seats_entitled": 10,
                "seats_in_use": 2,
                "seat_capacity": [_cap("default", 10, 1)],
            },
            {"seat_type_counts": {"default": 2}},
        )
        capacity, *_ = self._run(fetch_live_chatgpt_seat_capacity(client))
        self.assertEqual(capacity.available, 1)

    def test_live_default_falls_back_without_seat_capacity(self):
        client = FakeClient(
            {"seats_entitled": 10, "seats_in_use": 2},
            {"seat_type_counts": {"default": 2}},
        )
        capacity, *_ = self._run(fetch_live_chatgpt_seat_capacity(client))
        self.assertEqual(capacity.available, 8)

    def test_premium_missing_entry_is_zero(self):
        client = FakeClient(
            {"seats_entitled": 5, "seat_capacity": [_cap("default", 5, 5)]},
        )
        capacity, *_ = self._run(fetch_live_seat_type_capacity(client, "prolite"))
        self.assertEqual(capacity.available, 0)
        self.assertIsNone(capacity.paid)

    def test_premium_subtracts_pending_prolite_invites(self):
        client = FakeClient(
            {"seats_entitled": 5, "seat_capacity": [_cap("prolite", 3, 3)]},
            {"seat_type_counts": {"prolite": 0}},
            {
                "items": [
                    {"email_address": "a@example.com", "seat_type": "prolite"},
                    {"email_address": "b@example.com", "seat_type": "default"},
                ]
            },
        )
        capacity, *_ = self._run(fetch_live_seat_type_capacity(client, "prolite"))
        self.assertEqual(capacity.pending, 1)
        self.assertEqual(capacity.available, 2)

    def test_read_error_fails_closed(self):
        for broken in ("subscription", "counts", "pending"):
            client = FakeClient({"seat_capacity": [_cap("prolite", 3, 3)]})
            if broken == "subscription":
                client.subscription = {"error": "boom"}
            elif broken == "counts":
                client.seat_counts = {"error": "boom"}
            else:
                client.pending = {"error": "boom"}
            with self.assertRaises(SeatCapacityFetchError):
                self._run(fetch_live_seat_type_capacity(client, "prolite"))

    def test_non_billed_types_raise_value_error(self):
        client = FakeClient({"seat_capacity": []})
        for seat_type in ("usage_based", "automation"):
            with self.assertRaises(ValueError):
                self._run(fetch_live_seat_type_capacity(client, seat_type))


class PendingPaginationTest(_LiveCapacityCase):
    def test_premium_vacancy_held_by_invite_on_page_two_is_not_free(self):
        # 唯一的 Premium 空位已经被第二页上的邀请占着；上游 available 没扣它。
        client = PagedClient(
            _premium_subscription(paid=1, available=1),
            {"default": 0, "usage_based": 0, "prolite": 0},
            [
                {"items": _filler(100)},
                {"items": [{"email_address": OTHER, "seat_type": "prolite"}]},
            ],
        )
        capacity, _sub, _counts, pending = self._run(
            fetch_live_seat_type_capacity(client, "prolite")
        )
        self.assertEqual(capacity.pending, 1)
        self.assertEqual(capacity.available, 0)
        self.assertEqual(client.pending_calls, [(0, 100), (100, 100)])
        self.assertEqual(pending["total"], 101)
        self.assertEqual(len(pending["items"]), 101)

    def test_default_vacancy_held_by_invite_on_page_two_is_not_free(self):
        client = PagedClient(
            {"seats_entitled": 2, "seats_in_use": 1},
            {"default": 1, "usage_based": 0},
            [
                {"items": _filler(100), "total": 101},
                {"items": [{"email_address": OTHER, "seat_type": "default"}], "total": 101},
            ],
        )
        capacity, *_ = self._run(fetch_live_chatgpt_seat_capacity(client))
        self.assertEqual(capacity.pending_default, 1)
        self.assertEqual(capacity.available, 0)

    def test_total_reached_on_a_full_page_stops_without_another_request(self):
        client = PagedClient(
            {"seats_entitled": 200, "seats_in_use": 0},
            {"default": 0, "usage_based": 0},
            [{"items": _filler(100, "default"), "total": 100}],
        )
        capacity, *_ = self._run(fetch_live_chatgpt_seat_capacity(client))
        self.assertEqual(capacity.pending_default, 100)
        self.assertEqual(client.pending_calls, [(0, 100)])

    def _assert_unknown(self, pages, seat_type="prolite"):
        client = PagedClient(
            _premium_subscription(paid=3, available=3),
            {"default": 0, "usage_based": 0, "prolite": 0},
            pages,
        )
        with self.assertRaises(SeatCapacityFetchError):
            self._run(fetch_live_seat_type_capacity(client, seat_type))

    def test_malformed_pending_reply_is_unknown_not_zero(self):
        for reply in ({}, {"items": None}, {"data": []}, [], "oops", None):
            with self.subTest(reply=reply):
                self._assert_unknown([reply])
                self._assert_unknown([reply], seat_type="default")

    def test_non_object_entry_is_unknown(self):
        self._assert_unknown([{"items": [{"email_address": OTHER}, "x"]}])

    def test_short_page_before_reported_total_is_unknown(self):
        self._assert_unknown(
            [
                {"items": _filler(100), "total": 150},
                {"items": _filler(20, prefix="more"), "total": 150},
            ]
        )

    def test_error_on_a_later_page_is_unknown(self):
        self._assert_unknown([{"items": _filler(100)}, {"error": "upstream 502"}])
        self._assert_unknown([{"items": _filler(100)}, {"detail": "challenge"}])

    def test_page_cap_reached_is_unknown(self):
        endless = [{"items": _filler(100, prefix=f"p{n}-")} for n in range(5)]
        with patch.object(seat_capacity, "MAX_PENDING_PAGES", 3):
            self._assert_unknown(endless)


class PremiumFreeFormulaTest(_LiveCapacityCase):
    def _premium(self, *, paid, available, in_use, pending=()):
        counts = {"default": 0, "usage_based": 0}
        if in_use is not None:
            counts["prolite"] = in_use
        client = PagedClient(
            _premium_subscription(paid=paid, available=available),
            counts,
            [{"items": list(pending)}],
        )
        capacity, *_ = self._run(fetch_live_seat_type_capacity(client, "prolite"))
        return capacity

    def test_untyped_pending_invite_hides_premium_occupancy(self):
        capacity = self._premium(
            paid=1, available=1, in_use=0, pending=[{"email_address": OTHER}]
        )
        self.assertEqual(capacity.pending, 1)
        self.assertEqual(capacity.pending_untyped, 1)
        self.assertEqual(capacity.available, 0)

    def test_untyped_pending_invite_counts_for_every_billed_type(self):
        for missing in ({}, {"seat_type": None}, {"seat_type": "  "}, {"seat_type": 7}):
            with self.subTest(missing=missing):
                invite = {"email_address": OTHER, **missing}
                premium = self._premium(paid=2, available=2, in_use=0, pending=[invite])
                self.assertEqual(premium.available, 1)
                client = PagedClient(
                    {"seats_entitled": 2, "seats_in_use": 1},
                    {"default": 1, "usage_based": 0},
                    [{"items": [invite]}],
                )
                default, *_ = self._run(fetch_live_chatgpt_seat_capacity(client))
                self.assertEqual(default.pending_default, 1)
                self.assertEqual(default.available, 0)

    def test_paid_minus_occupancy_bounds_a_stale_available(self):
        # 上游 available 还说有 1 个空位，但已付 2 个、2 个在用：没有空位。
        capacity = self._premium(paid=2, available=1, in_use=2)
        self.assertEqual(capacity.available, 0)

    def test_available_bounds_paid_minus_occupancy(self):
        capacity = self._premium(paid=3, available=1, in_use=0)
        self.assertEqual(capacity.available, 1)

    def test_pending_still_comes_off_the_upstream_available(self):
        # 加了占用上界之后，available − 待接受 这一项照旧（不比修之前松）。
        capacity = self._premium(
            paid=5,
            available=1,
            in_use=0,
            pending=[{"email_address": OTHER, "seat_type": "prolite"}],
        )
        self.assertEqual(capacity.available, 0)

    def test_missing_premium_count_is_zero(self):
        capacity = self._premium(paid=2, available=2, in_use=None)
        self.assertEqual(capacity.available, 0)

    def test_chatgpt_invite_still_does_not_eat_a_premium_seat(self):
        capacity = self._premium(
            paid=1,
            available=1,
            in_use=0,
            pending=[{"email_address": OTHER, "seat_type": "default"}],
        )
        self.assertEqual(capacity.available, 1)


class ReservationsTest(unittest.TestCase):
    def setUp(self):
        team_locks._reservations.clear()
        self.addCleanup(team_locks._reservations.clear)
        # Premium 预留另在库里占一份（seat_holds），需要一个空库。
        start_temp_db(self)

    def _hold_rows(self):
        conn = sqlite3.connect(app_database.get_db_path())
        try:
            return conn.execute(
                "SELECT team_id, email, seat_type FROM seat_holds ORDER BY email"
            ).fetchall()
        finally:
            conn.close()

    def test_reservations_are_counted_per_type(self):
        async def scenario():
            await team_locks.reserve_seat("t1", "a@example.com", "prolite")
            await team_locks.reserve_seat("t1", "b@example.com", "default")
            await team_locks.reserve_default_seat("t1", "c@example.com")
            await team_locks.reserve_seat("t2", "d@example.com", "prolite")
            return (
                await team_locks.reserved_seats("t1", "prolite"),
                await team_locks.reserved_seats("t1", "default"),
                await team_locks.reserved_seats("t1", "default", exclude_email="B@example.com"),
                await team_locks.reserved_default_seats("t1"),
                await team_locks.reserved_seats("t1", "automation"),
            )

        self.assertEqual(asyncio.run(scenario()), (1, 2, 1, 2, 0))

    def test_premium_hold_survives_cleared_in_memory_reservations(self):
        asyncio.run(reserve_seat("t1", "A@example.com", "prolite"))
        team_locks._reservations.clear()  # 进程重启
        self.assertEqual(asyncio.run(reserved_seats("t1", "prolite")), 1)
        self.assertEqual(asyncio.run(reserved_seats("t1", "default")), 0)
        self.assertEqual(self._hold_rows(), [("t1", "a@example.com", "prolite")])

    def test_memory_and_db_count_once_per_email_and_respect_exclude(self):
        async def scenario():
            await reserve_seat("t1", "a@example.com", "prolite")
            await seat_holds.hold_seat("t1", "b@example.com", "prolite")
            return (
                await reserved_seats("t1", "prolite"),
                await reserved_seats("t1", "prolite", exclude_email="B@example.com"),
                await reserved_seats("t1", "prolite", exclude_email="a@example.com"),
                await reserved_seats("t2", "prolite"),
            )

        self.assertEqual(asyncio.run(scenario()), (2, 1, 1, 0))

    def test_default_reservation_stays_in_memory_only(self):
        asyncio.run(reserve_default_seat("t1", "a@example.com"))
        asyncio.run(reserve_seat("t1", "b@example.com", "default"))
        self.assertEqual(self._hold_rows(), [])
        self.assertEqual(asyncio.run(reserved_seats("t1", "default")), 2)


class SeatTypesHelpersTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(seat_types.seat_type_label("default"), "ChatGPT")
        self.assertEqual(seat_types.seat_type_label("usage_based"), "Codex")
        self.assertEqual(seat_types.seat_type_label("prolite"), "Premium")
        self.assertEqual(seat_types.seat_type_label("automation"), "其他（automation）")
        self.assertEqual(seat_types.seat_type_label(None), "ChatGPT")

    def test_normalize_never_maps_unknown_to_default(self):
        self.assertEqual(seat_types.normalize_seat_type(None), "default")
        self.assertEqual(seat_types.normalize_seat_type("  "), "default")
        self.assertEqual(seat_types.normalize_seat_type("automation"), "automation")
        self.assertFalse(seat_types.is_known_seat_type("automation"))
        self.assertFalse(seat_types.is_billed_seat_type("automation"))
        self.assertTrue(seat_types.is_billed_seat_type("prolite"))
        self.assertFalse(seat_types.is_billed_seat_type("usage_based"))

    def test_normalize_overage_policy(self):
        normalize = seat_types.normalize_overage_policy
        self.assertEqual(normalize(None), "confirm")
        self.assertEqual(normalize(""), "confirm")
        self.assertEqual(normalize("AUTO"), "auto")
        self.assertEqual(normalize("forbid"), "forbid")
        self.assertEqual(normalize("whatever"), "forbid")


class OveragePolicyMigrationTest(unittest.TestCase):
    def setUp(self):
        temp_db_dir(self)  # no schema yet: each test runs init_database() itself
        self.db_path = app_database.get_db_path()

    def _sql(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def _insert_team(self, team_id):
        self._sql(
            "INSERT INTO teams (id, name, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', '2026-10-01', '2026-10-01')",
            (team_id, team_id),
        )

    def _policies(self):
        return dict(self._sql("SELECT id, overage_policy FROM teams"))

    def test_fresh_db_defaults_to_confirm(self):
        asyncio.run(app_database.init_database())
        self._insert_team("t1")
        self.assertEqual(self._policies(), {"t1": "confirm"})

    def _legacy_db(self, skip_value):
        asyncio.run(app_database.init_database())
        self._insert_team("t1")
        self._insert_team("t2")
        self._sql("ALTER TABLE teams DROP COLUMN overage_policy")
        self._sql(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES "
            "('skip_overage_confirmation', ?, '2026-10-01')",
            (skip_value,),
        )

    def test_global_skip_true_turns_every_team_to_auto_once(self):
        self._legacy_db("true")
        asyncio.run(app_database.init_database())
        self.assertEqual(self._policies(), {"t1": "auto", "t2": "auto"})
        # The retired global toggle is reset so a stale pre-upgrade tab stops
        # auto-confirming overage from it.
        self.assertEqual(
            self._sql("SELECT value FROM settings WHERE key = 'skip_overage_confirmation'"),
            [("false",)],
        )

        self._sql("UPDATE teams SET overage_policy = 'forbid' WHERE id = 't1'")
        asyncio.run(app_database.init_database())
        self.assertEqual(self._policies(), {"t1": "forbid", "t2": "auto"})

    def test_global_skip_false_keeps_confirm(self):
        self._legacy_db("false")
        asyncio.run(app_database.init_database())
        self.assertEqual(self._policies(), {"t1": "confirm", "t2": "confirm"})


if __name__ == "__main__":
    unittest.main()
