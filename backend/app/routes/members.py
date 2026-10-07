from datetime import datetime
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, status

from ..chatgpt_limiter import run_chatgpt_call
from ..database import log_operation
from ..member_cache_service import add_member_watch, fetch_and_cache_members, get_cached_members, update_cached_member_expiry
from ..models import ChangeSeatRequest, ExtendExpiryRequest, InviteMemberRequest, SetExpiryRequest
from ..services.member_expiry import (
    APP_LOCAL_TZ,
    ConfirmedInviteExpiry,
    ExtensionReceiptMismatchError,
    PermanentMembershipError,
    delete_member_expiry,
    extend_member_expiry,
    expires_in_to_datetime,
    mark_member_kicked,
    record_confirmed_invite_expiry,
    record_uncertain_invite,
    upsert_member_expiry,
)
from ..seat_types import (
    CODEX_SEAT_TYPE,
    DEFAULT_SEAT_TYPE,
    PREMIUM_SEAT_TYPE,
    is_billed_seat_type,
    is_known_seat_type,
    normalize_seat_type,
    seat_type_label,
)
from ..services.open_redemptions import find_open_redemption, open_redemption_detail
from ..services.overage_policy import (
    OPERATION_INVITE,
    OPERATION_SEAT_SWITCH,
    SeatCheck,
    check_billed_seat,
    load_team_policy,
    refuse,
    restore_overage_confirmation,
)
from ..services.team_locks import member_operation_claim, team_invite_lock
from ..services.team_locks import reserve_default_seat, reserve_seat
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


def _snapshot_email_entry(snapshot: dict | None, email: str) -> tuple[str | None, dict | None]:
    """邮箱在快照里的身份与对应条目：``("member", m)`` / ``("invite", inv)``（待接受邀请）/ ``(None, None)``。"""
    if not snapshot:
        return None, None
    email_lower = (email or "").strip().lower()
    for member in snapshot.get("members", []):
        if (member.get("email") or "").strip().lower() == email_lower:
            return "member", member
    for invite in snapshot.get("pending_invites", []):
        if (invite.get("email") or "").strip().lower() == email_lower:
            return "invite", invite
    return None, None


def _snapshot_contains_email(snapshot: dict | None, email: str) -> bool:
    return _snapshot_email_entry(snapshot, email)[0] is not None


_INVITE_EXISTING_MEMBER_DETAIL = (
    "该邮箱已是此 Team 的成员，未重复邀请。"
    "如需调整有效期，请在成员列表中使用「续期」或「设置到期」。"
)
_INVITE_LOOKUP_FAILED_DETAIL = "无法确认该邮箱是否已在此 Team（成员列表拉取失败），未发送邀请，请稍后重试"
_INVITE_MEMBER_BUSY_DETAIL = "该邮箱的成员状态正在变更（续期、到期处理或巡逻进行中），未发送邀请，请稍后重试"
# 被拒时记日志用的 action，按管理员操作区分；说明文案见 services/open_redemptions.py。
_OPEN_REDEMPTION_LOG_ACTIONS = {
    "invite": "invite_member",
    "set_expiry": "set_expiry",
    "extend_expiry": "extend_expiry",
    "remove_member": "remove_member",
    "revoke_invite": "revoke_invite",
}


async def _refuse_if_open_redemption(team_id: str, email: str, *, operation: str = "invite") -> None:
    """邮箱有未结兑换时 409，什么都不写、不碰上游。

    兑换邀请结果不明时码锁着、人可能已在 Team 里。管理员这时邀请或重发，会把这次
    填的有效期写进到期记录；之后对账确认那笔兑换，又在上面累加一次兑换码的时长：
    30 天码 + 管理员按 30 天补发 = 60 天。管理员唯一安全的出口是先给那笔兑换一个
    终态（「待确认的兑换」确认成功 / 确认失败退码），所以这里拒绝，不替他合并。
    「设置到期」「续期」写的也是同一条到期记录，对账同样会在上面再加一次，所以
    ``operation`` 为 ``set_expiry`` / ``extend_expiry`` 时走同一个检查。

    pending 的兑换不论落在哪个 Team 都拒（它还会换 Team）。uncertain 的兑换钉在原
    Team，按操作区分：
    * 邀请 / 重发看所有 Team：对账日后在原 Team 看见人就确认成功，管理员这时把人
      邀进另一个 Team，客户就凭一张码占了两个席位。
    * 设置到期 / 续期只看这个 Team：对账只给原 Team 的到期记录记账，别的 Team 的
      uncertain 叠不到这条记录上。

    邀请必须在 team_invite_lock 和该邮箱的成员操作占用之内、发任何上游请求之前调用：
    * 兑换的邀请分支只在同一把 team_invite_lock 里把兑换落到这个 Team、发邀请、
      记账或锁成 uncertain；续期分支要拿同一个成员操作占用。所以检查之后直到本次
      写完到期，没有兑换能在这个 Team 上为这个邮箱新开一笔或记账。
    * 对账任务（兑换对账、调度器回填、管理员收尾）只结算检查时已经是
      pending/uncertain 的兑换，那些在这里已经看得见、已经拒绝。
    * 检查之后才发起的兑换，最早也要等本次写完到期才能在这个 Team 上记账，按累加
      语义加在管理员这次的记录之上，与"管理员先邀请、客户后兑换"的串行顺序结果
      相同：两笔都是真实授予，不是同一笔被记两次。

    续期、设置到期、移除和撤邀请均在成员操作占用之内调用，防止检查后并发记账。
    """
    open_redemption = await find_open_redemption(
        team_id, email, uncertain_in_any_team=operation == "invite"
    )
    if open_redemption is None:
        return
    token_use_id = open_redemption["token_use_id"]
    detail = (
        "该邮箱有一笔未结兑换，未移除成员或撤销邀请；请等待兑换完成，或在待确认的兑换中完成对账。"
        if operation in {"remove_member", "revoke_invite"}
        else open_redemption_detail(open_redemption, operation=operation)
    )
    await log_operation(
        team_id,
        _OPEN_REDEMPTION_LOG_ACTIONS[operation],
        email,
        f"open_redemption token_use_id={token_use_id} result={open_redemption['result']}",
        "skipped",
        detail,
    )
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def _pending_invite_or_refuse_member(team_id: str, client, email: str) -> dict | None:
    """邀请前现拉一次该 Team 的成员 + 待接受邀请，决定这次是新邀请还是重发。

    * 已是正式成员 → 409。对他再发邀请，OpenAI 侧只是重发一封邮件，调整时间是
      续期 / 设置到期的事。拒绝发生在任何上游写操作之前，本地记录原样不动。
    * 已有待接受的邀请 → 返回那条邀请，调用方按"重发"处理：上游 ``resend_emails``
      重发邮件；本地到期经 ``record_confirmed_invite`` 合并，只延长、不缩短、不把
      永久变成有限。不能让管理员"先撤销再邀请"：撤销会把本地记录标成 kicked，
      已付时长 / 永久授权随之丢失，重新邀请只剩这次填的有效期。
    * 不在 → 返回 None，按新邀请处理。

    必须在 team_invite_lock 和该邮箱的成员操作占用之内现拉：缓存可能是几分钟前的，
    "缓存里没有"不能当作"不在"的证据。拉不到（网络失败、残缺名单）就是未知状态，
    失败关闭、不发邀请。
    """
    try:
        snapshot = await fetch_and_cache_members(team_id, client)
    except Exception as exc:
        # 登录已失效时"稍后重试"是误导：重试不会好，只能重新导入 session。
        if is_auth_error(exc) and await is_team_auth_rejected(team_id):
            raise team_auth_rejected_error() from exc
        await log_operation(
            team_id, "invite_member", email, "pre_invite_lookup", "failed", str(exc)
        )
        raise HTTPException(status_code=502, detail=_INVITE_LOOKUP_FAILED_DETAIL) from exc

    kind, entry = _snapshot_email_entry(snapshot, email)
    if kind == "member":
        await log_operation(
            team_id, "invite_member", email, "existing=member", "skipped", _INVITE_EXISTING_MEMBER_DETAIL
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_INVITE_EXISTING_MEMBER_DETAIL)
    if kind == "invite":
        return entry
    return None


def _invite_expiry_display(
    outcome: ConfirmedInviteExpiry, requested: Optional[datetime], expires_in: str
) -> str:
    """邀请落库后实际生效的有效期，给管理员看（接口响应、Telegram 卡片）。

    合并规则会保留更晚的已有到期和已授权的永久记录，这时照抄申请的 ``expires_in``
    就是在告诉管理员一个并不存在的到期时间。
    """
    if not outcome.recorded:
        return f"{expires_in}（本地记录写入失败，已记入待对账，最终到期以对账结果为准）"
    stored = outcome.expires_at
    if stored is None:
        text = "永久"
    else:
        parsed = parse_optional_datetime(stored)
        text = (
            f"{parsed.astimezone(APP_LOCAL_TZ):%Y-%m-%d %H:%M}（北京时间）" if parsed else stored
        )
    requested_iso = requested.isoformat() if requested is not None else None
    if stored != requested_iso:
        kept = "原有的永久授权" if stored is None else "原有更晚的到期"
        text += f"，已保留{kept}，本次填写的 {expires_in} 未生效"
    return text


async def _ensure_default_seat_available(
    client,
    team_id: str,
    *,
    email: str = "",
    confirmation=None,
    seat_type: str = DEFAULT_SEAT_TYPE,
    operation: str = OPERATION_INVITE,
    log_action: str = "invite_member",
) -> SeatCheck:
    """加一个计费席位（ChatGPT 或 Premium）之前按 Team 的超员策略把关。

    名字沿用旧的：测试和调用方都按这个名字替换它，现在它管所有计费类型，不只 default。
    规则见 services/overage_policy.py；必须在 team_invite_lock 与成员操作占用之内、
    发上游写请求之前调用。不放行时记一条 skipped 日志并抛 409（禁止超员 /
    需要确认）；放行时返回判断结果，调用方把 policy / overage 写进成功日志。
    ``confirmation`` 是请求的 ``overage_confirmation``；旧的 allow_overage 不传进来，
    在 confirm 的 Team 上它不算确认。
    """
    team = await load_team_policy(team_id)
    check = await check_billed_seat(
        client, team, seat_type, email=email, confirmation=confirmation
    )
    if not check.allowed:
        await refuse(check, operation=operation, action=log_action, email=email or None)
    return check


def _seat_check_log_suffix(check) -> str:
    """成功日志里追加的 policy / overage / 用掉的确认（没做检查时为空）。"""
    if isinstance(check, SeatCheck):
        text = f", policy={check.policy}, overage={check.overage}"
        if check.confirmation is not None:
            text += f", {check.confirmation.log_detail()}"
        return text
    return ""


async def _overage_response_fields(team_id: str, check) -> dict:
    """成功响应里的 ``overage``（这次可能让 ChatGPT 加购了）和 ``policy``（Team 的超员策略）。"""
    if isinstance(check, SeatCheck):
        return {"overage": check.overage, "policy": check.policy}
    return {"overage": False, "policy": (await load_team_policy(team_id)).policy}


async def _restore_confirmation(check) -> None:
    """上游明确拒绝：把这次从确认里扣掉的 1 个还回去（没扣就什么都不做）。"""
    if isinstance(check, SeatCheck):
        await restore_overage_confirmation(check.confirmation)


# 上游可能已经执行了的状态码：不算明确拒绝（同 ChatGPTClient.invite_member 的定性）。
_POSSIBLY_APPLIED_STATUS_CODES = frozenset({408, 409, 425, 429})


def _seat_change_rejected(result) -> bool:
    """change_seat_type 的失败是不是上游明确拒绝（4xx，可能已执行的那几个除外）。

    超时、断线、5xx 都不算：切换可能已经生效、ChatGPT 可能已经加购。
    """
    status_code = result.get("status_code") if isinstance(result, dict) else None
    return (
        isinstance(status_code, int)
        and 400 <= status_code < 500
        and status_code not in _POSSIBLY_APPLIED_STATUS_CODES
    )


def _must_reserve(seat_type: str, *, shown: bool) -> bool:
    """邀请 / 切换成功后要不要先占住一个这个类型的席位（进程内预留，15 分钟过期）。

    * ChatGPT：刷新后的名单还没反映出来时才占（与以前相同）。
    * Premium：总是占。上游的待接受邀请万一不带 seat_type，会按 ChatGPT 计数，
      不再占着那个 Premium 席位；宁可 15 分钟内少卖一个，也不让下一个人触发加购。
    """
    if seat_type == PREMIUM_SEAT_TYPE:
        return True
    return not shown


async def _reserve_billed_seat(team_id: str, email: str, seat_type: str) -> None:
    """邀请 / 切换成功但刷新后的名单还没反映出来时，先占住这个类型的一个席位。"""
    if not email or not is_billed_seat_type(seat_type):
        return
    if seat_type == DEFAULT_SEAT_TYPE:
        await reserve_default_seat(team_id, email)
    else:
        await reserve_seat(team_id, email, seat_type)


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
        # 重发时这个邮箱已在 Team 里（待接受），续期、到期撤邀请、巡逻都可能正对同一
        # 个人动手：与它们共用同一份成员操作占用，拿到之后再现拉名单。
        async with member_operation_claim(
            team_id,
            email=req.email,
            operation="admin_invite",
        ) as acquired:
            if not acquired:
                await log_operation(
                    team_id,
                    "invite_member",
                    req.email,
                    "member operation in progress",
                    "skipped",
                    _INVITE_MEMBER_BUSY_DETAIL,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=_INVITE_MEMBER_BUSY_DETAIL
                )
            return await _invite_member_claimed(team_id, req, expires_in, expires_at)


async def _invite_member_claimed(team_id: str, req: InviteMemberRequest, expires_in: str, expires_at):
    """``invite_member`` 在 team_invite_lock 与成员操作占用之内的部分。"""
    # 新邀请和重发都先过这一关：有对账日后还会记账的兑换就 409，见函数注释。
    await _refuse_if_open_redemption(team_id, req.email)
    client = await get_team_client(team_id)
    # 正式成员 409、待接受邀请按重发处理、拉不到名单失败关闭，见函数注释。
    pending_invite = await _pending_invite_or_refuse_member(team_id, client, req.email)
    resend = pending_invite is not None
    seat_type = normalize_seat_type(req.seat_type)
    if resend:
        pending_seat_type = normalize_seat_type(pending_invite.get("seat_type"))
        if not is_known_seat_type(pending_seat_type):
            # 待接受邀请的席位类型 TeamBoss 不认识（例如 automation）：重发或改它，上游
            # 会怎么计费不确定，一律不动。在任何策略检查和上游写请求之前拒绝。
            message = (
                f"该邮箱有一个待接受的邀请，席位类型是 {seat_type_label(pending_seat_type)}，"
                "TeamBoss 不认识这个类型，不会重发或改动它。"
            )
            await log_operation(
                team_id,
                "invite_member",
                req.email,
                f"seat_type={seat_type}, pending_seat_type={pending_seat_type}, resend=True",
                "skipped",
                message,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "seat_type_unknown", "message": message, "seat_type": pending_seat_type},
            )
    # 同类席位的待接受邀请已经计入该类型的待接受数、占着那个席位，重发不新增席位，
    # 不该再按超员策略拦。席位类型不同时上游怎么处理不确定，照常检查。
    same_seat_resend = resend and normalize_seat_type(pending_invite.get("seat_type")) == seat_type
    seat_check = None
    if is_billed_seat_type(seat_type) and not same_seat_resend:
        seat_check = await _ensure_default_seat_available(
            client,
            team_id,
            email=req.email,
            confirmation=req.overage_confirmation,
            seat_type=seat_type,
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
                f"seat_type={seat_type}{_seat_check_log_suffix(seat_check)}",
                "uncertain",
                error,
            )
            # 邀请可能已经到了 OpenAI：照样占住这个类型的一个席位（Premium 持久占到对账），
            # 用掉的确认也不还。
            await _reserve_billed_seat(team_id, req.email, seat_type)
            raise HTTPException(
                status_code=409,
                detail="邀请结果确认中，请先刷新成员列表，勿重复提交",
            )

    error = _chatgpt_error(result)
    if error:
        if mutation_status == "rejected":
            await _restore_confirmation(seat_check)
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
    # 报给管理员的有效期以合并后实际落库的为准，不是这次填写的 expires_in。
    outcome = await record_confirmed_invite_expiry(team_id, user_id, req.email, expires_at)
    expiry_display = _invite_expiry_display(outcome, expires_at, expires_in)

    stored_detail = outcome.expires_at if outcome.recorded else "pending_reconciliation"
    await log_operation(
        team_id,
        "invite_member",
        req.email,
        f"seat_type={req.seat_type}, expires_in={expires_in}, "
        f"resend={resend}, stored_expires_at={stored_detail}{_seat_check_log_suffix(seat_check)}",
        "success",
    )
    snapshot = await _refresh_members_after_mutation(team_id, "invite", email=req.email)
    if _must_reserve(seat_type, shown=_snapshot_contains_email(snapshot, req.email)):
        await _reserve_billed_seat(team_id, req.email, seat_type)

    await notify_member_event(
        "后台拉人",
        team_id,
        email=req.email,
        source="admin",
        detail=f"{'重发待接受的邀请, ' if resend else ''}席位：{seat_type_label(seat_type)}, 有效期：{expiry_display}",
    )

    return {
        "status": "ok",
        "result": result,
        "resent": resend,
        # 合并后实际生效的到期（None = 永久）；expiry_recorded=False 时本地写入失败，
        # 这里是待对账的申请值。
        "expires_at": outcome.expires_at,
        "expiry_recorded": outcome.recorded,
        "expiry_display": expiry_display,
        **(await _overage_response_fields(team_id, seat_check)),
    }


@router.delete("/members/{user_id}")
async def remove_member(team_id: str, user_id: str):
    canonical_user_id, target_email = await _resolve_member_identity(team_id, user_id)
    async with member_operation_claim(
        team_id, email=target_email, user_id=canonical_user_id, operation="admin_remove_member"
    ) as acquired:
        if not acquired:
            raise HTTPException(status_code=409, detail="成员状态正在变更，请稍后重试")
        canonical_user_id, target_email = await _resolve_member_identity(
            team_id, user_id, target_email, refresh=True
        )
        await _refuse_if_open_redemption(team_id, target_email, operation="remove_member")
        return await _remove_member_claimed(team_id, canonical_user_id, target_email)


async def _remove_member_claimed(team_id: str, user_id: str, target_email: str):
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


_SEAT_MEMBER_BUSY_DETAIL = "该成员的状态正在变更（邀请、续期、到期处理或巡逻进行中），未切换席位，请稍后重试"
_SEAT_LOOKUP_FAILED_DETAIL = "无法确认该成员当前的席位（成员列表拉取失败），未切换席位，请稍后重试"


def _member_user_id(member: dict) -> str:
    return (member.get("id") or member.get("user_id") or "").strip()


async def _live_member_or_refuse(team_id: str, client, user_id: str) -> dict:
    """现拉名单里 ``user_id`` 对应的那一个成员。拉不到 502（失败关闭），不在 404。"""
    try:
        snapshot = await fetch_and_cache_members(team_id, client)
    except Exception as exc:
        if is_auth_error(exc) and await is_team_auth_rejected(team_id):
            raise team_auth_rejected_error() from exc
        await log_operation(
            team_id, "change_seat", None, f"user_id={user_id}, pre_switch_lookup", "failed", str(exc)
        )
        raise HTTPException(status_code=502, detail=_SEAT_LOOKUP_FAILED_DETAIL) from exc
    matches = [m for m in (snapshot or {}).get("members", []) if _member_user_id(m) == user_id]
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail="该成员已不在此 Team，请刷新后重试")
    return matches[0]


@router.patch("/members/{user_id}/seat")
async def change_seat(team_id: str, user_id: str, req: ChangeSeatRequest):
    """切换成员的席位类型。切到计费类型（ChatGPT / Premium）按超员策略把关，切到 Codex 不查。

    和邀请共用 team_invite_lock，再占住这个成员（与邀请、续期、巡逻同一份占用），
    然后现拉名单拿他当前的席位：不认识的类型一律不动；与目标相同直接返回。
    上游 change_seat_type 是唯一的写操作，只在检查通过之后发。
    """
    user_id = (user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="成员 ID 缺失")
    target = normalize_seat_type(req.seat_type)

    async with team_invite_lock(team_id):
        client = await get_team_client(team_id)
        # 占用按邮箱区分（邀请、续期、巡逻都按邮箱占），先从缓存拿邮箱，缓存里没有再现拉一次。
        email = await _cached_email_for_user(team_id, user_id)
        if not email:
            member = await _live_member_or_refuse(team_id, client, user_id)
            email = (member.get("email") or "").strip().lower()

        async with member_operation_claim(
            team_id,
            email=email,
            user_id=user_id,
            operation="admin_change_seat",
        ) as acquired:
            if not acquired:
                await log_operation(
                    team_id,
                    "change_seat",
                    email or None,
                    f"user_id={user_id}, seat_type={target}, member operation in progress",
                    "skipped",
                    _SEAT_MEMBER_BUSY_DETAIL,
                )
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_SEAT_MEMBER_BUSY_DETAIL)
            return await _change_seat_claimed(team_id, user_id, email, target, req, client)


async def _change_seat_claimed(team_id: str, user_id: str, email: str, target: str, req: ChangeSeatRequest, client):
    """``change_seat`` 在 team_invite_lock 与成员操作占用之内的部分。"""
    member = await _live_member_or_refuse(team_id, client, user_id)
    live_email = (member.get("email") or "").strip().lower()
    if email and live_email and live_email != email:
        raise HTTPException(status_code=409, detail="成员信息已变化，请刷新后重试")
    email = live_email or email
    current = normalize_seat_type(member.get("seat_type"))
    base_detail = f"user_id={user_id}, seat_type={target}, from_seat_type={current}"

    if not is_known_seat_type(current):
        message = f"该成员当前是 {seat_type_label(current)} 席位，TeamBoss 不认识这个类型，不会改动它。"
        await log_operation(team_id, "change_seat", email or None, base_detail, "skipped", message)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "seat_type_unknown", "message": message, "seat_type": current},
        )
    if current == target:
        return {"status": "ok", "result": None, "unchanged": True, **(await _overage_response_fields(team_id, None))}

    seat_check = None
    if is_billed_seat_type(target):
        seat_check = await _ensure_default_seat_available(
            client,
            team_id,
            email=email,
            confirmation=req.overage_confirmation,
            seat_type=target,
            operation=OPERATION_SEAT_SWITCH,
            log_action="change_seat",
        )
    elif target != CODEX_SEAT_TYPE:
        # 请求模型只收注册表里的类型，走不到这里；万一走到，不认识的类型一律不动。
        raise HTTPException(status_code=422, detail=f"不支持的席位类型：{seat_type_label(target)}")
    # 切到 Codex 会空出一个计费席位，不会加购，不查。

    result = await run_chatgpt_call(client.change_seat_type, user_id, target)
    policy_detail = f", policy={seat_check.policy}" if isinstance(seat_check, SeatCheck) else ""

    error = _chatgpt_error(result)
    if error:
        if _seat_change_rejected(result):
            await _restore_confirmation(seat_check)
            await log_operation(
                team_id,
                "change_seat",
                email or None,
                f"{base_detail}{policy_detail}",
                "failed",
                error,
            )
            raise HTTPException(status_code=502, detail=error)
        # 超时、5xx 等：切换可能已经生效（ChatGPT 可能已经加购），用掉的确认不还，
        # 并占住目标类型的一个席位（Premium 持久占到对账）。
        await log_operation(
            team_id,
            "change_seat",
            email or None,
            f"{base_detail}{_seat_check_log_suffix(seat_check) or policy_detail}",
            "uncertain",
            error,
        )
        await _reserve_billed_seat(team_id, email, target)
        raise HTTPException(
            status_code=502,
            detail=f"切换结果不明确，请先刷新成员列表确认，勿重复提交：{error}",
        )

    await log_operation(
        team_id,
        "change_seat",
        email or None,
        f"{base_detail}{_seat_check_log_suffix(seat_check)}",
        "success",
    )
    snapshot = await _refresh_members_after_mutation(team_id, "")
    refreshed = [
        m for m in (snapshot or {}).get("members", []) if _member_user_id(m) == user_id
    ]
    shown = bool(refreshed and normalize_seat_type(refreshed[0].get("seat_type")) == target)
    if _must_reserve(target, shown=shown):
        await _reserve_billed_seat(team_id, email, target)

    return {"status": "ok", "result": result, **(await _overage_response_fields(team_id, seat_check))}


@router.delete("/invites/{email:path}")
async def revoke_invite(team_id: str, email: str):
    email = unquote(email).strip().lower()
    async with team_invite_lock(team_id):
        async with member_operation_claim(
            team_id, email=email, operation="admin_revoke_invite"
        ) as acquired:
            if not acquired:
                raise HTTPException(status_code=409, detail="成员状态正在变更，请稍后重试")
            await _refuse_if_open_redemption(team_id, email, operation="revoke_invite")
            client = await get_team_client(team_id)
            snapshot = await fetch_and_cache_members(team_id, client)
            kind, _entry = _snapshot_email_entry(snapshot, email)
            if kind != "invite":
                raise HTTPException(status_code=409, detail="邀请状态已变化，请刷新后重试")
            return await _revoke_invite_claimed(team_id, email, client)


async def _revoke_invite_claimed(team_id: str, email: str, client):
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
    async with member_operation_claim(
        team_id, email=email, user_id=canonical_user_id, operation="admin_set_expiry"
    ) as acquired:
        if not acquired:
            raise HTTPException(status_code=409, detail="成员状态正在变更，请稍后重试")
        canonical_user_id, email = await _resolve_member_identity(
            team_id, user_id, email, refresh=True
        )
        # 有对账日后还会记账的兑换就 409：否则对账会在这次设的到期上再加一次兑换码时长。
        await _refuse_if_open_redemption(team_id, email, operation="set_expiry")
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
        # 有对账日后还会记账的兑换就 409：否则对账会在这次续的时长上再加一次兑换码时长。
        await _refuse_if_open_redemption(team_id, canonical_email, operation="extend_expiry")
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
