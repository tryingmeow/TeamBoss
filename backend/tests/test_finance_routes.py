"""/api/finance 的后端：财务总览（get_overview）和单个 Team 的发票（Team 卡片账单弹窗）。

发票部分覆盖发票接口的 limit / refresh / summary，以及 Team 接口带出的 invoice_count。
"""
import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import insert_row, start_temp_db

from app import database as app_database
from app.routes import finance
from app.routes.finance import get_overview, get_team_invoices, team_invoice_summary
from app.routes.teams import get_team, list_teams
from app.services.fx import DEFAULT_FX_RATES

# ═══ 财务总览 ═══

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


# ═══ 单个 Team 的发票 ═══

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _invoice(n, status="paid", currency="THB", due=1560.0, paid=None, days_ago=0.0):
    return {
        "invoice_id": f"in_{n}",
        "number": f"N-{n}",
        "status": status,
        "currency": currency,
        "amount_due": due,
        "amount_paid": due if paid is None else paid,
        "period_start": _iso(days_ago),
        "period_end": _iso(days_ago - 30),
        "description": "2 × ChatGPT Business",
        "hosted_invoice_url": f"https://invoice.stripe.com/i/{n}",
        "created_at": _iso(days_ago),
    }


class InvoiceSummaryTest(unittest.TestCase):
    def test_only_paid_amounts_are_summed(self):
        rows = [
            _invoice(5, status="open", paid=0.0, days_ago=1),
            _invoice(4, days_ago=10),
            _invoice(3, status="void", paid=0.0, days_ago=40),
            _invoice(2, days_ago=40),
            _invoice(1, status="uncollectible", paid=0.0, days_ago=70),
        ]
        summary = team_invoice_summary(rows, "USD", DEFAULT_FX_RATES, now=NOW)
        self.assertEqual(summary["invoice_count"], 5)
        self.assertEqual(summary["paid_count"], 2)
        self.assertEqual(summary["paid_total"]["amounts"], [{"currency": "THB", "amount": 3120.0}])
        self.assertAlmostEqual(summary["paid_total"]["base"], 3120.0 / 36.0, places=2)
        self.assertEqual(summary["paid_last_30_days"]["amounts"], [{"currency": "THB", "amount": 1560.0}])
        # 最新一期 = 最新的非作废发票；未支付用应付金额。
        latest = summary["latest_invoice"]
        self.assertEqual((latest["invoice_id"], latest["status"], latest["display_amount"]), ("in_5", "open", 1560.0))
        self.assertAlmostEqual(latest["display_amount_base"], 1560.0 / 36.0, places=2)

    def test_currencies_are_kept_apart_and_base_needs_every_rate(self):
        rows = [_invoice(2, currency="GBP", due=36.0, days_ago=5), _invoice(1, currency="thb", days_ago=50)]
        summary = team_invoice_summary(rows, "USD", DEFAULT_FX_RATES, now=NOW)
        self.assertEqual(
            summary["paid_total"]["amounts"],
            [{"currency": "GBP", "amount": 36.0}, {"currency": "THB", "amount": 1560.0}],
        )
        self.assertAlmostEqual(summary["paid_total"]["base"], 36.0 / 0.78 + 1560.0 / 36.0, places=2)
        unknown = team_invoice_summary([_invoice(1, currency="XYZ")], "USD", DEFAULT_FX_RATES, now=NOW)
        self.assertEqual(unknown["paid_total"]["amounts"], [{"currency": "XYZ", "amount": 1560.0}])
        self.assertIsNone(unknown["paid_total"]["base"])

    def test_no_invoices(self):
        summary = team_invoice_summary([], "USD", DEFAULT_FX_RATES, now=NOW)
        self.assertEqual(summary["paid_total"], {"amounts": [], "base": None})
        self.assertIsNone(summary["latest_invoice"])
        voided = team_invoice_summary([_invoice(1, status="void", paid=0.0)], "USD", DEFAULT_FX_RATES, now=NOW)
        self.assertIsNone(voided["latest_invoice"])
        self.assertEqual(voided["paid_count"], 0)


class InvoiceEndpointTest(unittest.TestCase):
    def setUp(self):
        start_temp_db(self)
        conn = sqlite3.connect(app_database.get_db_path())
        for team_id in ("t1", "t2"):
            conn.execute(
                "INSERT INTO teams (id, name, owner_email, status, created_at) VALUES (?, ?, 'o@example.com', 'active', '2026-05-01')",
                (team_id, team_id.upper()),
            )
        for n in range(1, 9):
            row = _invoice(n, days_ago=(8 - n) * 30)
            conn.execute(
                """INSERT INTO invoices (team_id, invoice_id, number, status, currency, amount_due, amount_paid,
                       period_start, period_end, description, hosted_invoice_url, created_at, fetched_at)
                   VALUES ('t1', :invoice_id, :number, :status, :currency, :amount_due, :amount_paid,
                       :period_start, :period_end, :description, :hosted_invoice_url, :created_at, :created_at)""",
                row,
            )
        conn.commit()
        conn.close()
        upstream = patch.object(finance, "refresh_invoices_for_team_blocking")
        self.upstream = upstream.start()
        self.addCleanup(upstream.stop)

    def test_default_lists_six_and_summary_covers_all(self):
        result = asyncio.run(get_team_invoices("t1", limit=6, refresh=True))
        self.assertEqual([row["invoice_id"] for row in result["invoices"]], [f"in_{n}" for n in range(8, 2, -1)])
        self.assertNotIn("created_at", result["invoices"][0])
        self.assertEqual(result["summary"]["invoice_count"], 8)
        self.assertEqual(result["summary"]["paid_total"]["amounts"], [{"currency": "THB", "amount": 1560.0 * 8}])
        longer = asyncio.run(get_team_invoices("t1", limit=100, refresh=False))
        self.assertEqual(len(longer["invoices"]), 8)
        self.upstream.assert_not_called()

    def test_refresh_false_never_goes_upstream(self):
        result = asyncio.run(get_team_invoices("t2", limit=100, refresh=False))
        self.assertEqual(result["invoices"], [])
        self.upstream.assert_not_called()
        asyncio.run(get_team_invoices("t2", limit=6, refresh=True))
        self.upstream.assert_called_once()

    def test_team_endpoints_carry_invoice_count(self):
        self.assertEqual(asyncio.run(get_team("t1"))["invoice_count"], 8)
        listed = {team["id"]: team for team in asyncio.run(list_teams())}
        self.assertEqual(listed["t1"]["invoice_count"], 8)
        self.assertEqual(listed["t2"]["invoice_count"], 0)


if __name__ == "__main__":
    unittest.main()
