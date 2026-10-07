"""续费前空闲计费席位：判定、Telegram 提醒、去重和 Team 接口字段。"""
import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes.teams import get_team, list_teams
from app.services import renewal_reminders, team_health_alerts
from app.services.renewal_reminders import (
    renewal_idle_seats,
    run_renewal_idle_seat_reminders_sync,
)
from app.services.seat_capacity import cached_seat_capacity, parse_seat_capacity

NOW = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)
IN_WINDOW = "2026-10-24T06:00:00Z"  # 正好 3 天后：窗口含右端
OUT_OF_WINDOW = "2026-10-24T06:00:01Z"


def _capacity(**entries):
    """_capacity(default=(paid, available[, renewal_requested]))"""
    out = {}
    for seat_type, values in entries.items():
        entry = {"paid": values[0], "available": values[1]}
        if len(values) > 2:
            entry["renewal_requested"] = values[2]
        out[seat_type] = entry
    return json.dumps(out)


def _team(**overrides):
    team = {
        "id": "t1",
        "name": "Lab",
        "owner_email": "owner.long@example.com",
        "active_until": IN_WINDOW,
        "will_renew": 1,
        "billing_period": "monthly",
        "price_per_seat": 780.0,
        "billing_currency": "THB",
        "seat_capacity_json": _capacity(default=(2, 1, 2), prolite=(0, 0, 0)),
        "seat_type_counts_json": json.dumps({"default": 1, "prolite": 0, "usage_based": 1}),
        "sync_suspended_at": None,
    }
    team.update(overrides)
    return team


class RenewalIdleSeatsTest(unittest.TestCase):
    def test_due_inside_window_only(self):
        self.assertIsNotNone(renewal_idle_seats(_team(), "[]", now=NOW))
        self.assertIsNone(renewal_idle_seats(_team(active_until=OUT_OF_WINDOW), "[]", now=NOW))
        self.assertIsNone(renewal_idle_seats(_team(active_until="2026-10-21T05:59:59Z"), "[]", now=NOW))
        self.assertIsNone(renewal_idle_seats(_team(active_until=None), "[]", now=NOW))

    def test_not_renewing_is_not_due(self):
        self.assertIsNone(renewal_idle_seats(_team(will_renew=0), "[]", now=NOW))
        self.assertIsNone(renewal_idle_seats(_team(will_renew=None), "[]", now=NOW))
        self.assertIsNotNone(renewal_idle_seats(_team(will_renew=True), "[]", now=NOW))

    def test_idle_zero_vs_positive(self):
        result = renewal_idle_seats(_team(), "[]", now=NOW)
        self.assertEqual(result.total_idle, 1)
        line = result.lines[0]
        self.assertEqual(
            (line.seat_type, line.paid, line.renewing, line.in_use, line.pending, line.idle),
            ("default", 2, 2, 1, 0, 1),
        )
        full = _team(seat_type_counts_json=json.dumps({"default": 2, "prolite": 0}))
        self.assertIsNone(renewal_idle_seats(full, "[]", now=NOW))
        # 超员不算负的空闲。
        over = _team(seat_type_counts_json=json.dumps({"default": 3, "prolite": 0}))
        self.assertIsNone(renewal_idle_seats(over, "[]", now=NOW))

    def test_pending_invites_hold_seats(self):
        typed = json.dumps([{"email": "a@example.com", "seat_type": "default"}])
        self.assertIsNone(renewal_idle_seats(_team(), typed, now=NOW))
        # 没带类型的邀请对每个计费类型都算一份。
        untyped = json.dumps([{"email": "a@example.com", "seat_type": None}])
        self.assertIsNone(renewal_idle_seats(_team(), untyped, now=NOW))
        # 别的类型的邀请不占 ChatGPT 席位。
        codex = json.dumps([{"email": "a@example.com", "seat_type": "usage_based"}])
        self.assertEqual(renewal_idle_seats(_team(), codex, now=NOW).total_idle, 1)

    def test_renewal_requested_wins_over_paid(self):
        # Owner 已经排了减席：已付 3，下期只续 2，在用 1 → 空闲 1。
        team = _team(seat_capacity_json=_capacity(default=(3, 2, 2)))
        result = renewal_idle_seats(team, "[]", now=NOW)
        self.assertEqual((result.lines[0].paid, result.lines[0].renewing, result.total_idle), (3, 2, 1))
        # 已经减到在用人数：不提醒。
        reduced = _team(seat_capacity_json=_capacity(default=(3, 2, 1)))
        self.assertIsNone(renewal_idle_seats(reduced, "[]", now=NOW))
        # 没给 renewal_requested 时按 paid。
        legacy = _team(seat_capacity_json=_capacity(default=(2, 1)))
        self.assertEqual(renewal_idle_seats(legacy, "[]", now=NOW).lines[0].renewing, 2)

    def test_premium_and_chatgpt_both_counted(self):
        team = _team(
            seat_capacity_json=_capacity(default=(2, 1, 2), prolite=(2, 1, 2)),
            seat_type_counts_json=json.dumps({"default": 1, "prolite": 1}),
        )
        result = renewal_idle_seats(team, "[]", now=NOW)
        self.assertEqual(result.idle_of("default"), 1)
        self.assertEqual(result.idle_of("prolite"), 1)
        self.assertEqual(result.total_idle, 2)
        only_premium = _team(
            seat_capacity_json=_capacity(default=(1, 0, 1), prolite=(1, 1, 1)),
            seat_type_counts_json=json.dumps({"default": 1, "prolite": 0}),
        )
        result = renewal_idle_seats(only_premium, "[]", now=NOW)
        self.assertEqual([(l.seat_type, l.idle) for l in result.lines], [("default", 0), ("prolite", 1)])

    def test_missing_or_malformed_data_skips(self):
        cases = {
            "no capacity": _team(seat_capacity_json=None),
            "capacity junk": _team(seat_capacity_json="{not json"),
            "no counts": _team(seat_type_counts_json=None),
            "counts junk": _team(seat_type_counts_json="[1, 2]"),
            "counts lack default": _team(seat_type_counts_json=json.dumps({"prolite": 0})),
            "renewal_requested untrusted": _team(seat_capacity_json=json.dumps(
                {"default": {"paid": 2, "available": 1, "renewal_requested": None}}
            )),
            "active_until junk": _team(active_until="next tuesday"),
            "sync suspended": _team(sync_suspended_at="2026-10-20T00:00:00+00:00"),
        }
        for label, team in cases.items():
            with self.subTest(label):
                self.assertIsNone(renewal_idle_seats(team, "[]", now=NOW))
        for label, pending in {"no cache row": None, "pending junk": "{", "pending not list": "{}",
                               "pending entry junk": '["x"]'}.items():
            with self.subTest(label):
                self.assertIsNone(renewal_idle_seats(_team(), pending, now=NOW))


class ParseRenewalRequestedTest(unittest.TestCase):
    def test_upstream_renewal_requested_is_kept(self):
        parsed = parse_seat_capacity({"seat_capacity": [
            {"type": "default", "paid": 2, "held": 0, "renewal_requested": 2, "available": 1},
            {"type": "prolite", "paid": 1, "available": 0, "renewal_requested": "1"},
            {"type": "usage_based", "paid": 0, "available": 0},
        ]})
        self.assertEqual(parsed["default"], {"paid": 2, "available": 1, "renewal_requested": 2})
        self.assertEqual(parsed["prolite"], {"paid": 1, "available": 0, "renewal_requested": None})
        self.assertEqual(parsed["usage_based"], {"paid": 0, "available": 0})
        self.assertEqual(cached_seat_capacity(json.dumps(parsed)), parsed)


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        self.messages: list[str] = []
        self.deliver = 1
        notify_patch = patch.object(renewal_reminders, "notify_admins_sync", self._notify)
        notify_patch.start()
        self.addCleanup(notify_patch.stop)

    def _notify(self, text):
        self.messages.append(text)
        return self.deliver

    def _insert(self, pending_json="[]", **overrides):
        team = _team(**overrides)
        team.setdefault("status", "active")
        team["created_at"] = "2026-10-01"
        columns = ", ".join(team)
        marks = ", ".join("?" for _ in team)
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"INSERT INTO teams ({columns}) VALUES ({marks})", tuple(team.values()))
        if pending_json is not None:
            conn.execute(
                "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, '[]', ?, ?)",
                (team["id"], pending_json, NOW.isoformat()),
            )
        conn.commit()
        conn.close()

    def _execute(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def _logs(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT action, detail, result, error_message, trigger_type FROM operation_logs ORDER BY id"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


class ReminderJobTest(_DbCase):
    def test_sends_once_and_survives_restart(self):
        self._insert()
        first = run_renewal_idle_seat_reminders_sync(now=NOW)
        self.assertEqual(first, {"due": 1, "sent": 1})
        self.assertEqual(len(self.messages), 1)
        # 去重状态只在库里（进程内的发送占位发完就清空），所以下一轮等同于重启后的一轮：不再发。
        self.assertEqual(team_health_alerts._SENDING_INCIDENTS, set())
        again = run_renewal_idle_seat_reminders_sync(now=NOW + timedelta(hours=12))
        self.assertEqual(again, {"due": 1, "sent": 0})
        self.assertEqual(len(self.messages), 1)

        logs = [r for r in self._logs() if r["action"] == "renewal_idle_seat_reminder"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["result"], "success")
        self.assertEqual(logs[0]["trigger_type"], "scheduler")
        self.assertEqual(
            logs[0]["detail"],
            "renews_at=2026-10-24T06:00:00Z, idle_default=1, delivered_to=1",
        )
        # 通用的 team_health_alert 行不写。
        self.assertFalse([r for r in self._logs() if r["action"] == "team_health_alert"])

    def test_idle_cleared_then_back_in_same_period_does_not_resend(self):
        self._insert()
        run_renewal_idle_seat_reminders_sync(now=NOW)
        self._execute("UPDATE teams SET seat_type_counts_json = ?", (json.dumps({"default": 2}),))
        self.assertEqual(run_renewal_idle_seat_reminders_sync(now=NOW + timedelta(hours=1))["due"], 0)
        self._execute("UPDATE teams SET seat_type_counts_json = ?", (json.dumps({"default": 1}),))
        run_renewal_idle_seat_reminders_sync(now=NOW + timedelta(hours=2))
        self.assertEqual(len(self.messages), 1)

    def test_next_billing_period_reminds_again(self):
        self._insert()
        run_renewal_idle_seat_reminders_sync(now=NOW)
        next_until = "2026-11-24T06:00:00Z"
        self._execute("UPDATE teams SET active_until = ?", (next_until,))
        later = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(run_renewal_idle_seat_reminders_sync(now=later)["sent"], 1)
        self.assertEqual(len(self.messages), 2)

    def test_not_due_sends_nothing(self):
        self._insert(active_until=OUT_OF_WINDOW)
        self._insert(id="t2", seat_type_counts_json=json.dumps({"default": 2}))
        self._insert(id="t3", will_renew=0)
        self._insert(id="t4", pending_json=None)
        self.assertEqual(run_renewal_idle_seat_reminders_sync(now=NOW), {"due": 0, "sent": 0})
        self.assertEqual(self.messages, [])
        self.assertEqual(self._logs(), [])

    def test_undelivered_logs_once_then_retries(self):
        self._insert()
        self.deliver = 0
        run_renewal_idle_seat_reminders_sync(now=NOW)
        run_renewal_idle_seat_reminders_sync(now=NOW + timedelta(minutes=30))
        logs = [r for r in self._logs() if r["action"] == "renewal_idle_seat_reminder"]
        self.assertEqual([r["result"] for r in logs], ["skipped"])
        self.deliver = 1
        self.assertEqual(run_renewal_idle_seat_reminders_sync(now=NOW + timedelta(hours=1))["sent"], 1)
        logs = [r for r in self._logs() if r["action"] == "renewal_idle_seat_reminder"]
        self.assertEqual([r["result"] for r in logs], ["skipped", "success"])

    def test_reminder_text(self):
        self._insert(
            seat_capacity_json=_capacity(default=(3, 2, 3), prolite=(2, 1, 2)),
            seat_type_counts_json=json.dumps({"default": 1, "prolite": 1}),
            pending_json=json.dumps([{"email": "x@example.com", "seat_type": "default"}]),
        )
        run_renewal_idle_seat_reminders_sync(now=NOW)
        text = self.messages[0]
        self.assertIn("⏰ 续费前有空闲席位 · Lab", text)
        self.assertIn("Owner：owner…@example", text)
        self.assertNotIn("owner.long@example.com", text)
        self.assertIn("续费：2026-10-24 14:00（北京时间）", text)
        self.assertIn("💺 ChatGPT：已付 3 · 在用 1 · 待接受 1 · 空闲 1", text)
        self.assertIn("💎 Premium：已付 2 · 在用 1 · 待接受 0 · 空闲 1", text)
        self.assertIn("780 THB/月（ChatGPT）", text)
        self.assertIn("Premium 单价未知", text)
        self.assertIn(renewal_reminders.ACTION_LINE, text)

    def test_unknown_price_says_so(self):
        self._insert(billing_period=None)
        run_renewal_idle_seat_reminders_sync(now=NOW)
        self.assertIn("ChatGPT 单价未知", self.messages[0])


class TeamResponseFieldTest(_DbCase):
    def test_team_endpoints_carry_the_breakdown(self):
        now = datetime.now(timezone.utc)
        until = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._insert(active_until=until)
        self._insert(id="t2", active_until=(now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        expected = {
            "renews_at": until,
            "total_idle": 1,
            "lines": [{"seat_type": "default", "paid": 2, "renewing": 2, "in_use": 1, "pending": 0, "idle": 1}],
        }
        self.assertEqual(asyncio.run(get_team("t1"))["renewal_idle_seats"], expected)
        listed = {t["id"]: t for t in asyncio.run(list_teams())}
        self.assertEqual(listed["t1"]["renewal_idle_seats"], expected)
        self.assertIsNone(listed["t2"]["renewal_idle_seats"])
        self.assertEqual(listed["t1"]["seat_capacity"]["default"]["renewal_requested"], 2)


if __name__ == "__main__":
    unittest.main()
