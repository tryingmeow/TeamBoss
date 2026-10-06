"""成员 / 邀请快照与实时容量的第三轮复核修复的端到端回归测试。

- F2：名单里每一行都要认得出是谁，id / 邮箱不能重复；否则整份名单不完整，异步刷新保留
  上一份缓存和席位占用，调度器的数据同步不关任何 member_expiry 行。

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

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app import member_cache_service
from app import scheduler as app_scheduler
from app.services import patrol as patrol_service
from app.services import team_health_alerts, tg_notify, tg_summary

OWNER = {
    "id": "user-owner-r3",
    "email": "owner-r3@example.com",
    "role": "account-owner",
    "seat_type": "default",
}


def _owner_twice():
    return [{"items": [dict(OWNER), dict(OWNER)], "total": 2}]


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


if __name__ == "__main__":
    unittest.main()
