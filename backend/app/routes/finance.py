import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_db, log_operation
from ..services.fx import DEFAULT_FX_RATES, FxRefreshError, convert, get_fx_config, refresh_fx_rates, save_fx_rates
from ..services.invoices import classify_latest_invoice, refresh_invoices_for_team_blocking
from ..services.pricing import discounted_monthly_total
from ..services.subscription_status import subscription_status_display

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


def _format_money(value: float, unit: str) -> str:
    """与前端 formatMoney 一致：整数不带小数，其余两位；符号前置，币种代码后置。"""
    rounded = round(abs(value), 2)
    body = f"{int(rounded):,}" if rounded == int(rounded) else f"{rounded:,.2f}"
    sign = "-" if value < 0 and body != "0" else ""
    if not unit:
        return f"{sign}{body}"
    if len(unit) == 3 and unit.isascii() and unit.isalpha():
        return f"{sign}{body} {unit.upper()}"
    return f"{sign}{unit}{body}"


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
        # 每团队取最新一期真实发票（void/draft 不算数），用于和推算月费对账。
        cursor = await db.execute(
            """SELECT * FROM invoices
               WHERE COALESCE(status, '') NOT IN ('void', 'draft')
               ORDER BY COALESCE(period_end, created_at) DESC, created_at DESC"""
        )
        invoice_rows = await cursor.fetchall()

    latest_invoice_by_team = {}
    for invoice_row in invoice_rows:
        latest_invoice_by_team.setdefault(invoice_row["team_id"], invoice_row)

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
    last_paid_total_base = 0.0
    last_paid_count = 0

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
        subscription_state = subscription_status_display(
            active_until, bool(will_renew), team_row["last_full_sync_at"]
        )
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

        # 最新一期发票：金额定性在原币种内完成，换算成基准币只为了展示。
        latest_invoice = None
        inv_row = latest_invoice_by_team.get(team_id)
        if inv_row is not None:
            inv = dict(inv_row)
            reconciliation, display_amount, diff_native = classify_latest_invoice(
                inv, monthly_total_native, billing_currency
            )
            invoice_currency = inv["currency"] or ""
            display_amount_base = (
                convert(display_amount, invoice_currency, base_currency, rates)
                if display_amount is not None and invoice_currency
                else None
            )
            diff_base = (
                convert(diff_native, invoice_currency, base_currency, rates)
                if diff_native is not None and invoice_currency
                else None
            )
            latest_invoice = {
                "invoice_id": inv["invoice_id"],
                "status": inv["status"],
                "currency": inv["currency"],
                "amount_due": inv["amount_due"],
                "amount_paid": inv["amount_paid"],
                "display_amount": display_amount,
                "display_amount_base": display_amount_base,
                "period_start": inv["period_start"],
                "period_end": inv["period_end"],
                "hosted_invoice_url": inv["hosted_invoice_url"],
                "reconciliation": reconciliation,
                "diff_native": diff_native,
                "diff_base": diff_base,
            }
            if inv["status"] == "paid" and display_amount_base is not None:
                last_paid_total_base += display_amount_base
                last_paid_count += 1

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
            "latest_invoice": latest_invoice,
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
        unit = team.get("billing_symbol") or team.get("billing_currency") or ""
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
                "detail": (
                    f"Credit 余额为负 · {_format_money(balance_float, unit)}"
                    if balance_float < 0
                    else (
                        f"Credit 余额 {_format_money(balance_float, unit)} "
                        f"低于阈值 {_format_money(float(low_balance_threshold), unit)}"
                    )
                ),
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
                "detail": "Session 已失效，需要重新导入",
            })

        if team["subscription_status"] == "expired":
            alerts.append({
                "type": "subscription_expired",
                "team_id": team_id,
                "team_name": team_name,
                "detail": "Team 订阅已到期",
            })

        # 发票对账：差额用基准币说严重程度，原币证据在明细展开区。
        invoice_info = team.get("latest_invoice")
        if invoice_info:
            reconciliation = invoice_info["reconciliation"]
            if reconciliation in ("over", "under"):
                word = "多" if (invoice_info["diff_native"] or 0) > 0 else "少"
                if invoice_info["diff_base"] is not None:
                    detail = (
                        f"上期实付比推算{word}约 "
                        f"{base_currency} {abs(invoice_info['diff_base']):.2f}"
                    )
                else:
                    detail = (
                        f"上期实付比推算{word} "
                        f"{invoice_info['currency']} {abs(invoice_info['diff_native'] or 0):.2f}"
                    )
                alerts.append({
                    "type": "invoice_mismatch",
                    "team_id": team_id,
                    "team_name": team_name,
                    "detail": detail,
                })
            elif reconciliation == "unpaid":
                if invoice_info["display_amount_base"] is not None:
                    detail = (
                        f"上期账单约 {base_currency} "
                        f"{invoice_info['display_amount_base']:.2f} 尚未支付"
                    )
                elif invoice_info["display_amount"] is not None:
                    detail = (
                        f"上期账单 {invoice_info['currency']} "
                        f"{invoice_info['display_amount']:.2f} 尚未支付"
                    )
                else:
                    detail = "上期账单尚未支付"
                alerts.append({
                    "type": "invoice_unpaid",
                    "team_id": team_id,
                    "team_name": team_name,
                    "detail": detail,
                })

    return {
        "base_currency": base_currency,
        "fx_updated_at": fx_updated_at,
        "low_balance_threshold": low_balance_threshold,
        "monthly_total_base": monthly_total_base,
        "discount_total_base": discount_total_base,
        "excluded_teams_count": excluded_teams_count,
        "last_paid_total_base": last_paid_total_base if last_paid_count else None,
        "last_paid_count": last_paid_count,
        "teams": teams_data,
        "timeline": timeline_data,
        "alerts": alerts,
    }


_TEAM_INVOICES_SQL = """
    SELECT invoice_id, number, status, currency, amount_due, amount_paid,
           period_start, period_end, description, hosted_invoice_url
    FROM invoices
    WHERE team_id = ?
    ORDER BY COALESCE(period_end, created_at) DESC, created_at DESC
    LIMIT 6
"""


@router.get("/invoices/{team_id}")
async def get_team_invoices(team_id: str):
    """最近 6 期发票，给明细行展开的对账子表用。金额保持原币种。"""
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM teams WHERE id = ?", (team_id,))
        team = await cursor.fetchone()
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        cursor = await db.execute(_TEAM_INVOICES_SQL, (team_id,))
        rows = await cursor.fetchall()

    if not rows:
        # 库里一条都没有就现场拉一次。这是用户主动触发的入口，force=True 绕过
        # 24 小时节流：定时轮次会因为上一次失败而退避，用户点开的时候必须还能
        # 立刻重试。失败不报错，照常返回空列表，界面显示「暂无账单数据」。
        try:
            await asyncio.to_thread(refresh_invoices_for_team_blocking, team_id, True)
        except Exception:
            pass
        async with get_db() as db:
            cursor = await db.execute(_TEAM_INVOICES_SQL, (team_id,))
            rows = await cursor.fetchall()

    return {"team_id": team_id, "invoices": [dict(row) for row in rows]}


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
async def refresh_fx_rates_route():
    """Refresh FX rates from external API."""
    try:
        count = await refresh_fx_rates()
    except FxRefreshError as e:
        await log_operation(None, "refresh_fx_rates", None, None, "failed", str(e))
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        await log_operation(None, "refresh_fx_rates", None, None, "failed", str(e))
        raise HTTPException(status_code=502, detail=f"Failed to refresh FX rates: {str(e)}")

    await log_operation(None, "refresh_fx_rates", None, f"updated_currencies={count}", "success")
    return {
        "status": "ok",
        "updated_currencies": count,
        "fx_updated_at": datetime.now(timezone.utc).isoformat(),
    }
