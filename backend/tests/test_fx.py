"""app.services.fx: currency conversion and the daily rate refresh."""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app.services import fx
from app.services.fx import (
    DEFAULT_FX_RATES,
    FxRefreshError,
    convert,
    get_fx_config,
    refresh_fx_rates,
    refresh_fx_rates_safely,
    save_fx_rates,
)
from app.services.pricing import CURRENCY_TO_COUNTRY


class FXTest(unittest.TestCase):
    def test_convert_normal_conversion(self):
        """Test converting USD to THB"""
        # 100 USD = 100 * 36 = 3600 THB
        result = convert(100, "USD", "THB", DEFAULT_FX_RATES)
        self.assertEqual(result, 3600.0)

    def test_convert_case_insensitive(self):
        """Test that conversion is case insensitive"""
        result1 = convert(100, "usd", "thb", DEFAULT_FX_RATES)
        result2 = convert(100, "USD", "THB", DEFAULT_FX_RATES)
        self.assertEqual(result1, result2)

    def test_convert_same_currency(self):
        """Test converting to same currency"""
        result = convert(100, "USD", "USD", DEFAULT_FX_RATES)
        self.assertEqual(result, 100.0)

    def test_convert_unknown_source_currency(self):
        """Test converting from unknown currency returns None"""
        result = convert(100, "XXX", "THB", DEFAULT_FX_RATES)
        self.assertIsNone(result)

    def test_convert_unknown_target_currency(self):
        """Test converting to unknown currency returns None"""
        result = convert(100, "USD", "YYY", DEFAULT_FX_RATES)
        self.assertIsNone(result)

    def test_convert_both_unknown(self):
        """Test converting unknown to unknown returns None"""
        result = convert(100, "XXX", "YYY", DEFAULT_FX_RATES)
        self.assertIsNone(result)

    def test_convert_zero_amount(self):
        """Test converting zero amount"""
        result = convert(0, "USD", "THB", DEFAULT_FX_RATES)
        self.assertEqual(result, 0.0)

    def test_default_fx_rates_covers_pricing_currencies(self):
        """Test that DEFAULT_FX_RATES covers all CURRENCY_TO_COUNTRY currencies"""
        pricing_currencies = set(CURRENCY_TO_COUNTRY.keys())
        fx_currencies = set(DEFAULT_FX_RATES.keys())

        # All pricing currencies should be in FX rates; extra FX currencies are fine.
        missing = pricing_currencies - fx_currencies
        self.assertEqual(len(missing), 0, f"Missing currencies in DEFAULT_FX_RATES: {missing}")
        self.assertTrue(len(pricing_currencies) > 0)


def _ok_response(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


class FxRefreshTest(unittest.TestCase):
    def setUp(self):
        start_temp_db(self)

    def test_success_saves_only_supported_currencies(self):
        payload = {"result": "success", "rates": {"USD": 1, "GBP": 0.5, "ZZZ": 9.0}}
        with patch.object(fx.requests, "get", return_value=_ok_response(payload)) as get:
            count = asyncio.run(refresh_fx_rates())
        self.assertEqual(count, 2)
        self.assertEqual(get.call_args.kwargs["timeout"], fx.FX_API_TIMEOUT_SECONDS)
        config = asyncio.run(get_fx_config())
        self.assertEqual(config["rates"]["GBP"], 0.5)
        self.assertNotIn("ZZZ", config["rates"])
        self.assertIsNotNone(config["fx_updated_at"])

    def test_failure_keeps_old_rates_and_does_not_raise_in_safe_wrapper(self):
        asyncio.run(save_fx_rates({"USD": 1.0, "GBP": 0.11}))
        with patch.object(fx.requests, "get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(FxRefreshError):
                asyncio.run(refresh_fx_rates())
            with self.assertLogs(fx.logger, "WARNING"):
                self.assertFalse(asyncio.run(refresh_fx_rates_safely()))
        self.assertEqual(asyncio.run(get_fx_config())["rates"]["GBP"], 0.11)

    def test_unsuccessful_api_result_keeps_old_rates(self):
        asyncio.run(save_fx_rates({"USD": 1.0, "GBP": 0.11}))
        with patch.object(fx.requests, "get", return_value=_ok_response({"result": "error"})):
            self.assertFalse(asyncio.run(refresh_fx_rates_safely()))
        self.assertEqual(asyncio.run(get_fx_config())["rates"]["GBP"], 0.11)

    def test_only_if_stale_skips_fresh_rates(self):
        asyncio.run(save_fx_rates({"USD": 1.0, "GBP": 0.11}))
        with patch.object(fx.requests, "get") as get:
            self.assertFalse(asyncio.run(refresh_fx_rates_safely(only_if_stale=True)))
        get.assert_not_called()

    def test_staleness_check(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(fx._fx_is_stale(None))
        self.assertTrue(fx._fx_is_stale((now - timedelta(hours=25)).isoformat()))
        self.assertFalse(fx._fx_is_stale((now - timedelta(hours=1)).isoformat()))


if __name__ == "__main__":
    unittest.main()
