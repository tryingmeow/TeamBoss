"""持久席位占用（seat_holds）什么时候放、怎么放。

占用只在两处放掉：上游明确拒绝了这次邀请（调用方 release_seat_hold），或者按版本删除的对账：
- 人在名单里不算证据。一份完整快照里这个邮箱带着被占的那个类型才放掉；不在、或者是
  别的类型，要等拉取开始时间至少晚于占下时间 15 分钟的完整快照仍看不见这个类型才放。
- 对账只删选中的那个版本（seat_type + created_at）；选中之后被重新占过的行留着。

不完整的名单不放占用，见 test_snapshot_completeness.py。上游一律是只记录调用的假客户端，绝不触网。
"""

import _isolation  # noqa: F401  must precede any app import
from _fixtures import direct_call
from _seat_fixtures import (
    HOLDS_EMAIL as EMAIL,
    HOLDS_TEAM as TEAM,
    HOLDS_USER_ID as USER_ID,
    SeatHoldsCase,
    _ListClient,
    _iso,
    _patched,
    capacity_entries,
)

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import member_cache_service
from app.models import ChangeSeatRequest
from app.routes import members
from app.services import seat_capacity, seat_holds, team_health_alerts

SNAPSHOT_START = "2026-10-06T12:00:00.000000+00:00"
GRACE = timedelta(seconds=seat_holds.HOLD_ABSENT_GRACE_SECONDS)


def _at(base: str, delta: timedelta) -> str:
    return _iso(datetime.fromisoformat(base) + delta)


def _live(email, user_id, seat_type, *, role="standard-user"):
    return {"id": user_id, "email": email, "seat_type": seat_type, "role": role}


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


# ═══ 只有带着被占类型出现才放，否则走 15 分钟规则 ═══════════════════════════════

class SwitchTimeoutHoldTest(SeatHoldsCase):
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


class ReleaseRuleTest(SeatHoldsCase):
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


# ═══ 只删选中的那个版本 ═══════════════════════════════════════════════════════════

class VersionExactDeleteTest(SeatHoldsCase):
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


if __name__ == "__main__":
    unittest.main()
