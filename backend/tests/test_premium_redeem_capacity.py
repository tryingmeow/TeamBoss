"""兑换分配席位时的两处空位算多的回归测试（review S1 / S2）。

S1：现拉容量只读了第一页待接受邀请，名单缺失 / 结构不对时按 0 个算。空位其实已经被
    第二页上的邀请占着，或者藏在一份读坏了的回复后面，就会被再分配一次。现在待接受邀请
    分页拉全，拉不全 = 占用未知 = 没有空位，兑换拒绝且不消耗码。
S2：Premium 空位只看上游一个字段；没带 seat_type 的待接受邀请被当成 ChatGPT，藏住了
    Premium 占用；Premium 的预留只在内存里，重启就丢。现在 Premium 空位 =
    min(available, 已付 − 在用 − 待接受 Premium − 没带类型的待接受) − 预留，预留同时落库。

上游一律是只记录调用的假客户端，不会发出任何真实请求。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import direct_call
from _seat_fixtures import (
    NO_CHATGPT_SEAT,
    REDEEM_EMAIL as EMAIL,
    PagedClient,
    RedeemFlowCase,
    _filler,
    _premium_subscription,
)

from app import database as app_database
from app.routes import access_tokens
from app.services import seat_capacity as seat_capacity_module
from app.services import seat_holds, team_locks
from app.services.seat_capacity import (
    SeatCapacityFetchError,
    fetch_live_chatgpt_seat_capacity,
    fetch_live_seat_type_capacity,
)
from app.services.team_locks import reserve_default_seat, reserve_seat, reserved_seats

OTHER = "second.redeemer@example.com"


class _AsyncRun(unittest.TestCase):
    def _run(self, coro):
        with patch.object(seat_capacity_module, "run_chatgpt_call", new=direct_call):
            return asyncio.run(coro)


# ---- S1：待接受邀请必须拉全 --------------------------------------------------------


class PendingPaginationTest(_AsyncRun):
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
        with patch.object(seat_capacity_module, "MAX_PENDING_PAGES", 3):
            self._assert_unknown(endless)


# ---- S2：Premium 空位公式、没带类型的待接受邀请 -------------------------------------


class PremiumFreeFormulaTest(_AsyncRun):
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


# ---- S2：持久预留 -------------------------------------------------------------------


class PersistentReservationTest(unittest.TestCase):
    def setUp(self):
        team_locks._reservations.clear()
        self.addCleanup(team_locks._reservations.clear)
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = patch.object(app_database, "get_db_dir", return_value=tmpdir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        asyncio.run(app_database.init_database())

    def _hold_rows(self):
        conn = sqlite3.connect(app_database.get_db_path())
        try:
            return conn.execute(
                "SELECT team_id, email, seat_type FROM seat_holds ORDER BY email"
            ).fetchall()
        finally:
            conn.close()

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


# ---- 真实兑换流程 -------------------------------------------------------------------


class RedeemRefusesUnknownOccupancyTest(RedeemFlowCase):
    async def test_premium_vacancy_held_on_page_two_is_not_sold(self):
        await self._team()
        self._premium_upstream(
            pages=[
                {"items": _filler(100)},
                {"items": [{"email_address": OTHER, "seat_type": "prolite"}]},
            ]
        )
        await self._assert_refused_unconsumed(
            "atm_s1_page2", "prolite", access_tokens._NO_PREMIUM_SEAT_DETAIL
        )
        self.assertEqual(self.invites, [])
        self.notify_admins.assert_awaited_once()

    async def test_malformed_pending_reply_refuses_premium_with_notice(self):
        await self._team()
        self._premium_upstream(pages=[{"detail": "gateway challenge"}])
        await self._assert_refused_unconsumed(
            "atm_s1_malformed", "prolite", access_tokens._NO_PREMIUM_SEAT_DETAIL
        )
        self.assertEqual(self.invites, [])
        self.notify_admins.assert_awaited_once()
        text = self.notify_admins.await_args.args[0]
        self.assertIn("读不到席位容量", text)
        self.assertIn("兑换码未消耗", text)

    async def test_malformed_pending_reply_refuses_chatgpt(self):
        await self._team()
        self._chatgpt_upstream(entitled=5, active=1, pages=[{"items": None}])
        await self._assert_refused_unconsumed("atm_s1_cg_malformed", "default", NO_CHATGPT_SEAT)
        self.assertEqual(self.invites, [])
        logs = await self._rows(
            "SELECT error_message FROM operation_logs WHERE action = 'self_service_redeem'"
        )
        self.assertTrue(any("capacity_unknown" in (log["error_message"] or "") for log in logs))

    async def test_chatgpt_vacancy_held_on_page_two_is_not_sold(self):
        await self._team()
        self._chatgpt_upstream(
            entitled=2,
            active=1,
            pages=[
                {"items": _filler(100)},
                {"items": [{"email_address": OTHER, "seat_type": "default"}]},
            ],
        )
        await self._assert_refused_unconsumed("atm_s1_cg_page2", "default", NO_CHATGPT_SEAT)
        self.assertEqual(self.invites, [])

    async def test_untyped_pending_invite_blocks_premium_sale(self):
        await self._team()
        self._premium_upstream(pages=[{"items": [{"email_address": OTHER}]}])
        await self._assert_refused_unconsumed(
            "atm_s2_untyped", "prolite", access_tokens._NO_PREMIUM_SEAT_DETAIL
        )
        self.assertEqual(self.invites, [])


class RedeemPremiumHoldTest(RedeemFlowCase):
    async def test_uncertain_premium_hold_survives_restart(self):
        await self._team()
        self._premium_upstream(paid=1, available=1)
        self.invite_result = {"error": "timeout", "_mutation_status": "uncertain"}
        await self._token("atm_s2_uncertain", "prolite")
        result = await self._redeem("atm_s2_uncertain")
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(await self._hold_rows(), [{"team_id": "team-a", "email": EMAIL, "seat_type": "prolite"}])

        team_locks._reservations.clear()  # 进程重启：内存预留没了，库里的占用还在
        self.invite_result = {"_mutation_status": "confirmed"}
        await self._assert_refused_unconsumed(
            "atm_s2_second", "prolite", access_tokens._NO_PREMIUM_SEAT_DETAIL, email=OTHER
        )
        self.assertEqual(self.invites, [("team-a", EMAIL, "prolite")])

    async def test_premium_seat_is_held_before_the_invite_is_sent(self):
        await self._team()
        self._premium_upstream(paid=1, available=1)
        # 发邀请时进程被打断：结果不明，库里必须已经占着这个空位。
        self.invite_raises = RuntimeError("worker died mid-request")
        await self._token("atm_s2_inflight", "prolite")
        with self.assertRaises(RuntimeError):
            await self._redeem("atm_s2_inflight")
        self.assertEqual(self.holds_at_invite, [[("team-a", EMAIL, "prolite")]])

        team_locks._reservations.clear()
        self.invite_raises = None
        await self._assert_refused_unconsumed(
            "atm_s2_after_crash", "prolite", access_tokens._NO_PREMIUM_SEAT_DETAIL, email=OTHER
        )
        self.assertEqual(len(self.invites), 1)

    async def test_explicit_rejection_releases_the_hold(self):
        await self._team()
        self._premium_upstream(paid=1, available=1)
        self.invite_result = {"error": "seat type not allowed", "_mutation_status": "rejected"}
        await self._assert_refused_unconsumed(
            "atm_s2_rejected", "prolite", access_tokens._NO_PREMIUM_SEAT_DETAIL
        )
        self.assertEqual(self.holds_at_invite, [[("team-a", EMAIL, "prolite")]])
        self.assertEqual(await self._hold_rows(), [])
        self.assertEqual(await reserved_seats("team-a", "prolite"), 0)

    async def test_rejection_keeps_a_hold_that_existed_before(self):
        await self._team()
        self._premium_upstream(paid=2, available=2)
        await seat_holds.hold_seat("team-a", EMAIL, "prolite", source="admin")
        self.invite_result = {"error": "seat type not allowed", "_mutation_status": "rejected"}
        await self._assert_refused_unconsumed(
            "atm_s2_rejected_kept", "prolite", access_tokens._NO_PREMIUM_SEAT_DETAIL
        )
        self.assertEqual(len(await self._hold_rows()), 1)

    async def test_confirmed_premium_invite_keeps_a_persistent_hold(self):
        await self._team()
        self._premium_upstream(paid=2, available=2)
        await self._token("atm_s2_ok", "prolite")
        result = await self._redeem("atm_s2_ok")
        self.assertEqual(result["status"], "ok")
        team_locks._reservations.clear()
        self.assertEqual(await reserved_seats("team-a", "prolite"), 1)

    async def test_uncertain_chatgpt_invite_holds_a_default_seat(self):
        await self._team()
        self._chatgpt_upstream(entitled=2, active=0)
        self.invite_result = {"error": "timeout", "_mutation_status": "uncertain"}
        await self._token("atm_s2_cg_uncertain", "default")
        result = await self._redeem("atm_s2_cg_uncertain")
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(self.holds_at_invite, [[]])  # ChatGPT 发之前不占（行为照旧）
        team_locks._reservations.clear()
        self.assertEqual(await reserved_seats("team-a", "default"), 1)


if __name__ == "__main__":
    unittest.main()
