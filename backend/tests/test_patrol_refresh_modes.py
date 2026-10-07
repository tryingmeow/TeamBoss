"""手动巡逻的两种数据来源（POST /api/patrol/run 的 source）和逐个 Team 刷新的回归测试。

- 缓存预览（source="cache"）只给空跑：真跑带 cache 在服务端直接 400，service 层 preview 也拒绝真跑。
- 缓存预览零上游调用、只读：不刷新、不发 Telegram、不写操作日志 / 告警状态、不建基线；
  没有可用缓存的 Team 单独列出；as_of 是用到的缓存里最旧的那份。
- 实时模式：空跑最多 3 个 Team 同时刷新，真跑仍一个一个来；某个 Team 刷新失败只跳过它，
  白名单和失败列表都按 Team 顺序，和完成先后无关。
- POST /api/patrol/refresh/{team_id}：和实时模式对每个 Team 做同一件事，失败给原因。

所有上游调用都是假的，绝不触网。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: I001  (_isolation first)

from test_premium_patrol import RecordingClient, _member
from test_review2_patrol import PROD_OWNER, _Fixture, _outsider

from app.routes import patrol as patrol_routes
from app.services import patrol

OLDER = "2026-10-07T08:00:00+00:00"
NEWER = "2026-10-07T09:30:00+00:00"


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


class _ModesFixture(_Fixture):
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


if __name__ == "__main__":
    unittest.main()
