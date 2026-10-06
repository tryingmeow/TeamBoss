"""Premium 席位在 Team 列表 / 资源接口 / Telegram 摘要里的展示。"""
import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes.resources import get_resource_usage
from app.routes.teams import get_team, list_teams
from app.services import tg_summary
from app.services.seat_capacity import member_seat_usage_from_members


def _member(email, seat_type):
    return {"id": email, "email": email, "seat_type": seat_type, "status": "active", "is_owner": False}


class PremiumDisplayTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()

    def _insert_team(self, members=None, **fields):
        fields.setdefault("status", "active")
        fields.setdefault("created_at", "2026-10-01")
        fields.setdefault("updated_at", "2026-10-01")
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"INSERT INTO teams ({columns}) VALUES ({marks})", tuple(fields.values()))
        if members is not None:
            conn.execute(
                "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) "
                "VALUES (?, ?, '[]', ?)",
                (fields["id"], json.dumps(members), "2026-10-01T00:00:00+00:00"),
            )
        conn.commit()
        conn.close()

    def test_team_response_carries_policy_capacity_and_counts(self):
        self._insert_team(
            id="t1",
            name="T1",
            overage_policy="forbid",
            seat_capacity_json=json.dumps({"default": {"paid": 2, "available": 1}, "prolite": {"paid": 1, "available": 0}}),
            seat_type_counts_json=json.dumps({"default": 1, "prolite": 1, "automation": 2}),
        )
        team = asyncio.run(get_team("t1"))
        self.assertEqual(team["overage_policy"], "forbid")
        self.assertEqual(team["seat_capacity"]["prolite"], {"paid": 1, "available": 0})
        self.assertEqual(team["seat_type_counts"], {"default": 1, "prolite": 1, "automation": 2})

        listed = asyncio.run(list_teams())
        self.assertEqual(listed[0]["overage_policy"], "forbid")
        self.assertEqual(listed[0]["seat_capacity"]["default"]["available"], 1)

    def test_team_response_defaults_when_columns_are_empty(self):
        self._insert_team(id="t2", name="T2")
        team = asyncio.run(get_team("t2"))
        self.assertEqual(team["overage_policy"], "confirm")
        self.assertIsNone(team["seat_capacity"])
        self.assertEqual(team["seat_type_counts"], {})

    def test_garbage_policy_and_json_are_safe(self):
        self._insert_team(
            id="t3", name="T3", overage_policy="???",
            seat_capacity_json="not json", seat_type_counts_json="[1]",
        )
        team = asyncio.run(get_team("t3"))
        self.assertEqual(team["overage_policy"], "forbid")
        self.assertIsNone(team["seat_capacity"])
        self.assertEqual(team["seat_type_counts"], {})

    def test_unknown_seat_types_are_not_chatgpt(self):
        usage = member_seat_usage_from_members(
            [_member("a@x.com", "default"), _member("b@x.com", "automation"), _member("c@x.com", "prolite")]
        )
        self.assertEqual(usage.active_chatgpt, 1)
        self.assertEqual(usage.seats_in_use_total, 3)

    def test_resources_free_seats_use_the_smaller_value(self):
        # 旧公式：5 − 1 = 4；分类型 available = 1 → 取 1。
        self._insert_team(
            [_member("a@x.com", "default")],
            id="t4", name="T4", seats_entitled=5, seats_in_use=1, codex_count=0, chatgpt_count=1,
            seat_capacity_json=json.dumps({"default": {"paid": 5, "available": 1}}),
        )
        # 没有 seat_capacity 的 Team 仍是旧公式。
        self._insert_team(
            [_member("b@x.com", "default")],
            id="t5", name="T5", seats_entitled=5, seats_in_use=1, codex_count=0, chatgpt_count=1,
        )
        result = asyncio.run(get_resource_usage(refresh=False))
        by_id = {item["team_id"]: item for item in result["teams"]}
        self.assertEqual(by_id["t4"]["free_gpt_seats"], 1)
        self.assertEqual(by_id["t5"]["free_gpt_seats"], 4)
        self.assertEqual(result["free_gpt_seats"], 5)

    def test_resources_reports_premium_without_counting_it_as_chatgpt(self):
        self._insert_team(
            [_member("a@x.com", "default"), _member("p@x.com", "prolite"), _member("z@x.com", "automation")],
            id="t6", name="T6", seats_entitled=3, seats_in_use=3, codex_count=0,
            seat_capacity_json=json.dumps({"default": {"paid": 3, "available": 2}, "prolite": {"paid": 2, "available": 1}}),
        )
        result = asyncio.run(get_resource_usage(refresh=False))
        item = result["teams"][0]
        self.assertEqual(item["inuse_gpt"], 1)
        self.assertEqual(item["inuse_premium"], 1)
        self.assertEqual(item["premium_seats_paid"], 2)
        self.assertEqual(result["inuse_premium"], 1)
        self.assertEqual(item["free_gpt_seats"], 2)


class PremiumTelegramSummaryTest(unittest.TestCase):
    def test_summary_shows_premium_line_only_when_in_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app_database, "get_db_dir", return_value=tmp):
                asyncio.run(app_database.init_database())
                conn = sqlite3.connect(app_database.get_db_path())
                now = "2026-10-06T00:00:00+00:00"
                conn.execute(
                    "INSERT INTO teams (id, name, status, seats_entitled, created_at, updated_at) "
                    "VALUES ('t', 'T', 'active', 3, ?, ?)",
                    (now, now),
                )
                members = [_member("a@x.com", "default"), _member("p@x.com", "prolite"), _member("z@x.com", "automation")]
                conn.execute(
                    "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES ('t', ?, '[]', ?)",
                    (json.dumps(members), now),
                )
                conn.commit()
                conn.close()
                text = tg_summary.build_summary_sync(now=datetime(2026, 10, 6, 0, 5, tzinfo=timezone.utc))
                self.assertIn("Premium 席位　　 1", text)
                self.assertIn("GPT 席位　　　  1 / 3", text)

                conn = sqlite3.connect(app_database.get_db_path())
                conn.execute("UPDATE member_cache SET members_json = ?", (json.dumps(members[:1]),))
                conn.commit()
                conn.close()
                text = tg_summary.build_summary_sync(now=datetime(2026, 10, 6, 0, 5, tzinfo=timezone.utc))
                self.assertNotIn("Premium", text)


if __name__ == "__main__":
    unittest.main()


class PendingInviteCountsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        asyncio.run(app_database.init_database())
        conn = sqlite3.connect(app_database.get_db_path())
        for team_id in ("c1", "c2", "c3"):
            conn.execute(
                "INSERT INTO teams (id, name, status, created_at, updated_at) "
                "VALUES (?, ?, 'active', '2026-10-01', '2026-10-01')",
                (team_id, team_id),
            )
        pending = [
            {"email": "a@x.com", "seat_type": "prolite"},
            {"email": "b@x.com", "seat_type": "default"},
            {"email": "c@x.com"},
            {"email": "d@x.com", "seat_type": "automation"},
            {"email": "e@x.com", "seat_type": "prolite"},
        ]
        conn.execute(
            "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES ('c1', '[]', ?, 'x')",
            (json.dumps(pending),),
        )
        conn.execute(
            "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES ('c2', '[]', '{bad', 'x')"
        )
        conn.commit()
        conn.close()

    def test_counts_per_raw_type_with_missing_as_default(self):
        expected = {"prolite": 2, "default": 2, "automation": 1}
        self.assertEqual(asyncio.run(get_team("c1"))["pending_invite_counts"], expected)
        listed = {t["id"]: t for t in asyncio.run(list_teams())}
        self.assertEqual(listed["c1"]["pending_invite_counts"], expected)

    def test_unparseable_or_missing_cache_gives_empty(self):
        self.assertEqual(asyncio.run(get_team("c2"))["pending_invite_counts"], {})
        self.assertEqual(asyncio.run(get_team("c3"))["pending_invite_counts"], {})
        listed = {t["id"]: t for t in asyncio.run(list_teams())}
        self.assertEqual(listed["c2"]["pending_invite_counts"], {})
        self.assertEqual(listed["c3"]["pending_invite_counts"], {})

    def test_policy_patch_helper_path_is_filled(self):
        from app.routes.teams import _team_row_to_response

        conn = sqlite3.connect(app_database.get_db_path())
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM teams WHERE id = 'c1'").fetchone()
        conn.close()
        self.assertEqual(_team_row_to_response(row)["pending_invite_counts"]["prolite"], 2)
