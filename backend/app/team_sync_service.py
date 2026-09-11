import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from .chatgpt_client import ChatGPTClient
from .chatgpt_limiter import run_chatgpt_call
from .database import get_db, log_operation
from .member_cache_service import fetch_and_cache_members, get_cached_members
from .services.pricing import account_billing_updates, fetch_seat_pricing
from .services.seat_capacity import (
    chatgpt_count_from_seat_counts,
    member_seat_usage_from_members,
    member_seat_usage_from_members_data,
    safe_int,
    seat_type_count_from_seat_counts,
    update_member_seat_usage_cache,
)
from .services.team_clients import get_team_client
from .services.team_health_alerts import (
    is_auth_error,
    report_team_failure,
    report_team_recovery,
)


TEAM_CACHE_TTL_SECONDS = 5 * 60


def normalize_default_seat_type(settings: dict[str, Any]) -> str:
    value = settings.get("default_seat_type")
    if value is None:
        value = settings.get("value")
    return "usage_based" if value == "usage_based" else "default"


def _chatgpt_error(result: dict[str, Any]) -> str | None:
    return result.get("error") if isinstance(result, dict) else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _is_fresh(value: str | None, now: datetime, ttl_seconds: int = TEAM_CACHE_TTL_SECONDS) -> bool:
    parsed = _parse_time(value)
    if parsed is None:
        return False
    return now - parsed < timedelta(seconds=ttl_seconds)


def _decode_cached_data(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cached_default_seat_type(raw: str | None) -> str | None:
    """Return the cached workspace default, preserving "not cached" as None."""
    cached_data = _decode_cached_data(raw)
    settings = cached_data.get("workspace_settings")
    if not isinstance(settings, dict):
        return None
    return normalize_default_seat_type(settings)


def _workspace_response(settings: dict[str, Any], cached_at: str | None, cached: bool) -> dict[str, Any]:
    return {
        "default_seat_type": normalize_default_seat_type(settings),
        "settings": settings,
        "cached": cached,
        "cached_at": cached_at,
    }


def _cached_workspace_from_row(row, *, now: datetime | None = None, max_age_seconds: int | None = None) -> dict[str, Any] | None:
    cached_data = _decode_cached_data(row["cached_data"] if row else None)
    settings = cached_data.get("workspace_settings")
    cached_at = cached_data.get("workspace_settings_cached_at")
    if not isinstance(settings, dict):
        return None
    if now is not None and max_age_seconds is not None and not _is_fresh(cached_at, now, max_age_seconds):
        return None
    return _workspace_response(settings, cached_at, True)


def member_emails_from_members_data(members_data: dict[str, Any] | None) -> list[str]:
    if not members_data:
        return []
    emails: set[str] = set()
    for member in members_data.get("members") or []:
        email = member.get("email")
        if email:
            emails.add(email.lower())
    for invite in members_data.get("pending_invites") or []:
        email = invite.get("email")
        if email:
            emails.add(email.lower())
    return sorted(emails)


async def _load_team_row(team_id: str):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM teams WHERE id = ?", (team_id,))
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Team not found")
    return row


def _members_cache_response(cached: dict[str, Any]) -> dict[str, Any]:
    return {
        "members": cached["members"],
        "pending_invites": cached["pending_invites"],
        "total": len(cached["members"]) + len(cached["pending_invites"]),
        "cached": True,
        "cached_at": cached["updated_at"],
    }


async def _fetch_overview(
    client: ChatGPTClient,
    *,
    fallback_country_code: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    subscription, balance_info, seat_counts, payment_methods, account_info = await asyncio.gather(
        run_chatgpt_call(client.get_subscription),
        run_chatgpt_call(client.get_remaining_balance),
        run_chatgpt_call(client.get_seat_type_counts),
        run_chatgpt_call(client.get_payment_methods),
        run_chatgpt_call(client.get_account_info),
    )

    # Only ever add a key when the corresponding ChatGPT API call actually
    # succeeded. _write_team_sync_cache builds its UPDATE from exactly the keys
    # present in this dict, so anything omitted here keeps its existing DB
    # value instead of being overwritten with a hardcoded guess.
    updates: dict[str, Any] = {}
    if "error" not in subscription:
        # 缺字段就不写这一列，避免把原本正确的值覆盖成 NULL（seats_entitled 被清成
        # NULL 会让 patrol 的 over_by 抬成全部席位，一趟踢光）。
        for col in ("seats_in_use", "seats_entitled", "billing_currency",
                    "active_start", "active_until"):
            if col in subscription:
                updates[col] = subscription.get(col)
        if "will_renew" in subscription:
            will_renew_raw = subscription.get("will_renew")
            updates["will_renew"] = None if will_renew_raw is None else (1 if will_renew_raw else 0)

        pricing_updates = await fetch_seat_pricing(
            client,
            subscription,
            fallback_country_code=fallback_country_code,
        )
        updates.update(pricing_updates)

    if "error" not in balance_info:
        balance_value = balance_info.get("balance")
        updates["balance"] = str(balance_value) if balance_value is not None else None

    if "error" not in seat_counts:
        official_codex = seat_type_count_from_seat_counts(seat_counts, "usage_based")
        official_chatgpt = chatgpt_count_from_seat_counts(seat_counts)
        if official_codex is not None:
            updates["codex_count"] = official_codex
        if official_chatgpt is not None:
            updates["chatgpt_count"] = official_chatgpt

    if "error" not in payment_methods:
        methods = payment_methods.get("payment_methods", [])
        if methods:
            card = methods[0].get("card", {})
            updates["card_last4"] = card.get("last4")
            updates["card_brand"] = card.get("brand")
            updates["payment_method_id"] = methods[0].get("id")

    updates.update(account_billing_updates(account_info, client.team_id))

    cached = {
        "subscription": subscription,
        "balance": balance_info,
        "seat_counts": seat_counts,
        "payment_methods": payment_methods,
        "account_info": account_info,
    }
    return updates, cached


async def _fetch_workspace_settings(client: ChatGPTClient) -> dict[str, Any]:
    result = await run_chatgpt_call(client.get_workspace_settings)
    error = _chatgpt_error(result)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return result


def _apply_member_seat_usage(
    overview_updates: dict[str, Any],
    overview_cache: dict[str, Any],
    members_data: dict[str, Any],
) -> None:
    usage = member_seat_usage_from_members_data(members_data)
    if usage is None:
        return

    overview_updates.setdefault("seats_in_use", usage.seats_in_use_total)
    overview_updates.setdefault("codex_count", usage.codex_count)
    overview_updates.setdefault("chatgpt_count", usage.active_chatgpt)
    overview_cache["member_seat_usage"] = {
        "seats_in_use_total": usage.seats_in_use_total,
        "codex_count": usage.codex_count,
        "active_chatgpt": usage.active_chatgpt,
    }


def _extract_sync_failures(overview_cache: dict[str, Any]) -> list[str]:
    """Extract list of failed sub-interfaces from overview_cache.
    Returns names of interfaces that have 'error' key."""
    failed = []
    for key in ("subscription", "balance", "seat_counts", "payment_methods", "account_info"):
        result = overview_cache.get(key, {})
        if isinstance(result, dict) and "error" in result:
            failed.append(key)
    return failed


async def _write_team_sync_cache(
    team_id: str,
    overview_updates: dict[str, Any],
    overview_cache: dict[str, Any],
    workspace_settings: dict[str, Any],
    now_iso: str,
):
    row = await _load_team_row(team_id)
    cached_data = _decode_cached_data(row["cached_data"])
    cached_data.update(overview_cache)
    cached_data["overview_cached_at"] = now_iso
    cached_data["workspace_settings"] = workspace_settings
    cached_data["workspace_settings_cached_at"] = now_iso

    updates = dict(overview_updates)
    updates["cached_data"] = json.dumps(cached_data, ensure_ascii=False)

    # Only update last_full_sync_at if overview sync was completely successful
    failed_interfaces = _extract_sync_failures(overview_cache)
    if not failed_interfaces:
        # All overview sub-interfaces succeeded
        updates["last_full_sync_at"] = now_iso
        updates["last_sync_partial_failures"] = None
        # 手动同步全绿 = 这个 Team 又能用了，解除定时同步挂起。手动同步是
        # 挂起之后唯一不受节流限制的入口，也就是界面上的那个"恢复"动作。
        updates["sync_failing_since"] = None
        updates["sync_suspended_at"] = None
        updates["sync_probe_at"] = None
    else:
        # Some overview sub-interfaces failed; record them but still update updated_at
        updates["last_sync_partial_failures"] = json.dumps(failed_interfaces, ensure_ascii=False)

    # Always update updated_at when cache is refreshed (even partial updates)
    updates["updated_at"] = now_iso

    set_clause = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [team_id]
    async with get_db() as db:
        await db.execute(f"UPDATE teams SET {set_clause} WHERE id = ?", values)
        await db.commit()

    return await _load_team_row(team_id)


async def get_cached_workspace_settings(team_id: str, max_age_seconds: int = TEAM_CACHE_TTL_SECONDS) -> dict[str, Any] | None:
    row = await _load_team_row(team_id)
    return _cached_workspace_from_row(row, now=_utcnow(), max_age_seconds=max_age_seconds)


async def fetch_and_cache_workspace_settings(team_id: str, client: ChatGPTClient | None = None) -> dict[str, Any]:
    client = client or await get_team_client(team_id)
    result = await _fetch_workspace_settings(client)
    now_iso = _utcnow().isoformat()

    row = await _load_team_row(team_id)
    cached_data = _decode_cached_data(row["cached_data"])
    cached_data["workspace_settings"] = result
    cached_data["workspace_settings_cached_at"] = now_iso

    async with get_db() as db:
        await db.execute(
            "UPDATE teams SET cached_data = ? WHERE id = ?",
            (json.dumps(cached_data, ensure_ascii=False), team_id),
        )
        await db.commit()

    return _workspace_response(result, now_iso, False)


async def update_workspace_settings_cache(team_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    now_iso = _utcnow().isoformat()
    row = await _load_team_row(team_id)
    cached_data = _decode_cached_data(row["cached_data"])
    cached_data["workspace_settings"] = settings
    cached_data["workspace_settings_cached_at"] = now_iso

    async with get_db() as db:
        await db.execute(
            "UPDATE teams SET cached_data = ? WHERE id = ?",
            (json.dumps(cached_data, ensure_ascii=False), team_id),
        )
        await db.commit()

    return _workspace_response(settings, now_iso, False)


async def sync_team_cache(team_id: str, force: bool = False, max_age_seconds: int = TEAM_CACHE_TTL_SECONDS) -> dict[str, Any]:
    row = await _load_team_row(team_id)
    now = _utcnow()
    cached_data = _decode_cached_data(row["cached_data"])
    cached_members = await get_cached_members(team_id)
    cached_workspace = _cached_workspace_from_row(row)

    overview_at = cached_data.get("overview_cached_at") or row["updated_at"]
    overview_fresh = _is_fresh(overview_at, now, max_age_seconds)
    members_fresh = bool(cached_members and _is_fresh(cached_members.get("updated_at"), now, max_age_seconds))
    workspace_fresh = bool(
        cached_workspace and _is_fresh(cached_workspace.get("cached_at"), now, max_age_seconds)
    )

    should_refresh = force or not (overview_fresh and members_fresh and workspace_fresh)

    if not should_refresh:
        usage = member_seat_usage_from_members(cached_members.get("members") if cached_members else None)
        if usage is not None and (
            safe_int(row["seats_in_use"]) != usage.seats_in_use_total
            or safe_int(row["codex_count"]) != usage.codex_count
            or safe_int(row["chatgpt_count"]) != usage.active_chatgpt
        ):
            await update_member_seat_usage_cache(team_id, cached_members.get("members"))
            row = await _load_team_row(team_id)

        return {
            "row": row,
            "members": _members_cache_response(cached_members),
            "workspace_settings": cached_workspace,
            "cached": True,
            "refreshed": False,
            "reason": "ttl_fresh",
        }

    client = await get_team_client(team_id)
    overview_task = _fetch_overview(client, fallback_country_code=row["country_code"])
    members_task = fetch_and_cache_members(team_id, client)
    workspace_task = _fetch_workspace_settings(client)

    try:
        (overview_updates, overview_cache), members, workspace_settings = await asyncio.gather(
            overview_task,
            members_task,
            workspace_task,
        )
    except HTTPException as exc:
        await log_operation(team_id, "sync_team", None, None, "failed", str(exc.detail), "manual")
        if not getattr(exc, "team_health_reported", False):
            await report_team_failure(
                team_id,
                "chatgpt_auth" if is_auth_error(exc) else "team_sync",
                exc,
                source="team_sync",
            )
        raise
    except Exception as exc:
        await log_operation(team_id, "sync_team", None, None, "failed", str(exc), "manual")
        if not getattr(exc, "team_health_reported", False):
            await report_team_failure(
                team_id,
                "chatgpt_auth" if is_auth_error(exc) else "team_sync",
                exc,
                source="team_sync",
            )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    _apply_member_seat_usage(overview_updates, overview_cache, members)

    now_iso = _utcnow().isoformat()
    refreshed_row = await _write_team_sync_cache(
        team_id,
        overview_updates,
        overview_cache,
        workspace_settings,
        now_iso,
    )
    workspace = _workspace_response(workspace_settings, now_iso, False)

    # Check for partial sync failures (some overview sub-interfaces failed)
    failed_interfaces = _extract_sync_failures(overview_cache)
    if failed_interfaces:
        detail = "force=true" if force else "ttl_expired"
        error_msg = f"overview sub-interface failures: {', '.join(failed_interfaces)}"
        await log_operation(team_id, "sync_team", None, detail, "partial", error_msg)
    else:
        await log_operation(team_id, "sync_team", None, "force=true" if force else "ttl_expired", "success")

    await report_team_recovery(team_id, "team_sync", source="team_sync")
    await report_team_recovery(team_id, "chatgpt_auth", source="team_sync")

    return {
        "row": refreshed_row,
        "members": members,
        "workspace_settings": workspace,
        "cached": False,
        "refreshed": True,
        "reason": "force" if force else "ttl_expired",
    }
