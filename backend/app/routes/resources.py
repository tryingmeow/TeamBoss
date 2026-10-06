from typing import Optional

from fastapi import APIRouter, Query

from ..database import get_db
from ..member_cache_service import fetch_and_cache_members, get_cached_members
from ..seat_types import DEFAULT_SEAT_TYPE, PREMIUM_SEAT_TYPE, normalize_seat_type
from ..services.seat_capacity import (
    cached_seat_capacity,
    chatgpt_seat_capacity,
    member_seat_usage_from_members,
    safe_int,
)
from ..services.team_clients import get_team_client


router = APIRouter(prefix="/api/resources", tags=["resources"])


def _pending_default_count(cache: Optional[dict]) -> int:
    if not cache:
        return 0
    pending = cache.get("pending_invites") or []
    if not isinstance(pending, list):
        return 0
    return sum(
        1
        for item in pending
        if isinstance(item, dict) and normalize_seat_type(item.get("seat_type")) == DEFAULT_SEAT_TYPE
    )


def _premium_in_use(cache: Optional[dict]) -> int | None:
    """缓存里 Premium 席位的在用人数；没有成员缓存时返回 None（未知）。"""
    members = cache.get("members") if cache else None
    if not isinstance(members, list):
        return None
    return sum(
        1
        for item in members
        if isinstance(item, dict)
        and item.get("status", "active") == "active"
        and normalize_seat_type(item.get("seat_type")) == PREMIUM_SEAT_TYPE
    )


async def _load_cache(team: dict, refresh: bool, errors: list[dict]) -> Optional[dict]:
    if not refresh:
        cached = await get_cached_members(team["id"])
        if cached is not None:
            return cached

    if team.get("status") != "active":
        return await get_cached_members(team["id"])

    try:
        client = await get_team_client(team["id"])
        snapshot = await fetch_and_cache_members(team["id"], client)
        return {
            "members": snapshot.get("members", []),
            "pending_invites": snapshot.get("pending_invites", []),
            "updated_at": snapshot.get("cached_at"),
        }
    except Exception as exc:
        errors.append({
            "team_id": team["id"],
            "team_name": team.get("name"),
            "error": str(getattr(exc, "detail", exc)),
        })
        return await get_cached_members(team["id"])


@router.get("/usage")
async def get_resource_usage(refresh: bool = Query(False)):
    """
    API-key protected capacity summary.

    Use `refresh=true` when the caller wants to refresh member/invite cache before
    calculating pending GPT-seat reservations.
    """
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM teams ORDER BY created_at DESC")
        rows = await cursor.fetchall()

    teams = [dict(row) for row in rows]
    errors: list[dict] = []
    team_items: list[dict] = []

    total_team = len(teams)
    active_team = 0
    inuse_gpt = 0
    inuse_codex = 0
    inuse_premium = 0
    pending_gpt_invites = 0
    total_gpt_seats = 0
    free_gpt_seats = 0
    free_team_count = 0

    for team in teams:
        seats_in_use = safe_int(team.get("seats_in_use"))
        seats_entitled = safe_int(team.get("seats_entitled"))
        codex_count = safe_int(team.get("codex_count"))
        chatgpt_count = (
            safe_int(team.get("chatgpt_count"))
            if team.get("chatgpt_count") is not None
            else None
        )
        cache = await _load_cache(team, refresh, errors)
        member_usage = member_seat_usage_from_members(cache.get("members") if cache else None)
        if member_usage is not None:
            seats_in_use = member_usage.seats_in_use_total
            codex_count = member_usage.codex_count
            chatgpt_count = member_usage.active_chatgpt
        pending_default = _pending_default_count(cache)
        seat_capacity = cached_seat_capacity(team.get("seat_capacity_json"))
        premium_entry = (seat_capacity or {}).get(PREMIUM_SEAT_TYPE)
        premium_in_use = _premium_in_use(cache)
        capacity = chatgpt_seat_capacity(
            seats_entitled=seats_entitled,
            seats_in_use=seats_in_use,
            codex_count=codex_count,
            active_chatgpt=chatgpt_count,
            pending_default=pending_default,
            # 与邀请路径同一条规则：旧公式和分类型空位取更小的，宁可少报空位。
            seat_capacity=seat_capacity,
        )

        is_active = team.get("status") == "active"
        active_gpt = capacity.active_chatgpt if is_active else 0
        team_free_gpt = capacity.available if is_active else 0
        team_is_idle = team_free_gpt > 0

        if is_active:
            active_team += 1
            total_gpt_seats += seats_entitled
            inuse_gpt += active_gpt
            inuse_codex += codex_count
            inuse_premium += premium_in_use or 0
            pending_gpt_invites += pending_default
            free_gpt_seats += team_free_gpt
            if team_is_idle:
                free_team_count += 1

        team_items.append({
            "team_id": team.get("id"),
            "team_name": team.get("name"),
            "owner_email": team.get("owner_email"),
            "status": team.get("status"),
            "is_idle": team_is_idle,
            "seats_entitled": seats_entitled,
            "seats_in_use": seats_in_use,
            "inuse_gpt": active_gpt if is_active else 0,
            "inuse_codex": codex_count if is_active else 0,
            "pending_gpt_invites": pending_default if is_active else 0,
            "free_gpt_seats": team_free_gpt,
            "inuse_premium": (premium_in_use or 0) if is_active else 0,
            "premium_seats_paid": premium_entry["paid"] if premium_entry else 0,
            "card_last4": team.get("card_last4"),
            "active_until": team.get("active_until"),
            "cache_loaded": cache is not None,
        })

    return {
        "is_idle": free_gpt_seats > 0,
        "total_team": total_team,
        "active_team": active_team,
        "inuse_gpt": inuse_gpt,
        "inuse_codex": inuse_codex,
        "inuse_premium": inuse_premium,
        "pending_gpt_invites": pending_gpt_invites,
        "total_gpt_seats": total_gpt_seats,
        "free_gpt_seats": free_gpt_seats,
        "free_team_count": free_team_count,
        "refresh": refresh,
        "teams": team_items,
        "errors": errors,
    }
