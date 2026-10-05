"""连续同步失败 24 小时后停止定时请求（sync suspension）。

上游吊销 token 之后，重试不会有别的结果：一个坏掉的 Team 会以每 15 分钟 9 个
请求的节奏一直打 chatgpt.com，并且每轮重复播报同一条「数据源同步失败」。这组
测试钉住挂起、探活、恢复三段行为，以及挂起的 Team 绝不能进巡逻。
"""

import _isolation  # noqa: F401  must precede any app import
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scheduler import (
    SYNC_FAILURE_SUSPEND_HOURS,
    SYNC_SUSPENDED_PROBE_HOURS,
    _record_team_sync_outcome,
    _sync_probe_due,
)


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _hours_ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


class ProbeDueTest(unittest.TestCase):
    def test_never_probed_is_due(self):
        self.assertTrue(_sync_probe_due(None, NOW))

    def test_unparsable_timestamp_is_due(self):
        self.assertTrue(_sync_probe_due("not-a-timestamp", NOW))

    def test_recent_probe_is_not_due(self):
        self.assertFalse(_sync_probe_due(_hours_ago(SYNC_SUSPENDED_PROBE_HOURS - 1), NOW))

    def test_old_probe_is_due(self):
        self.assertTrue(_sync_probe_due(_hours_ago(SYNC_SUSPENDED_PROBE_HOURS), NOW))

    def test_naive_timestamp_is_treated_as_utc(self):
        naive = (NOW - timedelta(hours=SYNC_SUSPENDED_PROBE_HOURS + 1)).replace(tzinfo=None)
        self.assertTrue(_sync_probe_due(naive.isoformat(), NOW))


class RecordSyncOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE teams (id TEXT PRIMARY KEY, sync_failing_since TEXT, "
            "sync_suspended_at TEXT, sync_probe_at TEXT)"
        )
        self.conn.execute("INSERT INTO teams (id) VALUES ('t1')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _row(self):
        return self.conn.execute("SELECT * FROM teams WHERE id = 't1'").fetchone()

    def _record(self, *, ok, failing_since=None, suspended_at=None, last_full_sync_at=None):
        return _record_team_sync_outcome(
            self.conn,
            "t1",
            ok=ok,
            now=NOW,
            failing_since=failing_since,
            suspended_at=suspended_at,
            last_full_sync_at=last_full_sync_at,
        )

    def test_first_failure_starts_the_clock_without_suspending(self):
        self.assertIsNone(self._record(ok=False, last_full_sync_at=_hours_ago(0.2)))
        row = self._row()
        self.assertEqual(row["sync_failing_since"], _hours_ago(0.2))
        self.assertIsNone(row["sync_suspended_at"])

    def test_failure_clock_does_not_restart_on_later_rounds(self):
        started = _hours_ago(3)
        self._record(ok=False, failing_since=started)
        self.assertEqual(self._row()["sync_failing_since"], started)

    def test_suspends_after_threshold(self):
        event = self._record(ok=False, failing_since=_hours_ago(SYNC_FAILURE_SUSPEND_HOURS))
        self.assertEqual(event, "suspended")
        row = self._row()
        self.assertEqual(row["sync_suspended_at"], NOW.isoformat())
        self.assertEqual(row["sync_probe_at"], NOW.isoformat())

    def test_long_broken_team_suspends_on_the_first_round_after_upgrade(self):
        """升级前就坏了半个月的 Team 不该再白等一天：失败起点回填 last_full_sync_at。"""
        event = self._record(ok=False, failing_since=None, last_full_sync_at=_hours_ago(360))
        self.assertEqual(event, "suspended")
        self.assertEqual(self._row()["sync_suspended_at"], NOW.isoformat())

    def test_team_that_never_synced_starts_a_fresh_clock(self):
        self.assertIsNone(self._record(ok=False, failing_since=None, last_full_sync_at=None))
        self.assertEqual(self._row()["sync_failing_since"], NOW.isoformat())

    def test_failed_probe_only_moves_the_probe_timestamp(self):
        suspended_at = _hours_ago(48)
        self.conn.execute(
            "UPDATE teams SET sync_failing_since = ?, sync_suspended_at = ?, sync_probe_at = ? "
            "WHERE id = 't1'",
            (_hours_ago(72), suspended_at, _hours_ago(SYNC_SUSPENDED_PROBE_HOURS)),
        )
        self.conn.commit()
        event = self._record(
            ok=False, failing_since=_hours_ago(72), suspended_at=suspended_at
        )
        self.assertIsNone(event)
        row = self._row()
        self.assertEqual(row["sync_suspended_at"], suspended_at)
        self.assertEqual(row["sync_probe_at"], NOW.isoformat())

    def test_successful_probe_lifts_suspension(self):
        event = self._record(
            ok=True, failing_since=_hours_ago(72), suspended_at=_hours_ago(48)
        )
        self.assertEqual(event, "resumed")
        row = self._row()
        self.assertIsNone(row["sync_failing_since"])
        self.assertIsNone(row["sync_suspended_at"])
        self.assertIsNone(row["sync_probe_at"])

    def test_recovery_before_suspension_clears_the_clock_quietly(self):
        self.conn.execute(
            "UPDATE teams SET sync_failing_since = ? WHERE id = 't1'", (_hours_ago(3),)
        )
        self.conn.commit()
        self.assertIsNone(self._record(ok=True, failing_since=_hours_ago(3)))
        self.assertIsNone(self._row()["sync_failing_since"])

    def test_healthy_team_writes_nothing(self):
        self.assertIsNone(self._record(ok=True))
        row = self._row()
        self.assertIsNone(row["sync_failing_since"])
        self.assertIsNone(row["sync_suspended_at"])

    def test_missing_columns_do_not_raise(self):
        """迁移还没跑到的旧库：挂起只是节流，不能把同步流程本身打断。"""
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.execute("CREATE TABLE teams (id TEXT PRIMARY KEY)")
        legacy.execute("INSERT INTO teams (id) VALUES ('t1')")
        legacy.commit()
        self.assertIsNone(
            _record_team_sync_outcome(
                legacy,
                "t1",
                ok=False,
                now=NOW,
                failing_since=_hours_ago(100),
                suspended_at=None,
                last_full_sync_at=None,
            )
        )
        legacy.close()


if __name__ == "__main__":
    unittest.main()
