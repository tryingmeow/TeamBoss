"""席位单价与月费的正本：ChatGPT（default）和 Premium（prolite）的每席月价都来自同一次
``checkout_pricing_config`` 响应，Team 的月费、续费提醒金额、超员确认里的金额都按这里算。

支持按月和按年计费的 Team。存的单价一律是「每席每月」：
* 月付：``currency_config[计划].month.amount``。
* 年付：``currency_config[计划].year.amount`` 是年付方案的每席月价，一席一年乘 12。
单价旁边存 ``price_period``（取的是哪个桶），只有它和订阅的 billing_period 一致时才认这个单价，
年付的价格永远不会被当成月付价格乘。计费周期缺失或不认识 → 单价未知，不猜。
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Mapping

from ..chatgpt_limiter import run_chatgpt_call
from ..seat_types import DEFAULT_SEAT_TYPE, PREMIUM_SEAT_TYPE
from .seat_capacity import cached_seat_capacity

# currency_config 里 ChatGPT Business 两种计费席位的价格键（都不含税，和工作区币种一致）。
# 注意同一个 currency_config 里还有个人版的 "prolite"（Pro Lite 订阅，含税、价格不同），
# 那不是 Business 的 Premium 席位，绝不能读它。
STANDARD_PRICING_KEY = "business"
PREMIUM_PRICING_KEY = "business_prolite"

# 费用跟踪支持的计费周期，以及一个周期有几个月。
MONTHS_PER_PERIOD: dict[str, int] = {"monthly": 1, "yearly": 12}
_PRICING_BUCKET = {"monthly": "month", "yearly": "year"}

SEAT_PRICE_UNKNOWN_TEXT = "单价未知，以 ChatGPT 账单为准"
# 年付 Team 上的加购：只报每期价格，不说 ChatGPT 怎么结算剩下的年度。
YEARLY_PURCHASE_NOTE = "年付加购按 ChatGPT 规则结算，以账单为准"

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


def _plan_price(pricing: dict[str, Any], plan_key: str, billing_period: str) -> float | None:
    """``currency_config[plan_key][month|year].amount``（每席每月；year 桶是年付方案的月价）。
    计费周期不认识、缺键、结构不对、不是正数都返回 None（未知）。"""
    bucket = _PRICING_BUCKET.get(billing_period)
    if bucket is None or not isinstance(pricing, dict) or "error" in pricing:
        return None
    currency_config = pricing.get("currency_config")
    plan = currency_config.get(plan_key) if isinstance(currency_config, dict) else None
    period = plan.get(bucket) if isinstance(plan, dict) else None
    amount = period.get("amount") if isinstance(period, dict) else None
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        return None
    return float(amount)


def price_per_seat_from_pricing(pricing: dict[str, Any], billing_period: str = "monthly") -> float | None:
    """ChatGPT 席位（``business``）的每席价格。"""
    return _plan_price(pricing, STANDARD_PRICING_KEY, billing_period)


def premium_price_per_seat_from_pricing(
    pricing: dict[str, Any], billing_period: str = "monthly"
) -> float | None:
    """Premium 席位（``business_prolite``）的每席价格。个人版的 ``prolite`` 键不读。"""
    return _plan_price(pricing, PREMIUM_PRICING_KEY, billing_period)


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
    if team_id:
        return accounts[team_id] if isinstance(accounts.get(team_id), dict) else {}
    for value in accounts.values():
        if isinstance(value, dict) and value.get("account", {}).get("structure") == "workspace":
            return value
    return {}


def _discount_updates(entitlement: Any) -> dict[str, Any]:
    """A reported fixed discount, explicit absence, or unknown; never invent coupon eligibility."""
    updates = {
        "discount_amount": None,
        "discount_duration_num_periods": None,
        "discount_expires_at": None,
        "discount_start_in_num_periods": None,
        "discount_quantity_off": None,
        "promo_campaign_id": None,
    }
    if not isinstance(entitlement, dict):
        return updates
    if "applied_discounts" in entitlement:
        discounts = entitlement["applied_discounts"]
        if not isinstance(discounts, list):
            return updates
        if len(discounts) > 1:
            return updates  # Multiple coupons need allocation rules not supplied by this API.
        discount = discounts[0] if discounts else entitlement.get("discount")
    elif "discount" in entitlement:
        discount = entitlement["discount"]
    else:
        return updates
    if discount is None:
        updates["discount_amount"] = 0.0
        return updates
    if not isinstance(discount, dict):
        return updates
    for key in updates:
        if key != "discount_amount":
            updates[key] = discount.get("duration_num_periods" if key == "discount_duration_num_periods" else key)
    updates["discount_quantity_off"] = discount.get("quantity_off")
    amount = discount.get("amount")
    if discount.get("discount_type") == "fixed" and isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount) and amount >= 0:
        updates["discount_amount"] = float(amount)
    return updates


def account_billing_updates(account_info: dict[str, Any], team_id: str | None = None) -> dict[str, Any]:
    if not isinstance(account_info, dict) or "error" in account_info:
        return {}
    entry = _selected_account_entry(account_info, team_id)
    updates: dict[str, Any] = {}
    account = entry.get("account")
    if isinstance(account, dict):
        updates["is_codex_enabled"] = 1 if account.get("is_usage_based_seat_enabled") else 0
    if "entitlement" in entry:
        updates.update(_discount_updates(entry["entitlement"]))
    return updates


def subscription_billing_updates(subscription: Any) -> dict[str, Any]:
    """Current subscriptions take precedence over the less frequently refreshed account view.

    Failed requests or absent entitlement provide no new discount state. An explicitly
    malformed entitlement clears the effective discount to unknown, not zero.
    """
    if not isinstance(subscription, dict) or "error" in subscription:
        return {}
    updates = {"billing_period": subscription.get("billing_period")}
    if "entitlement" in subscription:
        updates.update(_discount_updates(subscription["entitlement"]))
    return updates


def effective_discount(team: Mapping[str, Any], *, now: datetime | None = None) -> float | None:
    """The reported fixed amount for the current period; unknown stays unknown."""
    now = now or datetime.now(timezone.utc)
    expires = team.get("discount_expires_at")
    if expires:
        try:
            expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
        if expiry <= now:
            return 0.0
    starts = team.get("discount_start_in_num_periods")
    if starts is not None:
        if isinstance(starts, bool) or not isinstance(starts, (int, float)) or not math.isfinite(starts) or starts < 0 or int(starts) != starts:
            return None
        if starts > 0:
            return 0.0
    amount = team.get("discount_amount", 0)
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
        return None
    return float(amount)


def discounted_monthly_total(price_per_seat: Any, seats_entitled: Any, discount_amount: Any = 0) -> float:
    """Multiply price_per_seat by seats and subtract the discount.

    All three inputs must be for the SAME span of time: the discount is a fixed
    amount per billing period, so pass the per-period seat price (monthly price
    for a monthly Team, monthly price × 12 for a yearly one). This function has
    no way to tell periods apart; team_monthly_cost is the only place that picks
    the span (via priced_period), and every reader should go through it.
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


def format_money(value: float, unit: str) -> str:
    """与前端 formatMoney 一致：整数不带小数，其余两位；符号前置，币种代码后置。"""
    rounded = round(abs(value), 2)
    body = f"{int(rounded):,}" if rounded == int(rounded) else f"{rounded:,.2f}"
    sign = "-" if value < 0 and body != "0" else ""
    if not unit:
        return f"{sign}{body}"
    if len(unit) == 3 and unit.isascii() and unit.isalpha():
        return f"{sign}{body} {unit.upper()}"
    return f"{sign}{unit}{body}"


def _positive_amount(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) and amount > 0 else None


def priced_period(team: Mapping[str, Any]) -> str | None:
    """teams 行里存的单价对哪个计费周期有效：'monthly' / 'yearly'；对不上或未知为 None。

    单价只在 ``price_period`` 和订阅的 ``billing_period`` 一致时才算数。``price_period`` 为 NULL 的
    是加这一列之前写的行：那时的代码只给月付 Team 存单价，所以 NULL + monthly 按月付认，
    NULL + yearly 一律不认。
    """
    billing_period = team.get("billing_period")
    if billing_period not in MONTHS_PER_PERIOD:
        return None
    price_period = team.get("price_period")
    if price_period is None and billing_period == "monthly":
        price_period = "monthly"
    return billing_period if price_period == billing_period else None


def seat_price_per_month(team: Mapping[str, Any], seat_type: str) -> float | None:
    """teams 行里 ``seat_type`` 的每席每月价格（原币种、不含税；年付是年付方案的月价）。
    ChatGPT 读 price_per_seat，Premium 读 premium_price_per_seat；单价和计费周期对不上、
    其他类型、未知一律 None。"""
    if priced_period(team) is None:
        return None
    if seat_type == DEFAULT_SEAT_TYPE:
        return _positive_amount(team.get("price_per_seat"))
    if seat_type == PREMIUM_SEAT_TYPE:
        return _positive_amount(team.get("premium_price_per_seat"))
    return None


def seat_price_info(team: Mapping[str, Any], seat_type: str) -> dict[str, Any] | None:
    """给前端的一席价格：``{amount: 每月, currency, symbol, period: "monthly" | "yearly"}``；
    单价、币种或计费周期未知为 None。年付的 amount 也是每月，一年是 amount × 12。"""
    amount = seat_price_per_month(team, seat_type)
    currency = str(team.get("billing_currency") or "").strip().upper()
    if amount is None or not currency:
        return None
    symbol = str(team.get("billing_symbol") or "").strip() or None
    return {"amount": amount, "currency": currency, "symbol": symbol, "period": priced_period(team)}


def _price_unit(price: Mapping[str, Any]) -> str:
    return str(price.get("symbol") or price.get("currency") or "")


def seat_charge_text(price: Mapping[str, Any] | None, seats: int) -> str:
    """加购 ``seats`` 席多出的钱。月付「约 +฿780 + 税/月」，多席「约 +฿2,340 + 税/月（每席 ฿780）」；
    年付「约 +฿630 + 税/月（年付，一年 ฿7,560），年付加购按 ChatGPT 规则结算，以账单为准」，多席
    「约 +฿1,890 + 税/月（年付，一年 ฿22,680；每席 ฿630/月），…」。单价未知给 SEAT_PRICE_UNKNOWN_TEXT。
    只说每期价格，不说 ChatGPT 怎么折算。与前端 lib/seatPrice.ts 的 seatChargeText 同一格式。"""
    if not price:
        return SEAT_PRICE_UNKNOWN_TEXT
    count = max(1, int(seats))
    unit = _price_unit(price)
    amount = float(price["amount"])
    total = amount * count
    if price.get("period") == "yearly":
        each = f"；每席 {format_money(amount, unit)}/月" if count > 1 else ""
        annual = format_money(total * MONTHS_PER_PERIOD["yearly"], unit)
        return (
            f"约 +{format_money(total, unit)} + 税/月（年付，一年 {annual}{each}），"
            f"{YEARLY_PURCHASE_NOTE}"
        )
    text = f"约 +{format_money(total, unit)} + 税/月"
    return f"{text}（每席 {format_money(amount, unit)}）" if count > 1 else text


def seat_cost_totals(items: list[tuple[Mapping[str, Any] | None, int]]) -> list[dict[str, Any]]:
    """[(一席价格, 席数)] → 每个币种 + 计费周期一条每月合计 ``{amount, currency, symbol, period}``；
    不同币种、不同计费周期绝不相加，单价未知的不计入。"""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for price, seats in items:
        if not price or seats <= 0:
            continue
        currency = str(price["currency"]).upper()
        period = str(price.get("period") or "monthly")
        entry = totals.setdefault(
            (currency, period),
            {"amount": 0.0, "currency": currency, "symbol": price.get("symbol"), "period": period},
        )
        entry["amount"] += float(price["amount"]) * int(seats)
    return list(totals.values())


@dataclass(frozen=True)
class TeamMonthlyCost:
    """一个 Team 的计费（原币种、不含税）。财务总览、Team 列表、计费快照共用这一份算法。

    * 只算计费周期已知（月付 / 年付）且单价对得上周期的 Team（见 priced_period）。
    * ChatGPT：每席月价 × 已付 ChatGPT 席位（seat_capacity.default.paid，读不到退回 seats_entitled）。
    * Premium：真实每席月价 × 已付 Premium 席位（seat_capacity.prolite.paid，读不到按 0）。
    * 折扣（discount_amount）是每个计费周期的固定金额，只减一次，从 ChatGPT 部分减（不低于 0），
      不按 Premium 席位减：上游的固定折扣挂在订阅上、没说只抵哪种席位；从 ChatGPT 部分减、
      超出部分不去抵 Premium，费用只会多算不会少算。年付 Team 的月均折扣 = 年折扣 / 12。
    * period_total：一个计费周期的钱（月付 = 一个月，年付 = 一年）；monthly_total = period_total /
      周期月数（年付即「月均」）。ChatGPT 单价未知 → 都是 None（未知，不是 0）。
    * 有已付 Premium 但单价未知时，合计未知，不填入估算。
    """

    # 单价对得上的计费周期：'monthly' / 'yearly'；None = 未知 / 对不上。
    billing_period: str | None
    # 订阅自己报的计费周期是不是支持的（不管单价读没读到）。
    period_known: bool
    chatgpt_seats_billed: int
    premium_seats_paid: int
    # 每席每月（年付是年付方案的月价）。
    price_per_seat: float | None
    premium_price_per_seat: float | None
    # 每个计费周期的固定折扣。
    discount_amount: float | None
    # 以下 *_subtotal / monthly_* 是每月（年付为月均），period_* 是每个计费周期。
    chatgpt_subtotal: float | None
    premium_subtotal: float | None
    monthly_subtotal: float | None
    monthly_total: float | None
    period_subtotal: float | None
    period_total: float | None

    @property
    def months_per_period(self) -> int | None:
        return MONTHS_PER_PERIOD.get(self.billing_period or "")

    @property
    def discount_monthly(self) -> float | None:
        """每月（年付为月均）的折扣；计费周期未知为 None。"""
        months = self.months_per_period
        return (min(self.discount_amount, self.chatgpt_subtotal * months) / months
                if months and self.discount_amount is not None and self.chatgpt_subtotal is not None else None)

    @property
    def premium_price_source(self) -> str | None:
        return "upstream" if self.premium_price_per_seat is not None else None


def _seat_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def team_monthly_cost(team: Mapping[str, Any], *, now: datetime | None = None) -> TeamMonthlyCost:
    """``team`` 是 teams 表的一行（至少含 billing_period / price_period / price_per_seat /
    premium_price_per_seat / seats_entitled / seat_capacity_json / discount_amount）。规则见 TeamMonthlyCost。"""
    capacity = cached_seat_capacity(team.get("seat_capacity_json")) or {}
    default_entry = capacity.get(DEFAULT_SEAT_TYPE)
    premium_entry = capacity.get(PREMIUM_SEAT_TYPE)
    chatgpt_seats = (
        default_entry["paid"] if default_entry is not None else _seat_count(team.get("seats_entitled"))
    )
    premium_seats = premium_entry["paid"] if premium_entry is not None else 0
    period = priced_period(team)
    months = MONTHS_PER_PERIOD.get(period or "")
    price = seat_price_per_month(team, DEFAULT_SEAT_TYPE)
    premium_price = seat_price_per_month(team, PREMIUM_SEAT_TYPE)
    discount = effective_discount(team, now=now)

    chatgpt_subtotal = price * chatgpt_seats if price is not None else (0.0 if chatgpt_seats == 0 else None)
    premium_subtotal = premium_price * premium_seats if premium_price is not None else (0.0 if premium_seats == 0 else None)
    if chatgpt_subtotal is None or premium_subtotal is None or not months:
        period_subtotal = period_total = monthly_subtotal = monthly_total = None
    else:
        premium_period = (premium_subtotal or 0.0) * months
        period_subtotal = chatgpt_subtotal * months + premium_period
        # 折扣按计费周期减一次：月付减一个月的折扣，年付减一年的折扣。
        period_total = (max(0.0, chatgpt_subtotal * months - discount) + premium_period
                        if discount is not None else None)
        monthly_subtotal = period_subtotal / months
        monthly_total = period_total / months if period_total is not None else None
    return TeamMonthlyCost(
        billing_period=period,
        period_known=team.get("billing_period") in MONTHS_PER_PERIOD,
        chatgpt_seats_billed=chatgpt_seats,
        premium_seats_paid=premium_seats,
        price_per_seat=price,
        premium_price_per_seat=premium_price,
        discount_amount=discount,
        chatgpt_subtotal=chatgpt_subtotal,
        premium_subtotal=premium_subtotal,
        monthly_subtotal=monthly_subtotal,
        monthly_total=monthly_total,
        period_subtotal=period_subtotal,
        period_total=period_total,
    )


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
        "premium_price_per_seat": None,
        "price_period": None,
        "billing_symbol": None,
    }
    if country_code:
        updates["country_code"] = country_code

    pricing: dict[str, Any] = {"error": "no billing_currency or country_code available"}
    if billing_currency:
        pricing = await run_call(client.get_billing_pricing_config, billing_currency)
    if "error" in pricing and country_code:
        pricing = await run_call(client.get_pricing_config, country_code)

    updates.update(_prices_from_pricing(pricing, billing_period))
    return updates


def _prices_from_pricing(pricing: dict[str, Any], billing_period: Any) -> dict[str, Any]:
    """一次 pricing 响应 → 要写的单价、单价所属周期和币种符号（两条同步路径共用）。

    Prices are stored only for a confirmed billing period (monthly or yearly), taken
    from that period's bucket (month / year), and always tagged with ``price_period``.
    A yearly bucket price read as if it were the monthly price would silently put a
    wrong number on every total downstream; ``priced_period`` refuses any price whose
    ``price_period`` does not match the subscription's billing period. Unknown or
    unrecognised periods store nothing (NULL = unknown). The Premium price follows
    exactly the same rule and comes from the same response (no extra request).
    """
    updates: dict[str, Any] = {}
    if billing_period in MONTHS_PER_PERIOD:
        updates["price_period"] = billing_period
        price_per_seat = price_per_seat_from_pricing(pricing, billing_period)
        if price_per_seat is not None:
            updates["price_per_seat"] = price_per_seat
        premium_price = premium_price_per_seat_from_pricing(pricing, billing_period)
        if premium_price is not None:
            updates["premium_price_per_seat"] = premium_price

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
        "premium_price_per_seat": None,
        "price_period": None,
        "billing_symbol": None,
    }
    if country_code:
        updates["country_code"] = country_code

    pricing: dict[str, Any] = {"error": "no billing_currency or country_code available"}
    if billing_currency:
        pricing = run_call(client.get_billing_pricing_config, billing_currency)
    if "error" in pricing and country_code:
        pricing = run_call(client.get_pricing_config, country_code)

    # Same period guard as fetch_seat_pricing (see _prices_from_pricing).
    updates.update(_prices_from_pricing(pricing, billing_period))
    return updates
