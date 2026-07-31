import json
from typing import Any

from ..database import get_db

# 静态兜底汇率:1 USD 兑各币种数量(2026-07 参考值)
DEFAULT_FX_RATES: dict[str, float] = {
    "USD": 1.0, "CNY": 7.2, "THB": 36.0, "NZD": 1.65, "AUD": 1.50, "CAD": 1.37,
    "GBP": 0.78, "EUR": 0.92, "JPY": 155.0, "SGD": 1.34, "HKD": 7.8,
    "KRW": 1380.0, "INR": 84.0, "BRL": 5.6, "MXN": 18.5, "SEK": 10.5,
    "NOK": 10.8, "DKK": 6.9, "CHF": 0.88, "PLN": 4.0, "CZK": 23.0,
}


def convert(amount: float, from_currency: str, to_currency: str, rates: dict[str, float]) -> float | None:
    """Convert amount from one currency to another.

    Args:
        amount: Amount to convert
        from_currency: Source currency code
        to_currency: Target currency code
        rates: Exchange rates dictionary (1 USD = rates[currency])

    Returns:
        Converted amount, or None if currencies not in rates
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency not in rates or to_currency not in rates:
        return None

    if from_currency == to_currency:
        return amount

    # amount / rates[from] * rates[to]
    return amount / rates[from_currency] * rates[to_currency]


async def get_fx_config() -> dict[str, Any]:
    """Get FX configuration from settings table.

    Returns:
        Dict with keys: base_currency, rates, fx_updated_at, low_balance_threshold
    """
    from datetime import datetime, timezone

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?)",
            ("finance_base_currency", "finance_fx_rates", "finance_fx_updated_at", "finance_low_balance_threshold")
        )
        rows = await cursor.fetchall()

    settings = {row["key"]: row["value"] for row in rows}

    base_currency = (settings.get("finance_base_currency") or "USD").upper()

    # Parse FX rates JSON
    rates = DEFAULT_FX_RATES.copy()
    fx_rates_json = settings.get("finance_fx_rates")
    if fx_rates_json:
        try:
            parsed_rates = json.loads(fx_rates_json)
            if isinstance(parsed_rates, dict):
                rates.update(parsed_rates)
        except (json.JSONDecodeError, TypeError):
            pass

    fx_updated_at = settings.get("finance_fx_updated_at")

    try:
        low_balance_threshold = float(settings.get("finance_low_balance_threshold", "0"))
    except (ValueError, TypeError):
        low_balance_threshold = 0.0

    return {
        "base_currency": base_currency,
        "rates": rates,
        "fx_updated_at": fx_updated_at,
        "low_balance_threshold": low_balance_threshold,
    }


async def save_fx_rates(rates: dict[str, float]) -> None:
    """Save FX rates to settings table.

    Args:
        rates: Exchange rates dictionary
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    rates_json = json.dumps(rates)

    async with get_db() as db:
        await db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            ("finance_fx_rates", rates_json, now),
        )
        await db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            ("finance_fx_updated_at", now, now),
        )
        await db.commit()
