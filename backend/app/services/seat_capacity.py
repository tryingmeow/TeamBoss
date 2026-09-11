import asyncio
import json
from dataclasses import dataclass
from typing import Any

from ..chatgpt_limiter import run_chatgpt_call
from ..database import get_db
from ..utils.durations import utc_now


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ChatGPTSeatCapacity:
    seats_entitled: int
    seats_in_use_total: int
    codex_count: int
    active_chatgpt: int
    pending_default: int
    available: int


@dataclass(frozen=True)
class MemberSeatUsage:
    seats_in_use_total: int
    codex_count: int
    active_chatgpt: int


def chatgpt_seat_capacity(
    *,
    seats_entitled: Any,
    seats_in_use: Any,
    codex_count: Any,
    active_chatgpt: Any = None,
    pending_default: Any = 0,
) -> ChatGPTSeatCapacity:
    entitled = safe_int(seats_entitled)
    total_in_use = safe_int(seats_in_use)
    codex = safe_int(codex_count)
    pending = safe_int(pending_default)
    resolved_chatgpt = (
        max(0, total_in_use - codex)
        if active_chatgpt is None
        else max(0, safe_int(active_chatgpt))
    )
    available = max(0, entitled - resolved_chatgpt - pending)
    return ChatGPTSeatCapacity(
        seats_entitled=entitled,
        seats_in_use_total=total_in_use,
        codex_count=codex,
        active_chatgpt=resolved_chatgpt,
        pending_default=pending,
        available=available,
    )


def seat_type_count_from_seat_counts(
    seat_counts: dict[str, Any],
    seat_type: str,
) -> int | None:
    counts = seat_counts.get("seat_type_counts", {}) if isinstance(seat_counts, dict) else {}
    if not isinstance(counts, dict) or seat_type not in counts or counts.get(seat_type) is None:
        return None
    try:
        return max(0, int(counts[seat_type]))
    except (TypeError, ValueError):
        return None


def codex_count_from_seat_counts(seat_counts: dict[str, Any]) -> int:
    value = seat_type_count_from_seat_counts(seat_counts, "usage_based")
    return value if value is not None else 0


def chatgpt_count_from_seat_counts(seat_counts: dict[str, Any]) -> int | None:
    """Return the official ChatGPT/default seat count when the API supplied it."""
    return seat_type_count_from_seat_counts(seat_counts, "default")


def member_seat_usage_from_members(members: Any) -> MemberSeatUsage | None:
    if not isinstance(members, list):
        return None

    active_members = [
        item for item in members
        if isinstance(item, dict) and item.get("status", "active") == "active"
    ]
    codex_count = sum(1 for item in active_members if item.get("seat_type") == "usage_based")
    chatgpt_count = sum(
        1 for item in active_members
        if (item.get("seat_type") or "default") == "default"
    )
    seats_in_use_total = len(active_members)
    return MemberSeatUsage(
        seats_in_use_total=seats_in_use_total,
        codex_count=codex_count,
        active_chatgpt=chatgpt_count,
    )


def member_seat_usage_from_members_data(members_data: Any) -> MemberSeatUsage | None:
    if not isinstance(members_data, dict):
        return None
    return member_seat_usage_from_members(members_data.get("members"))


async def update_member_seat_usage_cache(team_id: str, members: Any) -> MemberSeatUsage | None:
    usage = member_seat_usage_from_members(members)
    if usage is None:
        return None

    async with get_db() as db:
        await db.execute(
            """UPDATE teams SET
                 seats_in_use = ?,
                 codex_count = ?,
                 chatgpt_count = ?
               WHERE id = ?""",
            (usage.seats_in_use_total, usage.codex_count, usage.active_chatgpt, team_id),
        )
        await db.commit()
    return usage


def pending_default_count_from_api(pending_data: dict[str, Any]) -> int:
    items = pending_data.get("items") if isinstance(pending_data, dict) else None
    if not isinstance(items, list):
        items = pending_data.get("invites") if isinstance(pending_data, dict) else None
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if (item.get("seat_type") or "default") == "default")


def chatgpt_api_error(result: dict[str, Any]) -> str | None:
    return result.get("error") if isinstance(result, dict) else None


class SeatCapacityFetchError(Exception):
    pass


async def fetch_live_chatgpt_seat_capacity(client: Any, pending_limit: int = 100) -> tuple[
    ChatGPTSeatCapacity,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    subscription, seat_counts, pending = await asyncio.gather(
        run_chatgpt_call(client.get_subscription),
        run_chatgpt_call(client.get_seat_type_counts),
        run_chatgpt_call(client.get_pending_invites, 0, pending_limit),
    )

    for result in (subscription, seat_counts, pending):
        error = chatgpt_api_error(result)
        if error:
            raise SeatCapacityFetchError(error)

    capacity = chatgpt_seat_capacity(
        seats_entitled=subscription.get("seats_entitled"),
        seats_in_use=subscription.get("seats_in_use"),
        codex_count=codex_count_from_seat_counts(seat_counts),
        active_chatgpt=chatgpt_count_from_seat_counts(seat_counts),
        pending_default=pending_default_count_from_api(pending),
    )
    return capacity, subscription, seat_counts, pending


async def update_capacity_cache(
    team_id: str,
    subscription: dict[str, Any],
    seat_counts: dict[str, Any],
) -> None:
    if chatgpt_api_error(subscription) or chatgpt_api_error(seat_counts):
        return

    now = utc_now().isoformat()
    async with get_db() as db:
        cursor = await db.execute("SELECT members_json FROM member_cache WHERE team_id = ?", (team_id,))
        row = await cursor.fetchone()
        member_usage = None
        if row:
            try:
                member_usage = member_seat_usage_from_members(json.loads(row["members_json"] or "[]"))
            except Exception:
                member_usage = None

        subscription_total = (
            safe_int(subscription.get("seats_in_use"))
            if subscription.get("seats_in_use") is not None
            else None
        )
        official_codex = seat_type_count_from_seat_counts(seat_counts, "usage_based")
        official_chatgpt = chatgpt_count_from_seat_counts(seat_counts)

        seats_in_use = (
            subscription_total
            if subscription_total is not None
            else member_usage.seats_in_use_total if member_usage is not None
            else 0
        )
        codex_count = (
            official_codex
            if official_codex is not None
            else member_usage.codex_count if member_usage is not None
            else 0
        )
        chatgpt_count = (
            official_chatgpt
            if official_chatgpt is not None
            else member_usage.active_chatgpt if member_usage is not None
            else max(0, seats_in_use - codex_count)
        )

        # Build update dict with only fields that are actually present in the API response.
        # Omitted fields keep their existing DB values (including NULL if never set).
        updates: dict[str, Any] = {
            "seats_in_use": seats_in_use,
            "codex_count": codex_count,
            "chatgpt_count": chatgpt_count,
            "updated_at": now,
        }

        # Only add these if they're actually in the subscription response
        if "active_start" in subscription:
            updates["active_start"] = subscription["active_start"]
        if "active_until" in subscription:
            updates["active_until"] = subscription["active_until"]
        if "seats_entitled" in subscription:
            updates["seats_entitled"] = subscription["seats_entitled"]
        if "billing_currency" in subscription:
            updates["billing_currency"] = subscription["billing_currency"]
        if "will_renew" in subscription:
            will_renew_raw = subscription["will_renew"]
            updates["will_renew"] = None if will_renew_raw is None else (1 if will_renew_raw else 0)

        set_clause = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [team_id]
        await db.execute(f"UPDATE teams SET {set_clause} WHERE id = ?", values)
        await db.commit()
