"""持久席位占用与成员快照的第二轮复核修复（H1–H4）的回归测试。

- H1：人在名单里不算证据。一份完整快照里这个邮箱带着被占的那个类型才放掉；不在、或者是
  别的类型，要等拉取开始时间至少晚于占下时间 15 分钟的完整快照仍看不见这个类型才放。
- H2：异步刷新和调度器的分页都按 SnapshotPageAccumulator 判完整；``{"items": [], "total": 1}``
  这类不完整的名单不写缓存、不对账、不关 member_expiry、不让到期踢人的查找说「人不在」。
- H3：对账只删选中的那个版本（seat_type + created_at）；选中之后被重新占过的行留着。
- H4：到期时间的就地修改是对读到那一版的比较后交换；读写之间落地的新快照不会被盖掉，
  修改落在新快照上。

上游一律是只记录调用的假客户端，绝不触网。
"""

from test_premium_overage_support import (  # noqa: I001  (_isolation first)
    FakeTeamClient,
    TempDbMixin,
    capacity_entries,
    direct_call,
)

import asyncio
import contextlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app import member_cache_service
from app import scheduler as app_scheduler
from app.models import ChangeSeatRequest
from app.routes import members
from app.services import patrol as patrol_service
from app.services import seat_capacity, seat_holds, team_health_alerts, team_locks, tg_notify, tg_summary

TEAM = "r2-holds-team"
USER_ID = "user-switch"
EMAIL = "switch.member@example.com"
OTHER = "other.member@example.com"
SNAPSHOT_START = "2026-10-06T12:00:00.000000+00:00"
GRACE = timedelta(seconds=seat_holds.HOLD_ABSENT_GRACE_SECONDS)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def _at(base: str, delta: timedelta) -> str:
    return _iso(datetime.fromisoformat(base) + delta)


def _live(email, user_id, seat_type, *, role="standard-user"):
    return {"id": user_id, "email": email, "seat_type": seat_type, "role": role}


class _Db(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._start_db()
        team_locks._reservations.clear()
        self.addCleanup(team_locks._reservations.clear)

    def put_hold(self, email, seat_type, created_at, team_id=TEAM):
        conn = self._conn()
        conn.execute(
            """INSERT INTO seat_holds (team_id, email, seat_type, source, created_at)
               VALUES (?, ?, ?, 'test', ?)""",
            (team_id, email, seat_type, created_at),
        )
        conn.commit()
        conn.close()

    def holds(self, team_id=TEAM):
        conn = self._conn()
        rows = conn.execute(
            "SELECT email, seat_type, created_at FROM seat_holds WHERE team_id = ? ORDER BY email",
            (team_id,),
        ).fetchall()
        conn.close()
        return [tuple(row) for row in rows]

    def cache_row(self, team_id=TEAM):
        conn = self._conn()
        row = conn.execute(
            "SELECT members_json, pending_json, updated_at, fetch_started_at FROM member_cache "
            "WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None


class _ListClient(FakeTeamClient):
    """读接口按 offset 回预设的成员 / 邀请分页；change_seat_type 超时（结果不明）。"""

    def __init__(self, *, member_pages=None, invite_pages=None, **kwargs):
        super().__init__(**kwargs)
        self.member_pages = member_pages if member_pages is not None else []
        self.invite_pages = invite_pages if invite_pages is not None else [{"items": [], "total": 0}]
        self.switch_result = {"error": "Request timed out"}

    @staticmethod
    def _page(pages, offset, limit):
        index = offset // limit
        if index < len(pages):
            return json.loads(json.dumps(pages[index]))
        return {"items": []}

    def get_members(self, offset=0, limit=100):
        self.reads.append("get_members")
        return self._page(self.member_pages, offset, limit)

    def get_pending_invites(self, offset=0, limit=100):
        self.reads.append("get_pending_invites")
        return self._page(self.invite_pages, offset, limit)

    def change_seat_type(self, user_id, seat_type):
        self.mutations.append(("change_seat_type", user_id, seat_type))
        return dict(self.switch_result)

    def show(self, *people):
        self.member_pages = [{"items": list(people), "total": len(people)}]


def _upstream_patches(client):
    return [
        patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
        patch.object(members, "run_chatgpt_call", new=direct_call),
        patch.object(members, "add_member_watch", new=AsyncMock()),
        patch.object(seat_capacity, "run_chatgpt_call", new=direct_call),
        patch.object(member_cache_service, "run_chatgpt_call", new=direct_call),
        patch.object(team_health_alerts, "report_team_failure", new=AsyncMock()),
        patch.object(team_health_alerts, "report_team_recovery", new=AsyncMock()),
    ]


@contextlib.contextmanager
def _patched(patches):
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


# ═══ H1：只有带着被占类型出现才放，否则走 15 分钟规则 ═══════════════════════════

class SwitchTimeoutHoldTest(_Db):
    """一次 Codex → Premium 切换超时，留下一个 Premium 占用，之后的完整快照怎么处理它。"""

    def setUp(self):
        super().setUp()
        self.insert_team(
            TEAM,
            policy="auto",
            members=[{"id": USER_ID, "email": EMAIL, "seat_type": "usage_based", "status": "active"}],
        )
        self.track_reservation(TEAM, EMAIL)
        self.client = _ListClient(
            seats_entitled=2,
            counts={"default": 1, "usage_based": 1},
            seat_capacity=capacity_entries(default=(2, 1), prolite=(1, 1)),
        )
        self.client.show(_live(EMAIL, USER_ID, "usage_based"))

    def _switch_times_out(self):
        with _patched(_upstream_patches(self.client)):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    members.change_seat(TEAM, USER_ID, ChangeSeatRequest(seat_type="prolite"))
                )
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("切换结果不明确", raised.exception.detail)
        self.assertEqual(self.client.mutations, [("change_seat_type", USER_ID, "prolite")])
        rows = self.holds()
        self.assertEqual([(email, seat) for email, seat, _ in rows], [(EMAIL, "prolite")])
        return rows[0][2]

    def _refresh(self, *, fetch_started_at=None):
        patches = _upstream_patches(self.client)
        if fetch_started_at is not None:
            patches.append(
                patch.object(
                    member_cache_service, "snapshot_fetch_started_now", return_value=fetch_started_at
                )
            )
        with _patched(patches):
            return asyncio.run(member_cache_service.fetch_and_cache_members(TEAM, self.client))

    def test_member_still_codex_keeps_the_hold_until_a_snapshot_15_minutes_later(self):
        created_at = self._switch_times_out()

        # 下一份完整快照里这个人还是 Codex：人在，但不是 Premium，占用不能放。
        self._refresh()
        self.assertEqual(self.holds(), [(EMAIL, "prolite", created_at)])

        # 拉取开始时间离占下差一秒不到 15 分钟：仍然留着。
        self._refresh(fetch_started_at=_at(created_at, GRACE - timedelta(seconds=1)))
        self.assertEqual(self.holds(), [(EMAIL, "prolite", created_at)])

        # 拉取开始时间至少晚 15 分钟、仍然是 Codex：切换没落地，放掉。
        self._refresh(fetch_started_at=_at(created_at, GRACE))
        self.assertEqual(self.holds(), [])

    def test_member_shown_as_premium_releases_the_hold(self):
        self._switch_times_out()

        self.client.show(_live(EMAIL, USER_ID, "prolite"))
        self._refresh()

        self.assertEqual(self.holds(), [])


class ReleaseRuleTest(_Db):
    """契约第 3 节逐条：对一份完整快照（拉取开始 S），一行占用 (T, C) 放不放。"""

    def _reconcile(self, members_list, pending_list):
        conn = self._conn()
        try:
            released = seat_holds.reconcile_seat_holds_sync(
                conn, TEAM, members_list, pending_list, SNAPSHOT_START
            )
            conn.commit()
        finally:
            conn.close()
        return released

    def _case(self, seat_type, age, members_list, pending_list):
        self.put_hold(EMAIL, seat_type, _at(SNAPSHOT_START, -age))
        released = self._reconcile(members_list, pending_list)
        left = self.holds()
        conn = self._conn()
        conn.execute("DELETE FROM seat_holds")
        conn.commit()
        conn.close()
        return released, left

    def test_rule(self):
        young = timedelta(minutes=10)
        member = lambda seat: [{"id": USER_ID, "email": EMAIL, "seat_type": seat}]  # noqa: E731
        invite = lambda seat: [{"id": "inv-1", "email": EMAIL, "seat_type": seat}]  # noqa: E731
        cases = [
            # (说明, 占的类型, 占下多久, members, pending, 放不放)
            ("premium shown as premium member", "prolite", young, member("prolite"), [], True),
            ("premium shown as premium invite", "prolite", young, [], invite("prolite"), True),
            ("premium shown as codex member", "prolite", young, member("usage_based"), [], False),
            ("premium shown as chatgpt member", "prolite", young, member("default"), [], False),
            # 不带类型的待接受邀请：缓存里记成 default，不能证明 Premium。
            ("premium vs untyped invite (cached)", "prolite", young, [], invite("default"), False),
            ("premium vs untyped invite (raw)", "prolite", young, [],
             [{"email_address": EMAIL.upper()}], False),
            ("premium absent, young", "prolite", young, [], [], False),
            ("premium absent, exactly 15 min", "prolite", GRACE, [], [], True),
            ("premium shown as codex, 15 min", "prolite", GRACE, member("usage_based"), [], True),
            # 拉取开始之后才占的：快照反映不了它，带着类型出现也不放。
            ("held after the fetch started", "prolite", -timedelta(seconds=1),
             member("prolite"), [], False),
            # 兑换结果不明时占的 ChatGPT 席位，规则一样。
            ("default shown as default", "default", young, member("default"), [], True),
            ("default shown with missing type", "default", young, [{"email": EMAIL}], [], True),
            ("default shown as codex", "default", young, member("usage_based"), [], False),
            ("default absent, 15 min", "default", GRACE, [], [], True),
        ]
        for label, seat, age, members_list, pending_list, expect_release in cases:
            with self.subTest(case=label):
                released, left = self._case(seat, age, members_list, pending_list)
                self.assertEqual(released, 1 if expect_release else 0)
                self.assertEqual(len(left), 0 if expect_release else 1)

    def test_unparsable_times_keep_the_hold(self):
        self.put_hold(EMAIL, "prolite", "not-a-time")
        conn = self._conn()
        try:
            self.assertEqual(
                seat_holds.reconcile_seat_holds_sync(conn, TEAM, [], [], SNAPSHOT_START), 0
            )
            self.assertEqual(seat_holds.reconcile_seat_holds_sync(conn, TEAM, [], [], "garbage"), 0)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(len(self.holds()), 1)


# ═══ H3：只删选中的那个版本 ═══════════════════════════════════════════════════════

class VersionExactDeleteTest(_Db):
    def test_hold_seat_returns_the_stored_version(self):
        async def scenario():
            first = await seat_holds.hold_seat(TEAM, EMAIL.upper(), "prolite", source="test")
            stored_first = self.holds()
            second = await seat_holds.hold_seat(TEAM, EMAIL, "default", source="test")
            return first, stored_first, second

        first, stored_first, second = asyncio.run(scenario())
        self.assertEqual(stored_first, [(EMAIL, "prolite", first)])
        self.assertEqual(self.holds(), [(EMAIL, "default", second)])
        self.assertNotEqual(first, second)
        # 固定带微秒：同一秒里的两次占用也是两个版本。
        self.assertRegex(first, r"T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")
        self.assertIsNone(asyncio.run(seat_holds.hold_seat(TEAM, "  ", "prolite")))

    def _rehold_after_selection(self, seat_type):
        """在对账选完要放的行之后、删除之前，另一个请求重新占了这个邮箱。"""
        original = seat_holds._holds_to_release
        replaced = {}

        def selecting_then_reheld(*args, **kwargs):
            chosen = original(*args, **kwargs)
            self.assertEqual(len(chosen), 1)  # 这一行被选中要放掉
            replaced["created_at"] = _iso(datetime.now(timezone.utc))
            other = self._conn()
            other.execute(
                """INSERT INTO seat_holds (team_id, email, seat_type, source, created_at)
                   VALUES (?, ?, ?, 'reservation', ?)
                   ON CONFLICT(team_id, email) DO UPDATE SET
                       seat_type = excluded.seat_type,
                       source = excluded.source,
                       created_at = excluded.created_at""",
                (TEAM, EMAIL, seat_type, replaced["created_at"]),
            )
            other.commit()
            other.close()
            return chosen

        return patch.object(seat_holds, "_holds_to_release", selecting_then_reheld), replaced

    def _old_absent_hold(self):
        # 一小时前占的、名单里没有：这份快照会选中它。
        self.put_hold(EMAIL, "prolite", _iso(datetime.now(timezone.utc) - timedelta(hours=1)))

    def test_async_reconcile_keeps_a_replacement_hold(self):
        for seat_type in ("prolite", "default"):
            with self.subTest(replacement=seat_type):
                self._old_absent_hold()
                started = _iso(datetime.now(timezone.utc))
                patcher, replaced = self._rehold_after_selection(seat_type)
                with patcher:
                    released = asyncio.run(seat_holds.reconcile_seat_holds(TEAM, [], [], started))
                self.assertEqual(released, 0)
                self.assertEqual(self.holds(), [(EMAIL, seat_type, replaced["created_at"])])
                conn = self._conn()
                conn.execute("DELETE FROM seat_holds")
                conn.commit()
                conn.close()

    def test_sync_reconcile_keeps_a_replacement_hold(self):
        for seat_type in ("prolite", "default"):
            with self.subTest(replacement=seat_type):
                self._old_absent_hold()
                started = _iso(datetime.now(timezone.utc))
                patcher, replaced = self._rehold_after_selection(seat_type)
                conn = self._conn()
                try:
                    with patcher:
                        released = seat_holds.reconcile_seat_holds_sync(conn, TEAM, [], [], started)
                    conn.commit()
                finally:
                    conn.close()
                self.assertEqual(released, 0)
                self.assertEqual(self.holds(), [(EMAIL, seat_type, replaced["created_at"])])
                conn = self._conn()
                conn.execute("DELETE FROM seat_holds")
                conn.commit()
                conn.close()

    def test_unreplaced_selected_hold_is_still_released(self):
        self._old_absent_hold()
        started = _iso(datetime.now(timezone.utc))
        self.assertEqual(asyncio.run(seat_holds.reconcile_seat_holds(TEAM, [], [], started)), 1)
        self.assertEqual(self.holds(), [])


# ═══ H2：不完整的名单什么都不能证明 ═══════════════════════════════════════════════

def _others(count, prefix="filler"):
    return [
        {"id": f"u-{prefix}{i}", "email": f"{prefix}{i}@example.com", "seat_type": "usage_based"}
        for i in range(count)
    ]


# 每一项都是目标邮箱不在里面、但名单并不完整的回复（按 offset 分页）。
INCOMPLETE_MEMBER_PAGES = {
    "empty page claiming one entry": [{"items": [], "total": 1}],
    "more entries than the total": [{"items": _others(2), "total": 1}],
    "total changed between pages": [
        {"items": _others(100), "total": 101},
        {"items": [], "total": 100},
    ],
}


class AsyncRefreshRejectsIncompleteSnapshotTest(_Db):
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
        conn.commit()
        conn.close()
        # 一小时前占下、名单里不在：一份完整快照会放掉它，不完整的不能。
        self.put_hold(EMAIL, "prolite", _iso(datetime.now(timezone.utc) - timedelta(hours=1)))

    def _refresh(self, client):
        failure = AsyncMock()
        patches = [
            patch.object(member_cache_service, "run_chatgpt_call", new=direct_call),
            patch.object(team_health_alerts, "report_team_failure", new=failure),
            patch.object(team_health_alerts, "report_team_recovery", new=AsyncMock()),
        ]
        with _patched(patches):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(member_cache_service.fetch_and_cache_members(TEAM, client))
        failure.assert_awaited_once()
        return raised.exception

    def test_incomplete_member_list_fails_the_refresh_and_changes_nothing(self):
        for label, pages in INCOMPLETE_MEMBER_PAGES.items():
            with self.subTest(reply=label):
                before_cache, before_holds = self.cache_row(), self.holds()

                exc = self._refresh(_ListClient(member_pages=pages))

                self.assertEqual(exc.status_code, 502)
                self.assertTrue(exc.detail.startswith("Failed to fetch members: "), exc.detail)
                self.assertEqual(self.cache_row(), before_cache)
                self.assertEqual(self.holds(), before_holds)
                self.assertEqual(len(before_holds), 1)

    def test_incomplete_invite_list_fails_the_refresh(self):
        owner = _live(f"owner-{TEAM}@example.com", "u-owner", "default", role="account-owner")
        exc = self._refresh(
            _ListClient(
                member_pages=[{"items": [owner], "total": 1}],
                invite_pages=[{"items": [], "total": 1}],
            )
        )
        self.assertTrue(exc.detail.startswith("Failed to fetch pending invites: "), exc.detail)
        self.assertEqual(len(self.holds()), 1)

    def test_upstream_error_page_keeps_its_error_text(self):
        exc = self._refresh(_ListClient(member_pages=[{"error": "HTTP 401 Unauthorized"}]))
        self.assertEqual(exc.detail, "Failed to fetch members: HTTP 401 Unauthorized")
        self.assertTrue(team_health_alerts.is_auth_error(exc))


class _SyncClient:
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
        return {"seat_type_counts": {"default": 0, "usage_based": 0, "prolite": 1}}

    def get_members(self, offset=0, limit=100):
        return self._page(type(self).member_pages, offset, limit)

    def get_pending_invites(self, offset=0, limit=100):
        return self._page(type(self).invite_pages, offset, limit)


class SchedulerRejectsIncompleteSnapshotTest(_Db):
    SEEDED_AT = "2026-09-01T00:00:00+00:00"

    def setUp(self):
        super().setUp()
        self.insert_team(
            TEAM,
            members=[{"id": USER_ID, "email": EMAIL, "seat_type": "prolite", "status": "active"}],
        )
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES (?, ?, ?, NULL, 0, 0, ?, 'system', ?)""",
            (TEAM, USER_ID, EMAIL, self.SEEDED_AT, self.SEEDED_AT),
        )
        # 展示字段刚同步过：这一轮只拉执行要用的接口（订阅、席位数、成员、邀请）。
        conn.execute(
            "UPDATE teams SET display_synced_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), TEAM),
        )
        conn.commit()
        conn.close()
        self.put_hold(EMAIL, "prolite", _iso(datetime.now(timezone.utc) - timedelta(hours=1)))

    def _expiry_row(self):
        conn = self._conn()
        row = conn.execute(
            "SELECT kicked, kick_source FROM member_expiry WHERE team_id = ? AND email = ?",
            (TEAM, EMAIL),
        ).fetchone()
        conn.close()
        return dict(row)

    def _run_data_sync(self, member_pages):
        _SyncClient.member_pages = member_pages
        run_patrol = MagicMock(return_value={})
        with patch.object(app_scheduler, "ChatGPTClient", _SyncClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync",
                          lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_recovery_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_failure_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None), \
             patch.object(patrol_service, "run_patrol", run_patrol), \
             patch.object(tg_notify, "notify_admins_sync", lambda *a, **kw: None), \
             patch.object(tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None):
            app_scheduler.data_sync_job()

    def test_data_sync_closes_nothing_on_an_incomplete_member_list(self):
        for label, pages in INCOMPLETE_MEMBER_PAGES.items():
            with self.subTest(reply=label):
                before_cache, before_holds = self.cache_row(), self.holds()

                self._run_data_sync(pages)

                # 这一轮走到了拉名单，并且按拉取失败处理。
                conn = self._conn()
                failure = conn.execute(
                    """SELECT error_message FROM operation_logs
                       WHERE action = 'data_sync' AND detail = 'member snapshot refresh failed'
                       ORDER BY id DESC LIMIT 1"""
                ).fetchone()
                conn.execute("DELETE FROM operation_logs")
                conn.commit()
                conn.close()
                self.assertIsNotNone(failure)
                self.assertTrue(failure["error_message"].startswith("members: "))
                self.assertEqual(self._expiry_row(), {"kicked": 0, "kick_source": None})
                self.assertEqual(self.cache_row(), before_cache)
                self.assertEqual(self.holds(), before_holds)

    def test_data_sync_still_closes_a_row_on_a_complete_list(self):
        self._run_data_sync([{"items": _others(1), "total": 1}])
        self.assertEqual(self._expiry_row(), {"kicked": 1, "kick_source": "detected"})
        self.assertEqual(self.holds(), [])

    def test_auto_kick_lookups_do_not_report_gone(self):
        direct = patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw))
        with direct:
            for label, pages in INCOMPLETE_MEMBER_PAGES.items():
                with self.subTest(reply=label, lookup="member"):
                    _SyncClient.member_pages = pages
                    user_id, error = app_scheduler._find_member_user_id_by_email(_SyncClient(), EMAIL)
                    self.assertIsNone(user_id)
                    self.assertTrue(error)
                with self.subTest(reply=label, lookup="invite"):
                    _SyncClient.invite_pages = [
                        {**page, "items": [{**item, "email_address": item["email"]} for item in page["items"]]}
                        for page in pages
                    ]
                    try:
                        exists, error = app_scheduler._pending_invite_exists(_SyncClient(), EMAIL)
                    finally:
                        _SyncClient.invite_pages = [{"items": [], "total": 0}]
                    self.assertFalse(exists)
                    self.assertTrue(error)

            # 上游报错页原样返回它的 error；error 为空时也不能被当成成功。
            _SyncClient.member_pages = [{"error": "HTTP 401 Unauthorized"}]
            self.assertEqual(
                app_scheduler._find_member_user_id_by_email(_SyncClient(), EMAIL),
                (None, "HTTP 401 Unauthorized"),
            )
            _SyncClient.member_pages = [{"error": None}]
            user_id, error = app_scheduler._find_member_user_id_by_email(_SyncClient(), EMAIL)
            self.assertIsNone(user_id)
            self.assertTrue(error)


# ═══ H4：到期时间的就地修改不盖掉更新的快照 ══════════════════════════════════════

class ExpiryEditRaceTest(_Db):
    OLD_START = "2026-10-06T10:00:00.000000+00:00"
    NEW_START = "2026-10-06T10:05:00.000000+00:00"

    def setUp(self):
        super().setUp()
        self.insert_team(
            TEAM,
            members=[{"id": USER_ID, "email": EMAIL, "seat_type": "usage_based",
                      "expires_at": "old-expiry", "status": "active"}],
        )
        conn = self._conn()
        conn.execute(
            "UPDATE member_cache SET fetch_started_at = ? WHERE team_id = ?", (self.OLD_START, TEAM)
        )
        conn.commit()
        conn.close()

    def _newer_snapshot(self):
        """另一次刷新拿到的新名单：这个人已经是 Premium，还多了一个人。"""
        conn = self._conn()
        member_cache_service.store_member_snapshot_sync(
            conn,
            TEAM,
            [
                {"id": USER_ID, "email": EMAIL, "seat_type": "prolite",
                 "expires_at": "old-expiry", "status": "active"},
                {"id": "u-other", "email": OTHER, "seat_type": "default",
                 "expires_at": None, "status": "active"},
            ],
            [],
            self.NEW_START,
        )
        conn.commit()
        conn.close()

    def _racing_get_db(self):
        """包住 get_db：这次修改的 UPDATE 到库之前，先让另一次刷新的快照落地（只一次）。"""
        real_get_db = member_cache_service.get_db
        fired = []
        test = self

        class RacingDb:
            def __init__(self, db):
                self._db = db

            async def execute(self, sql, *args, **kwargs):
                if not fired and sql.lstrip().upper().startswith("UPDATE MEMBER_CACHE"):
                    fired.append(True)
                    test._newer_snapshot()
                return await self._db.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._db, name)

        @contextlib.asynccontextmanager
        async def racing_get_db():
            async with real_get_db() as db:
                yield RacingDb(db)

        return patch.object(member_cache_service, "get_db", racing_get_db), fired

    def test_newer_snapshot_written_between_read_and_write_is_kept(self):
        patcher, fired = self._racing_get_db()
        with patcher:
            asyncio.run(
                member_cache_service.update_cached_member_expiry(
                    TEAM, user_id=USER_ID, email=EMAIL, expires_at="new-expiry"
                )
            )
        self.assertEqual(fired, [True])

        row = self.cache_row()
        self.assertEqual(row["fetch_started_at"], self.NEW_START)
        cached = {m["email"]: m for m in json.loads(row["members_json"])}
        self.assertEqual(set(cached), {EMAIL, OTHER})
        self.assertEqual(cached[EMAIL]["seat_type"], "prolite")
        # 修改落在新快照上。
        self.assertEqual(cached[EMAIL]["expires_at"], "new-expiry")
        self.assertIsNone(cached[OTHER]["expires_at"])

    def test_edit_without_a_race_still_applies(self):
        asyncio.run(
            member_cache_service.update_cached_member_expiry(
                TEAM, user_id=USER_ID, email=EMAIL, expires_at="new-expiry"
            )
        )
        row = self.cache_row()
        self.assertEqual(row["fetch_started_at"], self.OLD_START)
        self.assertEqual(json.loads(row["members_json"])[0]["expires_at"], "new-expiry")


if __name__ == "__main__":
    unittest.main()
