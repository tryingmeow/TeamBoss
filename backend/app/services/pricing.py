from typing import Any

from ..chatgpt_limiter import run_chatgpt_call

CURRENCY_TO_COUNTRY: dict[str, str] = {
    "THB": "TH",
    "NZD": "NZ",
    "USD": "US",
    "AUD": "AU",
    "CAD": "CA",
    "GBP": "GB",
    "EUR": "EU",
    "JPY": "JP",
    "SGD": "SG",
    "HKD": "HK",
    "KRW": "KR",
    "INR": "IN",
    "BRL": "BR",
    "MXN": "MX",
    "SEK": "SE",
    "NOK": "NO",
    "DKK": "DK",
    "CHF": "CH",
    "PLN": "PL",
    "CZK": "CZ",
}


def resolve_pricing_country_code(
    subscription: dict[str, Any],
    billing_currency: str,
    fallback_country_code: str | None = None,
) -> str | None:
    price_country = subscription.get("price_country")
    if price_country:
        return str(price_country).upper()

    mapped = CURRENCY_TO_COUNTRY.get(billing_currency.upper()) if billing_currency else None
    if mapped:
        return mapped

    if fallback_country_code:
        return fallback_country_code.upper()

    # Genuinely unknown — do not guess a country. Callers must treat this as
    # "no data" (skip the field) rather than defaulting to any specific market.
    return None


def price_per_seat_from_pricing(pricing: dict[str, Any], billing_period: str = "monthly") -> float | None:
    if "error" in pricing:
        return None

    currency_config = pricing.get("currency_config", {})
    business = currency_config.get("business", {})
    period_key = "year" if billing_period == "yearly" else "month"
    period = business.get(period_key, {})
    amount = period.get("amount")
    if amount is None:
        return None
    return float(amount)


def billing_symbol_from_pricing(pricing: dict[str, Any]) -> str | None:
    if "error" in pricing:
        return None
    currency_config = pricing.get("currency_config", {})
    symbol = currency_config.get("symbol")
    if symbol:
        return str(symbol)
    symbol_code = currency_config.get("symbol_code")
    return str(symbol_code) if symbol_code else None


def _selected_account_entry(account_info: dict[str, Any], team_id: str | None) -> dict[str, Any]:
    accounts = account_info.get("accounts", {}) if isinstance(account_info, dict) else {}
    if not isinstance(accounts, dict):
        return {}
    if team_id and isinstance(accounts.get(team_id), dict):
        return accounts[team_id]
    for value in accounts.values():
        if isinstance(value, dict) and value.get("account", {}).get("structure") == "workspace":
            return value
    return {}


def account_billing_updates(account_info: dict[str, Any], team_id: str | None = None) -> dict[str, Any]:
    if "error" in account_info:
        return {}

    entry = _selected_account_entry(account_info, team_id)
    account = entry.get("account", {}) if isinstance(entry.get("account"), dict) else {}
    entitlement = entry.get("entitlement", {}) if isinstance(entry.get("entitlement"), dict) else {}
    discounts = entitlement.get("applied_discounts")
    discount = None
    if isinstance(discounts, list) and discounts:
        discount = next((item for item in discounts if isinstance(item, dict)), None)
    if discount is None and isinstance(entitlement.get("discount"), dict):
        discount = entitlement["discount"]

    updates: dict[str, Any] = {
        "is_codex_enabled": 1 if account.get("is_usage_based_seat_enabled") else 0,
        "discount_amount": 0.0,
        "discount_duration_num_periods": None,
        "discount_expires_at": None,
        "discount_quantity_off": None,
        "promo_campaign_id": None,
    }

    if isinstance(discount, dict):
        amount = discount.get("amount") or 0
        try:
            updates["discount_amount"] = float(amount)
        except (TypeError, ValueError):
            updates["discount_amount"] = 0.0
        updates["discount_duration_num_periods"] = discount.get("duration_num_periods")
        updates["discount_expires_at"] = discount.get("discount_expires_at")
        updates["discount_quantity_off"] = discount.get("quantity_off")
        updates["promo_campaign_id"] = discount.get("promo_campaign_id")

    return updates


def discounted_monthly_total(price_per_seat: Any, seats_entitled: Any, discount_amount: Any = 0) -> float:
    """Multiply price_per_seat by seats and subtract the discount.

    Caller's responsibility: price_per_seat must already be a confirmed MONTHLY
    price. fetch_seat_pricing/fetch_seat_pricing_sync only populate
    teams.price_per_seat when billing_period == "monthly", so any price read
    back from the teams table is safe to pass here. Do not pass a yearly bucket
    price — this function has no way to detect that and will silently return a
    wildly inflated "monthly" total (no /12 normalization is performed).
    Missing/invalid inputs resolve to 0.0 rather than raising, so callers never
    crash — but 0.0 here means "no data", not "this Team costs nothing".
    """
    try:
        subtotal = float(price_per_seat) * int(seats_entitled)
    except (TypeError, ValueError):
        subtotal = 0.0
    try:
        discount = float(discount_amount or 0)
    except (TypeError, ValueError):
        discount = 0.0
    return max(0.0, subtotal - discount)


async def fetch_seat_pricing(
    client,
    subscription: dict[str, Any],
    *,
    fallback_country_code: str | None = None,
    run_call=run_chatgpt_call,
) -> dict[str, Any]:
    if "error" in subscription:
        return {}

    # No fallback here — an OpenAI subscription payload missing these fields is
    # genuinely "unknown", not "THB" / "monthly". Guessing a currency or billing
    # period is exactly the kind of fabricated data this function must not produce.
    billing_currency = subscription.get("billing_currency")
    billing_period = subscription.get("billing_period")
    country_code = resolve_pricing_country_code(
        subscription,
        billing_currency or "",
        fallback_country_code,
    )

    # billing_period always gets recorded (even as NULL) — it comes straight from
    # a subscription call that already succeeded, so it's the honest current
    # truth, not a fallback masking a failure.
    updates: dict[str, Any] = {
        "billing_period": billing_period,
        "price_per_seat": None,
        "billing_symbol": None,
    }
    if country_code:
        updates["country_code"] = country_code

    pricing: dict[str, Any] = {"error": "no billing_currency or country_code available"}
    if billing_currency:
        pricing = await run_call(client.get_billing_pricing_config, billing_currency)
    if "error" in pricing and country_code:
        pricing = await run_call(client.get_pricing_config, country_code)

    # Only persist a per-seat price when the subscription is confirmed monthly.
    # This project only supports monthly billing today; storing a yearly bucket
    # price here would silently get multiplied by seat count downstream as if it
    # were a monthly figure. Better to leave price_per_seat untouched (unknown)
    # than to display a wildly wrong "monthly" total.
    if billing_period == "monthly":
        price_per_seat = price_per_seat_from_pricing(pricing, billing_period)
        if price_per_seat is not None:
            updates["price_per_seat"] = price_per_seat

    billing_symbol = billing_symbol_from_pricing(pricing)
    if billing_symbol:
        updates["billing_symbol"] = billing_symbol

    return updates


def fetch_seat_pricing_sync(
    client,
    subscription: dict[str, Any],
    *,
    fallback_country_code: str | None = None,
    run_call,
) -> dict[str, Any]:
    if "error" in subscription:
        return {}

    billing_currency = subscription.get("billing_currency")
    billing_period = subscription.get("billing_period")
    country_code = resolve_pricing_country_code(
        subscription,
        billing_currency or "",
        fallback_country_code,
    )

    updates: dict[str, Any] = {
        "billing_period": billing_period,
        "price_per_seat": None,
        "billing_symbol": None,
    }
    if country_code:
        updates["country_code"] = country_code

    pricing: dict[str, Any] = {"error": "no billing_currency or country_code available"}
    if billing_currency:
        pricing = run_call(client.get_billing_pricing_config, billing_currency)
    if "error" in pricing and country_code:
        pricing = run_call(client.get_pricing_config, country_code)

    # Same monthly-only guard as fetch_seat_pricing (see comment there).
    if billing_period == "monthly":
        price_per_seat = price_per_seat_from_pricing(pricing, billing_period)
        if price_per_seat is not None:
            updates["price_per_seat"] = price_per_seat

    billing_symbol = billing_symbol_from_pricing(pricing)
    if billing_symbol:
        updates["billing_symbol"] = billing_symbol

    return updates
