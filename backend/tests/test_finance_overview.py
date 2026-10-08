import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import insert_row, start_temp_db

from app.routes.finance import get_overview


def _iso_in_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class FinanceOverviewTest(unittest.TestCase):
    """/api/finance/overview 的冒烟测试。

    这个接口一度整块 500（读 teams 行时用了 sqlite3.Row 没有的 .get()），
    前端财务卡片全变成 "—" 而测试全绿——因为当时没有任何用例调过它。
    """

    def setUp(self):
        self.db_path = start_temp_db(self)

    def _insert_team(self, **fields):
        fields.setdefault("status", "active")
        fields.setdefault("created_at", "2026-07-21")
        fields.setdefault("updated_at", "2026-07-21")
        conn = sqlite3.connect(self.db_path)
        insert_row(conn, "teams", fields)
        conn.commit()
        conn.close()

    def test_monthly_team_reports_a_real_monthly_total(self):
        """回归用例：整个接口能跑通，且月付队伍算得出钱。"""
        self._insert_team(
            id="team-monthly",
            name="Monthly Team",
            owner_email="owner@example.com",
            billing_period="monthly",
            billing_currency="USD",
            price_per_seat=25.0,
            seats_entitled=5,
            seats_in_use=3,
            codex_count=1,
            chatgpt_count=2,
            will_renew=1,
            active_until=_iso_in_days(20),
        )

        result = asyncio.run(get_overview())

        self.assertEqual(result["base_currency"], "USD")
        self.assertEqual(result["excluded_teams_count"], 0)
        self.assertAlmostEqual(result["monthly_total_base"], 125.0)

        team = result["teams"][0]
        self.assertEqual(team["billing_period"], "monthly")
        self.assertEqual(team["price_per_seat"], 25.0)
        self.assertEqual(team["chatgpt_in_use"], 2)
        self.assertAlmostEqual(team["monthly_total_native"], 125.0)
        self.assertEqual(len(result["timeline"]), 1)

    def test_unknown_billing_period_is_excluded_instead_of_guessed(self):
        """年付/未知周期不许拿单价当月费用，只能计入"算不出"。"""
        self._insert_team(
            id="team-unknown",
            name="Unknown Period Team",
            owner_email="owner2@example.com",
            billing_period=None,
            billing_currency="USD",
            price_per_seat=300.0,
            seats_entitled=5,
            seats_in_use=5,
            will_renew=1,
            active_until=_iso_in_days(30),
        )

        result = asyncio.run(get_overview())

        self.assertEqual(result["monthly_total_base"], 0.0)
        self.assertEqual(result["excluded_teams_count"], 1)

        team = result["teams"][0]
        self.assertIsNone(team["billing_period"])
        self.assertIsNone(team["price_per_seat"])
        self.assertIsNone(team["monthly_total_native"])
        self.assertIsNone(team["monthly_total_base"])

    def test_low_balance_alert_reads_like_the_ui(self):
        self._insert_team(
            id="team-low",
            name="Low Team",
            owner_email="owner4@example.com",
            billing_period="monthly",
            billing_currency="USD",
            billing_symbol="$",
            price_per_seat=25.0,
            seats_entitled=2,
            seats_in_use=1,
            balance="-300.0000",
            will_renew=1,
            active_until=_iso_in_days(30),
        )

        result = asyncio.run(get_overview())

        alerts = [a for a in result["alerts"] if a["type"] == "low_balance"]
        self.assertEqual([a["detail"] for a in alerts], ["Credit 余额为负 · -300"])

    def test_low_but_non_negative_balance_keeps_threshold_wording(self):
        self._insert_team(
            id="team-thin",
            name="Thin Team",
            owner_email="owner5@example.com",
            billing_period="monthly",
            billing_currency="USD",
            billing_symbol="$",
            price_per_seat=25.0,
            seats_entitled=2,
            seats_in_use=1,
            balance="0",
            will_renew=1,
            active_until=_iso_in_days(30),
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('finance_low_balance_threshold', '10', 'x')"
        )
        conn.commit()
        conn.close()

        result = asyncio.run(get_overview())

        details = [a["detail"] for a in result["alerts"] if a["type"] == "low_balance"]
        self.assertEqual(details, ["Credit 余额 0 低于阈值 10"])

    def test_missing_currency_balance_and_renewal_are_not_fabricated(self):
        self._insert_team(
            id="team-missing",
            name="Missing Data Team",
            owner_email="owner3@example.com",
            billing_period="monthly",
            billing_currency=None,
            price_per_seat=25.0,
            seats_entitled=2,
            seats_in_use=1,
            balance=None,
            will_renew=None,
            active_until=_iso_in_days(30),
        )

        result = asyncio.run(get_overview())

        self.assertEqual(result["monthly_total_base"], 0.0)
        self.assertEqual(result["excluded_teams_count"], 1)
        self.assertEqual(result["alerts"], [])
        team = result["teams"][0]
        self.assertEqual(team["billing_currency"], "")
        self.assertIsNone(team["monthly_total_base"])
        self.assertIsNone(team["balance"])


if __name__ == "__main__":
    unittest.main()
