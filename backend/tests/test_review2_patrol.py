"""巡逻严格模式动手前强制刷新的分页判定（H2）回归测试。

严格模式动手前的强制刷新按 SnapshotPageAccumulator 判定名单是否完整；不完整就不写缓存、不踢。

所有上游调用都是记录调用的假客户端，绝不触网。
"""

import _isolation  # noqa: F401  must precede any app import
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_premium_patrol import (  # noqa: I001  (_isolation first)
    OLD,
    RecordingClient,
    _Base,
    _live,
    _member,
)

from app.services import patrol

# 生产形状：Owner 的成员条目没有席位类型，没有来源记录。
PROD_OWNER = _member("owner@example.com", "u-owner", seat_type=None, is_owner=True, source=None)
LIVE_PROD_OWNER = {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}


def _iso(delta=timedelta()):
    return (datetime.now(timezone.utc) + delta).isoformat()


def _outsider(n, *, seat_type="default", day=10):
    return _member(f"out{n}@example.com", f"u-out{n}", seat_type=seat_type,
                   first_seen_at=f"2026-07-{day:02d}T00:00:00+00:00")


class _Fixture(_Base):
    def _closed_row(self, team_id, email, user_id, *, kick_source, source="system",
                    expires_at="2026-08-01T00:00:00+00:00"):
        """TeamBoss 以前管过这个人、后来关掉的 member_expiry 行。"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at, kick_source,
                first_seen_at, source, created_at)
               VALUES (?, ?, ?, ?, 1, 1, '2026-08-02T00:00:00+00:00', ?,
                       '2026-07-01T00:00:00+00:00', ?, '2026-07-01T00:00:00+00:00')""",
            (team_id, user_id, email, expires_at, kick_source, source),
        )
        conn.commit()
        conn.close()

    def _open_row(self, team_id, email, user_id, *, source="system"):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, first_seen_at, source, created_at)
               VALUES (?, ?, ?, '2026-12-01T00:00:00+00:00', 1, 0,
                       '2026-07-01T00:00:00+00:00', ?, '2026-07-01T00:00:00+00:00')""",
            (team_id, user_id, email, source),
        )
        conn.commit()
        conn.close()

    def _seat_log(self, team_id, action, target_email, detail, result, created_at):
        conn = self._conn()
        conn.execute(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, 'manual', ?)""",
            (team_id, action, target_email, detail, result, created_at),
        )
        conn.commit()
        conn.close()

    def _cache_row(self, team_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT members_json, pending_json, updated_at, fetch_started_at FROM member_cache "
            "WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def _kick(self, team_id, member, rule=patrol.KICK_RULE_OVER_QUOTA):
        conn = self._conn()
        try:
            return patrol._patrol_kick(conn, RecordingClient(), team_id, member, rule=rule)
        finally:
            conn.close()


# ═══ H2：严格模式动手前的强制刷新只认完整名单 ═══════════════════════════════════

class StrictRefreshPagingTest(_Fixture):
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

        full_page = {"items": [{"id": "u-a"}, {"id": "u-b"}]}
        items, error, requested = self._fetch([full_page] * 5, limit=2, max_items=6)
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
