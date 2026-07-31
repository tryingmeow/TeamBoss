import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.fx import convert, DEFAULT_FX_RATES
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

        # All pricing currencies should be in FX rates
        missing = pricing_currencies - fx_currencies
        self.assertEqual(len(missing), 0, f"Missing currencies in DEFAULT_FX_RATES: {missing}")

        # All FX rates should have pricing info (not strictly required but good to check)
        extra = fx_currencies - pricing_currencies
        # It's okay to have extra currencies in FX rates, but at least pricing ones should be there
        self.assertTrue(len(pricing_currencies) > 0)
        self.assertTrue(all(c in fx_currencies for c in pricing_currencies))


if __name__ == "__main__":
    unittest.main()
