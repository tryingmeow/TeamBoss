"""The member snapshots patrol acts on: where they come from and when they may be trusted.

- Manual run sources (POST /api/patrol/run "source"): the cache preview (source="cache") is for
  dry runs only; a real run with cache is a 400 in the route and the service-level preview
  refuses a real run too. The cache preview makes zero upstream calls and is read-only: no
  refresh, no Telegram, no operation logs / alert state, no baselines. Teams without a usable
  cache are listed separately; as_of is the oldest cache used.
- Live mode: dry runs refresh at most 3 Teams at a time, real runs one by one; a Team whose
  refresh fails is skipped alone, and the allow list and failure list follow Team order, not
  completion order. POST /api/patrol/refresh/{team_id} does the same for one Team.
- Snapshots are ordered by member_cache.fetch_started_at: a refresh that started earlier never
  overwrites one that started later, and the Premium veto compares TeamBoss seat changes with the
  start time, not the write time. Every writer of a complete snapshot records the start time and
  reconciles persisted seat holds with it.
- The forced refresh before a strict kick accepts only a complete roster (SnapshotPageAccumulator
  / _fetch_all_api_items_sync rules); an incomplete one writes no cache and kicks nobody.

Every upstream call is a fake.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: I001  (_isolation first)

from _patrol_fixtures import (
    LIVE_PROD_OWNER,
    OLD,
    OWNER,
    PROD_OWNER,
    PatrolHistoryCase,
    PatrolSnapshotCase,
    RecordingClient,
    _live,
    _member,
    _now,
)

from app import scheduler as app_scheduler
from app.routes import patrol as patrol_routes
from app.services import patrol

OLDER = "2026-10-07T08:00:00+00:00"
NEWER = "2026-10-07T09:30:00+00:00"

KEEPER = _member("keeper@example.com", "u-k", source="system")
LIVE_OWNER = _live("owner@example.com", "u-owner", "usage_based", owner=True)
LIVE_KEEPER = _live("keeper@example.com", "u-k", "default")
FUTURE_START = "2999-01-01T00:00:00.000000+00:00"


def _run(dry_run=True, source="live", team_ids=None):
    body = {"dry_run": dry_run, "source": source}
    if team_ids is not None:
        body["team_ids"] = team_ids
    return asyncio.run(patrol_routes.trigger_patrol_run(patrol_routes.PatrolRunRequest(**body)))


class _UpstreamMocks:
    """路由层所有会碰上游的入口都换成记录调用的假货。"""

    def __init__(self, test: unittest.TestCase, *, fail_team_ids=(), delays=None):
        self.in_flight = 0
        self.max_in_flight = 0
        self.refreshed: list[str] = []
        fail_team_ids = set(fail_team_ids)
        delays = delays or {}

        async def get_client(team_id):
            return f"client:{team_id}"

        async def fetch_members(team_id, client):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(delays.get(team_id, 0.01))
                if team_id in fail_team_ids:
                    raise RuntimeError("upstream 502")
                self.refreshed.append(team_id)
                return {"members": [], "pending_invites": [], "cached_at": None}
            finally:
                self.in_flight -= 1

        self.get_team_client = AsyncMock(side_effect=get_client)
        self.fetch_and_cache_members = AsyncMock(side_effect=fetch_members)
        self.fetch_live_chatgpt_seat_capacity = AsyncMock(return_value=(None, {}, {}, {}))
        self.update_capacity_cache = AsyncMock(return_value=None)
        for name in (
            "get_team_client",
            "fetch_and_cache_members",
            "fetch_live_chatgpt_seat_capacity",
            "update_capacity_cache",
        ):
            p = patch.object(patrol_routes, name, getattr(self, name))
            p.start()
            test.addCleanup(p.stop)

    def upstream_awaits(self) -> int:
        return sum(
            mock.await_count
            for mock in (
                self.get_team_client,
                self.fetch_and_cache_members,
                self.fetch_live_chatgpt_seat_capacity,
                self.update_capacity_cache,
            )
        )


class _ModesFixture(PatrolHistoryCase):
    def _over_quota_team(self, team_id, *, updated_at=None, baseline=True):
        """1 个席位：Owner + 1 个外部成员 = 超 1 个，空跑会点名这个外部成员。"""
        outsider = _member(f"out@{team_id}.com", f"u-out-{team_id}",
                           first_seen_at="2026-07-20T00:00:00+00:00")
        self._team(team_id, seats_entitled=1)
        self._add(team_id, outsider)
        self._cache(team_id, [PROD_OWNER, outsider], updated_at=updated_at)
        self._arm()
        if baseline:
            self._baseline(team_id)
        return outsider

    def _count(self, table):
        conn = self._conn()
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def _db_state(self):
        conn = self._conn()
        try:
            return {
                table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]
                for table in (
                    "operation_logs", "member_expiry", "member_cache",
                    "patrol_team_baselines", "team_health_incidents", "teams", "settings",
                )
            }
        finally:
            conn.close()


# ═══ 缓存预览只给空跑 ════════════════════════════════════════════════════════

class CacheModeIsDryRunOnlyTest(_ModesFixture):
    def test_real_run_with_cache_source_is_rejected_before_anything_happens(self):
        self._over_quota_team("team-a")
        upstream = _UpstreamMocks(self)
        before = self._db_state()

        with patch.object(patrol_routes, "run_patrol") as run_patrol:
            with self.assertRaises(HTTPException) as caught:
                _run(dry_run=False, source="cache")

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("真踢必须实时刷新", caught.exception.detail)
        run_patrol.assert_not_called()
        self.assertEqual(upstream.upstream_awaits(), 0)
        self.assertEqual(RecordingClient.calls, [])
        self.assertEqual(self._db_state(), before)

    def test_service_preview_refuses_a_real_run(self):
        self._over_quota_team("team-a")

        with self.assertRaises(ValueError):
            patrol.run_patrol(dry_run=False, allow_team_ids=["team-a"], preview=True)

        self.assertEqual(RecordingClient.calls, [])
        self.assertEqual(self._kicked("team-a", "u-out-team-a"), (0, None))

    def test_team_subset_is_cache_only(self):
        upstream = _UpstreamMocks(self)
        with self.assertRaises(HTTPException) as caught:
            _run(dry_run=True, source="live", team_ids=["team-a"])
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(upstream.upstream_awaits(), 0)


# ═══ 缓存预览：零上游、只读 ═══════════════════════════════════════════════════

class CachePreviewTest(_ModesFixture):
    def test_cache_preview_makes_zero_upstream_calls_and_names_candidates(self):
        self._over_quota_team("team-a", updated_at=NEWER)
        self._over_quota_team("team-b", updated_at=OLDER)
        upstream = _UpstreamMocks(self)

        result = _run(dry_run=True, source="cache")

        self.assertEqual(upstream.upstream_awaits(), 0)
        self.assertEqual(RecordingClient.calls, [])
        self.assertEqual(result["source"], "cache")
        self.assertEqual(result["would_kick"], 2)
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(
            sorted(e["email"] for e in result["events"] if e["action"] == "would_kick"),
            ["out@team-a.com", "out@team-b.com"],
        )
        self.assertEqual(result["as_of"], OLDER)
        self.assertEqual(result["team_count"], 2)
        self.assertEqual(result["no_cache_teams"], [])
        self.assertEqual(result["skipped_teams"], [])

    def test_cache_preview_writes_nothing_and_sends_nothing_even_when_armed(self):
        # 已武装（kick_enabled=1 + 基线）的 Team，加一个还没建基线的 Team：
        # 预览既不能踢人，也不能替它建基线、写日志、发 Telegram、动告警状态。
        self._over_quota_team("team-armed")
        self._over_quota_team("team-new", baseline=False)
        _UpstreamMocks(self)
        before = self._db_state()

        result = _run(dry_run=True, source="cache")

        self.assertEqual(self._db_state(), before)
        self.assertEqual(self.notify_calls, [])
        self.assertEqual(RecordingClient.calls, [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(result["would_kick"], 1)
        self.assertIn(
            {"team_id": "team-new", "team_name": "team-new", "action": "baseline_pending"},
            result["events"],
        )

    def test_live_dry_run_on_the_same_data_still_notifies_and_logs(self):
        # 对照：实时空跑的副作用（日志、Telegram）保持原样，只有缓存预览静音。
        self._over_quota_team("team-a")
        _UpstreamMocks(self)

        result = _run(dry_run=True, source="live")

        self.assertEqual(result["source"], "live")
        self.assertEqual(result["would_kick"], 1)
        self.assertEqual([l["target_email"] for l in self._logs("patrol_would_kick")],
                         ["out@team-a.com"])
        self.assertTrue(any("巡逻发现超员" in t for t in self.notify_calls))

    def test_teams_without_usable_cache_are_listed_not_guessed(self):
        self._over_quota_team("team-a", updated_at=NEWER)
        self._team("team-nocache", seats_entitled=1)
        self._team("team-empty", seats_entitled=1)
        self._cache("team-empty", [])
        upstream = _UpstreamMocks(self)

        result = _run(dry_run=True, source="cache")

        self.assertEqual(upstream.upstream_awaits(), 0)
        self.assertEqual(
            sorted(t["team_id"] for t in result["no_cache_teams"]),
            ["team-empty", "team-nocache"],
        )
        self.assertEqual(result["team_count"], 1)
        self.assertEqual(result["as_of"], NEWER)
        self.assertEqual(result["would_kick"], 1)

    def test_no_usable_cache_anywhere_gives_no_as_of(self):
        self._team("team-nocache", seats_entitled=1)
        _UpstreamMocks(self)

        result = _run(dry_run=True, source="cache")

        self.assertIsNone(result["as_of"])
        self.assertEqual(result["team_count"], 0)
        self.assertEqual(result["would_kick"], 0)

    def test_team_ids_limit_the_preview(self):
        self._over_quota_team("team-a", updated_at=NEWER)
        self._over_quota_team("team-b", updated_at=OLDER)
        _UpstreamMocks(self)

        result = _run(dry_run=True, source="cache", team_ids=["team-a"])

        self.assertEqual(result["team_count"], 1)
        self.assertEqual(result["as_of"], NEWER)
        self.assertEqual({e["team_id"] for e in result["events"]}, {"team-a"})

    def test_preview_failure_surfaces_instead_of_an_empty_result(self):
        self._over_quota_team("team-a")
        _UpstreamMocks(self)

        with patch.object(patrol, "_read_patrol_settings", side_effect=RuntimeError("db broken")):
            with self.assertRaises(HTTPException) as caught:
                _run(dry_run=True, source="cache")

        self.assertEqual(caught.exception.status_code, 500)
        self.assertIn("db broken", caught.exception.detail)


# ═══ 实时模式：并发刷新，按 Team 失败关闭 ═════════════════════════════════════

class LiveRefreshTest(_ModesFixture):
    TEAMS = ["team-1", "team-2", "team-3", "team-4", "team-5", "team-6"]

    def _teams(self):
        for team_id in self.TEAMS:
            self._over_quota_team(team_id)

    def _capture_run_patrol(self):
        calls = []
        real = patrol_routes.run_patrol

        def capture(dry_run, allow_team_ids, **kwargs):
            calls.append((dry_run, list(allow_team_ids), kwargs))
            return real(dry_run, allow_team_ids, **kwargs)

        p = patch.object(patrol_routes, "run_patrol", side_effect=capture)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_dry_run_refreshes_three_teams_at_a_time_in_team_order(self):
        self._teams()
        # 先开始的 Team 最慢，完成顺序和 Team 顺序相反；结果仍按 Team 顺序。
        delays = {team_id: 0.06 - 0.01 * i for i, team_id in enumerate(self.TEAMS)}
        upstream = _UpstreamMocks(self, delays=delays)
        calls = self._capture_run_patrol()

        result = _run(dry_run=True, source="live")

        self.assertEqual(upstream.max_in_flight, 3)
        self.assertNotEqual(upstream.refreshed, self.TEAMS)
        self.assertEqual(calls, [(True, self.TEAMS, {})])
        self.assertEqual(result["team_count"], 6)
        self.assertEqual(result["skipped_teams"], [])

    def test_real_run_still_refreshes_one_team_at_a_time(self):
        self._teams()
        upstream = _UpstreamMocks(self)
        calls = self._capture_run_patrol()

        _run(dry_run=False, source="live")

        self.assertEqual(upstream.max_in_flight, 1)
        self.assertEqual(upstream.refreshed, self.TEAMS)
        self.assertEqual(calls, [(False, self.TEAMS, {})])

    def _one_failed_refresh(self, dry_run):
        self._teams()
        upstream = _UpstreamMocks(self, fail_team_ids={"team-2", "team-5"})
        calls = self._capture_run_patrol()

        result = _run(dry_run=dry_run, source="live")

        expected = ["team-1", "team-3", "team-4", "team-6"]
        self.assertEqual(calls, [(dry_run, expected, {})])
        self.assertEqual(result["skipped_teams"], ["team-2: upstream 502", "team-5: upstream 502"])
        self.assertEqual(result["team_count"], 4)
        # 刷新失败的 Team 后两步都没做，巡逻也一个事件都没给它。
        refreshed_capacity = [c.args[0] for c in upstream.update_capacity_cache.await_args_list]
        self.assertEqual(sorted(refreshed_capacity), expected)
        self.assertEqual({e["team_id"] for e in result["events"]}, set(expected))
        skip_logs = self._logs("patrol_manual_run")
        self.assertEqual(len(skip_logs), 1)
        self.assertIn("skipped_unrefreshed=2", skip_logs[0]["detail"])
        return result

    def test_one_failed_refresh_skips_only_that_team_in_a_dry_run(self):
        result = self._one_failed_refresh(dry_run=True)

        self.assertEqual(result["would_kick"], 4)
        self.assertEqual(RecordingClient.calls, [])

    def test_one_failed_refresh_skips_only_that_team_in_a_real_run(self):
        result = self._one_failed_refresh(dry_run=False)

        self.assertEqual(result["kicked"], 4)
        self.assertEqual(
            sorted(c[1] for c in self._calls("remove_member")),
            ["u-out-team-1", "u-out-team-3", "u-out-team-4", "u-out-team-6"],
        )
        self.assertEqual(self._kicked("team-2", "u-out-team-2"), (0, None))
        self.assertEqual(self._kicked("team-5", "u-out-team-5"), (0, None))


# ═══ 单个 Team 的巡逻刷新 ═════════════════════════════════════════════════════

class RefreshOneTeamTest(_ModesFixture):
    def test_success_returns_the_cache_time(self):
        self._over_quota_team("team-a", updated_at=NEWER)
        upstream = _UpstreamMocks(self)

        result = asyncio.run(patrol_routes.refresh_team_for_patrol("team-a"))

        self.assertEqual(result, {"team_id": "team-a", "status": "ok", "cached_at": NEWER})
        self.assertEqual(upstream.fetch_and_cache_members.await_count, 1)
        self.assertEqual(upstream.fetch_live_chatgpt_seat_capacity.await_count, 1)
        self.assertEqual(upstream.update_capacity_cache.await_count, 1)

    def test_failure_is_a_502_with_the_reason(self):
        self._over_quota_team("team-a")
        upstream = _UpstreamMocks(self, fail_team_ids={"team-a"})

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(patrol_routes.refresh_team_for_patrol("team-a"))

        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(caught.exception.detail, "刷新失败：upstream 502")
        self.assertEqual(upstream.update_capacity_cache.await_count, 0)

    def test_unknown_or_inactive_team_is_a_404_without_upstream_calls(self):
        self._team("team-off", seats_entitled=1)
        conn = self._conn()
        conn.execute("UPDATE teams SET status = 'token_expired' WHERE id = 'team-off'")
        conn.commit()
        conn.close()
        upstream = _UpstreamMocks(self)

        for team_id in ("team-off", "team-missing"):
            with self.subTest(team_id=team_id):
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(patrol_routes.refresh_team_for_patrol(team_id))
                self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(upstream.upstream_awaits(), 0)


# ═══ 快照按开始拉取的时间定先后 ═══════════════════════════════════════════════

class StaleRefreshTest(PatrolSnapshotCase):
    def _premium_outsider_team(self, team_id):
        outsider = _member("outsider@example.com", "u-p", seat_type="prolite")
        # 早一个小时的一份快照：外部成员在 Premium 上。
        self._armed_team(team_id, [OWNER, KEEPER, outsider])
        earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._cache(team_id, [OWNER, KEEPER, outsider], updated_at=earlier)
        return outsider

    def test_stale_refresh_written_after_admin_downgrade_does_not_kick(self):
        # 场景：一次刷新先读到他在 Premium，卡在拉邀请上；管理员这时把他切回
        # ChatGPT（日志落在刷新开始之后）；卡住的刷新随后才把旧名单写进缓存。写入时间比日志晚，
        # 但名单是开始那一刻的：不能拿它踢人。
        team_id = "team-k1"
        self._premium_outsider_team(team_id)
        fixture = self

        class StallingClient:
            def get_members(self, offset=0, limit=100):
                items = [LIVE_OWNER, LIVE_KEEPER, _live("outsider@example.com", "u-p", "prolite")]
                return {"items": items if offset == 0 else [], "total": 3}

            def get_pending_invites(self, offset=0, limit=100):
                time.sleep(0.002)
                fixture._log(
                    team_id, "change_seat", "outsider@example.com",
                    "user_id=u-p, seat_type=default, from_seat_type=prolite, policy=confirm",
                    "success",
                )
                time.sleep(0.002)
                return {"items": [], "total": 0}

        self._async_refresh(team_id, StallingClient())
        row = self._cache_row(team_id)
        self.assertEqual(self._cached_seat(team_id, "u-p"), "prolite")
        self.assertLess(row["fetch_started_at"], row["updated_at"])

        result = self._patrol(dry_run=False)

        # 管理员动过他的席位：候选筛选就挡住，交给席位提醒。
        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(self._logs("patrol_kick"), [])
        logged = self._logs("patrol_premium_alert")
        self.assertIn("kind=premium_detected_with_record", logged[0]["detail"])
        self.assertIsNone(self._kicked(team_id, "u-p")[1])

    def test_older_started_refresh_does_not_overwrite_newer_snapshot(self):
        # 刷新 A 先开始、读到 Premium；它卡住期间管理员切回 ChatGPT，另一次刷新 B（开始得更晚）
        # 写进了 ChatGPT。A 最后才写：不能盖掉 B。
        team_id = "team-k1b"
        self._premium_outsider_team(team_id)
        fixture = self

        class SlowClient:
            def get_members(self, offset=0, limit=100):
                items = [LIVE_OWNER, LIVE_KEEPER, _live("outsider@example.com", "u-p", "prolite")]
                return {"items": items if offset == 0 else [], "total": 3}

            def get_pending_invites(self, offset=0, limit=100):
                fixture._log(
                    team_id, "change_seat", "outsider@example.com",
                    "user_id=u-p, seat_type=default, from_seat_type=prolite, policy=confirm",
                    "success",
                )
                RecordingClient.live_members = [
                    LIVE_OWNER, LIVE_KEEPER, _live("outsider@example.com", "u-p", "default"),
                ]
                conn = fixture._conn()
                ok, err, _ = patrol._refresh_team_snapshot_sync(conn, fixture._team_row(team_id))
                conn.close()
                assert ok, err
                fixture.newer = fixture._cache_row(team_id)
                return {"items": [], "total": 0}

        self._async_refresh(team_id, SlowClient())

        row = self._cache_row(team_id)
        self.assertEqual(row, self.newer)
        self.assertEqual(self._cached_seat(team_id, "u-p"), "default")

        self._patrol(dry_run=False)
        self.assertEqual(self._calls("remove_member"), [])


class SnapshotWritersTest(PatrolSnapshotCase):
    """每个写完整快照的地方：记下开始时间；库里的快照开始得更晚时不覆盖；随快照对账席位占用。"""

    def _run_async_writer(self, team_id):
        class Client(RecordingClient):
            pass
        self._async_refresh(team_id, Client())

    def _run_patrol_refresh(self, team_id):
        conn = self._conn()
        ok, err, _ = patrol._refresh_team_snapshot_sync(conn, self._team_row(team_id))
        conn.close()
        self.assertTrue(ok, err)

    def _run_member_watch(self, team_id):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_watch (team_id, reason, target_email, started_at, expires_at, done)
               VALUES (?, 'invite', 'keeper@example.com', ?, ?, 0)""",
            (team_id, _now(), (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()),
        )
        conn.commit()
        conn.close()
        with patch.object(app_scheduler, "ChatGPTClient", RecordingClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "edit_message_sync", lambda *a, **kw: None):
            app_scheduler.member_watch_job()

    def _run_data_sync(self, team_id):
        from app.services import tg_notify, tg_summary

        conn = self._conn()
        conn.execute("UPDATE teams SET display_synced_at = ? WHERE id = ?", (_now(), team_id))
        conn.commit()
        conn.close()

        class SyncClient(RecordingClient):
            def get_subscription(self):
                return {"seats_entitled": 5, "seats_in_use": 2}

            def get_seat_type_counts(self):
                return {"seat_type_counts": {"default": 1, "usage_based": 1}}

        with patch.object(app_scheduler, "ChatGPTClient", SyncClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_recovery_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_failure_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None), \
             patch.object(patrol, "run_patrol", lambda *a, **kw: {}), \
             patch.object(tg_notify, "notify_admins_sync", lambda *a, **kw: 0), \
             patch.object(tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None):
            app_scheduler.data_sync_job()

    def _writers(self):
        return {
            "member_cache_service": self._run_async_writer,
            "patrol_refresh": self._run_patrol_refresh,
            "member_watch_job": self._run_member_watch,
            "data_sync_job": self._run_data_sync,
        }

    def _retire(self, team_id):
        # 定时任务扫全部 active Team：跑完的子用例把自己的 Team 停掉，免得下一个子用例再碰它。
        conn = self._conn()
        conn.execute("UPDATE teams SET status = 'retired' WHERE id = ?", (team_id,))
        conn.commit()
        conn.close()

    def _setup_team(self, team_id, *, cached_start):
        self._team(team_id)
        self._expiry(team_id, "keeper@example.com", "u-k", source="system")
        self._cache(team_id, [OWNER, KEEPER, _member("gone@example.com", "u-gone")],
                    updated_at=cached_start)
        RecordingClient.live_members = [LIVE_OWNER, LIVE_KEEPER]

    def test_newer_started_snapshot_is_kept(self):
        for name, run in self._writers().items():
            with self.subTest(writer=name):
                team_id = f"team-newer-{name}"
                self._setup_team(team_id, cached_start=FUTURE_START)
                before = self._cache_row(team_id)

                run(team_id)

                self.assertEqual(self._cache_row(team_id), before)
                self._retire(team_id)

    def test_writer_records_fetch_start_and_reconciles_seat_holds(self):
        for name, run in self._writers().items():
            with self.subTest(writer=name):
                team_id = f"team-write-{name}"
                self._setup_team(team_id, cached_start=None)
                conn = self._conn()
                conn.execute("UPDATE member_cache SET fetch_started_at = NULL WHERE team_id = ?",
                             (team_id,))
                conn.commit()
                conn.close()
                # 一小时前占下、现在已在名单里的席位：这份完整快照之后应当放掉。
                self._hold(team_id, "keeper@example.com")
                started_after = _now()

                run(team_id)

                row = self._cache_row(team_id)
                emails = {m["email"] for m in json.loads(row["members_json"])}
                self.assertEqual(emails, {"owner@example.com", "keeper@example.com"})
                self.assertIsNotNone(row["fetch_started_at"])
                self.assertGreaterEqual(row["fetch_started_at"], started_after)
                self.assertEqual(self._holds(team_id), set())
                self._retire(team_id)


# ═══ 严格模式动手前的强制刷新只认完整名单 ═══════════════════════════════════

class StrictRefreshPagingTest(PatrolHistoryCase):
    def _strict_team(self, team_id):
        # 严格模式：一个早就过了等待期的 ChatGPT 外部成员，刷新成功就会被踢。
        plain = _member("plain@example.com", "u-d", first_seen_at=OLD)
        members = [PROD_OWNER, _member("keeper@example.com", "u-k", source="system"), plain]
        self._armed_team(team_id, members, seats_entitled=99)
        self._setting("patrol_strict_mode_enabled", "1")
        live = [LIVE_PROD_OWNER, _live("keeper@example.com", "u-k", "default"),
                _live("plain@example.com", "u-d", "default")]
        return live

    def _run_with_member_pages(self, team_id, pages):
        """get_members 依次返回 pages 里的每一页；返回 (本轮结果, 刷新前的缓存行)。"""
        served = list(pages)

        class PagedClient(RecordingClient):
            def get_members(self, offset=0, limit=100):
                RecordingClient.calls.append(("get_members", offset))
                return served.pop(0) if served else {"items": [], "total": 0}

        before = self._cache_row(team_id)
        with patch.object(patrol, "ChatGPTClient", PagedClient):
            result = self._patrol(dry_run=False)
        return result, before

    def test_empty_page_with_nonzero_total_fails_the_refresh(self):
        team_id = "team-h2-empty"
        self._strict_team(team_id)

        result, before = self._run_with_member_pages(team_id, [{"items": [], "total": 1}])

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(self._cache_row(team_id), before)
        self.assertTrue(any(e.get("action") == "strict_refresh_failed" for e in result["events"]))
        self.assertEqual(len(self._logs("patrol_strict_refresh_failed")), 1)

    def test_more_entries_than_the_reported_total_fails_the_refresh(self):
        team_id = "team-h2-over"
        live = self._strict_team(team_id)

        result, before = self._run_with_member_pages(team_id, [{"items": live, "total": 2}])

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(self._cache_row(team_id), before)
        failed = [e for e in result["events"] if e.get("action") == "strict_refresh_failed"]
        self.assertIn("more member/invite entries than the reported total", failed[0]["error"])

    def test_non_object_entry_fails_the_refresh(self):
        team_id = "team-h2-entry"
        live = self._strict_team(team_id)

        result, before = self._run_with_member_pages(
            team_id, [{"items": live[:2] + ["plain@example.com"], "total": 3}]
        )

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(self._cache_row(team_id), before)
        self.assertTrue(any(e.get("action") == "strict_refresh_failed" for e in result["events"]))

    def test_complete_snapshot_still_refreshes_and_kicks(self):
        team_id = "team-h2-ok"
        live = self._strict_team(team_id)

        self._run_with_member_pages(team_id, [{"items": live, "total": 3}])

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-d")])

    def _fetch(self, pages, **kwargs):
        served = list(pages)
        requested = []

        def method(offset=0, limit=100):
            requested.append((offset, limit))
            return served.pop(0)

        items, error = patrol._fetch_all_api_items_sync(method, "users", **kwargs)
        return items, error, requested

    def test_paginator_rules(self):
        a, b, c = ({"id": f"u-{n}"} for n in "abc")
        cases = {
            "total_changed_between_pages": ([{"items": [a, b], "total": 4}, {"items": [c], "total": 3}],
                                            "total changed"),
            "malformed_total": ([{"items": [a], "total": "1"}], "malformed total"),
            "page_larger_than_limit": ([{"items": [a, b, c], "total": 3}], "larger than the requested limit"),
            "missing_list": ([{"total": 0}], "unrecognized member/invite response structure"),
        }
        for name, (pages, expected) in cases.items():
            with self.subTest(case=name):
                items, error, _requested = self._fetch(pages, limit=2)
                self.assertIsNone(items)
                self.assertIn(expected, error)

    def test_paginator_keeps_the_upstream_error_text_and_the_page_cap(self):
        items, error, _ = self._fetch([{"error": "Unauthorized"}])
        self.assertEqual((items, error), (None, "Unauthorized"))

        full_pages = [{"items": [{"id": f"u-{n}a"}, {"id": f"u-{n}b"}]} for n in range(5)]
        items, error, requested = self._fetch(full_pages, limit=2, max_items=6)
        self.assertIsNone(items)
        self.assertIn("exceeds the paging limit", error)
        self.assertEqual(requested, [(0, 2), (2, 2), (4, 2)])

    def test_paginator_returns_a_complete_multi_page_list(self):
        a, b, c = ({"id": f"u-{n}"} for n in "abc")
        items, error, requested = self._fetch(
            [{"users": [a, b], "total": 3}, {"users": [c], "total": 3}], limit=2
        )
        self.assertIsNone(error)
        self.assertEqual(items, [a, b, c])
        self.assertEqual(requested, [(0, 2), (2, 2)])


if __name__ == "__main__":
    unittest.main()
