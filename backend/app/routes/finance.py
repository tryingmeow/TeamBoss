import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_db, log_operation
from ..services.fx import DEFAULT_FX_RATES, convert, get_fx_config, save_fx_rates
from ..services.pricing import discounted_monthly_total
from ..services.subscription_status import subscription_status

router = APIRouter(prefix="/api/finance", tags=["finance"])


class FinanceSettingsUpdate(BaseModel):
    base_currency: Optional[str] = None
    low_balance_threshold: Optional[float] = None


class FinanceCardNoteUpdate(BaseModel):
    card_brand: Optional[str] = None
    card_last4: str
    note: str = ""


def _card_key(card_brand: Optional[str], card_last4: Optional[str]) -> Optional[str]:
    last4 = (card_last4 or "").strip()
    if not last4:
        return None
    brand = (card_brand or "").strip().lower()
    return f"{brand}:{last4}"


@router.get("/overview")
async def get_overview():
    """Get comprehensive finance overview across all teams."""
    fx_config = await get_fx_config()
    base_currency = fx_config["base_currency"]
    rates = fx_config["rates"]
    fx_updated_at = fx_config["fx_updated_at"]
    low_balance_threshold = fx_config["low_balance_threshold"]

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM teams ORDER BY name")
        teams_rows = await cursor.fetchall()
        cursor = await db.execute("SELECT card_key, note FROM finance_card_notes")
        card_note_rows = await cursor.fetchall()

    card_notes = {row["card_key"]: row["note"] or "" for row in card_note_rows}
    card_team_counts = {}
    for team_row in teams_rows:
        key = _card_key(team_row["card_brand"], team_row["card_last4"])
        if key:
            card_team_counts[key] = card_team_counts.get(key, 0) + 1

    teams_data = []
    monthly_total_base = 0.0
    discount_total_base = 0.0
    timeline_data = []
    excluded_teams_count = 0

    for team_row in teams_rows:
        team_id = team_row["id"]
        team_name = team_row["name"] or ""
        owner_email = team_row["owner_email"] or ""
        status = team_row["status"] or "active"
        billing_currency = team_row["billing_currency"] or ""
        billing_symbol = team_row["billing_symbol"] or ""
        card_last4 = team_row["card_last4"]
        card_brand = team_row["card_brand"]
        card_key = _card_key(card_brand, card_last4)
        card_note = card_notes.get(card_key, "") if card_key else ""
        card_team_count = card_team_counts.get(card_key, 0) if card_key else 0
        price_per_seat = team_row["price_per_seat"]
        seats_entitled = team_row["seats_entitled"]
        seats_in_use = team_row["seats_in_use"]
        codex_count = team_row["codex_count"]
        chatgpt_count = team_row["chatgpt_count"]
        discount_amount = team_row["discount_amount"] or 0.0
        balance_str = team_row["balance"]
        active_until = team_row["active_until"]
        will_renew_raw = team_row["will_renew"]
        will_renew = bool(will_renew_raw)
        renewal_known = will_renew_raw is not None
        subscription_state = subscription_status(active_until, bool(will_renew))
        billing_period = team_row["billing_period"]

        # Calculate native monthly total only if billing_period is monthly and price is available
        if billing_period == "monthly" and price_per_seat is not None:
            monthly_total_native = discounted_monthly_total(price_per_seat, seats_entitled, discount_amount)
            # Convert to base currency
            monthly_total_base_value = convert(monthly_total_native, billing_currency, base_currency, rates)
        else:
            monthly_total_native = None
            monthly_total_base_value = None

        # Parse balance safely
        try:
            balance_float = float(balance_str) if balance_str not in (None, "") else None
        except (ValueError, TypeError):
            balance_float = None

        # Calculate days left
        days_left = None
        if active_until:
            try:
                active_until_dt = datetime.fromisoformat(active_until.replace("Z", "+00:00"))
                if active_until_dt.tzinfo is None:
                    active_until_dt = active_until_dt.replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                days_left = (active_until_dt.date() - now_utc.date()).days
            except (ValueError, AttributeError):
                pass

        team_dict = {
            "team_id": team_id,
            "name": team_name,
            "owner_email": owner_email,
            "remark": team_row["remark"],
            "status": status,
            "billing_currency": billing_currency,
            "billing_symbol": billing_symbol,
            "billing_period": billing_period,
            "card_last4": card_last4,
            "card_brand": card_brand,
            "card_key": card_key,
            "card_note": card_note,
            "card_team_count": card_team_count,
            "price_per_seat": price_per_seat if billing_period == "monthly" else None,
            "seats_entitled": seats_entitled,
            "seats_in_use": seats_in_use,
            "chatgpt_in_use": (
                chatgpt_count
                if chatgpt_count is not None
                else max(0, (seats_in_use or 0) - (codex_count or 0))
            ),
            "codex_count": codex_count or 0,
            "is_codex_enabled": 1 if team_row["is_codex_enabled"] else 0,
            "discount_amount": discount_amount,
            "monthly_total_native": monthly_total_native,
            "monthly_total_base": monthly_total_base_value,
            "balance": balance_str,
            "active_until": active_until,
            "days_left": days_left,
            "will_renew": 1 if will_renew else 0,
            "subscription_status": subscription_state,
        }
        teams_data.append(team_dict)

        # Track excluded teams (those that can't calculate monthly fees)
        if status == "active" and not renewal_known and subscription_state != "expired":
            excluded_teams_count += 1
        elif status == "active" and subscription_state == "renewing":
            if monthly_total_base_value is None:
                excluded_teams_count += 1
            else:
                monthly_total_base += monthly_total_base_value

            discount_base = convert(discount_amount, billing_currency, base_currency, rates)
            if discount_base is not None:
                discount_total_base += discount_base

        # Add to timeline if active
        if status == "active" and subscription_state != "expired":
            if active_until:
                timeline_data.append({
                    "date": active_until.split("T")[0] if "T" in active_until else active_until,
                    "team_id": team_id,
                    "team_name": team_name,
                    "owner_email": owner_email,
                    "amount_native": monthly_total_native,
                    "currency": billing_currency,
                    "amount_base": monthly_total_base_value,
                    "card_last4": card_last4,
                    "card_brand": card_brand,
                    "card_key": card_key,
                    "card_note": card_note,
                    "card_team_count": card_team_count,
                    "will_renew": 1 if will_renew else 0,
                    "billing_period": billing_period,
                })

    # Sort timeline by date
    timeline_data.sort(key=lambda x: x["date"])

    # Collect alerts
    alerts = []

    for team in teams_data:
        team_id = team["team_id"]
        team_name = team["name"]

        # Low balance alert
        try:
            balance_float = (
                float(team["balance"])
                if team["balance"] not in (None, "")
                else None
            )
        except (ValueError, TypeError):
            balance_float = None

        if balance_float is not None and balance_float < low_balance_threshold:
            alerts.append({
                "type": "low_balance",
                "team_id": team_id,
                "team_name": team_name,
                "detail": f"Credit {balance_float:.2f} 低于阈值 {low_balance_threshold}",
            })

        # Discount expiring alert
        async with get_db() as db:
            cursor = await db.execute("SELECT discount_expires_at FROM teams WHERE id = ?", (team_id,))
            row = await cursor.fetchone()
            if row and row["discount_expires_at"]:
                try:
                    expires_dt = datetime.fromisoformat(row["discount_expires_at"].replace("Z", "+00:00"))
                    if expires_dt.tzinfo is None:
                        expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                    now_utc = datetime.now(timezone.utc)
                    days_until_expiry = (expires_dt.date() - now_utc.date()).days
                    if 0 <= days_until_expiry <= 14:
                        alerts.append({
                            "type": "discount_expiring",
                            "team_id": team_id,
                            "team_name": team_name,
                            "detail": f"折扣将在 {days_until_expiry} 天后到期",
                        })
                except (ValueError, AttributeError):
                    pass

        # Token expired alert
        if team["status"] == "token_expired":
            alerts.append({
                "type": "token_expired",
                "team_id": team_id,
                "team_name": team_name,
                "detail": "Token 已过期，需要重新认证",
            })

        if team["subscription_status"] == "expired":
            alerts.append({
                "type": "subscription_expired",
                "team_id": team_id,
                "team_name": team_name,
                "detail": "团队订阅已到期",
            })

    return {
        "base_currency": base_currency,
        "fx_updated_at": fx_updated_at,
        "low_balance_threshold": low_balance_threshold,
        "monthly_total_base": monthly_total_base,
        "discount_total_base": discount_total_base,
        "excluded_teams_count": excluded_teams_count,
        "teams": teams_data,
        "timeline": timeline_data,
        "alerts": alerts,
    }


@router.get("/trends")
async def get_trends(days: int = 90):
    """Get billing trends over time."""
    # Clamp days to 1-365
    days = max(1, min(365, days))

    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    fx_config = await get_fx_config()
    base_currency = fx_config["base_currency"]
    rates = fx_config["rates"]

    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM billing_snapshots
               WHERE snapshot_date >= ?
               ORDER BY snapshot_date""",
            (start_date.isoformat(),)
        )
        snapshots = await cursor.fetchall()

    rows = []
    daily_totals = {}

    for snapshot in snapshots:
        monthly_total_base = None
        if snapshot["monthly_total"] is not None:
            monthly_total_base = convert(
                snapshot["monthly_total"],
                snapshot["billing_currency"] or "USD",
                base_currency,
                rates
            )

        row = {
            "snapshot_date": snapshot["snapshot_date"],
            "team_id": snapshot["team_id"],
            "billing_currency": snapshot["billing_currency"],
            "monthly_total_native": snapshot["monthly_total"],
            "monthly_total_base": monthly_total_base,
            "balance": snapshot["balance"],
        }
        rows.append(row)

        # Accumulate daily total
        date_key = snapshot["snapshot_date"]
        if date_key not in daily_totals:
            daily_totals[date_key] = 0.0
        if monthly_total_base is not None:
            daily_totals[date_key] += monthly_total_base

    # Convert daily totals to list format
    daily_total_list = [
        {"date": date, "total_base": total}
        for date, total in sorted(daily_totals.items())
    ]

    return {
        "days": days,
        "base_currency": base_currency,
        "rows": rows,
        "daily_total_base": daily_total_list,
    }


@router.patch("/settings")
async def update_finance_settings(req: FinanceSettingsUpdate):
    """Update finance settings."""
    from datetime import datetime, timezone

    updates = {}

    if req.base_currency is not None:
        bc = req.base_currency.upper()
        fx_config = await get_fx_config()
        if bc not in fx_config["rates"]:
            await log_operation(None, "update_finance_settings", None, f"base_currency={bc}", "failed", "Currency not supported")
            raise HTTPException(status_code=400, detail=f"Currency {bc} not supported")
        updates["finance_base_currency"] = bc

    if req.low_balance_threshold is not None:
        if req.low_balance_threshold < 0:
            await log_operation(None, "update_finance_settings", None, f"low_balance_threshold={req.low_balance_threshold}", "failed", "Threshold must be >= 0")
            raise HTTPException(status_code=400, detail="low_balance_threshold must be >= 0")
        updates["finance_low_balance_threshold"] = str(req.low_balance_threshold)

    if updates:
        try:
            now = datetime.now(timezone.utc).isoformat()
            async with get_db() as db:
                for key, value in updates.items():
                    await db.execute(
                        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                        (key, str(value), now),
                    )
                await db.commit()
            detail = ", ".join(f"{k}={v}" for k, v in updates.items())
            await log_operation(None, "update_finance_settings", None, detail, "success")
        except Exception as e:
            await log_operation(None, "update_finance_settings", None, None, "failed", str(e))
            raise

    return {"status": "ok", "updated": updates}


@router.patch("/card-note")
async def update_card_note(req: FinanceCardNoteUpdate):
    """Create, update, or clear a shared note for a card."""
    try:
        card_key = _card_key(req.card_brand, req.card_last4)
        if not card_key:
            await log_operation(None, "update_card_note", None, None, "failed", "card_last4 is required")
            raise HTTPException(status_code=400, detail="card_last4 is required")

        note = (req.note or "").strip()
        if len(note) > 80:
            await log_operation(None, "update_card_note", None, f"card_key={card_key}, note_len={len(note)}", "failed", "Note exceeds 80 characters")
            raise HTTPException(status_code=400, detail="note must be 80 characters or fewer")

        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as db:
            if note:
                await db.execute(
                    """INSERT INTO finance_card_notes
                       (card_key, card_brand, card_last4, note, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(card_key) DO UPDATE SET
                         card_brand = excluded.card_brand,
                         card_last4 = excluded.card_last4,
                         note = excluded.note,
                         updated_at = excluded.updated_at""",
                    (card_key, (req.card_brand or "").strip(), req.card_last4.strip(), note, now),
                )
            else:
                await db.execute("DELETE FROM finance_card_notes WHERE card_key = ?", (card_key,))
            await db.commit()

        detail = f"card_key={card_key}, note_len={len(note)}"
        await log_operation(None, "update_card_note", None, detail, "success")
        return {"status": "ok", "card_key": card_key, "note": note}
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "update_card_note", None, None, "failed", str(e))
        raise


@router.post("/fx/refresh")
async def refresh_fx_rates():
    """Refresh FX rates from external API."""
    try:
        def fetch_rates():
            response = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15)
            response.raise_for_status()
            return response.json()

        data = await asyncio.to_thread(fetch_rates)

        if data.get("result") != "success":
            await log_operation(None, "refresh_fx_rates", None, None, "failed", f"API returned: {data.get('result')}")
            raise HTTPException(
                status_code=502,
                detail=f"API returned unsuccessful result: {data.get('result')}"
            )

        api_rates = data.get("rates")
        if not isinstance(api_rates, dict):
            await log_operation(None, "refresh_fx_rates", None, None, "failed", "API response missing rates")
            raise HTTPException(status_code=502, detail="API response missing rates")

        # Filter to only currencies in DEFAULT_FX_RATES
        filtered_rates = {
            code: api_rates[code]
            for code in DEFAULT_FX_RATES.keys()
            if code in api_rates
        }

        if not filtered_rates:
            await log_operation(None, "refresh_fx_rates", None, None, "failed", "No supported currencies in API response")
            raise HTTPException(status_code=502, detail="No supported currencies in API response")

        await save_fx_rates(filtered_rates)

        await log_operation(None, "refresh_fx_rates", None, f"updated_currencies={len(filtered_rates)}", "success")
        return {
            "status": "ok",
            "updated_currencies": len(filtered_rates),
            "fx_updated_at": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except requests.RequestException as e:
        await log_operation(None, "refresh_fx_rates", None, None, "failed", f"API request failed: {str(e)}")
        raise HTTPException(status_code=502, detail=f"External API request failed: {str(e)}")
    except Exception as e:
        await log_operation(None, "refresh_fx_rates", None, None, "failed", str(e))
        raise HTTPException(status_code=502, detail=f"Failed to refresh FX rates: {str(e)}")
