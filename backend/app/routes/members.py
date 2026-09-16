from urllib.parse import unquote

from fastapi import APIRouter, HTTPException

from ..chatgpt_limiter import run_chatgpt_call
from ..database import log_operation
from ..member_cache_service import add_member_watch, fetch_and_cache_members, get_cached_members, update_cached_member_expiry
from ..models import ChangeSeatRequest, InviteMemberRequest, SetExpiryRequest
from ..services.member_expiry import (
    delete_member_expiry,
    expires_in_to_datetime,
    mark_member_kicked,
    record_confirmed_invite,
    record_uncertain_invite,
    upsert_member_expiry,
)
from ..services.seat_capacity import (
    SeatCapacityFetchError,
    fetch_live_chatgpt_seat_capacity,
    update_capacity_cache,
)
from ..services.team_locks import team_invite_lock
from ..services.team_locks import reserve_default_seat, reserved_default_seats
from ..services.team_clients import get_team_client
from ..services.tg_notify import notify_member_event
from ..services.user_display_names import attach_display_names
from ..utils.durations import DurationError, parse_optional_datetime


router = APIRouter(prefix="/api/teams/{team_id}", tags=["members"])


def _chatgpt_error(result: dict) -> str | None:
    return result.get("error") if isinstance(result, dict) else None


def _expiry_from_request(req: SetExpiryRequest):
    if req.expires_at:
        expires_at = parse_optional_datetime(req.expires_at)
        if not expires_at:
            raise HTTPException(status_code=400, detail="expires_at 格式无效，请使用 ISO 时间")
        return expires_at, f"expires_at={expires_at.isoformat()}"
    if req.expires_in:
        try:
            return expires_in_to_datetime(req.expires_in), f"expires_in={req.expires_in}"
        except DurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail="expires_in 或 expires_at 必填")


async def _cached_email_for_user(team_id: str, user_id: str) -> str:
    cached = await get_cached_members(team_id)
    if not cached:
        return ""
    for member in cached["members"]:
        if member.get("id") == user_id:
            return (member.get("email") or "").strip().lower()
    return ""


async def _refresh_members_after_mutation(
    team_id: str,
    reason: str,
    *,
    email: str | None = None,
    user_id: str | None = None,
) -> dict | None:
    try:
        client = await get_team_client(team_id)
        snapshot = await fetch_and_cache_members(team_id, client)
        if reason:
            await add_member_watch(team_id, reason, target_email=email, target_user_id=user_id)
        return snapshot
    except Exception as exc:
        await log_operation(team_id, "member_cache_refresh", email, f"reason={reason}", "failed", str(exc))
        return None


def _snapshot_contains_email(snapshot: dict | None, email: str) -> bool:
    if not snapshot:
        return False
    email_lower = (email or "").strip().lower()
    for member in snapshot.get("members", []):
        if (member.get("email") or "").strip().lower() == email_lower:
            return True
    for invite in snapshot.get("pending_invites", []):
        if (invite.get("email") or "").strip().lower() == email_lower:
            return True
    return False


async def _ensure_default_seat_available(
    client,
    team_id: str,
    *,
    email: str = "",
    allow_overage: bool = False,
) -> None:
    if allow_overage:
        return

    try:
        capacity, subscription, seat_counts, _pending = await fetch_live_chatgpt_seat_capacity(client)
    except SeatCapacityFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await update_capacity_cache(team_id, subscription, seat_counts)
    reserved = await reserved_default_seats(team_id, exclude_email=email)
    available_after_reservations = capacity.available - reserved

    if available_after_reservations <= 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "require_overage_confirmation",
                "message": (
                    "ChatGPT 席位不足，需要确认超额添加: "
                    f"active_chatgpt={capacity.active_chatgpt}/{capacity.seats_entitled}, "
                    f"total_in_use={capacity.seats_in_use_total}, "
                    f"codex={capacity.codex_count}, "
                    f"pending_default={capacity.pending_default}, "
                    f"reserved_default={reserved}"
                ),
                "capacity": {
                    "seats_entitled": capacity.seats_entitled,
                    "seats_in_use_total": capacity.seats_in_use_total,
                    "codex_count": capacity.codex_count,
                    "active_chatgpt": capacity.active_chatgpt,
                    "pending_default": capacity.pending_default,
                    "reserved_default": reserved,
                    "available": max(0, available_after_reservations),
                },
            },
        )


@router.get("/members")
async def get_members(team_id: str, refresh: bool = False):
    cached = None if refresh else await get_cached_members(team_id)
    if cached is not None:
        return await attach_display_names({
            "members": cached["members"],
            "pending_invites": cached["pending_invites"],
            "total": len(cached["members"]) + len(cached["pending_invites"]),
            "cached": True,
            "cached_at": cached["updated_at"],
        })

    client = await get_team_client(team_id)
    return await attach_display_names(await fetch_and_cache_members(team_id, client))


@router.post("/members/invite")
async def invite_member(team_id: str, req: InviteMemberRequest):
    expires_in = req.expires_in or "never"
    try:
        expires_at = expires_in_to_datetime(expires_in)
    except DurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with team_invite_lock(team_id):
        client = await get_team_client(team_id)
        if (req.seat_type or "default") == "default":
            await _ensure_default_seat_available(
                client,
                team_id,
                email=req.email,
                allow_overage=req.allow_overage,
            )

        result = await run_chatgpt_call(client.invite_member, req.email, req.seat_type)

        mutation_status = result.get("_mutation_status") if isinstance(result, dict) else None
        if mutation_status == "uncertain":
            try:
                snapshot = await fetch_and_cache_members(team_id, client)
            except Exception:
                snapshot = None
            if _snapshot_contains_email(snapshot, req.email):
                result = {"_mutation_status": "confirmed"}
            else:
                error = _chatgpt_error(result) or "OpenAI invite result is uncertain"
                await record_uncertain_invite(
                    team_id,
                    "",
                    req.email,
                    expires_at,
                    source="system",
                    reason=error,
                )
                await log_operation(
                    team_id,
                    "invite_member",
                    req.email,
                    None,
                    "uncertain",
                    error,
                )
                raise HTTPException(
                    status_code=409,
                    detail="邀请结果确认中，请先刷新成员列表，勿重复提交",
                )

        error = _chatgpt_error(result)
        if error:
            await log_operation(team_id, "invite_member", req.email, None, "failed", error)
            await notify_member_event(
                "后台拉人", team_id, email=req.email, result="failed", source="admin", detail=error
            )
            raise HTTPException(status_code=502, detail=error)

        invited_users = result.get("invited", result.get("items", [])) if isinstance(result, dict) else []
        user_id = ""
        if isinstance(invited_users, list) and invited_users:
            user_id = invited_users[0].get("id") or invited_users[0].get("user_id") or ""

        # OpenAI 邀请已在上面成功，本地记录必须最终落地，见 record_confirmed_invite 注释。
        await record_confirmed_invite(team_id, user_id, req.email, expires_at)

        await log_operation(
            team_id,
            "invite_member",
            req.email,
            f"seat_type={req.seat_type}, expires_in={expires_in}, allow_overage={req.allow_overage}",
            "success",
        )
        snapshot = await _refresh_members_after_mutation(team_id, "invite", email=req.email)
        if (req.seat_type or "default") == "default" and not _snapshot_contains_email(snapshot, req.email):
            await reserve_default_seat(team_id, req.email)

        await notify_member_event(
            "后台拉人",
            team_id,
            email=req.email,
            source="admin",
            detail=f"seat_type={req.seat_type}, expires_in={expires_in}",
        )

        return {"status": "ok", "result": result}


@router.delete("/members/{user_id}")
async def remove_member(team_id: str, user_id: str):
    target_email: str | None = None
    cached = await get_cached_members(team_id)
    if cached:
        for member in cached["members"]:
            if member.get("id") == user_id:
                target_email = member.get("email")
                break

    client = await get_team_client(team_id)
    result = await run_chatgpt_call(client.remove_member, user_id)

    error = _chatgpt_error(result)
    if error:
        await log_operation(team_id, "remove_member", target_email, f"user_id={user_id}", "failed", error)
        await notify_member_event(
            "后台踢人", team_id, email=target_email, result="failed", source="admin", detail=error
        )
        raise HTTPException(status_code=502, detail=error)

    await mark_member_kicked(team_id, kick_source="admin", user_id=user_id, email=target_email or "")
    await log_operation(team_id, "remove_member", target_email, f"user_id={user_id}", "success")
    await _refresh_members_after_mutation(team_id, "kick", email=target_email, user_id=user_id)
    await notify_member_event("后台踢人", team_id, email=target_email, source="admin")

    return {"status": "ok"}


@router.patch("/members/{user_id}/seat")
async def change_seat(team_id: str, user_id: str, req: ChangeSeatRequest):
    client = await get_team_client(team_id)
    target_email = await _cached_email_for_user(team_id, user_id)
    result = await run_chatgpt_call(client.change_seat_type, user_id, req.seat_type)

    error = _chatgpt_error(result)
    if error:
        await log_operation(
            team_id,
            "change_seat",
            target_email,
            f"user_id={user_id}, seat_type={req.seat_type}",
            "failed",
            error,
        )
        raise HTTPException(status_code=502, detail=error)

    await log_operation(team_id, "change_seat", target_email, f"user_id={user_id}, seat_type={req.seat_type}", "success")
    await _refresh_members_after_mutation(team_id, "")

    return {"status": "ok", "result": result}


@router.delete("/invites/{email:path}")
async def revoke_invite(team_id: str, email: str):
    email = unquote(email)
    client = await get_team_client(team_id)
    result = await run_chatgpt_call(client.revoke_invite, email)

    error = _chatgpt_error(result)
    if error:
        await log_operation(team_id, "revoke_invite", email, None, "failed", error)
        await notify_member_event(
            "后台撤邀请", team_id, email=email, result="failed", source="admin", detail=error
        )
        raise HTTPException(status_code=502, detail=error)

    await mark_member_kicked(team_id, kick_source="admin", email=email)
    await log_operation(team_id, "revoke_invite", email, None, "success")
    await _refresh_members_after_mutation(team_id, "kick", email=email)
    await notify_member_event("后台撤邀请", team_id, email=email, source="admin")

    return {"status": "ok"}


@router.put("/members/{user_id}/expiry")
async def set_expiry(team_id: str, user_id: str, req: SetExpiryRequest):
    expires_at, detail = _expiry_from_request(req)
    email = req.email or await _cached_email_for_user(team_id, user_id)
    expires_iso = await upsert_member_expiry(team_id, user_id, email, expires_at)
    await log_operation(team_id, "set_expiry", email, f"user_id={user_id}, {detail}", "success")
    await update_cached_member_expiry(team_id, user_id=user_id, email=email, expires_at=expires_iso)

    return {"status": "ok", "expires_at": expires_iso}


@router.delete("/members/{user_id}/expiry")
async def remove_expiry(team_id: str, user_id: str):
    email = await _cached_email_for_user(team_id, user_id)
    await delete_member_expiry(team_id, user_id=user_id, email=email)
    await log_operation(team_id, "remove_expiry", email, f"user_id={user_id}", "success")
    await update_cached_member_expiry(team_id, user_id=user_id, email=email, expires_at=None)
    return {"status": "ok"}
