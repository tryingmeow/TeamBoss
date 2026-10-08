"""Premium 的真实单价和年付：从同一次 pricing 响应里读 business / business_prolite 的 month 或
year 桶（按订阅的计费周期），带上 price_period 存；财务总览、Team 列表、续费提醒、超员确认、
计费快照都用它；缺失的单价与费用保持未知，不使用固定估算。

价格用的是公开的泰铢定价（ChatGPT「管理席位」里写的 标准 ฿780 + 税费/月、高级版 ฿3,900 + 税费/月；
年付方案的月价 630 / 3150，一年 = 月价 × 12，按公开定价推断）。
"""
import _isolation  # noqa: F401  must precede any app import
from _seat_fixtures import (
    INVITE_TEAM as TEAM,
    NOW,
    BatchHarness,
    InviteHarness,
    _capacity,
    _full_default_client,
    _renewal_team as _reminder_team,
    capacity_entries,
)

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app import database as app_database
from app import tg_bot
from app.routes import finance as finance_route
from app.routes.finance import get_overview
from app.routes.teams import get_team, list_teams
from app.services import pricing
from app.services.pricing import (
    fetch_seat_pricing,
    fetch_seat_pricing_sync,
    premium_price_per_seat_from_pricing,
    price_per_seat_from_pricing,
    priced_period,
    seat_charge_text,
    seat_cost_totals,
    seat_price_info,
    team_monthly_cost,
    subscription_billing_updates,
    account_billing_updates,
    effective_discount,
)

YEARLY_NOTE = "年付加购按 ChatGPT 规则结算，以账单为准"
from app.services.renewal_reminders import render_reminder_text, renewal_idle_seats


def _thb_pricing(**plans):
    """一次真实形状的 THB pricing 响应。``plans`` 覆盖 / 删掉（值为 None）某个计划。"""
    config = {
        "business": {
            "month": {"amount": 780.0, "tax": "exclusive"},
            "year": {"amount": 630.0, "tax": "exclusive"},
        },
        "business_prolite": {
            "month": {"amount": 3900.0, "tax": "exclusive"},
            "year": {"amount": 3150.0, "tax": "exclusive"},
        },
        # 个人版 Pro Lite：含税、价格不同，不是 Business 的 Premium 席位。
        "prolite": {"month": {"amount": 3350.0, "tax": "inclusive", "psp_override": {"amount": 3350.0}}},
        "pro": {"month": {"amount": 6700.0, "tax": "inclusive"}},
        "plus": {"month": {"amount": 670.0, "tax": "inclusive"}},
        "business_non_profit": {"month": {"amount": 390.0, "tax": "exclusive"}},
        "symbol": "฿",
        "symbol_code": "THB",
        "tax_percent": 7.0,
        "minor_unit_exponent": 2,
    }
    for key, value in plans.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    return {"currency_config": config}


class _Client:
    get_billing_pricing_config = object()
    get_pricing_config = object()


class _Recorder:
    """记下每一次上游调用；第一个 pricing 接口回 ``first``，备用接口回 ``fallback``。"""

    def __init__(self, first, fallback=None):
        self.first = first
        self.fallback = fallback if fallback is not None else first
        self.calls = []

    def _answer(self, func, args):
        self.calls.append((func, args))
        return self.first if func is _Client.get_billing_pricing_config else self.fallback

    def sync(self, func, *args):
        return self._answer(func, args)

    async def run(self, func, *args):
        return self._answer(func, args)


def _fetch_both(subscription, recorder_factory):
    """异步、同步两条同步路径各跑一次，返回 [(updates, recorder), ...]。"""
    results = []
    rec = recorder_factory()
    results.append((asyncio.run(fetch_seat_pricing(_Client(), subscription, run_call=rec.run)), rec))
    rec = recorder_factory()
    results.append((fetch_seat_pricing_sync(_Client(), subscription, run_call=rec.sync), rec))
    return zip(("async", "sync"), results)


MONTHLY_THB = {"billing_currency": "THB", "billing_period": "monthly"}


class PricingParseTest(unittest.TestCase):
    def test_business_prolite_is_parsed_from_the_same_response(self):
        for path, (updates, rec) in _fetch_both(MONTHLY_THB, lambda: _Recorder(_thb_pricing())):
            with self.subTest(path=path):
                self.assertEqual(updates["price_per_seat"], 780.0)
                self.assertEqual(updates["premium_price_per_seat"], 3900.0)
                self.assertEqual(updates["price_period"], "monthly")
                self.assertEqual(updates["billing_symbol"], "฿")
                # 零额外请求：只有原本那一次 pricing 请求。
                self.assertEqual(rec.calls, [(_Client.get_billing_pricing_config, ("THB",))])

    def test_fallback_country_lookup_adds_no_premium_request(self):
        # billing_currency 那条失败时退到国家代码，和改动前一样是两次，Premium 不多请求。
        for _path, (updates, rec) in _fetch_both(
            MONTHLY_THB, lambda: _Recorder({"error": "no billing pricing"}, _thb_pricing())
        ):
            self.assertEqual(updates["premium_price_per_seat"], 3900.0)
            self.assertEqual(
                [func for func, _args in rec.calls],
                [_Client.get_billing_pricing_config, _Client.get_pricing_config],
            )

    def test_consumer_prolite_key_is_never_used(self):
        # 只有个人版 prolite（3350，含税）：Premium 单价是未知，不是 3350。
        pricing_without_business_premium = _thb_pricing(business_prolite=None)
        self.assertIsNone(premium_price_per_seat_from_pricing(pricing_without_business_premium))
        for _path, (updates, _rec) in _fetch_both(MONTHLY_THB, lambda: _Recorder(pricing_without_business_premium)):
            self.assertIsNone(updates["premium_price_per_seat"])
            self.assertEqual(updates["price_per_seat"], 780.0)
        # 两个键都在时取的是 business_prolite。
        self.assertEqual(premium_price_per_seat_from_pricing(_thb_pricing()), 3900.0)

    def test_yearly_billing_stores_the_year_bucket(self):
        yearly = {"billing_currency": "THB", "billing_period": "yearly"}
        for path, (updates, rec) in _fetch_both(yearly, lambda: _Recorder(_thb_pricing())):
            with self.subTest(path=path):
                self.assertEqual(updates["billing_period"], "yearly")
                # year 桶（年付方案的月价），不是 month 桶的 780 / 3900。
                self.assertEqual(updates["price_per_seat"], 630.0)
                self.assertEqual(updates["premium_price_per_seat"], 3150.0)
                self.assertEqual(updates["price_period"], "yearly")
                self.assertEqual(len(rec.calls), 1)

    def test_yearly_without_a_year_bucket_is_unknown_not_the_month_price(self):
        pricing_payload = _thb_pricing(
            business={"month": {"amount": 780.0}}, business_prolite={"month": {"amount": 3900.0}}
        )
        yearly = {"billing_currency": "THB", "billing_period": "yearly"}
        for _path, (updates, _rec) in _fetch_both(yearly, lambda: _Recorder(pricing_payload)):
            self.assertIsNone(updates["price_per_seat"])
            self.assertIsNone(updates["premium_price_per_seat"])

    def test_unknown_billing_period_stores_no_price(self):
        for period in (None, "quarterly", "Monthly"):
            subscription = {"billing_currency": "THB", "billing_period": period}
            for _path, (updates, _rec) in _fetch_both(subscription, lambda: _Recorder(_thb_pricing())):
                with self.subTest(period=period):
                    self.assertIsNone(updates["price_per_seat"])
                    self.assertIsNone(updates["premium_price_per_seat"])
                    self.assertIsNone(updates["price_period"])
        self.assertIsNone(price_per_seat_from_pricing(_thb_pricing(), "quarterly"))

    def test_pricing_failure_clears_premium_price(self):
        for _path, (updates, _rec) in _fetch_both(MONTHLY_THB, lambda: _Recorder({"error": "down"})):
            self.assertIn("premium_price_per_seat", updates)
            self.assertIsNone(updates["premium_price_per_seat"])

    def test_malformed_amounts_are_unknown(self):
        for bad in ("3900", True, 0, -1, None, [3900]):
            with self.subTest(amount=bad):
                pricing_payload = _thb_pricing(business_prolite={"month": {"amount": bad}})
                self.assertIsNone(premium_price_per_seat_from_pricing(pricing_payload))
        self.assertIsNone(premium_price_per_seat_from_pricing({"currency_config": {"business_prolite": []}}))
        self.assertIsNone(premium_price_per_seat_from_pricing({"currency_config": None}))


def _row(**fields):
    row = {
        "billing_period": "monthly",
        "price_period": "monthly",
        "billing_currency": "THB",
        "billing_symbol": "฿",
        "price_per_seat": 780.0,
        "premium_price_per_seat": 3900.0,
        "seats_entitled": 7,
        "seat_capacity_json": json.dumps(
            {"default": {"paid": 5, "available": 0}, "prolite": {"paid": 2, "available": 0}}
        ),
        "discount_amount": 999.0,
    }
    row.update(fields)
    return row


def _yearly_row(**fields):
    """年付 THB Team：ChatGPT 630、Premium 3150（年付方案的月价），折扣 999 是每年的。"""
    return _row(**{ "billing_period": "yearly", "price_period": "yearly", "price_per_seat": 630.0,
                "premium_price_per_seat": 3150.0, **fields})


# 年付：一年 = max(0, 630 × 12 × 5 − 999) + 3150 × 12 × 2；月均 = 一年 / 12。
YEARLY_ANNUAL = (630 * 12 * 5 - 999) + 3150 * 12 * 2
YEARLY_MONTHLY = YEARLY_ANNUAL / 12
MONTHLY_TOTAL = 780 * 5 - 999 + 3900 * 2


class SubscriptionDiscountTest(unittest.TestCase):
    def test_per_team_fixed_amount_and_explicit_absence(self):
        for amount in (52, 999, 1350):
            state = subscription_billing_updates({"billing_period": "monthly", "entitlement": {
                "discount": {"discount_type": "fixed", "amount": amount, "duration_num_periods": 48}
            }})
            self.assertEqual(state["discount_amount"], amount)
            self.assertEqual(state["discount_duration_num_periods"], 48)
        self.assertEqual(subscription_billing_updates({"entitlement": {"discount": None}})["discount_amount"], 0)
        self.assertEqual(subscription_billing_updates({"entitlement": {"applied_discounts": []}})["discount_amount"], 0)

    def test_unknown_or_unsupported_is_not_a_fixed_discount(self):
        for entitlement in (None, {}, {"discount": {"discount_type": "percent", "amount": 25}},
                            {"discount": {"amount": 999}}, {"applied_discounts": "invalid"}):
            self.assertIsNone(subscription_billing_updates({"entitlement": entitlement})["discount_amount"])
        self.assertNotIn("discount_amount", subscription_billing_updates({"error": "offline"}))
        self.assertNotIn("discount_amount", subscription_billing_updates({"billing_period": "monthly"}))

    def test_explicit_account_id_never_borrows_another_team(self):
        account = {"accounts": {"other": {"account": {"structure": "workspace"}, "entitlement": {
            "discount": {"discount_type": "fixed", "amount": 88}}}}}
        self.assertEqual(account_billing_updates(account, "wanted"), {})

    def test_expiry_future_start_and_unknown_discount(self):
        now = datetime(2032, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(effective_discount(_row(discount_expires_at="2031-12-31T00:00:00Z"), now=now), 0)
        self.assertEqual(effective_discount(_row(discount_expires_at="2032-01-02T00:00:00Z"), now=now), 999)
        self.assertEqual(effective_discount(_row(discount_start_in_num_periods=1), now=now), 0)
        self.assertIsNone(effective_discount(_row(discount_amount=None), now=now))
        self.assertIsNone(team_monthly_cost(_row(discount_amount=None)).monthly_total)

    def test_monthly_yearly_no_discount_and_variable_discount(self):
        for make, months in ((_row, 1), (_yearly_row, 12)):
            for discount in (0, 52, 999, 1350):
                row = make(discount_amount=discount)
                cost = team_monthly_cost(row)
                expected = max(0, row["price_per_seat"] * 5 * months - discount) + row["premium_price_per_seat"] * 2 * months
                self.assertEqual(cost.period_total, expected)
                self.assertEqual(cost.monthly_total, expected / months)
            cost = team_monthly_cost(make(discount_amount=100000))
            self.assertEqual(cost.discount_monthly, make()["price_per_seat"] * 5)

    def test_premium_only_does_not_require_unused_standard_price(self):
        row = _row(price_per_seat=None, seat_capacity_json=json.dumps({
            "default": {"paid": 0, "available": 0}, "prolite": {"paid": 2, "available": 0}}))
        self.assertEqual(team_monthly_cost(row).monthly_total, 7800)



class PricedPeriodTest(unittest.TestCase):
    """年付的价格永远不会被当成月付价格乘。"""

    def test_price_only_counts_for_the_period_it_was_read_for(self):
        self.assertEqual(priced_period(_row()), "monthly")
        self.assertEqual(priced_period(_yearly_row()), "yearly")
        mismatched = {
            "yearly price, now billed monthly": _row(price_period="yearly", price_per_seat=630.0),
            "monthly price, now billed yearly": _row(billing_period="yearly"),
            "legacy row (no price_period) billed yearly": _row(billing_period="yearly", price_period=None),
            "unknown period": _row(billing_period=None),
            "unrecognised period": _row(billing_period="quarterly", price_period="quarterly"),
        }
        for label, row in mismatched.items():
            with self.subTest(label):
                self.assertIsNone(priced_period(row))
                cost = team_monthly_cost(row)
                self.assertIsNone(cost.price_per_seat)
                self.assertIsNone(cost.monthly_total)
                self.assertIsNone(cost.period_total)
                self.assertIsNone(seat_price_info(row, "default"))

    def test_legacy_monthly_row_without_price_period_still_counts(self):
        # 加 price_period 之前的行：那时只有月付 Team 存单价。
        legacy = _row(price_period=None)
        self.assertEqual(priced_period(legacy), "monthly")
        self.assertAlmostEqual(team_monthly_cost(legacy).monthly_total, MONTHLY_TOTAL)


class TeamMonthlyCostTest(unittest.TestCase):
    def test_premium_is_added_and_discount_comes_off_once(self):
        cost = team_monthly_cost(_row())
        self.assertEqual((cost.chatgpt_seats_billed, cost.premium_seats_paid), (5, 2))
        self.assertAlmostEqual(cost.premium_subtotal, 7800.0)
        self.assertAlmostEqual(cost.monthly_subtotal, 780 * 5 + 3900 * 2)
        self.assertAlmostEqual(cost.monthly_total, MONTHLY_TOTAL)
        self.assertAlmostEqual(cost.period_total, MONTHLY_TOTAL)
        self.assertAlmostEqual(cost.discount_monthly, 999.0)
        self.assertEqual(cost.premium_price_source, "upstream")

    def test_yearly_team_monthly_equivalent_and_annual_figure(self):
        cost = team_monthly_cost(_yearly_row())
        self.assertEqual(cost.billing_period, "yearly")
        self.assertEqual(cost.months_per_period, 12)
        self.assertEqual((cost.price_per_seat, cost.premium_price_per_seat), (630.0, 3150.0))
        self.assertAlmostEqual(cost.premium_subtotal, 3150 * 2)
        self.assertAlmostEqual(cost.period_subtotal, (630 * 5 + 3150 * 2) * 12)
        self.assertAlmostEqual(cost.period_total, YEARLY_ANNUAL)
        self.assertAlmostEqual(cost.monthly_total, YEARLY_MONTHLY)

    def test_discount_is_per_billing_period(self):
        # 同一个 999 的固定折扣：月付每月减 999，年付每年减 999（月均 83.25）。
        monthly = team_monthly_cost(_row())
        yearly = team_monthly_cost(_yearly_row())
        self.assertAlmostEqual(monthly.discount_monthly, 999.0)
        self.assertAlmostEqual(monthly.monthly_total, MONTHLY_TOTAL)
        self.assertAlmostEqual(yearly.discount_monthly, 999 / 12)
        self.assertAlmostEqual(yearly.period_total, YEARLY_ANNUAL)
        self.assertAlmostEqual(yearly.monthly_total, YEARLY_MONTHLY)
        # 年折扣比一年的 ChatGPT 部分还大：减到 0 为止，不去抵 Premium。
        big = team_monthly_cost(_yearly_row(discount_amount=50000.0))
        self.assertAlmostEqual(big.period_total, 3150 * 12 * 2)

    def test_discount_beyond_the_chatgpt_part_is_not_taken_off_premium(self):
        cost = team_monthly_cost(_row(seat_capacity_json=json.dumps(
            {"default": {"paid": 1, "available": 0}, "prolite": {"paid": 1, "available": 0}}
        )))
        # 780 - 999 → 0（不低于 0），Premium 3900 原价照算：月费只会多算不会少算。
        self.assertAlmostEqual(cost.monthly_total, 3900.0)

    def test_zero_premium_seats(self):
        cost = team_monthly_cost(_row(seat_capacity_json=json.dumps({"default": {"paid": 5, "available": 0}})))
        self.assertEqual(cost.premium_seats_paid, 0)
        self.assertAlmostEqual(cost.monthly_total, 780 * 5 - 999)

    def test_unknown_premium_price_makes_total_unknown(self):
        cost = team_monthly_cost(_row(premium_price_per_seat=None))
        self.assertIsNone(cost.premium_price_source)
        self.assertIsNone(cost.premium_subtotal)
        # 有已付席位但单价未知，合计也未知。
        self.assertIsNone(cost.monthly_total)

    def test_yearly_team_without_premium_price_has_unknown_total(self):
        cost = team_monthly_cost(_yearly_row(premium_price_per_seat=None))
        self.assertIsNone(cost.premium_price_source)
        self.assertIsNone(cost.monthly_total)

    def test_unknown_period_has_no_cost_and_no_estimate(self):
        cost = team_monthly_cost(_row(billing_period=None, price_period=None))
        self.assertIsNone(cost.monthly_total)
        self.assertIsNone(cost.price_per_seat)
        self.assertIsNone(cost.premium_price_per_seat)
        self.assertIsNone(cost.premium_price_source)
        self.assertIsNone(cost.discount_monthly)


FX = {
    "base_currency": "USD",
    "rates": {"USD": 1.0, "THB": 32.5},
    "fx_updated_at": None,
    "low_balance_threshold": 0.0,
}


def _iso_in_days(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class _DbTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()

    def _execute(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def _insert_team(self, team_id, yearly=False, **fields):
        row = _yearly_row(**fields) if yearly else _row(**fields)
        row.update({
            "id": team_id,
            "name": f"{team_id}-name",
            "status": "active",
            "will_renew": 1,
            "active_until": _iso_in_days(20),
            "created_at": "2026-10-01",
            "updated_at": "2026-10-01",
        })
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._execute(f"INSERT INTO teams ({columns}) VALUES ({marks})", tuple(row.values()))


class FinanceOverviewTest(_DbTest):
    def overview(self):
        with patch.object(finance_route, "get_fx_config", new=AsyncMock(return_value=dict(FX))):
            result = asyncio.run(get_overview())
        return result, {team["team_id"]: team for team in result["teams"]}

    def test_n_premium_seats_use_the_real_price_and_convert(self):
        self._insert_team("thb")
        result, teams = self.overview()
        team = teams["thb"]
        native = 780 * 5 - 999 + 3900 * 2
        self.assertEqual(team["premium_price_source"], "upstream")
        self.assertEqual(team["premium_price_per_seat"], 3900.0)
        self.assertAlmostEqual(team["premium_price_per_seat_base"], 3900 / 32.5)
        self.assertAlmostEqual(team["price_per_seat_base"], 780 / 32.5)
        self.assertAlmostEqual(team["premium_monthly_native"], 7800.0)
        self.assertAlmostEqual(team["premium_monthly_base"], 7800 / 32.5)
        self.assertAlmostEqual(team["monthly_total_native"], native)
        self.assertAlmostEqual(team["monthly_total_base"], native / 32.5)
        self.assertAlmostEqual(team["period_total_native"], native)
        self.assertAlmostEqual(result["monthly_total_base"], native / 32.5)
        self.assertAlmostEqual(result["premium_monthly_base_total"], 7800 / 32.5)
        self.assertAlmostEqual(result["discount_total_base"], 999 / 32.5)
        # 时间线（续费日）的金额也是含 Premium 的月费。
        self.assertAlmostEqual(result["timeline"][0]["amount_native"], native)

    def test_mixed_monthly_and_yearly_teams(self):
        self._insert_team("thb")
        self._insert_team("thb-yr", yearly=True)
        result, teams = self.overview()
        yearly = teams["thb-yr"]
        self.assertEqual(yearly["billing_period"], "yearly")
        self.assertEqual((yearly["price_per_seat"], yearly["premium_price_per_seat"]), (630.0, 3150.0))
        self.assertAlmostEqual(yearly["monthly_total_native"], YEARLY_MONTHLY)
        self.assertAlmostEqual(yearly["monthly_total_base"], YEARLY_MONTHLY / 32.5)
        self.assertAlmostEqual(yearly["period_total_native"], YEARLY_ANNUAL)
        self.assertAlmostEqual(yearly["premium_monthly_native"], 3150 * 2)
        self.assertEqual(yearly["premium_price_source"], "upstream")
        # 月预计支出：月付的月费 + 年付的月均，都换成基准币种。
        self.assertAlmostEqual(result["monthly_total_base"], (MONTHLY_TOTAL + YEARLY_MONTHLY) / 32.5)
        self.assertEqual(result["excluded_teams_count"], 0)
        self.assertAlmostEqual(result["premium_monthly_base_total"], (7800 + 6300) / 32.5)
        # 折扣每月折算：月付 999，年付 999 / 12。
        self.assertAlmostEqual(result["discount_total_base"], (999 + 999 / 12) / 32.5)
        # 续费那天扣的是一个计费周期：年付 Team 是一年的钱。
        amounts = {item["team_id"]: item for item in result["timeline"]}
        self.assertAlmostEqual(amounts["thb"]["amount_native"], MONTHLY_TOTAL)
        self.assertAlmostEqual(amounts["thb-yr"]["amount_native"], YEARLY_ANNUAL)
        self.assertEqual(amounts["thb-yr"]["billing_period"], "yearly")

    def test_zero_premium_seats(self):
        self._insert_team("thb", seat_capacity_json=json.dumps({"default": {"paid": 5, "available": 0}}))
        result, teams = self.overview()
        self.assertAlmostEqual(teams["thb"]["monthly_total_native"], 780 * 5 - 999)
        self.assertEqual(teams["thb"]["premium_seats_paid"], 0)
        self.assertAlmostEqual(teams["thb"]["premium_monthly_native"], 0.0)
        self.assertEqual(result["premium_monthly_base_total"], 0.0)

    def test_missing_premium_price_excludes_unknown_total(self):
        self._insert_team("thb")
        self._insert_team("usd", billing_currency="USD", billing_symbol="$", price_per_seat=30.0,
                          premium_price_per_seat=None, discount_amount=0.0,
                          seat_capacity_json=json.dumps(
                              {"default": {"paid": 2, "available": 0}, "prolite": {"paid": 1, "available": 0}}))
        result, teams = self.overview()
        usd = teams["usd"]
        self.assertIsNone(usd["premium_price_source"])
        self.assertIsNone(usd["premium_price_per_seat"])
        self.assertIsNone(usd["premium_monthly_native"])
        # 未知费用不进汇总；价格完整的 Team 照常计入。
        self.assertIsNone(usd["monthly_total_native"])
        thb_native = 780 * 5 - 999 + 3900 * 2
        self.assertAlmostEqual(result["monthly_total_base"], thb_native / 32.5)
        self.assertAlmostEqual(result["premium_monthly_base_total"], 7800 / 32.5)

    def test_unknown_period_shows_no_number(self):
        self._insert_team("unk", billing_period=None, price_period=None)
        result, teams = self.overview()
        team = teams["unk"]
        self.assertIsNone(team["monthly_total_native"])
        self.assertIsNone(team["period_total_native"])
        self.assertIsNone(team["price_per_seat"])
        self.assertIsNone(team["premium_price_per_seat"])
        self.assertIsNone(team["premium_price_source"])
        self.assertEqual(result["excluded_teams_count"], 1)

    def test_invoice_with_premium_reconciles(self):
        # 上期账单含 Premium：月费里有了 Premium，对账才对得上。
        self._insert_team("thb")
        self._execute(
            """INSERT INTO invoices (team_id, invoice_id, status, currency, amount_due, amount_paid,
                                     period_start, period_end, created_at)
               VALUES ('thb', 'in_placeholder', 'paid', 'THB', ?, ?, ?, ?, ?)""",
            (10701.0, 10701.0, _iso_in_days(-30), _iso_in_days(0), _iso_in_days(-30)),
        )
        _result, teams = self.overview()
        self.assertEqual(teams["thb"]["latest_invoice"]["reconciliation"], "match")

    def test_renewal_prediction_drops_discount_expiring_before_renewal(self):
        self._insert_team("ending", discount_expires_at=_iso_in_days(10))
        result, teams = self.overview()
        self.assertEqual(teams["ending"]["monthly_total_native"], MONTHLY_TOTAL)
        renewal = next(item for item in result["timeline"] if item["team_id"] == "ending")
        self.assertEqual(renewal["amount_native"], 780 * 5 + 3900 * 2)

    def test_yearly_invoice_reconciles_against_the_annual_total(self):
        self._insert_team("thb-yr", yearly=True)
        self._execute(
            """INSERT INTO invoices (team_id, invoice_id, status, currency, amount_due, amount_paid,
                                     period_start, period_end, created_at)
               VALUES ('thb-yr', 'in_placeholder', 'paid', 'THB', ?, ?, ?, ?, ?)""",
            (YEARLY_ANNUAL, YEARLY_ANNUAL, _iso_in_days(-345), _iso_in_days(20), _iso_in_days(-345)),
        )
        _result, teams = self.overview()
        self.assertEqual(teams["thb-yr"]["latest_invoice"]["reconciliation"], "match")


class TeamsApiTest(_DbTest):
    def test_team_response_carries_premium_price_and_full_monthly_total(self):
        self._insert_team("thb")
        team = asyncio.run(get_team("thb"))
        self.assertEqual(team["premium_price_per_seat"], 3900.0)
        self.assertAlmostEqual(team["monthly_subtotal"], 780 * 5 + 3900 * 2)
        self.assertAlmostEqual(team["monthly_total"], 780 * 5 - 999 + 3900 * 2)
        listed = {t["id"]: t for t in asyncio.run(list_teams())}
        self.assertEqual(listed["thb"]["premium_price_per_seat"], 3900.0)

    def test_yearly_team_shows_monthly_equivalent_and_annual_total(self):
        self._insert_team("thb-yr", yearly=True)
        team = asyncio.run(get_team("thb-yr"))
        self.assertEqual((team["price_per_seat"], team["premium_price_per_seat"]), (630.0, 3150.0))
        self.assertAlmostEqual(team["monthly_total"], YEARLY_MONTHLY)
        self.assertAlmostEqual(team["period_total"], YEARLY_ANNUAL)

    def test_unknown_period_hides_prices(self):
        self._insert_team("unk", billing_period=None, price_period=None)
        team = asyncio.run(get_team("unk"))
        self.assertIsNone(team["price_per_seat"])
        self.assertIsNone(team["premium_price_per_seat"])
        self.assertIsNone(team["monthly_total"])
        self.assertIsNone(team["period_total"])


class RenewalReminderTest(unittest.TestCase):
    def _text(self, **overrides):
        team = _reminder_team(
            seat_capacity_json=_capacity(default=(2, 1, 2), prolite=(2, 1, 2)),
            seat_type_counts_json=json.dumps({"default": 1, "prolite": 1}),
            billing_symbol="฿",
            **overrides,
        )
        result = renewal_idle_seats(team, "[]", now=NOW)
        self.assertEqual(result.idle_of("prolite"), 1)
        return render_reminder_text(team, result)

    def test_real_premium_price_is_used(self):
        text = self._text(premium_price_per_seat=3900.0)
        self.assertIn("780 THB/月（ChatGPT） + 3,900 THB/月（Premium），税费另计", text)
        self.assertNotIn("估算", text)

    def test_missing_premium_price_is_unknown(self):
        text = self._text(premium_price_per_seat=None)
        self.assertIn("Premium 单价未知", text)

    def test_yearly_team_shows_monthly_equivalent_and_year(self):
        text = self._text(billing_period="yearly", price_period="yearly", price_per_seat=630.0,
                          premium_price_per_seat=3150.0)
        self.assertIn(
            "630 THB/月（ChatGPT，年付，一年 7,560 THB） + 3,150 THB/月（Premium，年付，一年 37,800 THB），税费另计",
            text,
        )
        self.assertNotIn("估算", text)

    def test_unknown_period_says_price_unknown(self):
        text = self._text(billing_period=None, premium_price_per_seat=3900.0)
        self.assertIn("ChatGPT 单价未知 + Premium 单价未知", text)
        self.assertNotIn("估算", text)
        self.assertNotIn("3,900", text)


class SeatPriceTextTest(unittest.TestCase):
    def test_charge_text(self):
        price = seat_price_info(_row(), "default")
        self.assertEqual(price, {"amount": 780.0, "currency": "THB", "symbol": "฿", "period": "monthly"})
        self.assertEqual(seat_charge_text(price, 1), "约 +฿780 + 税/月")
        self.assertEqual(seat_charge_text(price, 3), "约 +฿2,340 + 税/月（每席 ฿780）")
        self.assertEqual(seat_price_info(_row(), "prolite")["amount"], 3900.0)
        self.assertEqual(seat_charge_text(None, 2), "单价未知，以 ChatGPT 账单为准")
        self.assertIsNone(seat_price_info(_row(billing_period=None), "default"))
        yearly = seat_price_info(_yearly_row(), "default")
        self.assertEqual(yearly, {"amount": 630.0, "currency": "THB", "symbol": "฿", "period": "yearly"})
        self.assertEqual(seat_charge_text(yearly, 1), f"约 +฿630 + 税/月（年付，一年 ฿7,560），{YEARLY_NOTE}")
        self.assertEqual(
            seat_charge_text(yearly, 3),
            f"约 +฿1,890 + 税/月（年付，一年 ฿22,680；每席 ฿630/月），{YEARLY_NOTE}",
        )
        self.assertEqual(seat_price_info(_yearly_row(), "prolite")["amount"], 3150.0)
        self.assertIsNone(seat_price_info(_row(), "usage_based"))
        no_symbol = seat_price_info(_row(billing_symbol=None), "default")
        self.assertEqual(seat_charge_text(no_symbol, 1), "约 +780 THB + 税/月")

    def test_cost_totals_never_add_different_currencies(self):
        thb = seat_price_info(_row(), "default")
        usd = seat_price_info(_row(billing_currency="USD", billing_symbol="$", price_per_seat=30.0), "default")
        thb_yearly = seat_price_info(_yearly_row(), "default")
        totals = seat_cost_totals([(thb, 2), (usd, 1), (thb, 1), (None, 4), (thb_yearly, 2)])
        self.assertEqual(
            totals,
            [
                {"amount": 2340.0, "currency": "THB", "symbol": "฿", "period": "monthly"},
                {"amount": 30.0, "currency": "USD", "symbol": "$", "period": "monthly"},
                # 同币种的年付不和月付相加。
                {"amount": 1260.0, "currency": "THB", "symbol": "฿", "period": "yearly"},
            ],
        )


class OverageConfirmationPriceTest(InviteHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="confirm")

    def _price(self, **fields):
        fields = {"billing_period": "monthly", "price_period": "monthly", "billing_currency": "THB",
                  "billing_symbol": "฿", "price_per_seat": 780.0, "premium_price_per_seat": 3900.0, **fields}
        conn = self._conn()
        conn.execute(
            f"UPDATE teams SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
            (*fields.values(), TEAM),
        )
        conn.commit()
        conn.close()

    def test_chatgpt_confirmation_carries_the_seat_price(self):
        self._price()
        _response, exc = self.invite(_full_default_client())
        detail = self.assert_refused(exc, "require_overage_confirmation")
        self.assertEqual(
            detail["seat_price"], {"amount": 780.0, "currency": "THB", "symbol": "฿", "period": "monthly"}
        )
        self.assertNotIn("年付", detail["message"])
        self.assertTrue(detail["message"].endswith("自动加购 1 个 ChatGPT 席位并扣费，约 +฿780 + 税/月。"))

    def test_premium_confirmation_uses_the_premium_price(self):
        self._price()
        client = _full_default_client(seat_capacity=capacity_entries(default=(2, 0), prolite=(1, 0)))
        _response, exc = self.invite(client, seat_type="prolite")
        detail = self.assert_refused(exc, "require_overage_confirmation", seat_type="prolite")
        self.assertEqual(detail["seat_price"]["amount"], 3900.0)
        self.assertIn("自动加购 1 个 Premium 席位并扣费，约 +฿3,900 + 税/月。", detail["message"])

    def test_yearly_team_shows_the_yearly_price(self):
        self._price(billing_period="yearly", price_period="yearly", price_per_seat=630.0,
                    premium_price_per_seat=3150.0)
        _response, exc = self.invite(_full_default_client())
        detail = self.assert_refused(exc, "require_overage_confirmation")
        self.assertEqual(detail["seat_price"]["period"], "yearly")
        self.assertEqual(detail["seat_price"]["amount"], 630.0)
        self.assertTrue(detail["message"].endswith(
            f"并扣费，约 +฿630 + 税/月（年付，一年 ฿7,560），{YEARLY_NOTE}。"
        ))

    def test_monthly_price_on_a_now_yearly_team_is_not_used(self):
        self._price(billing_period="yearly")  # price_period 还是 monthly
        _response, exc = self.invite(_full_default_client())
        detail = self.assert_refused(exc, "require_overage_confirmation")
        self.assertIsNone(detail["seat_price"])
        self.assertIn("并扣费，单价未知，以 ChatGPT 账单为准。", detail["message"])

    def test_unknown_period_says_unknown_instead_of_guessing(self):
        self._price(billing_period=None, price_period=None)
        _response, exc = self.invite(_full_default_client())
        detail = self.assert_refused(exc, "require_overage_confirmation")
        self.assertIsNone(detail["seat_price"])
        self.assertIn("并扣费，单价未知，以 ChatGPT 账单为准。", detail["message"])

    def test_forbid_mentions_no_money(self):
        self._price()
        _response, exc = self.invite(_full_default_client(), policy="forbid")
        detail = self.assert_refused(exc, "overage_forbidden")
        self.assertNotIn("seat_price", detail)
        self.assertNotIn("฿", detail["message"])


class BatchConfirmationPriceTest(BatchHarness):
    def test_plan_and_totals_carry_the_price(self):
        self.team("t-confirm", policy="confirm", created_at="2026-10-02")
        conn = self._conn()
        conn.execute(
            """UPDATE teams SET billing_period = 'monthly', billing_currency = 'THB', billing_symbol = '฿',
                                price_per_seat = 780.0, premium_price_per_seat = 3900.0
               WHERE id = 't-confirm'"""
        )
        conn.commit()
        conn.close()

        _result, exc = self.submit(["a@example.com", "b@example.com"])

        detail = exc.detail
        self.assertEqual(detail["overage_plan"][0]["seat_price"]["amount"], 780.0)
        self.assertEqual(
            detail["cost_totals"], [{"amount": 1560.0, "currency": "THB", "symbol": "฿", "period": "monthly"}]
        )
        self.assertIn("「t-confirm-name」2 个，约 +฿1,560 + 税/月（每席 ฿780）", detail["message"])

    def test_yearly_plan_carries_the_yearly_price(self):
        self.team("t-confirm", policy="confirm", created_at="2026-10-02")
        conn = self._conn()
        conn.execute(
            """UPDATE teams SET billing_period = 'yearly', price_period = 'yearly', billing_currency = 'THB',
                                billing_symbol = '฿', price_per_seat = 630.0, premium_price_per_seat = 3150.0
               WHERE id = 't-confirm'"""
        )
        conn.commit()
        conn.close()

        _result, exc = self.submit(["a@example.com", "b@example.com"])

        detail = exc.detail
        self.assertEqual(
            detail["cost_totals"], [{"amount": 1260.0, "currency": "THB", "symbol": "฿", "period": "yearly"}]
        )
        self.assertIn(
            f"「t-confirm-name」2 个，约 +฿1,260 + 税/月（年付，一年 ฿15,120；每席 ฿630/月），{YEARLY_NOTE}",
            detail["message"],
        )

    def test_unknown_price_is_not_in_the_totals(self):
        self.team("t-confirm", policy="confirm", created_at="2026-10-02")
        _result, exc = self.submit(["a@example.com"])
        self.assertIsNone(exc.detail["overage_plan"][0]["seat_price"])
        self.assertEqual(exc.detail["cost_totals"], [])
        self.assertIn("单价未知，以 ChatGPT 账单为准", exc.detail["message"])


class TelegramInviteCardTest(_DbTest):
    def test_card_states_the_seat_price(self):
        self._insert_team("thb")
        self.assertEqual(tg_bot._team_chatgpt_seat_charge("thb"), "约 +฿780 + 税/月")
        self._insert_team("yr", yearly=True)
        self.assertEqual(
            tg_bot._team_chatgpt_seat_charge("yr"), f"约 +฿630 + 税/月（年付，一年 ฿7,560），{YEARLY_NOTE}"
        )
        self._insert_team("unk", billing_period=None, price_period=None)
        self.assertEqual(tg_bot._team_chatgpt_seat_charge("unk"), "单价未知，以 ChatGPT 账单为准")
        self.assertEqual(tg_bot._team_chatgpt_seat_charge("missing"), "单价未知，以 ChatGPT 账单为准")


# ── 定时同步：同一次 pricing 请求写入 Premium 单价，计费快照含 Premium ──────────

class _FakeSchedulerClient:
    pricing_calls: list = []
    billing_period = "monthly"
    entitlement = {"discount": None}

    def __init__(self, access_token, team_id, device_id, proxy_url=None):
        self.team_id = team_id

    def get_subscription(self):
        return {
            "seats_in_use": 7,
            "seats_entitled": 7,
            "billing_currency": "THB",
            "billing_period": _FakeSchedulerClient.billing_period,
            "entitlement": _FakeSchedulerClient.entitlement,
            "active_start": "2026-10-01T00:00:00+00:00",
            "active_until": "2026-11-01T00:00:00+00:00",
            "will_renew": True,
            "seat_capacity": [
                {"type": "default", "paid": 5, "available": 0},
                {"type": "prolite", "paid": 2, "available": 0},
            ],
        }

    def get_seat_type_counts(self):
        return {"seat_type_counts": {"default": 5, "prolite": 2, "usage_based": 0}}

    def get_remaining_balance(self):
        return {"balance": "0"}

    def get_payment_methods(self):
        return {"payment_methods": []}

    def get_account_info(self):
        return {"accounts": {self.team_id: {"entitlement": {"discount": {"discount_type": "fixed", "amount": 88}}}}}

    def get_workspace_settings(self):
        return {}

    def get_billing_pricing_config(self, currency):
        _FakeSchedulerClient.pricing_calls.append(("billing", currency))
        return _thb_pricing()

    def get_pricing_config(self, country):
        _FakeSchedulerClient.pricing_calls.append(("country", country))
        return _thb_pricing()

    def get_members(self, offset=0, limit=100):
        return {"items": [{"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}], "total": 1}

    def get_pending_invites(self, offset=0, limit=100):
        return {"items": [], "total": 0}


class SchedulerSyncTest(_DbTest):
    def _sync(self, billing_period, entitlement=None):
        self._execute(
            """INSERT INTO teams (id, name, status, access_token, device_id, created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', 'stub-token', 'stub-device', '2026-10-01', '2026-10-01')"""
        )
        from app import scheduler as app_scheduler
        from app.services import patrol as patrol_service
        from app.services import tg_notify, tg_summary

        _FakeSchedulerClient.pricing_calls = []
        _FakeSchedulerClient.billing_period = billing_period
        _FakeSchedulerClient.entitlement = entitlement if entitlement is not None else {"discount": None}
        with patch.object(app_scheduler, "ChatGPTClient", _FakeSchedulerClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_recovery_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_failure_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None), \
             patch.object(patrol_service, "run_patrol", MagicMock(return_value={})), \
             patch.object(tg_notify, "notify_admins_sync", lambda *a, **kw: None), \
             patch.object(tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None):
            app_scheduler.data_sync_job()

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        team = conn.execute(
            "SELECT price_per_seat, premium_price_per_seat, price_period, billing_symbol FROM teams"
            " WHERE id = 'team-1'"
        ).fetchone()
        snapshot = conn.execute(
            "SELECT monthly_total, billing_period, premium_price_per_seat, premium_seats_paid"
            " FROM billing_snapshots WHERE team_id = 'team-1'"
        ).fetchone()
        conn.close()
        return dict(team), dict(snapshot)

    def test_scheduled_sync_stores_premium_price_and_snapshot_includes_it(self):
        team, snapshot = self._sync("monthly")
        self.assertEqual(_FakeSchedulerClient.pricing_calls, [("billing", "THB")])
        self.assertEqual((team["price_per_seat"], team["premium_price_per_seat"]), (780.0, 3900.0))
        self.assertEqual(team["price_period"], "monthly")
        self.assertEqual(team["billing_symbol"], "฿")
        self.assertAlmostEqual(snapshot["monthly_total"], 780 * 5 + 3900 * 2)
        self.assertEqual(snapshot["billing_period"], "monthly")
        self.assertEqual((snapshot["premium_price_per_seat"], snapshot["premium_seats_paid"]), (3900.0, 2))

    def test_current_subscription_discount_overrides_account_discount(self):
        _team, snapshot = self._sync("monthly", {"discount": {"discount_type": "fixed", "amount": 999}})
        self.assertEqual(snapshot["monthly_total"], MONTHLY_TOTAL)

    def test_scheduled_sync_of_a_yearly_team(self):
        team, snapshot = self._sync("yearly")
        self.assertEqual(_FakeSchedulerClient.pricing_calls, [("billing", "THB")])
        self.assertEqual((team["price_per_seat"], team["premium_price_per_seat"]), (630.0, 3150.0))
        self.assertEqual(team["price_period"], "yearly")
        # 快照记月均。
        self.assertAlmostEqual(snapshot["monthly_total"], 630 * 5 + 3150 * 2)
        self.assertEqual(snapshot["billing_period"], "yearly")


class MigrationTest(unittest.TestCase):
    def test_existing_database_gains_the_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app_database, "get_db_dir", return_value=tmp):
                asyncio.run(app_database.init_database())
                # 第二次启动（列已存在）不报错。
                asyncio.run(app_database.init_database())
                conn = sqlite3.connect(app_database.get_db_path())
                team_cols = {row[1] for row in conn.execute("PRAGMA table_info(teams)")}
                snap_cols = {row[1] for row in conn.execute("PRAGMA table_info(billing_snapshots)")}
                conn.close()
        self.assertTrue({"premium_price_per_seat", "price_period", "discount_start_in_num_periods"} <= team_cols)
        self.assertTrue({"premium_price_per_seat", "premium_seats_paid", "billing_period"} <= snap_cols)


if __name__ == "__main__":
    unittest.main()
