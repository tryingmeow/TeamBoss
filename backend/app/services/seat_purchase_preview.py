"""Sanitize official purchase quotes without authorizing or performing purchases."""
from datetime import datetime, timezone
from typing import Any

from ..chatgpt_limiter import run_chatgpt_call
from .overage_policy import CONFIRMATION_MAX_SEATS


SEAT_TYPES = {"default", "prolite"}


class QuoteUnavailable(Exception):
    pass


def _object(value: Any) -> dict:
    if not isinstance(value, dict) or "error" in value:
        raise QuoteUnavailable()
    return value


def _integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise QuoteUnavailable()
    return value


def _quantities(value: Any, *, capacity: bool = False) -> dict[str, int]:
    if not isinstance(value, list):
        raise QuoteUnavailable()
    result = {}
    for raw in value:
        entry = _object(raw)
        seat_type = entry.get("type" if capacity else "seat_type")
        if not isinstance(seat_type, str) or seat_type not in SEAT_TYPES or seat_type in result:
            raise QuoteUnavailable()
        paid = _integer(entry.get("paid" if capacity else "quantity"))
        result[seat_type] = paid
    if set(result) != SEAT_TYPES:
        raise QuoteUnavailable()
    return result


def _currency(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha():
        raise QuoteUnavailable()
    return value.upper()


def _recurring(raw: Any, quantities: dict[str, int], divisor: int) -> dict:
    recurring = _object(raw)
    if _quantities(recurring.get("seat_quantities")) != quantities:
        raise QuoteUnavailable()
    period = {"month": "monthly", "year": "yearly"}.get(recurring.get("price_interval"))
    if period is None:
        raise QuoteUnavailable()
    due = _object(recurring.get("amount_due"))
    return {
        "period": period,
        "amount": _integer(due.get("amount")) / divisor,
        "discount": _integer(recurring.get("discount_amount")) / divisor,
    }


async def fetch_seat_purchase_preview(client, seat_type: str, additional_seats: int) -> dict:
    """Always quote against fresh paid quantities; reject incomplete or stale quotes."""
    if seat_type not in SEAT_TYPES or type(additional_seats) is not int or not 1 <= additional_seats <= CONFIRMATION_MAX_SEATS:
        raise QuoteUnavailable()
    try:
        subscription = _object(await run_chatgpt_call(client.get_subscription))
        baseline = _quantities(subscription.get("seat_capacity"), capacity=True)
        currency = _currency(subscription.get("billing_currency"))
        proposed = dict(baseline)
        proposed[seat_type] += additional_seats
        pricing = _object(await run_chatgpt_call(client.get_billing_pricing_config, currency))
        config = _object(pricing.get("currency_config"))
        if _currency(config.get("symbol_code")) != currency:
            raise QuoteUnavailable()
        exponent = _integer(config.get("minor_unit_exponent"))
        if exponent > 6:
            raise QuoteUnavailable()
        divisor = 10 ** exponent
        quote = _object(await run_chatgpt_call(client.preview_seat_purchase, proposed))
        if _currency(quote.get("currency")) != currency or quote.get("change_effective_at") != "now":
            raise QuoteUnavailable()
        current = _recurring(quote.get("current_recurring"), baseline, divisor)
        updated = _recurring(quote.get("proposed_recurring"), proposed, divisor)
        if current["period"] != updated["period"] or current["period"] != subscription.get("billing_period"):
            raise QuoteUnavailable()
        due = _object(quote.get("amount_due"))
        return {
            "currency": currency,
            "minor_unit_exponent": exponent,
            "quoted_at": datetime.now(timezone.utc).isoformat(),
            "seat_type": seat_type,
            "additional_seats": additional_seats,
            "baseline_quantities": baseline,
            "proposed_quantities": proposed,
            "current_recurring": current,
            "proposed_recurring": updated,
            "due_now": {
                "amount": _integer(due.get("amount")) / divisor,
                "tax_amount": _integer(due.get("tax_amount")) / divisor,
            },
        }
    except Exception:
        # Never expose billing addresses, cards, account IDs, or upstream error text.
        raise QuoteUnavailable() from None
