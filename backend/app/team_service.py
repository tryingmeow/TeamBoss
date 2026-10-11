import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import jwt
from fastapi import HTTPException

from .chatgpt_client import ChatGPTClient
from .chatgpt_limiter import (
    log_refresh_diagnostics,
    refresh_diagnostic_fields,
    run_chatgpt_call,
)
from .database import get_db, log_operation
from .models import TeamSession
from .proxy_resolve import ProxyUnavailableError, resolve_proxy_url
from .seat_types import CODEX_SEAT_TYPE
from .services.pricing import account_billing_updates, subscription_billing_updates, fetch_seat_pricing
from .services.seat_capacity import (
    chatgpt_count_from_seat_counts,
    seat_counts_column_updates,
    seat_type_count_from_seat_counts,
    subscription_column_updates,
)
from .session_store import write_session_file


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: expected UUID")


def _decode_team_payload(access_token: str) -> tuple[dict, str]:
    try:
        payload = jwt.decode(access_token, options={"verify_signature": False})
        auth_info = payload.get("https://api.openai.com/auth", {})
        team_id = auth_info.get("chatgpt_account_id")
        if not team_id:
            raise ValueError("No chatgpt_account_id found in token")
        return payload, _normalize_uuid(team_id, "chatgpt_account_id")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to decode access token: {str(e)}")


def _assert_session_account_matches(session_data: TeamSession, team_id: str) -> None:
    account_id = session_data.account.get("id") if isinstance(session_data.account, dict) else None
    if not account_id:
        return
    normalized_account_id = _normalize_uuid(account_id, "account.id")
    if normalized_account_id != team_id:
        raise HTTPException(
            status_code=400,
            detail="Session account.id does not match access token chatgpt_account_id",
        )


async def _resolve_proxy_url(proxy_id: int | None) -> str | None:
    """选了代理却解析不出来时拒绝导入，而不是从本机 IP 去验证这个 session。"""
    try:
        return await resolve_proxy_url(proxy_id)
    except ProxyUnavailableError as exc:
        raise HTTPException(status_code=400, detail=f"选定的代理不可用：{exc}") from exc


async def upsert_team_from_session(
    session_data: TeamSession,
    log_action: str = "add_team",
    proxy_id: int | None = None,
    expected_team_id: str | None = None,
) -> dict:
    access_token = session_data.accessToken
    session_token = session_data.sessionToken
    owner_email = session_data.user.get("email", "") if isinstance(session_data.user, dict) else ""

    payload, team_id = _decode_team_payload(access_token)
    _assert_session_account_matches(session_data, team_id)
    if expected_team_id and team_id != _normalize_uuid(expected_team_id, "team_id"):
        raise HTTPException(
            status_code=400,
            detail="导入的 Session 不属于当前 Team",
        )

    proxy_url = await _resolve_proxy_url(proxy_id)
    device_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, team_id))
    client = ChatGPTClient(access_token, team_id, device_id, proxy_url=proxy_url)

    account_info = await run_chatgpt_call(client.get_account_info)
    if "error" in account_info:
        refresh_result = await run_chatgpt_call(ChatGPTClient.refresh_token, session_token, proxy_url)
        log_refresh_diagnostics(
            team_id,
            "import_verify",
            "failed" if not isinstance(refresh_result, dict) or "error" in refresh_result else "ok",
            refresh_diagnostic_fields(refresh_result, access_token),
        )
        if "error" in refresh_result:
            raise HTTPException(status_code=502, detail=f"Failed to verify account: {account_info['error']}")

        access_token = refresh_result.get("accessToken", access_token)
        session_token = refresh_result.get("sessionToken", session_token)
        refreshed_payload, refreshed_team_id = _decode_team_payload(access_token)
        if refreshed_team_id != team_id:
            raise HTTPException(status_code=400, detail="Refreshed token belongs to a different team")
        payload = refreshed_payload
        client = ChatGPTClient(access_token, team_id, device_id, proxy_url=proxy_url)
        account_info = await run_chatgpt_call(client.get_account_info)
        if "error" in account_info:
            raise HTTPException(status_code=502, detail=f"Failed to verify account: {account_info['error']}")

    accounts = account_info.get("accounts", {})
    if team_id not in accounts:
        raise HTTPException(status_code=400, detail="Access token does not include the requested team")

    import asyncio

    subscription, balance_info, seat_counts, payment_methods = await asyncio.gather(
        run_chatgpt_call(client.get_subscription),
        run_chatgpt_call(client.get_remaining_balance),
        run_chatgpt_call(client.get_seat_type_counts),
        run_chatgpt_call(client.get_payment_methods),
    )

    team_name = accounts[team_id].get("account", {}).get("name", "")

    # Only ever record a field when the corresponding ChatGPT API call actually
    # succeeded. A transient failure on one endpoint must never silently
    # overwrite (or, for a brand-new Team, fabricate) another field's value
    # with a hardcoded guess — missing data must stay NULL, not become a fake
    # number that a later "success" log line makes look trustworthy.
    updates: dict[str, Any] = {}

    if "error" not in subscription:
        # 缺字段不写这一列；seats_entitled 只认正整数，不合格就保留库里上一次的
        # 合法值（null/0 落库会让 patrol 把全部默认席位算成超员）。规则见
        # seat_capacity.subscription_column_updates。
        updates.update(subscription_column_updates(subscription, team_id=team_id))

    # fetch_seat_pricing itself only returns keys it could actually resolve
    # (country_code/billing_symbol/price_per_seat/premium_price_per_seat/price_period/
    # billing_period); the prices are only non-NULL for a confirmed monthly or yearly period.
    updates.update(await fetch_seat_pricing(client, subscription))

    if "error" not in balance_info:
        balance_value = balance_info.get("balance")
        updates["balance"] = str(balance_value) if balance_value is not None else None

    if "error" not in seat_counts:
        updates.update(seat_counts_column_updates(seat_counts))
        official_codex = seat_type_count_from_seat_counts(seat_counts, CODEX_SEAT_TYPE)
        official_chatgpt = chatgpt_count_from_seat_counts(seat_counts)
        if official_codex is not None:
            updates["codex_count"] = official_codex
        if official_chatgpt is not None:
            updates["chatgpt_count"] = official_chatgpt

    # account_info is guaranteed non-error at this point (an earlier check
    # raises HTTPException otherwise), so these keys are always populated.
    updates.update(account_billing_updates(account_info, team_id))
    updates.update(subscription_billing_updates(subscription))

    if "error" not in payment_methods:
        methods = payment_methods.get("payment_methods", [])
        card = methods[0].get("card", {}) if methods else {}
        updates["card_last4"] = card.get("last4")
        updates["card_brand"] = card.get("brand")
        updates["payment_method_id"] = methods[0].get("id") if methods else None

    token_expires = None
    try:
        exp = payload.get("exp")
        if exp:
            token_expires = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    except Exception:
        pass

    now = datetime.now(timezone.utc).isoformat()

    # Identity/session fields always come from this call's own inputs (the
    # session being imported, the resolved proxy) — never from a ChatGPT API
    # call that could fail, so they're always safe to write unconditionally.
    identity_fields: dict[str, Any] = {
        "session_token": session_token,
        "access_token": access_token,
        "device_id": device_id,
        "token_expires": token_expires,
        "name": team_name,
        "owner_email": owner_email,
        "proxy_id": proxy_id,
    }

    async with get_db() as db:
        existing = await db.execute("SELECT id, status FROM teams WHERE id = ?", (team_id,))
        row = await existing.fetchone()

        if row:
            if row["status"] != "active":
                await db.execute(
                    "DELETE FROM patrol_team_baselines WHERE team_id = ?", (team_id,)
                )
            # Only the fields we actually resolved this run get overwritten.
            # Anything an API call failed to provide keeps its existing value
            # in the DB instead of being clobbered by a fake default.
            row_updates: dict[str, Any] = dict(updates)
            row_updates.update(identity_fields)
            row_updates["status"] = "active"
            # 能走到这里说明新 token 已经通过上面的 get_account_info 校验，授权是好的。
            # 不清掉 auth_state，界面会在导入成功之后继续挂着「会话失效」。
            row_updates["auth_state"] = "ok"
            row_updates["auth_state_since"] = None
            # 新会话已经校验通过，定时同步的挂起计时器一起清零。
            row_updates["sync_failing_since"] = None
            row_updates["sync_suspended_at"] = None
            row_updates["sync_probe_at"] = None
            row_updates["updated_at"] = now
            set_clause = ", ".join(f"{key} = ?" for key in row_updates)
            values = list(row_updates.values()) + [team_id]
            await db.execute(f"UPDATE teams SET {set_clause} WHERE id = ?", values)
        else:
            insert_columns = [
                "id", "name", "owner_email", "session_token", "access_token", "device_id",
                "token_expires", "card_last4", "card_brand", "payment_method_id",
                "seats_in_use", "seats_entitled", "codex_count", "chatgpt_count",
                "seat_capacity_json", "seat_type_counts_json",
                "is_codex_enabled",
                "country_code", "billing_currency", "billing_symbol", "billing_period",
                "price_per_seat", "premium_price_per_seat", "price_period",
                "discount_amount", "discount_duration_num_periods",
                "discount_expires_at", "discount_start_in_num_periods", "discount_quantity_off", "promo_campaign_id",
                "balance", "active_start", "active_until", "will_renew",
                "proxy_id", "status", "created_at", "updated_at",
            ]
            insert_values: dict[str, Any] = dict(updates)
            insert_values.update(identity_fields)
            insert_values.update({
                "id": team_id,
                "status": "active",
                "created_at": now,
                "updated_at": now,
            })
            if insert_values.get("chatgpt_count") is None:
                insert_values["chatgpt_count"] = max(
                    0,
                    int(insert_values.get("seats_in_use") or 0)
                    - int(insert_values.get("codex_count") or 0),
                )
            # Any column with no data this run (e.g. every ChatGPT API call
            # failed except get_account_info) is inserted as NULL, never a
            # fabricated business value.
            placeholders = ", ".join("?" for _ in insert_columns)
            await db.execute(
                f"INSERT INTO teams ({', '.join(insert_columns)}) VALUES ({placeholders})",
                [insert_values.get(col) for col in insert_columns],
            )
        await db.commit()

    session_dump = session_data.model_dump()
    session_dump["accessToken"] = access_token
    session_dump["sessionToken"] = session_token
    write_session_file(team_id, session_dump)

    await log_operation(team_id, log_action, owner_email, "Team added/updated", "success")

    return {"status": "ok", "team_id": team_id, "name": team_name}
