from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, status

from ..chatgpt_limiter import run_chatgpt_call
from ..database import log_operation
from ..member_cache_service import add_member_watch, fetch_and_cache_members, get_cached_members, update_cached_member_expiry
from ..models import ChangeSeatRequest, ExtendExpiryRequest, InviteMemberRequest, SetExpiryRequest
from ..services.member_expiry import (
    ExtensionReceiptMismatchError,
    PermanentMembershipError,
    delete_member_expiry,
    extend_member_expiry,
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
from ..services.team_locks import member_operation_claim, team_invite_lock
from ..services.team_locks import reserve_default_seat, reserved_default_seats
from ..services.team_clients import (
    get_team_client,
    is_team_auth_rejected,
    team_auth_rejected_error,
)
from ..services.team_health_alerts import is_auth_error
from ..services.tg_notify import notify_member_event
from ..services.user_display_names import attach_display_names
from ..utils.durations import DurationError, normalize_duration, parse_optional_datetime


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


async def _resolve_member_identity(
    team_id: str,
    user_id: str,
    supplied_email: str | None = None,
    *,
    refresh: bool = False,
) -> tuple[str, str]:
    """Return the one current Team member addressed by ``user_id``.

    Expiry writes are per-team and must never pair one member's user id with
    another member's email.  The client-provided email is therefore only a
    consistency check; the canonical identity always comes from the Team
    snapshot.
    """
    canonical_user_id = (user_id or "").strip()
    if not canonical_user_id:
        raise HTTPException(status_code=400, detail="成员 ID 缺失")

    cached = None if refresh else await get_cached_members(team_id)
    if cached is None:
        try:
            client = await get_team_client(team_id)
            cached = await fetch_and_cache_members(team_id, client)
        except Exception as exc:
            # 登录已失效时"稍后重试"是误导：重试不会好，只能重新导入 session。
            if is_auth_error(exc) and await is_team_auth_rejected(team_id):
                raise team_auth_rejected_error() from exc
            raise HTTPException(status_code=502, detail="无法确认成员身份，请稍后重试") from exc

    matches = [
        member
        for member in cached.get("members", [])
        if (member.get("id") or member.get("user_id") or "").strip() == canonical_user_id
    ]
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail="该成员已不在此 Team，请刷新后重试")

    canonical_email = (matches[0].get("email") or "").strip().lower()
    if not canonical_email:
        raise HTTPException(status_code=409, detail="成员邮箱缺失，无法安全续期")
    requested_email = (supplied_email or "").strip().lower()
    if requested_email and requested_email != canonical_email:
        raise HTTPException(status_code=409, detail="提交邮箱与此成员不一致，请刷新后重试")
    return canonical_user_id, canonical_email


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
    canonical_user_id, email = await _resolve_member_identity(team_id, user_id, req.email)
    expires_iso = await upsert_member_expiry(team_id, canonical_user_id, email, expires_at)
    await log_operation(team_id, "set_expiry", email, f"user_id={canonical_user_id}, {detail}", "success")
    await update_cached_member_expiry(
        team_id, user_id=canonical_user_id, email=email, expires_at=expires_iso
    )

    return {"status": "ok", "expires_at": expires_iso}


@router.post("/members/{user_id}/expiry/extend")
async def extend_expiry(team_id: str, user_id: str, req: ExtendExpiryRequest):
    """Append a duration without overwriting the member's unused time."""
    try:
        # Validate before making any change; ``extend_member_expiry`` then
        # owns the atomic max(existing expiry, now) + duration calculation.
        duration = normalize_duration(req.expires_in, allow_never=True)
    except DurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    canonical_user_id, canonical_email = await _resolve_member_identity(team_id, user_id, req.email)
    async with member_operation_claim(
        team_id,
        email=canonical_email,
        user_id=canonical_user_id,
        operation="admin_extend_expiry",
    ) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="成员状态正在变更，请稍后重试",
            )

        # A patrol/removal may have completed between the first snapshot and
        # acquiring the persistent claim.  Re-read the Team under the claim
        # and reject an identity change instead of mixing user_id and email.
        canonical_user_id, canonical_email = await _resolve_member_identity(
            team_id, user_id, req.email, refresh=True
        )
        detail = f"user_id={canonical_user_id}, expires_in={duration}, request_id={req.request_id}"
        try:
            expires_iso = await extend_member_expiry(
                team_id,
                canonical_user_id,
                canonical_email,
                duration,
                admin_request_id=req.request_id,
                admin_audit_detail=detail,
            )
        except PermanentMembershipError as exc:
            await log_operation(
                team_id,
                "extend_expiry",
                canonical_email,
                detail,
                "failed",
                "永久成员不能增加有限时长",
            )
            raise HTTPException(status_code=409, detail="永久成员不能增加有限时长") from exc
        except ExtensionReceiptMismatchError as exc:
            raise HTTPException(status_code=409, detail="续期请求标识已用于另一笔操作") from exc

    # The financial write, receipt, and audit log have committed together.
    # Cache is presentation-only: a failure is recoverable by the next member
    # refresh and must not turn a successful extension into a failed response.
    try:
        await update_cached_member_expiry(
            team_id,
            user_id=canonical_user_id,
            email=canonical_email,
            expires_at=expires_iso,
        )
    except Exception:
        # Keep the committed success response; user listings merge expiry data
        # from member_expiry and a later snapshot repairs this cache naturally.
        import logging

        logging.getLogger(__name__).exception(
            "extend_expiry: cache update failed after committed extension team=%s user_id=%s",
            team_id,
            canonical_user_id,
        )
    return {"status": "ok", "expires_at": expires_iso}


@router.delete("/members/{user_id}/expiry")
async def remove_expiry(team_id: str, user_id: str):
    canonical_user_id, email = await _resolve_member_identity(team_id, user_id)
    await delete_member_expiry(team_id, user_id=canonical_user_id, email=email)
    await log_operation(team_id, "remove_expiry", email, f"user_id={canonical_user_id}", "success")
    await update_cached_member_expiry(team_id, user_id=canonical_user_id, email=email, expires_at=None)
    return {"status": "ok"}
