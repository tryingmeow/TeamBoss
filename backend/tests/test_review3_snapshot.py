"""成员 / 邀请快照与实时容量的第三轮复核修复的端到端回归测试。

- F2：名单里每一行都要认得出是谁，id / 邮箱不能重复；否则整份名单不完整，异步刷新保留
  上一份缓存和席位占用，调度器的数据同步不关任何 member_expiry 行。
- F3：空的 /users 回复（``{"items": [], "total": 0}``）不完整——真实名单里至少有 owner。
  数据同步不关行、异步刷新不动缓存和占用、踢人监视不收尾、到期踢人的查找不说「人不在」、
  patrol 严格模式的刷新失败。空的邀请名单照常有效。
- F1：卖座前现拉的待接受邀请和快照同一套完整性规则。第一页 100 条、total 101，第二页空、
  total 100（翻页期间少了一个邀请，Premium 邀请挪到了第一页范围里）：占用未知，兑换按没有
  空位拒绝、不消耗码。
- N3：预留层没有不比版本就删持久占用的出口。

累加器本身的规则在 test_snapshot_pages.py。上游一律是只记录调用的假客户端，绝不触网。
"""

from test_review2_holds import (  # noqa: I001  (_isolation first, via the support module)
    EMAIL,
    TEAM,
    USER_ID,
    _Db,
    _ListClient,
    _iso,
    _patched,
)
from test_premium_overage_support import direct_call
from test_premium_patrol import OLD, RecordingClient, _live, _member
from test_review_fix_premium_patrol import _Fixture as _PatrolFixture
from test_review_fix_premium_capacity import (
    NO_CHATGPT_SEAT,
    PagedClient,
    _filler,
    _premium_subscription,
    _RedeemFlow,
)

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app import member_cache_service
from app import scheduler as app_scheduler
from app.services import patrol as patrol_service
from app.routes import access_tokens
from app.services import seat_capacity as seat_capacity_module
from app.services import team_health_alerts, team_locks, tg_notify, tg_summary
from app.services.seat_capacity import (
    SeatCapacityFetchError,
    fetch_all_pending_invites,
    fetch_live_chatgpt_seat_capacity,
    fetch_live_seat_type_capacity,
)
from app.services.snapshot_pages import SnapshotPageAccumulator

OWNER = {
    "id": "user-owner-r3",
    "email": "owner-r3@example.com",
    "role": "account-owner",
    "seat_type": "default",
}


EMPTY_USERS = [{"items": [], "total": 0}]


def _owner_twice():
    return [{"items": [dict(OWNER), dict(OWNER)], "total": 2}]


def _owner_only():
    return [{"items": [dict(OWNER)], "total": 1}]


class _SyncClient:
    """调度器用的同步假客户端：成员 / 邀请按 offset 回预设分页，没有任何写接口。"""

    member_pages: list = []
    invite_pages: list = [{"items": [], "total": 0}]

    def __init__(self, access_token=None, team_id=None, device_id=None, proxy_url=None):
        self.team_id = team_id

    @staticmethod
    def _page(pages, offset, limit):
        index = offset // limit
        return json.loads(json.dumps(pages[index])) if index < len(pages) else {"items": []}

    def get_subscription(self):
        return {"seats_entitled": 5, "seats_in_use": 1, "billing_currency": "USD"}

    def get_seat_type_counts(self):
        return {"seat_type_counts": {"default": 1, "usage_based": 0, "prolite": 0}}

    def get_members(self, offset=0, limit=100):
        return self._page(type(self).member_pages, offset, limit)

    def get_pending_invites(self, offset=0, limit=100):
        return self._page(type(self).invite_pages, offset, limit)


class _SnapshotCase(_Db):
    """一个 Team：缓存里有 EMAIL、一条未关的 member_expiry、一小时前占下的 prolite 席位。

    一份**完整**且 EMAIL 不在里面的名单会关掉那条 member_expiry、放掉那个占用；
    不完整的名单两样都不能动。
    """

    SEEDED_AT = "2026-09-01T00:00:00+00:00"
    PREVIOUS_START = "2026-10-01T00:00:00.000000+00:00"

    def setUp(self):
        super().setUp()
        self.insert_team(
            TEAM,
            members=[{"id": USER_ID, "email": EMAIL, "seat_type": "prolite", "status": "active"}],
        )
        conn = self._conn()
        conn.execute(
            "UPDATE member_cache SET fetch_started_at = ? WHERE team_id = ?",
            (self.PREVIOUS_START, TEAM),
        )
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES (?, ?, ?, NULL, 0, 0, ?, 'system', ?)""",
            (TEAM, USER_ID, EMAIL, self.SEEDED_AT, self.SEEDED_AT),
        )
        # 展示字段刚同步过：数据同步这一轮只拉执行要用的接口。
        conn.execute(
            "UPDATE teams SET display_synced_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), TEAM),
        )
        conn.commit()
        conn.close()
        self.put_hold(EMAIL, "prolite", _iso(datetime.now(timezone.utc) - timedelta(hours=1)))
        _SyncClient.member_pages = []
        _SyncClient.invite_pages = [{"items": [], "total": 0}]

    def expiry_row(self):
        conn = self._conn()
        row = conn.execute(
            "SELECT kicked, kick_source FROM member_expiry WHERE team_id = ? AND email = ?",
            (TEAM, EMAIL),
        ).fetchone()
        conn.close()
        return dict(row)

    # ── 异步刷新（member_cache_service） ──

    def refresh_async(self, client):
        """跑一次异步刷新；返回 (异常或 None)。"""
        patches = [
            patch.object(member_cache_service, "run_chatgpt_call", new=direct_call),
            patch.object(team_health_alerts, "report_team_failure", new=AsyncMock()),
            patch.object(team_health_alerts, "report_team_recovery", new=AsyncMock()),
        ]
        with _patched(patches):
            try:
                asyncio.run(member_cache_service.fetch_and_cache_members(TEAM, client))
            except HTTPException as exc:
                return exc
        return None

    def assert_async_refresh_changes_nothing(self, client, detail_prefix):
        before_cache, before_holds = self.cache_row(), self.holds()
        self.assertEqual(len(before_holds), 1)

        exc = self.refresh_async(client)

        self.assertIsNotNone(exc, "an incomplete list must fail the refresh")
        self.assertEqual(exc.status_code, 502)
        self.assertTrue(exc.detail.startswith(detail_prefix), exc.detail)
        self.assertEqual(self.cache_row(), before_cache)
        self.assertEqual(self.holds(), before_holds)

    # ── 调度器数据同步 ──

    def run_data_sync(self, member_pages, invite_pages=None):
        _SyncClient.member_pages = member_pages
        if invite_pages is not None:
            _SyncClient.invite_pages = invite_pages
        with patch.object(app_scheduler, "ChatGPTClient", _SyncClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync",
                          lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_recovery_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_failure_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None), \
             patch.object(patrol_service, "run_patrol", MagicMock(return_value={})), \
             patch.object(tg_notify, "notify_admins_sync", lambda *a, **kw: None), \
             patch.object(tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None):
            app_scheduler.data_sync_job()

    def snapshot_failure(self):
        conn = self._conn()
        row = conn.execute(
            """SELECT error_message FROM operation_logs
               WHERE action = 'data_sync' AND detail = 'member snapshot refresh failed'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        return row["error_message"] if row else None

    def assert_data_sync_changes_nothing(self, member_pages, invite_pages=None, error_prefix="members: "):
        before_cache, before_holds = self.cache_row(), self.holds()

        self.run_data_sync(member_pages, invite_pages)

        failure = self.snapshot_failure()
        self.assertIsNotNone(failure, "the round must treat the snapshot as a failed fetch")
        self.assertTrue(failure.startswith(error_prefix), failure)
        self.assertEqual(self.expiry_row(), {"kicked": 0, "kick_source": None})
        self.assertEqual(self.cache_row(), before_cache)
        self.assertEqual(self.holds(), before_holds)


# ═══ F2：行身份与重复 ═══════════════════════════════════════════════════════════

class DuplicateOwnerSnapshotTest(_SnapshotCase):
    """上游把 owner 回了两遍：条数凑满 total，但 EMAIL 其实没拉到——不能据此判他不在。"""

    def test_async_refresh_keeps_the_cache_and_the_hold(self):
        self.assert_async_refresh_changes_nothing(
            _ListClient(member_pages=_owner_twice()), "Failed to fetch members: "
        )

    def test_async_refresh_rejects_an_identity_less_row(self):
        self.assert_async_refresh_changes_nothing(
            _ListClient(member_pages=[{"items": [dict(OWNER), {}], "total": 2}]),
            "Failed to fetch members: ",
        )

    def test_async_refresh_rejects_a_duplicated_invite(self):
        invite = {"id": "inv-1", "email_address": "Invited@Example.com", "seat_type": "default"}
        twin = {"id": "inv-2", "email_address": "invited@example.com", "seat_type": "default"}
        self.assert_async_refresh_changes_nothing(
            _ListClient(
                member_pages=[{"items": [dict(OWNER)], "total": 1}],
                invite_pages=[{"items": [invite, twin], "total": 2}],
            ),
            "Failed to fetch pending invites: ",
        )

    def test_data_sync_closes_nothing(self):
        self.assert_data_sync_changes_nothing(_owner_twice())

    def test_data_sync_still_closes_the_row_on_a_clean_list(self):
        self.run_data_sync([{"items": [dict(OWNER)], "total": 1}])
        self.assertIsNone(self.snapshot_failure())
        self.assertEqual(self.expiry_row(), {"kicked": 1, "kick_source": "detected"})
        self.assertEqual(self.holds(), [])


# ═══ F3：空的成员名单不完整 ═════════════════════════════════════════════════════

class EmptyMemberListTest(_SnapshotCase):
    def test_data_sync_closes_nothing(self):
        self.assert_data_sync_changes_nothing(EMPTY_USERS)
        self.assertIn("empty member list", self.snapshot_failure())

    def test_async_refresh_keeps_the_cache_and_the_hold(self):
        self.assert_async_refresh_changes_nothing(
            _ListClient(member_pages=EMPTY_USERS), "Failed to fetch members: empty member list"
        )

    def test_auto_kick_member_lookup_does_not_report_gone(self):
        _SyncClient.member_pages = EMPTY_USERS
        with patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)):
            user_id, error = app_scheduler._find_member_user_id_by_email(_SyncClient(), EMAIL)
        self.assertIsNone(user_id)
        self.assertEqual(error, "empty member list")

    def test_kick_watch_is_not_concluded(self):
        # 「人已经走了」的踢人监视：一份空名单不能证明他不在，监视留着、缓存和占用不动。
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_watch (team_id, reason, target_email, target_user_id,
                                         started_at, expires_at, done)
               VALUES (?, 'kick', ?, ?, ?, ?, 0)""",
            (
                TEAM, EMAIL, USER_ID, datetime.now(timezone.utc).isoformat(),
                (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        before_cache, before_holds = self.cache_row(), self.holds()
        _SyncClient.member_pages = EMPTY_USERS

        with patch.object(app_scheduler, "ChatGPTClient", _SyncClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "edit_message_sync", lambda *a, **kw: None):
            app_scheduler.member_watch_job()

        conn = self._conn()
        done = conn.execute("SELECT done FROM member_watch WHERE team_id = ?", (TEAM,)).fetchone()["done"]
        conn.close()
        self.assertEqual(done, 0)
        self.assertEqual(self.cache_row(), before_cache)
        self.assertEqual(self.holds(), before_holds)

    def test_empty_invite_list_is_still_a_complete_snapshot(self):
        self.assertIsNone(
            self.refresh_async(_ListClient(member_pages=_owner_only(), invite_pages=[{"items": [], "total": 0}]))
        )
        cached = json.loads(self.cache_row()["members_json"])
        self.assertEqual([m["email"] for m in cached], [OWNER["email"]])
        self.assertEqual(json.loads(self.cache_row()["pending_json"]), [])
        self.assertEqual(self.holds(), [])

    def test_every_member_pagination_requires_rows(self):
        # 每一处 /users 分页都打开 require_items；邀请名单的分页不打开。
        seen = []
        real = SnapshotPageAccumulator.__init__

        def spy(acc, *keys, limit, require_items=False):
            seen.append((keys, require_items))
            real(acc, *keys, limit=limit, require_items=require_items)

        with patch.object(SnapshotPageAccumulator, "__init__", spy):
            self.refresh_async(_ListClient(member_pages=_owner_only()))
            self.run_data_sync(_owner_only())
        self.assertTrue(seen)
        for keys, require_items in seen:
            with self.subTest(keys=keys):
                self.assertEqual(require_items, "users" in keys)


class StrictRefreshEmptyMemberListTest(_PatrolFixture):
    """严格模式：一个早过了等待期的 ChatGPT 外部成员，刷新成功就会被踢。"""

    TEAM_ID = "team-r3-empty"

    def setUp(self):
        super().setUp()
        owner = _member("owner@example.com", "u-owner", seat_type=None, is_owner=True, source=None)
        keeper = _member("keeper@example.com", "u-k", source="system")
        plain = _member("plain@example.com", "u-d", first_seen_at=OLD)
        self._armed_team(self.TEAM_ID, [owner, keeper, plain], seats_entitled=99)
        self._setting("patrol_strict_mode_enabled", "1")
        self._hold(self.TEAM_ID, "keeper@example.com")

    def test_empty_reply_fails_the_refresh_and_kicks_nobody(self):
        RecordingClient.live_members = []
        before = self._cache_row(self.TEAM_ID)

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        failed = [e for e in result["events"] if e.get("action") == "strict_refresh_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], "empty member list")
        self.assertEqual(len(self._logs("patrol_strict_refresh_failed")), 1)
        self.assertEqual(self._cache_row(self.TEAM_ID), before)
        self.assertEqual(self._holds(self.TEAM_ID), {"keeper@example.com"})

    def test_the_same_team_with_a_real_reply_is_kicked(self):
        RecordingClient.live_members = [
            {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"},
            _live("keeper@example.com", "u-k", "default"),
            _live("plain@example.com", "u-d", "default"),
        ]

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-d")])


# ═══ F1：卖座前的待接受邀请读取 ═════════════════════════════════════════════════

# 读第一页时有 101 个邀请（第 101 个是 Premium 邀请）；读第二页之前少了一个，Premium 邀请
# 挪到 offset 99，第二页是空的、total 100。拼起来的 100 条里没有那个 Premium 邀请。
SHIFTED_INVITE_PAGES = [
    {"items": _filler(100), "total": 101},
    {"items": [], "total": 100},
]


def _premium_counts():
    return {"default": 0, "usage_based": 0, "prolite": 0}


class CapacityPendingReadTest(unittest.TestCase):
    def _run(self, coro):
        with patch.object(seat_capacity_module, "run_chatgpt_call", new=direct_call):
            return asyncio.run(coro)

    def test_total_changing_between_pages_is_unknown(self):
        client = PagedClient({}, {}, SHIFTED_INVITE_PAGES)
        with self.assertRaises(SeatCapacityFetchError) as ctx:
            self._run(fetch_all_pending_invites(client))
        self.assertIn("total changed between pages", str(ctx.exception))
        self.assertEqual(client.pending_calls, [(0, 100), (100, 100)])

    def test_premium_and_chatgpt_capacity_are_unknown(self):
        premium = PagedClient(_premium_subscription(paid=1, available=1), _premium_counts(),
                              SHIFTED_INVITE_PAGES)
        with self.assertRaises(SeatCapacityFetchError):
            self._run(fetch_live_seat_type_capacity(premium, "prolite"))
        chatgpt = PagedClient({"seats_entitled": 2, "seats_in_use": 1},
                              {"default": 1, "usage_based": 0}, SHIFTED_INVITE_PAGES)
        with self.assertRaises(SeatCapacityFetchError):
            self._run(fetch_live_chatgpt_seat_capacity(chatgpt))

    def test_identity_rules_apply_to_capacity_reads(self):
        for label, pages in {
            "duplicate invite email": [{"items": [
                {"email_address": "dup@example.com", "seat_type": "prolite"},
                {"email_address": "DUP@example.com", "seat_type": "prolite"},
            ], "total": 2}],
            "identity-less invite": [{"items": [{"seat_type": "prolite"}], "total": 1}],
            "more invites than the total": [{"items": _filler(3), "total": 2}],
        }.items():
            with self.subTest(reply=label):
                with self.assertRaises(SeatCapacityFetchError):
                    self._run(fetch_all_pending_invites(PagedClient({}, {}, pages)))

    def test_page_cap_is_unknown(self):
        endless = [{"items": _filler(100, prefix=f"cap{n}-")} for n in range(5)]
        with patch.object(seat_capacity_module, "MAX_PENDING_PAGES", 3):
            with self.assertRaises(SeatCapacityFetchError) as ctx:
                self._run(fetch_all_pending_invites(PagedClient({}, {}, endless)))
        self.assertIn("paging limit", str(ctx.exception))

    def test_upstream_error_text_is_kept(self):
        with self.assertRaises(SeatCapacityFetchError) as ctx:
            self._run(fetch_all_pending_invites(PagedClient({}, {}, [{"error": "HTTP 401 Unauthorized"}])))
        self.assertEqual(str(ctx.exception), "HTTP 401 Unauthorized")

    def test_consistent_pages_still_read_in_full(self):
        pages = [
            {"items": _filler(100), "total": 101},
            {"items": [{"email_address": "held@example.com", "seat_type": "prolite"}], "total": 101},
        ]
        capacity, _sub, _counts, pending = self._run(fetch_live_seat_type_capacity(
            PagedClient(_premium_subscription(paid=1, available=1), _premium_counts(), pages), "prolite"
        ))
        self.assertEqual(pending["total"], 101)
        self.assertEqual(capacity.pending, 1)
        self.assertEqual(capacity.available, 0)


class RedeemFailsClosedOnShiftingInvitesTest(_RedeemFlow):
    async def test_premium_seat_is_not_sold(self):
        await self._team()
        self._premium_upstream(paid=1, available=1, pages=SHIFTED_INVITE_PAGES)
        await self._assert_refused_unconsumed(
            "atm_f1_premium", "prolite", access_tokens._NO_PREMIUM_SEAT_DETAIL
        )
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._hold_rows(), [])

    async def test_chatgpt_seat_is_not_sold(self):
        await self._team()
        self._chatgpt_upstream(entitled=2, active=1, pages=SHIFTED_INVITE_PAGES)
        await self._assert_refused_unconsumed("atm_f1_chatgpt", "default", NO_CHATGPT_SEAT)
        self.assertEqual(self.invites, [])



# ═══ N3：预留层不删持久占用 ═════════════════════════════════════════════════════

class ReservationLayerTest(unittest.TestCase):
    def test_no_versionless_hold_release_in_the_reservation_layer(self):
        # 持久占用只在两处放掉：上游明确拒绝（兑换流程直接调 seat_holds.release_seat_hold）
        # 和按版本（seat_type + created_at）删除的对账。预留层那个连库里占用一起、不比版本
        # 就删的出口没人调用，留着只会被误用成「放掉这个人的占用」。
        self.assertFalse(hasattr(team_locks, "release_seat_reservation"))

if __name__ == "__main__":
    unittest.main()
