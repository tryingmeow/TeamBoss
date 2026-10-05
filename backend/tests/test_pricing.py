import _isolation  # noqa: F401  must precede any app import
import asyncio
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.pricing import (
    account_billing_updates,
    billing_symbol_from_pricing,
    discounted_monthly_total,
    fetch_seat_pricing,
    price_per_seat_from_pricing,
    resolve_pricing_country_code,
)


class PricingTest(unittest.TestCase):
    def test_resolve_country_from_billing_currency(self):
        subscription = {"billing_currency": "NZD", "price_country": None}
        self.assertEqual(resolve_pricing_country_code(subscription, "NZD"), "NZ")

    def test_resolve_country_from_price_country(self):
        subscription = {"billing_currency": "NZD", "price_country": "NZ"}
        self.assertEqual(resolve_pricing_country_code(subscription, "NZD"), "NZ")

    def test_resolve_country_falls_back_to_stored_value(self):
        subscription = {"billing_currency": "XXX", "price_country": None}
        self.assertEqual(resolve_pricing_country_code(subscription, "XXX", "TH"), "TH")

    def test_price_per_seat_from_monthly_pricing(self):
        pricing = {
            "currency_config": {
                "business": {
                    "month": {"amount": 41.0},
                    "year": {"amount": 33.0},
                }
            }
        }
        self.assertEqual(price_per_seat_from_pricing(pricing, "monthly"), 41.0)

    def test_price_per_seat_from_yearly_pricing(self):
        pricing = {
            "currency_config": {
                "business": {
                    "month": {"amount": 780.0},
                    "year": {"amount": 630.0},
                }
            }
        }
        self.assertEqual(price_per_seat_from_pricing(pricing, "yearly"), 630.0)

    def test_billing_symbol_from_pricing(self):
        pricing = {"currency_config": {"symbol": "NZ$", "symbol_code": "NZD"}}
        self.assertEqual(billing_symbol_from_pricing(pricing), "NZ$")

    def test_account_billing_updates_extracts_codex_and_discount(self):
        account_info = {
            "accounts": {
                "team-1": {
                    "account": {"is_usage_based_seat_enabled": True},
                    "entitlement": {
                        "applied_discounts": [
                            {
                                "amount": 52,
                                "duration_num_periods": 48,
                                "discount_expires_at": "2030-05-19T17:16:55+00:00",
                                "quantity_off": None,
                                "promo_campaign_id": "smb-channel-partner-adaca-NZ-NZD",
                            }
                        ]
                    },
                }
            }
        }

        updates = account_billing_updates(account_info, "team-1")

        self.assertEqual(updates["is_codex_enabled"], 1)
        self.assertEqual(updates["discount_amount"], 52.0)
        self.assertEqual(updates["discount_duration_num_periods"], 48)
        self.assertEqual(updates["promo_campaign_id"], "smb-channel-partner-adaca-NZ-NZD")

    def test_discounted_monthly_total_never_goes_negative(self):
        self.assertEqual(discounted_monthly_total(41, 2, 52), 30.0)
        self.assertEqual(discounted_monthly_total(41, 1, 52), 0.0)

    def test_nonmonthly_subscription_explicitly_clears_stale_monthly_price(self):
        async def run_call(_func, *_args):
            return {
                "currency_config": {
                    "symbol": "$",
                    "business": {"year": {"amount": 300}},
                }
            }

        class Client:
            get_billing_pricing_config = object()
            get_pricing_config = object()

        updates = asyncio.run(fetch_seat_pricing(
            Client(),
            {"billing_currency": "USD", "billing_period": "yearly"},
            run_call=run_call,
        ))

        self.assertEqual(updates["billing_period"], "yearly")
        self.assertIsNone(updates["price_per_seat"])

    def test_pricing_failure_explicitly_clears_stale_price_and_symbol(self):
        async def run_call(_func, *_args):
            return {"error": "pricing unavailable"}

        class Client:
            get_billing_pricing_config = object()
            get_pricing_config = object()

        updates = asyncio.run(fetch_seat_pricing(
            Client(),
            {"billing_currency": "USD", "billing_period": "monthly"},
            run_call=run_call,
        ))

        self.assertIsNone(updates["price_per_seat"])
        self.assertIsNone(updates["billing_symbol"])


if __name__ == "__main__":
    unittest.main()
