"""财务总览里的 Premium 未知单价与 ChatGPT 计费席位数。"""
import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import insert_row, start_temp_db

from app.routes.finance import get_overview


def _iso_in_days(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class PremiumFinanceTest(unittest.TestCase):
    def setUp(self):
        self.db_path = start_temp_db(self)

    def _insert_team(self, **fields):
        fields.setdefault("status", "active")
        fields.setdefault("created_at", "2026-10-01")
        fields.setdefault("updated_at", "2026-10-01")
        fields.setdefault("billing_period", "monthly")
        fields.setdefault("billing_currency", "USD")
        fields.setdefault("will_renew", 1)
        fields.setdefault("active_until", _iso_in_days(20))
        conn = sqlite3.connect(self.db_path)
        insert_row(conn, "teams", fields)
        conn.commit()
        conn.close()

    def _team(self, result, team_id):
        return next(item for item in result["teams"] if item["team_id"] == team_id)

    def test_unknown_premium_and_billed_chatgpt_seats(self):
        self._insert_team(
            id="p1", name="P1", price_per_seat=25.0, seats_entitled=5, seats_in_use=3,
            seat_capacity_json=json.dumps(
                {"default": {"paid": 3, "available": 1}, "prolite": {"paid": 2, "available": 0}}
            ),
        )
        result = asyncio.run(get_overview())
        team = self._team(result, "p1")
        self.assertEqual(team["chatgpt_seats_billed"], 3)
        self.assertEqual(team["premium_seats_paid"], 2)
        # ChatGPT 月费只乘 default 已付席位，不含 Premium。
        self.assertIsNone(team["monthly_total_native"])
        self.assertEqual(result["monthly_total_base"], 0.0)

    def test_unknown_capacity_falls_back_to_entitled_and_zero_premium(self):
        self._insert_team(
            id="p2", name="P2", price_per_seat=25.0, seats_entitled=5, seats_in_use=3,
        )
        result = asyncio.run(get_overview())
        team = self._team(result, "p2")
        self.assertEqual(team["chatgpt_seats_billed"], 5)
        self.assertEqual(team["premium_seats_paid"], 0)
        self.assertAlmostEqual(team["monthly_total_native"], 125.0)

    def test_capacity_without_premium_entry_is_zero_premium(self):
        self._insert_team(
            id="p3", name="P3", price_per_seat=10.0, seats_entitled=4,
            seat_capacity_json=json.dumps({"default": {"paid": 4, "available": 0}}),
        )
        team = self._team(asyncio.run(get_overview()), "p3")
        self.assertEqual(team["chatgpt_seats_billed"], 4)
        self.assertEqual(team["premium_seats_paid"], 0)

    def test_no_fx_rate_gives_unknown_total(self):
        self._insert_team(
            id="p4", name="P4", price_per_seat=25.0, seats_entitled=2,
            seat_capacity_json=json.dumps({"prolite": {"paid": 1, "available": 0}}),
        )
        with patch("app.routes.finance.convert", return_value=None):
            result = asyncio.run(get_overview())
        team = self._team(result, "p4")


if __name__ == "__main__":
    unittest.main()
