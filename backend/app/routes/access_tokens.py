import asyncio
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
from datetime import datetime
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..client_ip import RateLimiter as _RateLimiter, get_client_ip, rate_limit_key
from ..chatgpt_client import ChatGPTClient, mask_secrets
from ..chatgpt_limiter import run_chatgpt_call
from ..database import get_db, log_operation
from ..member_cache_service import add_member_watch, fetch_and_cache_members, get_cached_members
from ..security import require_admin
from ..services.member_expiry import (
    PermanentMembershipError,
    extend_member_expiry,
    get_active_expiry_state,
    insert_pending_invite_reconciliation_row,
    record_confirmed_invite_extension,
    resolve_token_use_reconciliations_in_tx,
)
from ..services.team_clients import get_proxy_url as _get_proxy_url
from ..services.open_redemptions import (
    INTERRUPTED_INVITE_AFTER_SECONDS as _INTERRUPTED_INVITE_AFTER_SECONDS,
    STALE_LOCAL_REDEMPTION_AFTER_SECONDS as _STALE_LOCAL_REDEMPTION_AFTER_SECONDS,
)
from ..seat_types import (
    CODE_SEAT_TYPES,
    CODEX_SEAT_TYPE,
    DEFAULT_SEAT_TYPE,
    PREMIUM_SEAT_TYPE,
    CodeSeatTypeLiteral,
    is_known_seat_type,
    normalize_seat_type,
    seat_type_label,
)
from ..services.seat_capacity import (
    SeatCapacityFetchError,
    billed_free_seats,
    cached_seat_capacity,
    fetch_live_chatgpt_seat_capacity,
    fetch_live_seat_type_capacity,
    update_capacity_cache,
)
from ..services.team_clients import (
    load_active_teams,
    subscription_lapsed,
    team_login_rejected,
)
from ..services.team_locks import (
    member_operation_claim,
    reserve_default_seat,
    reserve_seat,
    reserved_default_seats,
    reserved_seats,
    team_invite_lock,
)
from ..services.tg_notify import mask_email_for_notice, notify_admins, notify_member_event
from ..tg_format import detail_card
from ..utils.durations import (
    DurationError,
    expiry_from_duration,
    normalize_duration,
    parse_optional_datetime,
    utc_now,
)


admin_router = APIRouter(
    prefix="/api/access-tokens",
    tags=["access-tokens"],
    dependencies=[Depends(require_admin)],
)
public_router = APIRouter(prefix="/api/self-service", tags=["self-service"])

logger = logging.getLogger(__name__)




# 限流器实例
_limiter_query = _RateLimiter(max_requests=30, window_seconds=60)  # 查询：30请求/分钟
_limiter_redeem = _RateLimiter(max_requests=10, window_seconds=60)  # 兑换：10请求/分钟


class _RedeemLookupBudget:
    """公开兑换接口里「会去上游实时拉成员列表」的尝试次数预算。

    兑换以「团队不存在 / 需要选 Team / Owner / 永久成员 / 没有空位」等结尾时码会退回，
    同一张有效未用的码因此可以无限重放；而每一次都要实时拉 Owner 账号下各 Team 的成员
    列表（账号风控风险），还和同步、自动踢人抢同一个全局上游并发额度。这里给两层上限：

    * 每张码：``per_code`` 次 / ``per_code_window`` 秒；
    * 全站：``global_limit`` 次 / ``global_window`` 秒（攻击者手里码再多也有上限）。

    只在码校验通过之后、占用之前计数（无效码根本不碰上游，按 IP 限流就够了）。先判断
    两层是否都有余量，都有才同时记账——否则一张已被单码上限挡住的码还能继续消耗全站
    额度，一张码就能把所有人的兑换堵死。码的消耗时机和规则完全不受影响。

    以服务端/上游故障（5xx）结尾的尝试，事后用 ``refund_code`` 退回单码的那一次：客户在
    上游故障期间反复重试，不该把自己的码锁一小时。「请选择 Team」提示（正常兑换的第一步）
    用 ``refund_prompt`` 退，但每张码每小时最多退 2 次，之后照常计数。
    全站那一次不退——这些尝试照样实时拉了上游，全站上限本来就是给上游兜底的。
    """

    def __init__(self, per_code: int, per_code_window: int, global_limit: int, global_window: int):
        self.per_code = per_code
        self.per_code_window = per_code_window
        self.global_limit = global_limit
        self.global_window = global_window
        self._per_code_hits: dict[int, list[float]] = {}
        self._global_hits: list[float] = []
        self._prompt_refunds: dict[int, list[float]] = {}

    def try_take(self, token_id: int, now: float) -> Optional[str]:
        """有余量就记一次并返回 None；否则返回被哪一层挡住（"per_code" / "global"）。"""
        self._global_hits = [ts for ts in self._global_hits if ts > now - self.global_window]
        code_hits = [
            ts for ts in self._per_code_hits.get(token_id, ()) if ts > now - self.per_code_window
        ]
        if len(code_hits) >= self.per_code:
            self._per_code_hits[token_id] = code_hits
            return "per_code"
        if len(self._global_hits) >= self.global_limit:
            if code_hits:
                self._per_code_hits[token_id] = code_hits
            else:
                self._per_code_hits.pop(token_id, None)
            return "global"
        code_hits.append(now)
        self._per_code_hits[token_id] = code_hits
        self._global_hits.append(now)
        if len(self._per_code_hits) > 10000:
            cutoff = now - self.per_code_window
            for key in [k for k, hits in self._per_code_hits.items() if not hits or hits[-1] <= cutoff]:
                del self._per_code_hits[key]
        return None

    def refund_code(self, token_id: int, taken_at: float) -> None:
        """退回 ``try_take(token_id, taken_at)`` 记在这张码上的那一次（全站计数不动）。"""
        hits = self._per_code_hits.get(token_id)
        if hits and taken_at in hits:
            hits.remove(taken_at)

    def refund_untouched(self, token_id: int, taken_at: float) -> None:
        """单码和全站的那一次都退回。**只给一次上游请求都没发出的尝试用。**

        两层预算都是给上游兜底的；这种尝试只读了本地库，计进全站额度反而让一张码
        配一个会被拒的邮箱就能把全站额度耗光，挡住所有人的兑换。
        """
        self.refund_code(token_id, taken_at)
        if taken_at in self._global_hits:
            self._global_hits.remove(taken_at)

    def refund_prompt(self, token_id: int, taken_at: float, now: float, limit: int = 2) -> bool:
        """「请选择 Team」提示的退回：每张码每个滚动窗口最多退 ``limit`` 次，超出照常计数。

        否则一张码配一个在多个 Team 里的邮箱就能无限提示、永不触达单码上限，一张码耗尽全站额度。
        """
        recent = [
            ts for ts in self._prompt_refunds.get(token_id, ()) if ts > now - self.per_code_window
        ]
        if len(recent) >= limit:
            self._prompt_refunds[token_id] = recent
            return False
        self.refund_code(token_id, taken_at)
        recent.append(now)
        self._prompt_refunds[token_id] = recent
        if len(self._prompt_refunds) > 10000:
            cutoff = now - self.per_code_window
            for key in [k for k, v in self._prompt_refunds.items() if not v or v[-1] <= cutoff]:
                del self._prompt_refunds[key]
        return True


# 每张码每小时 10 次（5xx 和前 2 次选择提示不算）：选错 Team、没空位后重试都够用；
# 全站每 10 分钟 60 次：正常售卖远低于此，被挡的人码不消耗，稍后重试即可。
_redeem_lookup_budget = _RedeemLookupBudget(
    per_code=10, per_code_window=3600, global_limit=60, global_window=600
)

# 公开兑换响应不能让持码人借此确认「某个邮箱是不是 Team 的 Owner」。Owner 邮箱和
# 「没有到期时间的成员」（续期同样被拒）对外是同一句话、同一种 Team 选项、同一条兑换
# 记录；内部审计（access_token_uses / 操作日志）照旧记 owner_email，管理端看得到。
_NOT_RENEWABLE_DETAIL = "该邮箱不能使用兑换码续期。兑换码未使用，如需处理请联系管理员。"
_UNAVAILABLE_TEAM_DETAIL = (
    "该邮箱所在的 Team 暂时不可用，暂不能自助兑换或续期。兑换码未使用，请联系管理员处理。"
)


class _LocalRefusal(HTTPException):
    """在发出任何上游请求之前就拒绝、且码不消耗的兑换。

    ``redeem_access_token`` 据此把这次尝试的预算（单码 + 全站）全部退回，见
    ``_RedeemLookupBudget.refund_untouched``。
    """


# ---- 兑换码的席位类型 ------------------------------------------------------------
# ChatGPT 码（default）照旧；Premium 码（prolite）只进「有已付 Premium 空位」的 Team，
# 不看超员策略、绝不超员（没有空位时邀请会让 ChatGPT 自动加购并扣费）。
# 续期要求码的类型和成员当前的席位类型对得上，见 _renewal_seat_type_block。

_NO_PREMIUM_SEAT_DETAIL = "暂时没有可用的 Premium 席位，兑换码未使用。请稍后再试或联系管理员。"
_INVALID_CODE_SEAT_TYPE_DETAIL = "这张兑换码的席位类型无效，暂不能兑换。兑换码未使用，请联系管理员。"
_UNKNOWN_MEMBER_SEAT_TYPE_DETAIL = "你当前的席位类型不能用兑换码续期。兑换码未使用，如需处理请联系管理员。"
# 告诉管理员 Premium 码没位置时，最多列出这么多个 Team 的原因。
_NOTICE_MAX_TEAMS = 8
# 同一个码「Premium 没位置」的 Telegram 通知一小时最多一条（客户可能反复重试）；日志每次都写。
_PREMIUM_NOTICE_WINDOW_SECONDS = 3600
_premium_notice_sent: dict[Any, float] = {}


def _renewal_seat_type_block(code_seat_type: str, member_seat_type: Any) -> Optional[str]:
    """码能不能给当前席位类型的成员续期：能 → None；不能 → 原因码。

    * Premium 码只续 Premium（prolite）成员；
    * ChatGPT 码续 ChatGPT（default）和 Codex（usage_based）成员（与以前一样），不续 Premium；
    * 成员的席位类型缺失按 ChatGPT；不在注册表里的类型一律不续（``unknown_member_seat_type``）。
    """
    member = normalize_seat_type(member_seat_type)
    if not is_known_seat_type(member):
        return "unknown_member_seat_type"
    if normalize_seat_type(code_seat_type) == PREMIUM_SEAT_TYPE:
        allowed: tuple[str, ...] = (PREMIUM_SEAT_TYPE,)
    else:
        allowed = (DEFAULT_SEAT_TYPE, CODEX_SEAT_TYPE)
    return None if member in allowed else "seat_type_mismatch"


def _seat_type_mismatch_detail(code_seat_type: str, member_seat_type: Any, reason: str) -> str:
    if reason == "unknown_member_seat_type":
        return _UNKNOWN_MEMBER_SEAT_TYPE_DETAIL
    return (
        f"兑换码是 {seat_type_label(code_seat_type)} 码，你当前是 "
        f"{seat_type_label(member_seat_type)} 席位，不能用它续期。兑换码未使用。"
    )


def _seat_type_mismatch_log_message(code_seat_type: str, member_seat_type: Any, reason: str) -> str:
    """同一次拒绝写进操作日志的说明：读者是管理员，用第三人称（客户看到的是
    _seat_type_mismatch_detail，第二人称）。"""
    if reason == "unknown_member_seat_type":
        return (
            f"该成员当前是 {seat_type_label(member_seat_type)} 席位，TeamBoss 不认识这种席位，"
            "未续期，兑换码未使用。"
        )
    return (
        f"兑换码是 {seat_type_label(code_seat_type)} 码，该成员当前是 "
        f"{seat_type_label(member_seat_type)} 席位，未续期，兑换码未使用。"
    )


class _SeatTypeMismatch(Exception):
    """续期时码的席位类型和成员当前的席位类型对不上。在任何本地写入之前抛出，码不消耗。"""

    def __init__(
        self,
        *,
        team_id: str,
        user_id: str,
        code_seat_type: str,
        member_seat_type: Any,
        reason: str,
    ) -> None:
        super().__init__(reason)
        self.team_id = team_id
        self.user_id = user_id
        self.code_seat_type = normalize_seat_type(code_seat_type)
        self.member_seat_type = normalize_seat_type(member_seat_type)
        self.reason = reason
        # detail 给客户（HTTP 答复、公开兑换记录）；log_message 给管理员（操作日志）。
        self.detail = _seat_type_mismatch_detail(code_seat_type, member_seat_type, reason)
        self.log_message = _seat_type_mismatch_log_message(code_seat_type, member_seat_type, reason)


class _NoPremiumSeat(HTTPException):
    """Premium 码没有任何一个 Team 有空的已付 Premium 席位：拒绝，码不消耗。

    ``checked``：[(Team 名, 给管理员看的原因)]；``reasons``：[(team_id, 机器可读原因)]。
    """

    def __init__(self, checked: list[tuple[str, str]], reasons: list[tuple[str, str]]) -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=_NO_PREMIUM_SEAT_DETAIL)
        self.checked = checked
        self.reasons = reasons


_PUBLIC_USE_ACTION_ALIASES = {"renew_owner_rejected": "renew_permanent_rejected"}
_PUBLIC_USE_ERROR_ALIASES = {"owner_email": "permanent_membership"}


def _public_use_fields(item: dict[str, Any]) -> dict[str, Any]:
    """兑换记录对外展示前，把只有 Owner 才会出现的动作/原因码换成通用的那个。"""
    action = item.get("action")
    if action in _PUBLIC_USE_ACTION_ALIASES:
        item["action"] = _PUBLIC_USE_ACTION_ALIASES[action]
    error = item.get("error_message")
    if error in _PUBLIC_USE_ERROR_ALIASES:
        item["error_message"] = _PUBLIC_USE_ERROR_ALIASES[error]
    return item


async def _check_rate_limit(request: Request, limiter: _RateLimiter) -> None:
    """检查速率限制，超限时抛出 429 异常。IPv6 按 /64 计数。"""
    client_ip = get_client_ip(request)
    if not limiter.is_allowed(rate_limit_key(client_ip)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
        )


class GenerateAccessTokenRequest(BaseModel):
    grant_expires_in: str = Field(..., description="成员加入/续期时长，如 7d/30d/360d/never")
    token_ttl: str = Field("7d", description="token 自身有效期，如 1d/7d/never")
    max_uses: int = Field(1, ge=1, le=1, description="固定为 1：token 一经兑换即失效")
    note: Optional[str] = None
    # default = ChatGPT 码，prolite = Premium 码（只进有已付 Premium 空位的 Team）。
    seat_type: CodeSeatTypeLiteral = DEFAULT_SEAT_TYPE


class AccessTokenResponse(BaseModel):
    id: int
    token: str
    token_prefix: str
    grant_expires_in: str
    token_expires_at: Optional[str]
    max_uses: int
    used_count: int
    note: Optional[str]
    seat_type: str
    created_at: str


class AccessTokenListItem(BaseModel):
    id: int
    token_prefix: str
    grant_expires_in: str
    token_expires_at: Optional[str]
    max_uses: int
    used_count: int
    note: Optional[str]
    seat_type: str = DEFAULT_SEAT_TYPE
    disabled: bool
    created_at: str
    last_used_at: Optional[str]


class RedeemAccessTokenRequest(BaseModel):
    email: str
    token: str
    # 一人多 Team 时由前端回传用户选中的 Team；服务端会重新核对这个 Team 仍然在
    # 该邮箱的成员列表里，不信任前端给的值。这个值会进兑换审计行、并被
    # 后续查询当成"这张码落在哪个队"的定位键，所以先卡长度：兑换接口匿名可调，
    # 不能让任意长度的字符串落库再被原样读回。
    team_id: Optional[str] = Field(default=None, max_length=128)


class RedeemTeamChoice(BaseModel):
    team_id: str
    team_name: Optional[str] = None
    status: Literal["joined", "pending"]
    expires_at: Optional[str] = None
    # 这个队能不能续：Owner 与永久成员点了必然 409，前端据此禁用按钮。
    renewable: bool = True
    # 为兼容响应结构保留，恒为 False（公开响应不区分 Owner，见 _build_team_choices）。
    is_owner: bool = False
    # dated=有到期时间；permanent=本地记录为永久（续期会被拒）；
    # unmanaged=本地没有记录（续期会新建一条到期即踢的记录）。
    expiry_state: Literal["dated", "permanent", "unmanaged"] = "dated"
    blocked_reason: Optional[str] = None


class RedeemAccessTokenResponse(BaseModel):
    status: Literal["ok", "pending_confirmation", "team_selection_required"]
    action: Optional[Literal["invited", "renewed_member", "renewed_invite"]] = None
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    email: str
    expires_at: Optional[str]
    message: str
    # 仅 team_selection_required 时非空：让用户点一个 Team 再重新提交。
    choices: list[RedeemTeamChoice] = Field(default_factory=list)


class ResolvePendingConfirmationRequest(BaseModel):
    # "success"：管理员已核实原 Team 确实有这个成员/邀请，兑换按面额补齐期限，码保持已用。
    # "released"：管理员已核实原 Team 里既没有成员也没有邀请，把码退回未使用。
    outcome: Literal["success", "released"]
    note: Optional[str] = None


class QueryMembershipRequest(BaseModel):
    email: str
    # 兑换历史属于隐私数据：只返回出示的这张码在这个邮箱名下的使用记录。
    # 不传（或传错）时响应结构不变，只是 redemption_history 为空列表。
    token: Optional[str] = None


class QuerySelfServiceRequest(BaseModel):
    query: str = Field(..., description="邮箱或 token")
    token: Optional[str] = None


class RedemptionHistoryItem(BaseModel):
    action: str
    result: str
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    token_prefix: Optional[str] = None
    grant_expires_in: Optional[str] = None
    expires_at: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str


class MembershipTeamEntry(BaseModel):
    status: Literal["joined", "pending"]
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    expires_at: Optional[str] = None
    is_owner: bool = False
    # 到期时间为空时的真实含义，见 _public_expiry_state；缺省 None = 老数据，不得当永久。
    expiry_state: Optional[Literal["dated", "permanent", "external", "unrecorded"]] = None
    cache_updated_at: Optional[str] = None


class QueryMembershipResponse(BaseModel):
    status: Literal["joined", "pending", "absent"]
    email: str
    # 顶层 team_id/team_name/expires_at 保留为第一个命中的 Team（向后兼容）；
    # 一人多 Team 时的完整列表在 memberships 里。
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    expires_at: Optional[str] = None
    is_owner: bool = False
    message: str
    memberships: list[MembershipTeamEntry] = Field(default_factory=list)
    redemption_history: list[RedemptionHistoryItem] = Field(default_factory=list)
    cache_updated_at: Optional[str] = None  # 缓存更新时间，None 表示数据不来自缓存


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _duration_or_400(raw: str, *, allow_never: bool) -> str:
    try:
        return normalize_duration(raw, allow_never=allow_never)
    except DurationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return "atm_" + secrets.token_urlsafe(24)


def _normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱格式无效")
    return email



async def _set_token_use_phase(
    token_use_id: int,
    action: str,
    *,
    team_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    # 钉到某个 Team 时，这个 Team 必须还在：delete_team 从检查未结兑换到提交一直
    # 握着写锁，所以要么删除先拿到锁并答 409（兑换已钉在那里），要么这条更新先
    # 提交、删除看见它；删除先提交则这里匹配 0 行，下面照常抛错、退码，不会向
    # 一个已删除的 Team 发邀请。
    pin_guard = "AND EXISTS (SELECT 1 FROM teams WHERE id = ?)" if team_id is not None else ""
    params = (action, team_id, user_id, token_use_id) + ((team_id,) if team_id is not None else ())
    async with get_db() as db:
        cursor = await db.execute(
            f"""UPDATE access_token_uses
               SET action = ?, team_id = ?, user_id = ?
               WHERE id = ? AND result = 'pending' {pin_guard}""",
            params,
        )
        await db.commit()
        if cursor.rowcount != 1:
            raise RuntimeError(f"redemption attempt is no longer pending: {token_use_id}")


async def _lock_uncertain_with_barrier(
    token_use_id: int,
    *,
    team_id: str,
    email: str,
    error_message: str,
    reason: str,
) -> bool:
    """在同一个写事务里把兑换从 pending 锁成 uncertain，并立 kind='barrier' 的巡逻屏障。

    两步必须同生同灭。分两个事务写时，这笔兑换在中间那一刻已经是 uncertain、
    别的流程看得见：管理员收尾或对账任务若恰好在这时把它结清，结清时撤屏障是空操作，
    随后才写进来的屏障就永远停在 resolved=0——巡逻和自动踢人从此永远跳过这个人。
    同一事务里，结清要么发生在前（行已不是 pending，什么都不写），要么发生在后
    （屏障已在，结清会把它一起撤掉）。

    行已不是 pending 时什么都不写，返回 False；两步都提交才返回 True；任何一步失败
    都整体回滚并抛出，兑换保持 pending，由 ``reconcile_pending_redemptions`` 稍后重试。

    屏障行经 ``insert_pending_invite_reconciliation_row`` 写在本事务里：user_id 为空、
    expires_at 为 NULL、source='self_service'、kind='barrier'。时长结算只走对账的累加语义。
    """
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                """UPDATE access_token_uses
                   SET action = 'invite_pending', team_id = ?, result = 'uncertain',
                       error_message = ?
                   WHERE id = ? AND result = 'pending'""",
                (team_id, error_message, token_use_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await insert_pending_invite_reconciliation_row(
                db,
                team_id,
                "",
                email,
                None,
                "self_service",
                reason,
                token_use_id=token_use_id,
                kind="barrier",
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return True


async def _get_redemption_history(
    email: str, *, token_id: int, limit: int = 20
) -> list[dict[str, Any]]:
    """这张码（``token_id``）在这个邮箱名下的使用记录。

    必须按码限定：只凭"出示过一张码"就返回邮箱名下的全部记录，等于任何持有一张
    未用码的人都能读别人的消费流水——拿自己的码对别人的邮箱发起一次必然失败的
    兑换，就能造出"这张码用在了这个邮箱上"的记录（见 _history_proof_accepted）。
    """
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT
                   atu.action,
                   atu.result,
                   atu.team_id,
                   t.name AS team_name,
                   at.token_prefix,
                   at.grant_expires_in,
                   atu.expires_at,
                   atu.error_message,
                   atu.created_at
               FROM access_token_uses atu
               LEFT JOIN access_tokens at ON at.id = atu.token_id
               LEFT JOIN teams t ON t.id = atu.team_id
               WHERE lower(atu.email) = ? AND atu.token_id = ?
               ORDER BY atu.created_at DESC
               LIMIT ?""",
            (email.lower(), token_id, limit),
        )
        rows = await cursor.fetchall()
    # 这份历史会经匿名自助查询返回，error_message 里可能留有上游原始报错，先抹掉
    # 其中的 token / cookie 值。
    history: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("error_message"):
            item["error_message"] = mask_secrets(str(item["error_message"]))
        history.append(item)
    return history


async def _history_proof_accepted(email: str, raw_token: Optional[str]) -> bool:
    """出示的兑换码在这个邮箱名下是否有使用记录。

    ``/status`` 和 ``/query`` 是匿名接口：只给一个邮箱地址就能拿到该邮箱最近的
    兑换记录（动作、结果、Team、码前缀、面额、到期时间、报错原文、时间戳），等于
    任何人猜中邮箱就能看别人的消费流水。成员身份和到期时间照旧公开（用户要靠它
    自查），历史则要求出示凭据：这张码必须存在，且这张码的使用记录就落在这个邮箱
    名下。

    这只说明"这张码碰过这个邮箱"，不说明持码人就是邮箱主人：任何人都能拿自己
    未用的码对别人的邮箱发起一次失败的兑换。所以通过之后也只能看这张码自己的
    记录（_get_redemption_history 按 token_id 限定）。
    """
    token = (raw_token or "").strip()
    if not token:
        return False
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return False
    token_row = await _get_token_by_raw(token)
    if not token_row:
        return False
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT 1 FROM access_token_uses
               WHERE token_id = ? AND lower(email) = ?
               LIMIT 1""",
            (int(token_row["id"]), normalized_email),
        )
        return await cursor.fetchone() is not None


async def _get_token_by_raw(raw_token: str) -> Optional[dict[str, Any]]:
    token = (raw_token or "").strip()
    if not token:
        return None
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM access_tokens WHERE token_hash = ?",
            (_hash_token(token),),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _get_latest_token_use(token_id: int) -> Optional[dict[str, Any]]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT atu.*, t.name AS team_name
               FROM access_token_uses atu
               LEFT JOIN teams t ON t.id = atu.team_id
               WHERE atu.token_id = ?
               ORDER BY atu.created_at DESC
               LIMIT 1""",
            (token_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


class _TokenConsumption:
    """标记这次兑换有没有跨过"不可回滚"的那个点。

    ``confirm()`` 必须是同步的、并且在**外部副作用刚刚生效的那一行**就调用：
    OpenAI 邀请一旦成功，人就已经在 Team 里了，这次兑换码的消耗就已经成立，之后
    的落库/日志/缓存/通知再怎么炸也不能把码退回去（退回 = 同一张码能再用一次）。
    """

    __slots__ = ("confirmed",)

    def __init__(self) -> None:
        self.confirmed = False

    def confirm(self) -> None:
        self.confirmed = True

    def revert_for_rejected(self) -> None:
        """撤回消耗标记。**只有上游明确拒绝时可以调用。**

        ``confirm()`` 在发出远端写操作之前就打上，因为请求一旦发出就无法证明
        远端没有副作用。而 ``rejected`` 是上游给出的明确否定答复——这一次调用
        没有创建任何成员或邀请，码理应还给用户（接着换下一个 Team 重试）。
        超时、断网、取消都不是 ``rejected``，绝不能走这里。
        """
        self.confirmed = False


async def _reserve_token_use(
    token_id: int,
    email: str,
    nominal_expires_at: Optional[str],
) -> int:
    """Atomically occupy the token's single use slot.

    This is the concurrency guard: the conditional ``UPDATE ... WHERE
    used_count < 1`` only lets one concurrent redeemer win the row, so a
    second submission of the same token gets rowcount=0 and a 409 instead of
    racing into a duplicate invite. The reservation is provisional — call
    ``_fail_and_release_token_use`` if the redemption ultimately fails so the token
    can be retried, or leave it as-is once the redemption truly succeeds.
    """
    now = utc_now().isoformat()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """UPDATE access_tokens
               SET used_count = used_count + 1, last_used_at = ?
               WHERE id = ? AND disabled = 0 AND used_count < 1""",
            (now, token_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="兑换码已用完")
        cursor = await db.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                error_message, created_at)
               VALUES (?, ?, 'lookup_pending', NULL, NULL, ?, 'pending', NULL, ?)""",
            (token_id, email, nominal_expires_at, now),
        )
        token_use_id = int(cursor.lastrowid)
        try:
            await db.execute(
                """INSERT INTO redemption_email_claims (email, token_use_id, created_at)
                   VALUES (?, ?, ?)""",
                (email, token_use_id, now),
            )
        except sqlite3.IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该邮箱有一笔兑换正在处理，请稍后用原兑换码查询",
            ) from exc
        await db.commit()
        return token_use_id


async def _fail_and_release_token_use(
    token_use_id: int,
    *,
    action: str,
    error_message: Optional[str],
    team_id: Optional[str] = None,
    user_id: Optional[str] = None,
    result_value: str = "failed",
    clear_expires_at: bool = False,
) -> bool:
    """Undo a provisional reservation from ``_reserve_token_use``.

    只有仍为 ``pending`` 的同一次 attempt 才能释放；``uncertain`` 和
    ``success`` 都拒绝释放，避免结果不确定时双花。

    ``result_value`` 只允许 ``failed`` 或 ``notice``。``notice`` 用于"这次调用
    没出错、只是还需要用户补一个选择"的中断（多 Team 选择提示）：码同样退回，但
    这行不是失败，公开兑换历史里不能给用户挂一条红色失败记录。它绝不能写成
    ``pending``/``uncertain``——那两个值代表"结果未定、码仍被锁"，会让查询接口
    把一张已退回的码显示成"结果确认中"。

    ``clear_expires_at`` 把这行的名义到期抹掉。名义到期是"这张码的面额"，只有
    真正授出去时才有意义；提示行留着它会在历史里显示一个从未发生过的到期时间。

    这里不撤 pending_invite_reconciliations：pending 的兑换名下没有行——屏障和
    pending→uncertain 同一事务写下，'extend' 行只在远端邀请已确认之后才写，那时码
    已消耗、请求路径不会再退码，对账的本地回滚也不碰 invite_pending。真有行时它是
    "远端已有席位"的唯一记录，该挡住的是退码，不能随退码一起抹掉。
    """
    if result_value not in {"failed", "notice"}:
        raise ValueError(f"unsupported result_value: {result_value}")

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT token_id, result FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        )
        row = await cursor.fetchone()
        if not row or row["result"] != "pending":
            await db.rollback()
            return False

        await db.execute(
            f"""UPDATE access_token_uses
               SET action = ?, team_id = ?, user_id = ?, result = '{result_value}',
                   error_message = ?
                   {', expires_at = NULL' if clear_expires_at else ''}
               WHERE id = ? AND result = 'pending'""",
            (action, team_id, user_id, error_message, token_use_id),
        )
        await db.execute(
            "DELETE FROM redemption_email_claims WHERE token_use_id = ?",
            (token_use_id,),
        )
        await db.execute(
            """UPDATE access_tokens
               SET used_count = 0, last_used_at = NULL
               WHERE id = ? AND used_count = 1""",
            (row["token_id"],),
        )
        await db.commit()
        return True


async def _load_token(raw_token: str) -> dict[str, Any]:
    token = (raw_token or "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="兑换码不能为空")

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM access_tokens WHERE token_hash = ?",
            (_hash_token(token),),
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="兑换码无效")

    token_row = dict(row)
    if token_row.get("disabled"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="兑换码已禁用")

    expires_at = parse_optional_datetime(token_row.get("token_expires_at"))
    if expires_at and expires_at <= utc_now():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="兑换码已过期")

    if int(token_row.get("used_count") or 0) >= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="兑换码已用完")

    return token_row


async def _find_all_memberships(
    email: str, teams: list[dict[str, Any]], use_cache_only: bool = False
) -> list[dict[str, Any]]:
    """
    查找邮箱在全部 Team 中的成员身份（已加入或待接受邀请），每个 Team 至多一条，
    同一 Team 内已加入优先于待接受。

    当 use_cache_only=True 时，仅读本地缓存，不触发实时 OpenAI 请求（用于公开查询路径）。
    当 use_cache_only=False 时，实时拉取成员列表（用于兑换/邀请等写操作）。
    """
    hits: list[dict[str, Any]] = []
    for team in teams:
        if use_cache_only:
            # 只读缓存，不触发实时请求
            snapshot = await get_cached_members(team["id"])
            if not snapshot:
                # 缓存不存在，跳过该 team
                continue
        else:
            # 实时拉取并缓存
            _proxy_url = await _get_proxy_url(team.get("proxy_id"))
            client = ChatGPTClient(team["access_token"], team["id"], team["device_id"], proxy_url=_proxy_url)
            try:
                snapshot = await fetch_and_cache_members(team["id"], client)
            except Exception as exc:
                await log_operation(
                    team["id"],
                    "self_service_lookup",
                    email,
                    None,
                    "failed",
                    str(exc),
                    "manual",
                )
                # 写操作前必须确认这个邮箱不在任何 Team。只要有一个 Team
                # 拉取失败，就不能假装“不存在”后把人邀请到另一个 Team。
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="暂时无法确认全部 Team 的成员状态，请稍后重试",
                ) from exc

        team_hit = None
        for member in snapshot.get("members", []):
            if (member.get("email") or "").lower() == email:
                team_hit = {
                    "kind": "member",
                    "team": team,
                    "user_id": member.get("id") or "",
                    "is_owner": bool(member.get("is_owner")),
                    "expires_at": member.get("expires_at"),
                    "source": member.get("source"),
                    # 续期按它核对码的席位类型（见 _renewal_seat_type_block），缺失 = ChatGPT。
                    "seat_type": normalize_seat_type(member.get("seat_type")),
                    "cache_updated_at": snapshot.get("updated_at"),
                }
                break

        if team_hit is None:
            for invite in snapshot.get("pending_invites", []):
                if (invite.get("email") or "").lower() == email:
                    team_hit = {
                        "kind": "invite",
                        "team": team,
                        "user_id": "",
                        "is_owner": False,
                        "expires_at": invite.get("expires_at"),
                        "source": invite.get("source"),
                        # 待接受邀请接受后就是这个席位类型，续期按它核对。
                        "seat_type": normalize_seat_type(invite.get("seat_type")),
                        "cache_updated_at": snapshot.get("updated_at"),
                    }
                    break

        if team_hit is not None:
            hits.append(team_hit)
    return hits


# 本次兑换无法实时核对成员名单的 Team：teams 表里还在，但不是 active（load_active_teams
# 不返回它），或者登录已被上游拒绝（每个实时请求都 401，_redeem_valid_token 把它从
# 实时扫描里拿掉，见 team_login_rejected）。必须和 _redeem_valid_token 里实时扫描的
# 那组 Team 恰好互补。
_UNAVAILABLE_TEAM_SQL = (
    "(COALESCE(t.status, '') != 'active' OR COALESCE(t.auth_state, '') = 'rejected')"
)


async def _memberships_in_unavailable_teams(email: str) -> list[dict[str, Any]]:
    """本地记录显示这个邮箱身在其中、但本次兑换无法实时核对的 Team，按 team_id 排序。

    兑换只实时扫描 active 且登录正常的 Team。邮箱若身在一个非 active（例如
    token_expired）或登录被拒的 Team 里，实时扫描看不到他：没指定 Team 时会给他
    在别的队另开一个席位，旧队的到期照样在走；他若同时在一个可实时扫描的 Team，
    那个队会在没问过他的情况下被续期。这两种都是替用户选了 Team。调用方据此：
    要发新邀请时拒绝；人在可用 Team 里但没选 Team 时把这些队列进选择提示（不可续）。

    算作"在里面"的本地迹象（都只算 teams 表里仍存在的 Team）：
    * member_expiry 中 kicked=0 的记录，任何 source——包括 ``detected``：detected
      不是授权，但它说明这个人确实在那个 Team 里。
    * 该 Team 最近一次成员缓存里有这个邮箱（已加入或待接受邀请）。

    Team 已从 teams 表删除的记录不算：删除 Team 时 member_expiry 刻意保留作审计
    （见 routes/teams.py delete_team），那个 Team 已不受本系统管理，也没有任何
    可续的对象；拿它挡兑换只会让这些人永远无法再兑换。team_id 为空的老记录同理。

    每项：``team_id``、``team_name``、``kind``（member：有到期记录或缓存里已加入；
    invite：只在缓存的待接受邀请里）、``expires_at``（最新一条 kicked=0 记录的到期，
    取法同 get_active_expiry_state，没有记录时为 None）。
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return []
    found: dict[str, dict[str, Any]] = {}
    async with get_db() as db:
        cursor = await db.execute(
            f"""SELECT me.team_id, t.name AS team_name, me.expires_at
                FROM member_expiry me
                JOIN teams t ON t.id = me.team_id
                WHERE me.kicked = 0 AND lower(me.email) = ? AND {_UNAVAILABLE_TEAM_SQL}
                ORDER BY COALESCE(me.created_at, me.first_seen_at) DESC, me.id DESC""",
            (normalized,),
        )
        for row in await cursor.fetchall():
            # 按最新在前排序，每个 Team 只留第一条。
            found.setdefault(
                row["team_id"],
                {
                    "team_id": row["team_id"],
                    "team_name": row["team_name"],
                    "kind": "member",
                    "expires_at": row["expires_at"],
                },
            )
        cursor = await db.execute(
            f"""SELECT mc.team_id, t.name AS team_name, mc.members_json, mc.pending_json
                FROM member_cache mc
                JOIN teams t ON t.id = mc.team_id
                WHERE {_UNAVAILABLE_TEAM_SQL}"""
        )
        cache_rows = await cursor.fetchall()
    for row in cache_rows:
        if row["team_id"] in found:
            continue
        try:
            members = json.loads(row["members_json"] or "[]") or []
            pending = json.loads(row["pending_json"] or "[]") or []
        except (TypeError, ValueError):
            logger.warning("unreadable member cache for team=%s", row["team_id"])
            continue
        if _snapshot_contains_email({"members": members}, normalized):
            kind = "member"
        elif _snapshot_contains_email({"pending_invites": pending}, normalized):
            kind = "invite"
        else:
            continue
        found[row["team_id"]] = {
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "kind": kind,
            "expires_at": None,
        }
    return [found[team_id] for team_id in sorted(found)]


async def _unavailable_team_choices(
    email: str, unavailable: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """选择提示里的不可用 Team：照常列出（用户要认得出自己在哪些队），一律不可续。

    选了它服务端也只会按"所选 Team 当前不可用"退码，这里先把按钮禁掉。
    """
    choices: list[dict[str, Any]] = []
    for hit in unavailable:
        expiry_state = await get_active_expiry_state(hit["team_id"], "", email)
        choices.append(
            {
                "team_id": hit["team_id"],
                "team_name": hit.get("team_name"),
                "status": "joined" if hit["kind"] == "member" else "pending",
                "expires_at": hit.get("expires_at") if expiry_state == "dated" else None,
                "is_owner": False,
                "expiry_state": expiry_state,
                "renewable": False,
                "blocked_reason": "team_unavailable",
            }
        )
    return choices


async def _log_unavailable_team_refusal(email: str, team_ids: list[str]) -> None:
    try:
        await log_operation(
            None,
            "self_service_redeem",
            email,
            "reason=unavailable_team_membership, teams=" + ",".join(team_ids),
            "failed",
        )
    except Exception:
        logger.exception("failed to log unavailable-team refusal email=%s", email)


async def _find_existing_membership(
    email: str, teams: list[dict[str, Any]], use_cache_only: bool = False
) -> Optional[dict[str, Any]]:
    """单命中版包装：取第一个 Team 的身份。多 Team 场景要用 _find_all_memberships。"""
    hits = await _find_all_memberships(email, teams, use_cache_only=use_cache_only)
    return hits[0] if hits else None


def _token_status(token_row: dict[str, Any]) -> str:
    if int(token_row.get("used_count") or 0) >= 1:
        return "used"
    if token_row.get("disabled"):
        return "disabled"
    expires_at = parse_optional_datetime(token_row.get("token_expires_at"))
    if expires_at and expires_at <= utc_now():
        return "expired"
    return "unused"


def _email_status_label(status_value: str) -> str:
    return {
        "pending": "待接受",
        "joined": "已加入",
        "expired_removed": "已过期移出",
        "absent": "未找到",
        "unknown": "未知",
    }.get(status_value, status_value)


async def _resolve_email_status(
    email: str,
    expires_at: Optional[str],
    *,
    team_id: Optional[str] = None,
) -> dict[str, Any]:
    """这张兑换码对应邮箱的当前状态。

    ``team_id`` 是这次兑换实际落到的 Team。一人多 Team 时必须按它限定范围，否则
    跨 Team 取第一命中会把别的队的状态/到期时间贴到这张码上。
    """
    if not email:
        return {"status": "unknown", "status_label": _email_status_label("unknown")}

    scoped_team_id = (team_id or "").strip()
    # 限定的 Team 已经不在活跃列表里（暂停/下架/删除）时，我们没有任何数据源能
    # 判断这个人还在不在——既不能跨队去别的队取答案（那正是加 scope 要挡的），
    # 也不能因此说"未找到"：那等于告诉一个正常成员他的会员不存在。
    scoped_team_missing = False
    try:
        teams = await load_active_teams()
        if scoped_team_id:
            teams = [team for team in teams if team["id"] == scoped_team_id]
            scoped_team_missing = not teams
        # 公开查询路径，只读缓存不触发实时请求
        existing = await _find_existing_membership(email, teams, use_cache_only=True)
    except Exception:
        existing = None

    if existing and existing["kind"] == "invite":
        return {
            "status": "pending",
            "status_label": _email_status_label("pending"),
            "team_id": existing["team"]["id"],
            "team_name": existing["team"]["name"],
            "expires_at": existing.get("expires_at"),
            "cache_updated_at": existing.get("cache_updated_at"),
        }
    if existing and existing["kind"] == "member":
        return {
            "status": "joined",
            "status_label": _email_status_label("joined"),
            "team_id": existing["team"]["id"],
            "team_name": existing["team"]["name"],
            "expires_at": existing.get("expires_at"),
            "cache_updated_at": existing.get("cache_updated_at"),
        }

    async with get_db() as db:
        cursor = await db.execute(
            """SELECT me.*, t.name AS team_name
               FROM member_expiry me
               LEFT JOIN teams t ON t.id = me.team_id
               WHERE lower(me.email) = ? AND me.kicked = 1
                 AND (? = '' OR me.team_id = ? OR me.team_id IS NULL)
               ORDER BY me.kicked_at DESC, me.created_at DESC
               LIMIT 1""",
            (email.lower(), scoped_team_id, scoped_team_id),
        )
        kicked_row = await cursor.fetchone()
    if kicked_row:
        kicked = dict(kicked_row)
        return {
            "status": "expired_removed",
            "status_label": _email_status_label("expired_removed"),
            "team_id": kicked.get("team_id"),
            "team_name": kicked.get("team_name"),
            "expires_at": kicked.get("expires_at") or expires_at,
            "kicked_at": kicked.get("kicked_at"),
            "cache_updated_at": None,  # 已踢出状态，不从缓存得来
        }

    if scoped_team_missing:
        return {
            "status": "unknown",
            "status_label": _email_status_label("unknown"),
            "team_id": scoped_team_id,
            "expires_at": expires_at,
            "cache_updated_at": None,
        }

    expires_dt = parse_optional_datetime(expires_at)
    status_value = "expired_removed" if expires_dt and expires_dt <= utc_now() else "absent"
    return {
        "status": status_value,
        "status_label": _email_status_label(status_value),
        "expires_at": expires_at,
        "cache_updated_at": None,  # 未找到状态，不从缓存得来
    }


async def _query_token(raw_token: str) -> dict[str, Any]:
    token_row = await _get_token_by_raw(raw_token)
    if not token_row:
        return {
            "query_type": "token",
            "token": {
                "token_status": "invalid",
                "token_status_label": "无效",
            },
            "usage": None,
        }

    latest_use = await _get_latest_token_use(int(token_row["id"]))
    status_value = _token_status(token_row)
    if latest_use and latest_use.get("result") in {"pending", "uncertain"}:
        status_value = "pending_confirmation"
    usage = None
    if latest_use:
        if latest_use.get("result") in {"success", "pending", "uncertain"}:
            email_status = await _resolve_email_status(
                latest_use.get("email") or "",
                latest_use.get("expires_at"),
                team_id=latest_use.get("team_id"),
            )
        else:
            # 这次尝试没用掉这张码（失败/提示后已退回），码和那个邮箱之间没有任何
            # 授权关系。邮箱是持码人自己填的，任何人都能拿未用的码对别人的邮箱试
            # 一次；这里若照样解析邮箱状态，就会把对方的移出记录（Team、到期、
            # 移出时间）交给持码人。只回这次尝试本身。
            email_status = {"status": "unknown", "status_label": _email_status_label("unknown")}
        usage = {
            "email": latest_use.get("email"),
            "email_status": email_status.get("status"),
            "email_status_label": email_status.get("status_label"),
            "team_id": latest_use.get("team_id") or email_status.get("team_id"),
            "team_name": latest_use.get("team_name") or email_status.get("team_name"),
            # 上游成员 id 只在这次兑换真的落到这个人身上时才给持码人。失败的尝试也可能
            # 记着它（例如 Owner/永久成员续期被拒时记下的是对方的 id），而邮箱是持码人
            # 自己填的，任何人都能拿未用的码对别人的邮箱试一次。
            "user_id": latest_use.get("user_id") if latest_use.get("result") == "success" else None,
            "action": _PUBLIC_USE_ACTION_ALIASES.get(latest_use.get("action"), latest_use.get("action")),
            "result": latest_use.get("result"),
            "error_message": (
                "结果确认中，兑换码已暂时锁定，请稍后查询"
                if latest_use.get("result") in {"pending", "uncertain"}
                # 匿名可读，抹掉上游报错里可能夹带的 token / cookie。
                else _PUBLIC_USE_ERROR_ALIASES.get(latest_use.get("error_message"))
                or mask_secrets(str(latest_use.get("error_message") or "")) or None
            ),
            "expires_at": latest_use.get("expires_at") or email_status.get("expires_at"),
            "kicked_at": email_status.get("kicked_at"),
            "used_at": latest_use.get("created_at"),
        }

    return {
        "query_type": "token",
        "token": {
            "id": token_row["id"],
            "token_prefix": token_row["token_prefix"],
            "token_status": status_value,
            "token_status_label": {
                "unused": "未使用",
                "used": "已使用",
                "pending_confirmation": "结果确认中",
                "expired": "已过期",
                "disabled": "已禁用",
            }.get(status_value, status_value),
            "grant_expires_in": token_row["grant_expires_in"],
            "seat_type": normalize_seat_type(token_row.get("seat_type")),
            "seat_type_label": seat_type_label(token_row.get("seat_type")),
            "token_expires_at": token_row.get("token_expires_at"),
            "max_uses": 1,
            "used_count": int(token_row.get("used_count") or 0),
            "created_at": token_row.get("created_at"),
            "last_used_at": token_row.get("last_used_at"),
        },
        "usage": usage,
    }


def _snapshot_contains_email(snapshot: dict[str, Any] | None, email: str) -> bool:
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


async def _chatgpt_available(client: ChatGPTClient, team_id: str, *, email: str = "") -> tuple[bool, str]:
    """锁内实时复查 ChatGPT 空位。读不到、或待接受邀请没拉全 = 没有空位（失败关闭）。"""
    try:
        capacity, subscription, seat_counts, _pending = await fetch_live_chatgpt_seat_capacity(client)
    except SeatCapacityFetchError as exc:
        return False, f"no_chatgpt_seat: capacity_unknown: {exc}"

    await update_capacity_cache(team_id, subscription, seat_counts)
    reserved = await reserved_default_seats(team_id, exclude_email=email)
    available_after_reservations = capacity.available - reserved

    if available_after_reservations <= 0:
        return (
            False,
            "no_chatgpt_seat: "
            f"active_chatgpt={capacity.active_chatgpt}/{capacity.seats_entitled}, "
            f"total_in_use={capacity.seats_in_use_total}, "
            f"codex={capacity.codex_count}, "
            f"pending_default={capacity.pending_default}, "
            f"reserved_default={reserved}",
        )
    return True, f"available={available_after_reservations}, active_chatgpt={capacity.active_chatgpt}"


async def _cached_premium_free(team_ids: list[str]) -> dict[str, int]:
    """缓存（teams.seat_capacity_json）里各 Team 的 Premium 空位，只用来预筛。

    缓存没有可信的 Premium 条目 = 0（见 billed_free_seats）。预筛只会少问几个 Team，
    真正放行要看锁内的实时复查（_premium_available）。
    """
    if not team_ids:
        return {}
    placeholders = ", ".join("?" for _ in team_ids)
    async with get_db() as db:
        cursor = await db.execute(
            f"SELECT id, seat_capacity_json FROM teams WHERE id IN ({placeholders})",
            tuple(team_ids),
        )
        rows = await cursor.fetchall()
    return {
        row["id"]: billed_free_seats(
            PREMIUM_SEAT_TYPE, entries=cached_seat_capacity(row["seat_capacity_json"])
        )
        for row in rows
    }


async def _premium_available(
    client: ChatGPTClient, team_id: str, *, email: str = ""
) -> tuple[bool, str, str]:
    """锁内实时复查 Premium 空位：min(seat_capacity.prolite.available，已付 − 在用 Premium)
    − 待接受的 Premium 及没带类型的邀请 − Premium 预留。
    读不到、或待接受邀请没拉全 = 没有空位（失败关闭）。

    返回 (有没有空位, 给日志的原因, 给管理员通知的原因)。超员策略在这里**不看**：
    兑换永远不超员，哪怕 Team 设成「超员自动」。
    """
    try:
        capacity, subscription, seat_counts, _pending = await fetch_live_seat_type_capacity(
            client, PREMIUM_SEAT_TYPE
        )
    except SeatCapacityFetchError as exc:
        return False, f"no_premium_seat: capacity_unknown: {exc}", "读不到席位容量，按已满处理"

    await update_capacity_cache(team_id, subscription, seat_counts)
    reserved = await reserved_seats(team_id, PREMIUM_SEAT_TYPE, exclude_email=email)
    free = capacity.available - reserved
    if free <= 0:
        paid = "?" if capacity.paid is None else capacity.paid
        in_use = "?" if capacity.in_use is None else capacity.in_use
        return (
            False,
            f"no_premium_seat: {capacity.describe()}, reserved={reserved}",
            f"已付 {paid}，在用 {in_use}，待接受 {capacity.pending}，预留 {reserved}，没有空位",
        )
    return True, f"premium_available={free}, {capacity.describe()}", ""


async def _invite_to_available_team(
    email: str,
    grant_duration: str,
    teams: list[dict[str, Any]],
    *,
    token_use_id: int,
    on_invite_confirmed: Optional[Callable[[], None]] = None,
    on_invite_rejected: Optional[Callable[[], None]] = None,
    seat_type: str = DEFAULT_SEAT_TYPE,
) -> dict[str, Any]:
    """给 email 在第一个有 ``seat_type`` 空位的 Team 上发邀请。

    ``seat_type``：码的席位类型。ChatGPT（default）照旧；Premium（prolite）先按缓存
    预筛，再在 Team 锁内实时复查已付 Premium 空位。两种都不看超员策略、绝不超员；
    Premium 一个 Team 都不合格时抛 ``_NoPremiumSeat``（码由调用方退回）。

    ``on_invite_confirmed``：在**向 OpenAI 发出这个不可幂等的写操作之前**同步调用
    一次，把这次兑换标记为已消耗。请求一旦发出就没有任何办法证明远端没有副作用，
    所以之后的超时、断网、落库失败、systemd 停服时的 CancelledError，一律不再退码
    ——异常兑换由管理员收尾，绝不能让同一张码有机会用第二次。

    ``on_invite_rejected``：仅在上游给出**明确否定答复**（``rejected``）时调用，
    把上面的标记撤回。那一次调用确实没有创建任何成员或邀请，码要还给用户，函数
    接着换下一个 Team 重试。
    """
    seat_type = normalize_seat_type(seat_type)
    if seat_type not in CODE_SEAT_TYPES:
        raise ValueError(f"redemption cannot invite into seat type {seat_type!r}")
    premium = seat_type == PREMIUM_SEAT_TYPE
    last_error: Optional[str] = None
    # 仅 Premium：每个没放行的 Team 的原因，给拒绝通知和日志用。
    premium_checked: list[tuple[str, str]] = []
    premium_reasons: list[tuple[str, str]] = []
    cached_premium_free = (
        await _cached_premium_free([team["id"] for team in teams]) if premium else {}
    )

    for team in teams:
        if premium and cached_premium_free.get(team["id"], 0) <= 0:
            premium_checked.append((team.get("name") or team["id"], "缓存显示没有空的 Premium 席位"))
            premium_reasons.append((team["id"], "no_premium_seat: cached"))
            continue
        async with team_invite_lock(team["id"]):
            _proxy_url_inv = await _get_proxy_url(team.get("proxy_id"))
            client = ChatGPTClient(team["access_token"], team["id"], team["device_id"], proxy_url=_proxy_url_inv)
            if premium:
                ok, reason, notice_reason = await _premium_available(client, team["id"], email=email)
            else:
                ok, reason = await _chatgpt_available(client, team["id"], email=email)
            if not ok:
                last_error = reason
                if premium:
                    premium_checked.append((team.get("name") or team["id"], notice_reason))
                    premium_reasons.append((team["id"], reason))
                continue

            # 进入不可幂等的远端写操作前先持久化目标 Team。进程即使在请求
            # 返回前退出，恢复任务也知道应该去哪个 Team 对账。
            await _set_token_use_phase(
                token_use_id,
                "invite_pending",
                team_id=team["id"],
            )

            # 跨过这一行就算已消耗：下面这个请求一旦发出，就再也无法证明 OpenAI
            # 侧没有副作用。明确被拒时下面会撤回。
            if on_invite_confirmed is not None:
                on_invite_confirmed()

            mutation_task = asyncio.create_task(
                run_chatgpt_call(client.invite_member, email, seat_type)
            )
            try:
                result = await asyncio.shield(mutation_task)
            except asyncio.CancelledError:
                # executor 中的 requests 请求不会随协程取消而停止。这里等它真正
                # 结束并继续完成持久化，避免停服恰好把结果留在半空。
                result = await mutation_task

            mutation_status = result.get("_mutation_status")
            if mutation_status is None:
                # 兼容测试桩和旧版 client：无 error 即已确认，带 error 则按
                # 明确失败处理；生产 client 会始终给出精确分类。
                mutation_status = "rejected" if "error" in result else "confirmed"

            if mutation_status == "uncertain":
                # 超时后先立即读取原 Team：能看见成员/邀请就可以确认成功；
                # 看不见仍不能证明失败（远端可能正在最终一致），继续锁码等待。
                try:
                    snapshot = await fetch_and_cache_members(team["id"], client)
                except Exception:
                    snapshot = None
                if _snapshot_contains_email(snapshot, email):
                    mutation_status = "confirmed"

            if mutation_status == "uncertain":
                error = str(result.get("error") or "OpenAI invite result is uncertain")
                # 结果未定的邀请也必须在 pending_invite_reconciliations 里立一道屏障，
                # 否则同步任务会把远端那个对象当成"陌生邀请"，巡逻随后撤销我们自己刚
                # 发出去的邀请。锁 uncertain 和立屏障在同一个事务里（见
                # _lock_uncertain_with_barrier），中间不留能被结清的空当。
                try:
                    locked = await _lock_uncertain_with_barrier(
                        token_use_id,
                        team_id=team["id"],
                        email=email,
                        error_message=error,
                        reason=error,
                    )
                except Exception:
                    # 两步一起回滚：兑换仍是 invite_pending/pending，码照旧锁在这个
                    # Team 上。请求结束后对账任务会按"被中断的邀请"把它锁成 uncertain
                    # 并立屏障（_lock_interrupted_invite），对用户的答复不变。
                    logger.exception(
                        "failed to lock uncertain invite with its patrol barrier "
                        "team=%s token_use_id=%s",
                        team["id"],
                        token_use_id,
                    )
                else:
                    if not locked:
                        raise RuntimeError(
                            f"failed to lock uncertain redemption: {token_use_id}"
                        )
                try:
                    await log_operation(
                        team["id"],
                        "self_service_invite",
                        email,
                        f"seat_type={seat_type}",
                        "uncertain",
                        error,
                        "manual",
                    )
                except Exception:
                    logger.exception(
                        "failed to log uncertain invite team=%s email=%s",
                        team["id"],
                        email,
                    )
                if premium:
                    # 这个 Premium 邀请可能已经生效、只是名单里还看不见：先占住这个
                    # 空位，免得下一次兑换按「还有空位」再发一个、触发自动加购。
                    try:
                        await reserve_seat(team["id"], email, PREMIUM_SEAT_TYPE)
                    except Exception:
                        logger.exception(
                            "failed to reserve uncertain Premium invite seat team=%s",
                            team["id"],
                        )
                return {
                    "status": "pending_confirmation",
                    "team": team,
                    "user_id": "",
                    "expires_at": None,
                }

            if mutation_status == "rejected":
                # 上游明确拒绝：这一次调用没有任何远端副作用，把消耗标记撤回，
                # 换下一个 Team 重试；全部失败时 finally 才能把码退给用户。
                #
                # 判定只看 mutation_status，绝不能再补一个 `or "error" in result`：
                # ChatGPTClient._error() 给每一种结果（含 uncertain）都塞了 error 键，
                # 那个条件对所有不确定结果恒为真——上面那段"超时后重新拉快照、看到人
                # 就升级成 confirmed"的补救会被直接架空，一次超时但其实成功的邀请
                # 会被当成拒绝退码，然后同一张码去下一个 Team 再发一次（一码两座），
                # 或者把码整个退还而用户已经占着席位。2xx 响应体里恰好带 error 字段
                # 的情况同理：分类是 confirmed 就不是拒绝。
                # 上面 `mutation_status is None` 的兼容推断已经把"裸 error 字典"映射
                # 成 rejected，这里不需要第二层。
                if on_invite_rejected is not None:
                    on_invite_rejected()
                last_error = str(result.get("error") or "OpenAI rejected the invite")
                if premium:
                    premium_checked.append((team.get("name") or team["id"], "上游拒绝了邀请"))
                    premium_reasons.append((team["id"], f"rejected: {last_error}"))
                try:
                    await log_operation(
                        team["id"],
                        "self_service_invite",
                        email,
                        f"seat_type={seat_type}",
                        "failed",
                        last_error,
                        "manual",
                    )
                except Exception:
                    logger.exception(
                        "failed to log rejected invite team=%s email=%s",
                        team["id"],
                        email,
                    )
                continue

            # ↓↓↓ 从这一行往后，OpenAI 侧的邀请已经生效且无法回滚 ↓↓↓
            # 消耗标记在发出请求之前就已经打上，这里不需要再补。

            # OpenAI 邀请已在上面成功，本地记录必须最终落地，见
            # record_confirmed_invite_extension 注释；它不会抛异常，所以下面这段
            # 属于"邀请已经确定成功"之后的收尾。
            expires_iso = await record_confirmed_invite_extension(
                team["id"],
                "",
                email,
                grant_duration,
                source="self_service",
                token_use_id=token_use_id,
                token_action="invited",
            )
            try:
                await log_operation(
                    team["id"],
                    "self_service_invite",
                    email,
                    f"seat_type={seat_type}, expires_at={expires_iso}",
                    "success",
                    None,
                    "manual",
                )
            except Exception as exc:
                # 邀请和本地成员记录都已经成功；这里只是审计日志写入抖动，绝不能让它
                # 把已经确定成功的邀请变成上层的失败分支（那会导致 token 被误退回）。
                logger.warning(
                    "self_service_invite: log_operation failed after successful invite "
                    "team=%s email=%s: %s", team["id"], email, exc,
                )

            snapshot = None
            try:
                snapshot = await fetch_and_cache_members(team["id"], client)
                await add_member_watch(team["id"], "invite", target_email=email)
            except Exception as exc:
                try:
                    await log_operation(
                        team["id"],
                        "self_service_cache_refresh",
                        email,
                        None,
                        "failed",
                        str(exc),
                        "manual",
                    )
                except Exception:
                    logger.exception(
                        "failed to log cache refresh failure team=%s email=%s",
                        team["id"],
                        email,
                    )
            # Premium：不管刷新后的名单里看不看得见这个邀请，都占住一个 Premium 空位
            # （15 分钟）。待接受邀请要是没带 seat_type、或上游的 available 还没扣掉它，
            # 只靠名单就会把这个空位再卖一次、触发自动加购；多占一会儿最多少卖一单。
            # ChatGPT 照旧：名单里还看不见时才占。
            if premium or not _snapshot_contains_email(snapshot, email):
                try:
                    if premium:
                        await reserve_seat(team["id"], email, PREMIUM_SEAT_TYPE)
                    else:
                        await reserve_default_seat(team["id"], email)
                except Exception:
                    logger.exception(
                        "failed to reserve confirmed invite seat team=%s email=%s",
                        team["id"],
                        email,
                    )

            try:
                await notify_member_event(
                    "自助拉人",
                    team["id"],
                    email=email,
                    source="self_service",
                    detail=(
                        f"{seat_type_label(seat_type)} 席位，expires_at={expires_iso}"
                        if premium
                        else f"expires_at={expires_iso}"
                    ),
                )
            except Exception:
                logger.exception(
                    "failed to notify successful invite team=%s email=%s",
                    team["id"],
                    email,
                )

            return {
                "status": "ok",
                "action": "invited",
                "team": team,
                "user_id": "",
                "expires_at": expires_iso,
            }

    if premium:
        # 日志、退码和给管理员的通知由调用方在退码之后做（_report_no_premium_seat）。
        raise _NoPremiumSeat(premium_checked, premium_reasons)

    # 这是匿名接口。上游 OpenAI 的原始报错可能夹带请求头（access token / session
    # cookie）、内部路径或库名，一律不回给调用方；详情只写进操作日志供管理员排查。
    if last_error:
        await log_operation(
            None,
            "self_service_redeem",
            None,
            "reason=no_available_seat",
            "failed",
            mask_secrets(str(last_error)),
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="没有可用 ChatGPT 席位，请联系管理员",
    )


# 本进程里还在跑的兑换（token_use_id）。_redeem_valid_token 占用成功后登记、整个
# 请求结束（含 finally 里的退码）后才移除。对账任务在调度器线程里读它：set 的
# add/discard/in 在 GIL 下是原子的；后端规定单进程运行（README），所以"不在这里"
# 就等于"处理它的那个请求已经不在了"（被杀、或带着异常结束）。
_inflight_token_uses: set[int] = set()


_UNCERTAIN_INVITE_ERROR = "OpenAI invite result is uncertain"


async def _lock_interrupted_invite(
    attempt: dict[str, Any], created_at: Optional[datetime]
) -> bool:
    """把一笔被中断的 invite_pending/pending 兑换转成 uncertain，交给管理员收尾。

    进程在邀请请求途中被杀（停服超时 30 秒，短于 60 秒的上游请求）会留下 pending。
    对账任务看不见这个人时原先只能永远等：管理员收尾列表只列 uncertain，码和邮箱
    占用永远锁死，巡逻也没有屏障挡着可能已经发出去的那个邀请。

    这里只做和在线 uncertain 分支完全相同的三件事：锁成 uncertain（WHERE
    result='pending'，并发收尾的兑换赢）、立 kind='barrier' 的巡逻屏障、记日志。
    不退码、不放邮箱占用、不换 Team、不给时长——这些只走既有的对账/管理员收尾。
    返回是否真的转换了。
    """
    if attempt.get("result") != "pending":
        return False
    team_id = attempt.get("team_id")
    if not team_id:
        return False
    token_use_id = int(attempt["id"])
    if token_use_id in _inflight_token_uses:
        return False
    if created_at is None:
        return False
    if utc_now().timestamp() - created_at.timestamp() < _INTERRUPTED_INVITE_AFTER_SECONDS:
        return False

    reason = "invite interrupted before its result was recorded"
    try:
        locked = await _lock_uncertain_with_barrier(
            token_use_id,
            team_id=team_id,
            email=attempt["email"],
            error_message=_UNCERTAIN_INVITE_ERROR,
            reason=reason,
        )
    except Exception:
        # 两步一起回滚了，兑换仍是 pending：下一轮对账再试。
        logger.exception(
            "failed to lock interrupted invite as uncertain team=%s token_use_id=%s",
            team_id,
            token_use_id,
        )
        return False
    if not locked:
        # 行已经不是 pending：处理它的兑换刚好落了终态，以那个终态为准，也不立屏障。
        return False

    try:
        await log_operation(
            team_id,
            "self_service_invite_interrupted",
            attempt["email"],
            f"token_use_id={token_use_id}, reserved_at={attempt.get('created_at')}",
            "uncertain",
            reason,
            "scheduler",
        )
    except Exception:
        logger.exception(
            "failed to log interrupted invite token_use_id=%s", token_use_id
        )
    return True


async def reconcile_pending_redemptions() -> dict[str, int]:
    """恢复进程中断留下的兑换。

    ``invite_pending`` 已经越过“可能向 OpenAI 发出请求”的边界，只能读取原
    Team 对账，绝不自动释放或换 Team 重试：原 Team 里看得见人就确认成功；看不见、
    且已超过 ``_INTERRUPTED_INVITE_AFTER_SECONDS`` 仍是 pending 的，转成 uncertain
    交给管理员（见 ``_lock_interrupted_invite``）。纯本地的 lookup/renew pending
    超过 ``_STALE_LOCAL_REDEMPTION_AFTER_SECONDS`` 仍未完成则可安全回滚，因为续期和收据
    本来就在同一事务里。两个时长的正本在 ``services/open_redemptions.py``。
    """
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT atu.id, atu.email, atu.action, atu.team_id, atu.user_id,
                      atu.result, atu.created_at, at.grant_expires_in,
                      t.access_token, t.device_id, t.proxy_id
               FROM access_token_uses atu
               JOIN access_tokens at ON at.id = atu.token_id
               LEFT JOIN teams t ON t.id = atu.team_id
               WHERE atu.result IN ('pending', 'uncertain')
               ORDER BY atu.id"""
        )
        attempts = [dict(row) for row in await cursor.fetchall()]

    counts = {"confirmed": 0, "released": 0, "waiting": 0, "uncertain": 0}
    stale_before = utc_now().timestamp() - _STALE_LOCAL_REDEMPTION_AFTER_SECONDS
    for attempt in attempts:
        token_use_id = int(attempt["id"])
        action = attempt.get("action") or ""
        created_at = parse_optional_datetime(attempt.get("created_at"))

        if action != "invite_pending":
            if created_at and created_at.timestamp() <= stale_before:
                released = await _fail_and_release_token_use(
                    token_use_id,
                    action="redeem_interrupted",
                    error_message="local redemption interrupted before remote mutation",
                    team_id=attempt.get("team_id"),
                    user_id=attempt.get("user_id"),
                )
                counts["released" if released else "waiting"] += 1
            else:
                counts["waiting"] += 1
            continue

        # 先在原 Team 里找人：看得见就确认成功。原 Team 已删除/凭据缺失/名单拉不到
        # 时同样落到下面——这些情况自动确认永远等不来，被中断的那笔得交给管理员。
        snapshot = None
        if attempt.get("team_id") and attempt.get("access_token") and attempt.get("device_id"):
            proxy_url = await _get_proxy_url(attempt.get("proxy_id"))
            client = ChatGPTClient(
                attempt["access_token"],
                attempt["team_id"],
                attempt["device_id"],
                proxy_url=proxy_url,
            )
            try:
                snapshot = await fetch_and_cache_members(attempt["team_id"], client)
            except Exception:
                snapshot = None
        if not _snapshot_contains_email(snapshot, attempt["email"]):
            if await _lock_interrupted_invite(attempt, created_at):
                counts["uncertain"] += 1
            else:
                counts["waiting"] += 1
            continue

        await record_confirmed_invite_extension(
            attempt["team_id"],
            attempt.get("user_id") or "",
            attempt["email"],
            attempt["grant_expires_in"],
            source="self_service",
            token_use_id=token_use_id,
            token_action="invited",
        )
        latest = await _get_latest_token_use_by_id(token_use_id)
        if latest and latest.get("result") == "success":
            # 这次兑换名下的屏障和兜底行已在结清它的同一事务里撤掉
            # （extend_member_expiry → _finalize_token_use）：这个人现在有正式的
            # member_expiry 记录。
            counts["confirmed"] += 1
            try:
                await log_operation(
                    attempt["team_id"],
                    "self_service_invite_reconciled",
                    attempt["email"],
                    f"token_use_id={token_use_id}",
                    "success",
                    None,
                    "scheduler",
                )
            except Exception:
                logger.exception(
                    "failed to log reconciled invite token_use_id=%s",
                    token_use_id,
                )
        else:
            counts["waiting"] += 1
    return counts


async def _get_latest_token_use_by_id(token_use_id: int) -> Optional[dict[str, Any]]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _build_team_choices(
    email: str,
    memberships: list[dict[str, Any]],
    *,
    code_seat_type: str = DEFAULT_SEAT_TYPE,
) -> list[dict[str, Any]]:
    """多 Team 选择提示里每个 Team 一项。

    除了 Team 名和到期时间，还要带上"这个队到底能不能续"：Owner 邮箱和永久成员
    点下去必然 409，不能只画一个看起来可点的按钮。``expiry_state`` 则用来区分
    两种同样显示"没有到期时间"的情况——本地记录为永久（续期被拒），和本地根本
    没有记录（续期会新建一条到期即踢的记录，等于给一个原本不受管的人装上倒计时）。

    这是公开响应：Owner 那一项和「没有到期时间、不能续」的成员长得完全一样
    （is_owner 为兼容响应结构保留，恒为 False），见 _NOT_RENEWABLE_DETAIL。

    席位类型和码对不上的队（见 _renewal_seat_type_block）同样不可续，
    ``blocked_reason='seat_type_mismatch'``；服务端续期时还会在 claim 内按实时名单再核一次。
    """
    choices: list[dict[str, Any]] = []
    for hit in memberships:
        team = hit["team"]
        expiry_state = await get_active_expiry_state(
            team["id"], hit.get("user_id") or "", email
        )
        expires_at = hit.get("expires_at")
        blocked_reason = None
        if hit.get("is_owner"):
            expiry_state, expires_at = "permanent", None
            blocked_reason = "permanent_membership"
        elif expiry_state == "permanent":
            blocked_reason = "permanent_membership"
        elif _renewal_seat_type_block(code_seat_type, hit.get("seat_type")):
            blocked_reason = "seat_type_mismatch"
        choices.append(
            {
                "team_id": team["id"],
                "team_name": team.get("name"),
                "status": "joined" if hit["kind"] == "member" else "pending",
                "expires_at": expires_at,
                "is_owner": False,
                "expiry_state": expiry_state,
                "renewable": blocked_reason is None,
                "blocked_reason": blocked_reason,
            }
        )
    return choices


async def _renew_existing_membership(
    existing: dict[str, Any],
    email: str,
    grant_duration: str,
    token_use_id: int,
    *,
    rescan_teams: Optional[list[dict[str, Any]]] = None,
    code_seat_type: str = DEFAULT_SEAT_TYPE,
) -> Optional[dict[str, Any]]:
    """续期前与踢人任务互斥，并在拿锁后重新确认远端成员仍存在。

    ``code_seat_type``：码的席位类型。拿锁后按刚拉到的实时名单核对成员当前的席位
    类型，对不上抛 ``_SeatTypeMismatch``（此时什么都还没写，码由调用方退回）。

    ``rescan_teams``：只有"用户没有指定 Team"的路径需要传。第一次全量扫描发生在
    拿锁之前，这中间用户可能又进了第二个 Team；续期是花钱操作，落到哪个队不能替
    用户猜，所以拿锁后要按这份 Team 列表重新全量扫一遍。扫出多个身份就退回选择提示
    （返回 ``{"needs_selection": [...]}``）。用户已经显式选过 Team 的路径不传，继续
    只复查目标队。
    """
    team = existing["team"]
    async with member_operation_claim(
        team["id"],
        email=email,
        user_id=existing.get("user_id") or "",
        operation="self_service_renew",
    ) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="成员状态正在变更，请稍后重试；兑换码未使用",
            )

        # 可能在第一次查询后、claim 拿到前已被自动踢出。必须重新拉原 Team；
        # 若已不在，则退出续期分支，后续按新邀请处理。
        if rescan_teams is not None:
            memberships = await _find_all_memberships(email, rescan_teams)
            if len(memberships) > 1:
                return {"needs_selection": memberships}
            refreshed = memberships[0] if memberships else None
            if refreshed and refreshed["team"]["id"] != team["id"]:
                # 原队的身份没了、别处又出现一个。这个 claim 锁的是原队，拿它去写
                # 另一个队的记录是错的；也不能往下走新邀请分支（人已经在别的队里，
                # 那会变成第二份成员身份）。按"状态刚变化"退回重试，码不消耗。
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="成员状态刚刚发生变化，请重新查询后再兑换。兑换码未使用。",
                )
        else:
            refreshed = await _find_existing_membership(email, [team])
        if not refreshed:
            return None
        if refreshed["is_owner"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_NOT_RENEWABLE_DETAIL,
            )
        seat_block = _renewal_seat_type_block(code_seat_type, refreshed.get("seat_type"))
        if seat_block:
            raise _SeatTypeMismatch(
                team_id=team["id"],
                user_id=refreshed.get("user_id") or "",
                code_seat_type=code_seat_type,
                member_seat_type=refreshed.get("seat_type"),
                reason=seat_block,
            )

        action = "renewed_member" if refreshed["kind"] == "member" else "renewed_invite"
        await _set_token_use_phase(
            token_use_id,
            "renew_pending",
            team_id=team["id"],
            user_id=refreshed.get("user_id") or "",
        )
        expires_at = await extend_member_expiry(
            team["id"],
            refreshed.get("user_id") or "",
            email,
            grant_duration,
            source="self_service",
            token_use_id=token_use_id,
            token_action=action,
        )
        return {
            "existing": refreshed,
            "action": action,
            "expires_at": expires_at,
        }


@admin_router.post("", response_model=AccessTokenResponse)
async def generate_access_token(req: GenerateAccessTokenRequest):
    grant_expires_in = _duration_or_400(req.grant_expires_in, allow_never=True)
    token_ttl = _duration_or_400(req.token_ttl, allow_never=True)
    max_uses = 1
    token = _new_token()
    now = utc_now()
    token_expires_at = expiry_from_duration(token_ttl, base=now)

    seat_type = req.seat_type
    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, token_expires_at,
                max_uses, used_count, note, disabled, created_at, seat_type)
               VALUES (?, ?, ?, ?, ?, 0, ?, 0, ?, ?)""",
            (
                _hash_token(token),
                token[:12],
                grant_expires_in,
                token_expires_at.isoformat() if token_expires_at else None,
                max_uses,
                req.note,
                now.isoformat(),
                seat_type,
            ),
        )
        await db.commit()
        token_id = cursor.lastrowid

    try:
        await log_operation(
            None,
            "create_access_token",
            None,
            f"token_id={token_id}, grant_expires_in={grant_expires_in}, "
            f"token_ttl={token_ttl}, seat_type={seat_type}",
            "success",
            None,
            "manual",
        )
    except Exception:
        # 码已经落库；审计日志写失败不能让管理员以为没生成、再生成一张。
        logger.exception("failed to log access token creation token_id=%s", token_id)

    return {
        "id": token_id,
        "token": token,
        "token_prefix": token[:12],
        "grant_expires_in": grant_expires_in,
        "token_expires_at": token_expires_at.isoformat() if token_expires_at else None,
        "max_uses": max_uses,
        "used_count": 0,
        "note": req.note,
        "seat_type": seat_type,
        "created_at": now.isoformat(),
    }


@admin_router.get("", response_model=list[AccessTokenListItem])
async def list_access_tokens():
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, token_prefix, grant_expires_in, token_expires_at,
                      max_uses, used_count, note, disabled, created_at, last_used_at,
                      seat_type
               FROM access_tokens
               ORDER BY created_at DESC"""
        )
        rows = await cursor.fetchall()

    return [
        {
            **dict(row),
            "max_uses": 1,
            "disabled": bool(row["disabled"]),
            "seat_type": normalize_seat_type(row["seat_type"]),
        }
        for row in rows
    ]


@admin_router.delete("/{token_id}")
async def disable_access_token(token_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE access_tokens SET disabled = 1 WHERE id = ?",
            (token_id,),
        )
        await db.commit()
        if cursor.rowcount != 1:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="兑换码不存在")
    return {"status": "ok"}


@admin_router.get("/pending-confirmations")
async def list_pending_confirmations():
    """所有卡在"结果确认中"的兑换。

    这些是远端邀请结果无法自动判定的兑换：码已锁死，人可能进去了也可能没进去。
    系统绝不按时间自动退码——只有管理员核实后从下面那个接口给出终态。
    """
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT atu.id, atu.email, atu.action, atu.team_id, atu.user_id,
                      atu.error_message, atu.created_at,
                      at.token_prefix, at.grant_expires_in,
                      t.name AS team_name
               FROM access_token_uses atu
               JOIN access_tokens at ON at.id = atu.token_id
               LEFT JOIN teams t ON t.id = atu.team_id
               WHERE atu.result = 'uncertain'
               ORDER BY atu.id DESC"""
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    for row in rows:
        # 缓存快照只是给管理员的参考，不作为判定依据：真正的核实由管理员在
        # OpenAI 后台完成，这里绝不替他下结论。
        snapshot = await get_cached_members(row["team_id"]) if row["team_id"] else None
        row["seen_in_cached_snapshot"] = _snapshot_contains_email(snapshot, row["email"])
        row["cache_updated_at"] = (snapshot or {}).get("updated_at")
    return rows


@admin_router.post("/pending-confirmations/{token_use_id}/resolve")
async def resolve_pending_confirmation(
    token_use_id: int, req: ResolvePendingConfirmationRequest
):
    """给一笔"结果确认中"的兑换一个终态。管理员专用，永远不会自动触发。"""
    attempt = await _get_latest_token_use_by_id(token_use_id)
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="兑换记录不存在")
    if attempt.get("result") != "uncertain":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"这笔兑换当前不是结果确认中（{attempt.get('result')}），无需处理",
        )

    team_id = attempt.get("team_id") or ""
    email = attempt.get("email") or ""
    if not team_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="这笔兑换没有记录目标 Team，无法收尾，请查审计日志人工处理",
        )

    token_row = await _get_token_row_by_use(token_use_id)
    grant_duration = _duration_or_400(token_row["grant_expires_in"], allow_never=True)

    if req.outcome == "success":
        # 累加语义：绝不覆盖成员已有的到期时间。兑换名下的屏障和兜底行在结清兑换的
        # 同一事务里撤掉；本地写入全部失败时兑换仍是 uncertain、屏障也还在，再加一条
        # 'extend' 兜底行，一起等调度器回填或下一次确认结清。
        expires_iso = await record_confirmed_invite_extension(
            team_id,
            attempt.get("user_id") or "",
            email,
            grant_duration,
            source="self_service",
            token_use_id=token_use_id,
            token_action="invited",
        )
        # 本地写入对一笔已被别的路径结清的兑换什么都不写（例如退码先提交），却仍会
        # 返回名义到期。按兑换的真实终态答复，不报成功、不记成功日志。
        settled = await _get_latest_token_use_by_id(token_use_id)
        if settled is None or settled.get("result") not in ("success", "uncertain"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"这笔兑换（兑换记录 #{token_use_id}）已被退码，未记入任何时长。"
                    "请让客户用同一兑换码重新兑换，不要手动邀请或设置到期。"
                ),
            )
        await log_operation(
            team_id,
            "self_service_invite_admin_confirmed",
            email,
            f"token_use_id={token_use_id}, expires_at={expires_iso}, note={req.note or ''}",
            "success",
            None,
            "admin",
        )
        return {"status": "ok", "outcome": "success", "expires_at": expires_iso}

    # outcome == "released"：把码退回未使用。
    # 安全网：本地记录或实时名单里只要还看得见这个人，就不许退——退码等于宣布
    # 远端什么都没发生，而这两处任何一处看得见都直接推翻了这个判断。
    if await get_active_expiry_state(team_id, attempt.get("user_id") or "", email) != "unmanaged":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="本地已有这个成员的授权记录，不能退码；请改选「确认成功」",
        )
    teams = await load_active_teams()
    team = next((t for t in teams if t["id"] == team_id), None)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="原 Team 当前不可用，无法核实远端状态，暂不能退码",
        )
    proxy_url = await _get_proxy_url(team.get("proxy_id"))
    client = ChatGPTClient(team["access_token"], team["id"], team["device_id"], proxy_url=proxy_url)
    try:
        snapshot = await fetch_and_cache_members(team_id, client)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="暂时拉不到原 Team 的实时名单，无法确认远端确实没有这个人，暂不能退码",
        ) from exc
    if _snapshot_contains_email(snapshot, email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="原 Team 里仍能看到这个成员或邀请，不能退码；请改选「确认成功」",
        )

    released = await _release_uncertain_token_use(
        token_use_id,
        error_message=f"admin_released: {req.note or 'verified absent on OpenAI side'}",
    )
    if not released:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="这笔兑换刚刚被其他流程收尾了，请刷新后再看",
        )
    await log_operation(
        team_id,
        "self_service_invite_admin_released",
        email,
        f"token_use_id={token_use_id}, note={req.note or ''}",
        "success",
        None,
        "admin",
    )
    return {"status": "ok", "outcome": "released"}


async def _get_token_row_by_use(token_use_id: int) -> dict[str, Any]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT at.* FROM access_tokens at
               JOIN access_token_uses atu ON atu.token_id = at.id
               WHERE atu.id = ?""",
            (token_use_id,),
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="兑换码不存在")
    return dict(row)


async def _release_uncertain_token_use(token_use_id: int, *, error_message: str) -> bool:
    """把一笔"结果确认中"的兑换退回未使用。**只允许管理员显式核实后调用。**

    与 ``_fail_and_release_token_use`` 的区别只有一个：那个函数刻意拒绝释放
    ``uncertain``，因为任何自动流程都无权在结果不明时退码。这里是人工出口。
    """
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT token_id, result FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        )
        row = await cursor.fetchone()
        if not row or row["result"] != "uncertain":
            await db.rollback()
            return False
        await db.execute(
            """UPDATE access_token_uses
               SET action = 'redeem_admin_released', result = 'failed',
                   error_message = ?, expires_at = NULL
               WHERE id = ? AND result = 'uncertain'""",
            (error_message, token_use_id),
        )
        await db.execute(
            "DELETE FROM redemption_email_claims WHERE token_use_id = ?",
            (token_use_id,),
        )
        await db.execute(
            """UPDATE access_tokens
               SET used_count = 0, last_used_at = NULL
               WHERE id = ? AND used_count = 1""",
            (row["token_id"],),
        )
        # 这次兑换名下的行（屏障，以及远端曾确认、本地落库失败留下的 'extend' 行）
        # 和退码同一事务提交：撤掉之后巡逻才可以按常规规则处理远端可能残留的对象，
        # 所以绝不能先于退码撤；分两个事务时第二个一失败这些行就永远留着。
        await resolve_token_use_reconciliations_in_tx(db, token_use_id)
        await db.commit()
        return True


async def _report_no_premium_seat(
    email: str,
    token_row: dict[str, Any],
    token_use_id: int,
    no_seat: _NoPremiumSeat,
    *,
    released: bool,
) -> None:
    """Premium 码没位置：记一条 ``redeem_no_premium_seat`` 日志，并发 Telegram 通知管理员。

    best-effort：日志或 Telegram 失败都不影响给客户的答复。通知里邮箱脱敏，码只给前缀。
    """
    reasons = "; ".join(f"{team_id}: {reason}" for team_id, reason in no_seat.reasons)
    try:
        await log_operation(
            None,
            "redeem_no_premium_seat",
            email,
            f"seat_type={PREMIUM_SEAT_TYPE}, token_use_id={token_use_id}, "
            f"teams_checked={len(no_seat.reasons)}, released={released}",
            "failed",
            reasons or "no active team",
            "manual",
        )
    except Exception:
        logger.exception("failed to log no-Premium-seat refusal token_use_id=%s", token_use_id)

    checked = [f"{name}：{why}" for name, why in no_seat.checked[:_NOTICE_MAX_TEAMS]]
    if len(no_seat.checked) > _NOTICE_MAX_TEAMS:
        checked.append(f"另有 {len(no_seat.checked) - _NOTICE_MAX_TEAMS} 个 Team 同样没有空位")
    rows = [
        f"兑换码：{token_row.get('token_prefix') or '?'}…",
        f"客户：{mask_email_for_notice(email)}",
        *(checked or ["没有可用的 Team"]),
        (
            "兑换码未消耗。要接这单：先在某个 Team 买好 Premium 席位，在面板里同步这个 Team"
            "（或等下一次自动同步），再让客户用同一个码重试。"
            if released
            else "兑换码状态待核对，请在兑换记录里查看这笔兑换。"
        ),
    ]
    notice_key = token_row.get("id") if token_row.get("id") is not None else token_row.get("token_prefix")
    now = time.monotonic()
    last_sent = _premium_notice_sent.get(notice_key)
    if last_sent is not None and now - last_sent < _PREMIUM_NOTICE_WINDOW_SECONDS:
        return
    _premium_notice_sent[notice_key] = now
    try:
        await notify_admins(detail_card("⚠️ Premium 兑换码没有可用席位", rows))
    except Exception:
        logger.exception("failed to notify admins about no Premium seat token_use_id=%s", token_use_id)


@public_router.post("/redeem", response_model=RedeemAccessTokenResponse)
async def redeem_access_token(req: RedeemAccessTokenRequest, request: Request):
    # 限流检查：兑换操作严格限制
    await _check_rate_limit(request, _limiter_redeem)

    email = _normalize_email(req.email)
    token_row = await _load_token(req.token)
    token_id = int(token_row["id"])

    # 走到这里的是一张有效未用的码，接下来就要实时查上游。先过尝试预算（见
    # _RedeemLookupBudget）；被挡时什么都还没占用，码原样保留。
    charged_at = time.time()
    blocked = _redeem_lookup_budget.try_take(token_id, charged_at)
    if blocked is not None:
        await log_operation(
            None,
            "self_service_redeem",
            None,
            f"reason=lookup_budget_{blocked}, token_id={token_id}",
            "failed",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "这张兑换码短时间内尝试次数过多，请稍后再试。兑换码未使用。"
                if blocked == "per_code"
                else "当前兑换人数较多，请稍后再试。兑换码未使用。"
            ),
        )

    # 多 Team 选择提示和 5xx 不占这张码的尝试次数（见 _RedeemLookupBudget）；没碰上游
    # 就被拒的（_LocalRefusal）连全站那一次也退。退回只动内存里的计数，码的占用/
    # 消耗由下面的流程自己负责。
    try:
        result = await _redeem_valid_token(req, email, token_row, token_id)
    except _LocalRefusal:
        _redeem_lookup_budget.refund_untouched(token_id, charged_at)
        raise
    except HTTPException as exc:
        if exc.status_code >= 500:
            _redeem_lookup_budget.refund_code(token_id, charged_at)
        raise
    except Exception:
        _redeem_lookup_budget.refund_code(token_id, charged_at)
        raise
    if result.get("status") == "team_selection_required":
        _redeem_lookup_budget.refund_prompt(token_id, charged_at, time.time())
    return result


async def _redeem_valid_token(
    req: RedeemAccessTokenRequest,
    email: str,
    token_row: dict[str, Any],
    token_id: int,
) -> dict[str, Any]:
    """一张已通过校验和尝试预算的码的兑换流程：占用 → 查上游 → 续期/邀请。"""
    grant_duration = _duration_or_400(token_row["grant_expires_in"], allow_never=True)
    # 这只是这张码的"面额"（从现在起算的名义到期），仅用于失败记录/审计展示。
    # 真正落库的到期时间由 extend_member_expiry 按 max(现有到期, now) + 时长 算出来，
    # 绝不能拿这个值直接覆盖成员现有的到期时间——那会把客户已购时长清零。
    nominal_expires_at = expiry_from_duration(grant_duration)
    nominal_expires_iso = nominal_expires_at.isoformat() if nominal_expires_at else None

    # 码的席位类型。库里只该有 default / prolite；别的值（手工改库、未来的类型）不猜，
    # 占用之前就拒绝：什么都没写、没碰上游。
    code_seat_type = normalize_seat_type(token_row.get("seat_type"))
    if code_seat_type not in CODE_SEAT_TYPES:
        try:
            await log_operation(
                None,
                "self_service_redeem",
                email,
                f"reason=invalid_code_seat_type, token_id={token_id}, seat_type={code_seat_type}",
                "failed",
            )
        except Exception:
            logger.exception("failed to log invalid code seat type token_id=%s", token_id)
        raise _LocalRefusal(
            status_code=status.HTTP_409_CONFLICT,
            detail=_INVALID_CODE_SEAT_TYPE_DETAIL,
        )

    # token 一经兑换成功即失效；先原子占用次数挡住并发重放（同一个 token 只有一次
    # 占用能成功），兑换流程中途真正失败时会在 finally 里把占用释放掉，让用户可以
    # 拿同一个 token 重试——只有真正走到下面的续期/邀请成功点才会保留这次消耗。
    token_use_id = await _reserve_token_use(token_id, email, nominal_expires_iso)
    # 登记为"本进程正在处理"，对账任务不会把它当成被中断的兑换（见 _inflight_token_uses）。
    # 和 try 之间不能有 await：finally 必须保证移除。
    _inflight_token_uses.add(token_use_id)
    consumption = _TokenConsumption()
    failure_recorded = False

    try:
        # 登录已被上游拒绝的 Team 实时拉名单必然失败，留在扫描里只会让每一次兑换都
        # 503（_find_all_memberships 是 fail-closed 的）。把它和非 active Team 同等
        # 对待：不实时扫描、不发邀请，人若在里面由下面的本地记录检查接手。
        teams = [team for team in await load_active_teams() if not team_login_rejected(team)]

        # "人在一个本次无法实时核对的 Team 里"的本地迹象（见
        # _memberships_in_unavailable_teams）。下面的实时扫描只覆盖 teams，看不到那些队。
        # 它只挡"下一步会是新邀请"的兑换；用户选了一个实时确认在里面的 Team 照常续，
        # 没选 Team 而人在可用 Team 里则进选择提示（不可用的队列出但不可续）。
        # 放在 load_active_teams 之后读：Team 在两次读取之间变成非 active 时，它要么
        # 还在 teams 里被实时扫描，要么在这里被看到，不会两边都漏掉；两边都有时以实时
        # 扫描为准，不重复列。
        scanned_team_ids = {team["id"] for team in teams}
        unavailable = [
            hit
            for hit in await _memberships_in_unavailable_teams(email)
            if hit["team_id"] not in scanned_team_ids
        ]
        unavailable_team_ids = [hit["team_id"] for hit in unavailable]

        if not teams and unavailable_team_ids:
            # 没有任何可实时核对的 Team，下一步只可能是新邀请。只读了本地库、没碰上游，
            # 按 _LocalRefusal 全额退回尝试预算。
            failure_recorded = await _fail_and_release_token_use(
                token_use_id,
                action="redeem_failed",
                error_message="unavailable_team_membership",
            )
            await _log_unavailable_team_refusal(email, unavailable_team_ids)
            raise _LocalRefusal(
                status_code=status.HTTP_409_CONFLICT,
                detail=_UNAVAILABLE_TEAM_DETAIL,
            )

        if not teams:
            failure_recorded = await _fail_and_release_token_use(
                token_use_id,
                action="none",
                error_message="no_active_team",
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="没有可用 Team")

        # 一个邮箱同时在多个 Team：续期落到哪个 Team 不能替用户猜——这是花钱的
        # 操作，猜错等于把时长记到别的队上。让用户自己选，选完的 Team 在这里按刚拉到
        # 的实时成员列表重新核对一遍；不匹配（比如选完之前刚被踢）就退回兑换码。
        selected_team_id = (req.team_id or "").strip()

        # 用户已经选定 Team 时，只实时拉这一个队：其余队的成员列表对这次续期没有
        # 任何影响，而 _find_all_memberships 是 fail-closed 的——多拉一个无关的队
        # 只会多一次上游请求，并让那个队的会话失效把这次续期一起打成 503。
        # （只有"没指定 Team"的路径才需要全量扫描：那条路径可能走到新邀请，
        #   邀请前必须确认这个邮箱不在任何一个 Team 里。）
        if selected_team_id:
            lookup_teams = [team for team in teams if team["id"] == selected_team_id]
            if not lookup_teams:
                # 前端回传了一个不属于任何活跃 Team 的 id。别把这个来路不明的字符串
                # 写进审计行——它之后会被当作"这张码落在哪个队"的定位键读回。
                failure_recorded = await _fail_and_release_token_use(
                    token_use_id,
                    action="renew_team_choice_invalid",
                    error_message="team_choice_unknown",
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="所选 Team 当前不可用，请重新查询后再兑换。兑换码未使用。",
                )
        else:
            lookup_teams = teams

        memberships = await _find_all_memberships(email, lookup_teams)

        if selected_team_id and not memberships:
            failure_recorded = await _fail_and_release_token_use(
                token_use_id,
                action="renew_team_choice_invalid",
                team_id=selected_team_id,
                error_message="team_choice_not_found",
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="所选 Team 已不在该邮箱的成员列表中，请重新查询后再兑换。兑换码未使用。",
            )

        if not selected_team_id and unavailable:
            if not memberships:
                # 可用 Team 里实时都没有他，下一步就是新邀请：另开席位而不可用 Team 里的
                # 到期照样在走。退码，请管理员处理。实时扫描已经碰过上游，这次尝试照常
                # 计入预算（和其他实时查询之后的拒绝一样）。
                failure_recorded = await _fail_and_release_token_use(
                    token_use_id,
                    action="redeem_failed",
                    error_message="unavailable_team_membership",
                )
                await _log_unavailable_team_refusal(email, unavailable_team_ids)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_UNAVAILABLE_TEAM_DETAIL,
                )
            # 人同时在可用 Team 和不可用 Team 里：续哪一个不能替他选。走多 Team 提示，
            # 不可用的队列出但不可续；他选了可用的队再提交，就走上面"已选 Team"的路径。
            choices = await _build_team_choices(
                email, memberships, code_seat_type=code_seat_type
            )
            choices += await _unavailable_team_choices(email, unavailable)
            failure_recorded = await _fail_and_release_token_use(
                token_use_id,
                action="renew_multi_team_prompt",
                error_message=None,
                result_value="notice",
                clear_expires_at=True,
            )
            return {
                "status": "team_selection_required",
                "action": None,
                "team_id": None,
                "team_name": None,
                "email": email,
                "expires_at": None,
                "message": "该邮箱同时在多个 Team 中，请选择要续期的 Team 后再提交。兑换码未使用。",
                "choices": choices,
            }

        if len(memberships) > 1:
            choices = await _build_team_choices(
                email, memberships, code_seat_type=code_seat_type
            )
            failure_recorded = await _fail_and_release_token_use(
                token_use_id,
                action="renew_multi_team_prompt",
                error_message=None,
                result_value="notice",
                clear_expires_at=True,
            )
            return {
                "status": "team_selection_required",
                "action": None,
                "team_id": None,
                "team_name": None,
                "email": email,
                "expires_at": None,
                "message": "该邮箱同时在多个 Team 中，请选择要续期的 Team 后再提交。兑换码未使用。",
                "choices": choices,
            }

        existing = memberships[0] if memberships else None
        if existing:
            if existing["is_owner"]:
                failure_recorded = await _fail_and_release_token_use(
                    token_use_id,
                    action="renew_owner_rejected",
                    team_id=existing["team"]["id"],
                    user_id=existing.get("user_id"),
                    error_message="owner_email",
                )
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NOT_RENEWABLE_DETAIL)

            try:
                renewed = await _renew_existing_membership(
                    existing,
                    email,
                    grant_duration,
                    token_use_id,
                    rescan_teams=None if selected_team_id else teams,
                    code_seat_type=code_seat_type,
                )
            except _SeatTypeMismatch as mismatch:
                # 码的席位类型和成员当前的不一致（或成员在一个不认识的席位类型上）：
                # 在任何本地写入之前拒绝，码退回。不替用户换席位，也不另发邀请。
                failure_recorded = await _fail_and_release_token_use(
                    token_use_id,
                    action="redeem_failed",
                    team_id=mismatch.team_id,
                    user_id=mismatch.user_id or None,
                    error_message=mismatch.detail,
                )
                try:
                    await log_operation(
                        mismatch.team_id,
                        "redeem_seat_type_mismatch",
                        email,
                        f"seat_type={mismatch.code_seat_type}, "
                        f"member_seat_type={mismatch.member_seat_type}, "
                        f"reason={mismatch.reason}, token_use_id={token_use_id}",
                        "failed",
                        mismatch.log_message,
                        "manual",
                    )
                except Exception:
                    logger.exception(
                        "failed to log seat type mismatch token_use_id=%s", token_use_id
                    )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=mismatch.detail,
                ) from None
            except PermanentMembershipError:
                # 当前成员没有到期时间（永久）。给他续一段有限时长只会是降级，
                # 所以拒绝并保留兑换码（finally 里会把占用退回）。
                failure_recorded = await _fail_and_release_token_use(
                    token_use_id,
                    action="renew_permanent_rejected",
                    team_id=existing["team"]["id"],
                    user_id=existing.get("user_id"),
                    error_message="permanent_membership",
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_NOT_RENEWABLE_DETAIL,
                )
            if renewed and renewed.get("needs_selection"):
                # 拿锁后重扫才出现的第二个 Team：与拿锁前发现多队走完全一样的出口。
                choices = await _build_team_choices(
                    email, renewed["needs_selection"], code_seat_type=code_seat_type
                )
                failure_recorded = await _fail_and_release_token_use(
                    token_use_id,
                    action="renew_multi_team_prompt",
                    error_message=None,
                    result_value="notice",
                    clear_expires_at=True,
                )
                return {
                    "status": "team_selection_required",
                    "action": None,
                    "team_id": None,
                    "team_name": None,
                    "email": email,
                    "expires_at": None,
                    "message": "该邮箱同时在多个 Team 中，请选择要续期的 Team 后再提交。兑换码未使用。",
                    "choices": choices,
                }

            if renewed:
                existing = renewed["existing"]
                action = renewed["action"]
                actual_expires_iso = renewed["expires_at"]
                # 成员到期时间与兑换收据已经同事务提交。
                consumption.confirm()
                try:
                    await log_operation(
                        existing["team"]["id"],
                        "self_service_renew",
                        email,
                        f"action={action}, expires_at={actual_expires_iso}",
                        "success",
                        None,
                        "manual",
                    )
                except Exception:
                    logger.exception(
                        "failed to log successful renewal team=%s email=%s",
                        existing["team"]["id"],
                        email,
                    )

                return {
                    "status": "ok",
                    "action": action,
                    "team_id": existing["team"]["id"],
                    "team_name": existing["team"]["name"],
                    "email": email,
                    "expires_at": actual_expires_iso,
                    "message": "已续期",
                }

            if selected_team_id:
                # 拿到 claim 后重新确认时人已经不在这个队了（拿锁期间被巡检/到期
                # 任务踢掉）。这条路绝不能往下走到新邀请分支：用户明确指定了 Team，
                # 而下面的 _invite_to_available_team 会按空位多少挑一个队，等于拿
                # 用户花钱的码把人塞进一个他没选的队。按既定规则失败退码。
                failure_recorded = await _fail_and_release_token_use(
                    token_use_id,
                    action="renew_team_choice_invalid",
                    team_id=selected_team_id,
                    error_message="team_choice_vanished",
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="所选 Team 中的成员身份刚刚失效，请重新查询后再兑换。兑换码未使用。",
                )

        # 消耗标记不能等这个函数返回再打：邀请在函数内部就已经不可回滚了，
        # 所以通过 on_invite_confirmed 在那一刻同步标记（见该函数注释）。
        # 新席位不发到订阅已到期的 Team（规则同管理员邀请）。只过滤这里：上面找人和
        # 续期仍覆盖这些 Team，否则那里的成员会被当成"不在任何 Team"另开一个席位。
        try:
            joined = await _invite_to_available_team(
                email,
                grant_duration,
                [team for team in teams if not subscription_lapsed(team)],
                token_use_id=token_use_id,
                on_invite_confirmed=consumption.confirm,
                on_invite_rejected=consumption.revert_for_rejected,
                seat_type=code_seat_type,
            )
        except _NoPremiumSeat as no_seat:
            # Premium 码没有任何 Team 有空的已付 Premium 席位。没有发出过成功或结果未定
            # 的邀请（那两种都会直接返回），码按普通「没位置」一样退回；退回之后才记日志、
            # 通知管理员，通知里「码未消耗」才是真的。
            if not consumption.confirmed:
                failure_recorded = await _fail_and_release_token_use(
                    token_use_id,
                    action="redeem_failed",
                    error_message=no_seat.detail,
                )
                await _report_no_premium_seat(
                    email,
                    token_row,
                    token_use_id,
                    no_seat,
                    released=failure_recorded,
                )
            raise
        if joined["status"] == "pending_confirmation":
            return {
                "status": "pending_confirmation",
                "action": None,
                "team_id": joined["team"]["id"],
                "team_name": joined["team"]["name"],
                "email": email,
                "expires_at": None,
                "message": "结果确认中，兑换码已暂时锁定，请稍后用原兑换码查询",
            }
        return {
            "status": "ok",
            "action": "invited",
            "team_id": joined["team"]["id"],
            "team_name": joined["team"]["name"],
            "email": email,
            "expires_at": joined["expires_at"],
            "message": "已发送邀请",
        }
    except HTTPException as exc:
        if not failure_recorded and not consumption.confirmed:
            failure_recorded = await _fail_and_release_token_use(
                token_use_id,
                action="redeem_failed",
                error_message=str(exc.detail),
            )
        raise
    except Exception as exc:
        if not consumption.confirmed:
            failure_recorded = await _fail_and_release_token_use(
                token_use_id,
                action="redeem_failed",
                error_message=str(exc),
            )
        raise
    finally:
        # 只有真正没跨过"不可回滚点"才释放占用。confirm() 是同步调用、发生在
        # OpenAI 邀请成功 / 本地续期落库成功的那一刻，所以之后无论抛出什么
        # ——包括 systemd 停服时的 CancelledError 这类 BaseException（上面的
        # except Exception 接不住它）——都不会把已消耗的 token 退回去。
        if not consumption.confirmed and not failure_recorded:
            # 走到这里要么是异常正在向外传播，要么是进程/连接被掐断——两种情况都
            # 还没把占用退回去。已经显式退过的路径（含多 Team 选择提示这种正常返回）
            # 由 failure_recorded 挡在外面：那行已经不是 pending，再退一次只是白拿
            # 一次写锁。所以下面吞掉的只可能是"退回动作自己"引发的新异常，不会把
            # 一次成功的返回悄悄改成失败。
            try:
                # shield：即使当前任务正在被取消（停服/客户端断开），退回动作本身
                # 也要跑完，不能半路又被取消而留下一个已占用但没人用的 token。
                await asyncio.shield(
                    _fail_and_release_token_use(
                        token_use_id,
                        action="redeem_aborted",
                        error_message="request_aborted",
                    )
                )
            except BaseException as release_exc:  # noqa: BLE001 - 见上方注释
                logger.error(
                    "failed to release token use during unwind token_use_id=%s: %r",
                    token_use_id, release_exc,
                )
        # 放在最后：退码完成之前它仍算"正在处理"，对账任务不能抢先把它锁成 uncertain。
        # 上面那段的异常全被接住，这一行一定会执行。
        _inflight_token_uses.discard(token_use_id)


@public_router.post("/query")
async def query_self_service(req: QuerySelfServiceRequest, request: Request):
    # 限流检查：查询操作允许更高频率
    await _check_rate_limit(request, _limiter_query)

    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="查询内容不能为空")

    if query.startswith("atm_"):
        return await _query_token(query)

    token_row = await _get_token_by_raw(query)
    if token_row:
        return await _query_token(query)

    email = _normalize_email(query)
    membership = await _query_membership_status(email, req.token)
    return {
        "query_type": "email",
        "membership": membership,
    }


def _public_expiry_state(hit: dict[str, Any]) -> str:
    """公开查询里"到期时间"的真实含义，规则与管理端 noExpiryKind 一致。

    有到期时间 = dated；没有时看本地到期记录的 source：detected = external（面板外
    加入，巡逻可能移出），无记录 = unrecorded，其余 = permanent（记录里没设到期时间）。
    permanent 也包括巡逻启用时系统补登的老成员，并不都是管理员有意设成的永久，所以
    客户页面对它只说「未设置到期时间」，不承诺永久。
    """
    if hit.get("expires_at"):
        return "dated"
    source = hit.get("source")
    if source == "detected":
        return "external"
    if source is None:
        return "unrecorded"
    return "permanent"


async def _query_membership_status(email: str, proof_token: Optional[str] = None):
    """Membership lookup core; callers apply their public endpoint limiter once.

    ``proof_token``：调用方转交的兑换码。只有它确实属于这个邮箱时才附带兑换历史，
    否则 ``redemption_history`` 返回空列表——响应结构对老客户端保持不变。
    """
    history: list[dict[str, Any]] = []
    if await _history_proof_accepted(email, proof_token):
        token_row = await _get_token_by_raw(proof_token or "")
        if token_row:
            history = [
                _public_use_fields(item)
                for item in await _get_redemption_history(email, token_id=int(token_row["id"]))
            ]
    teams = await load_active_teams()
    hits = (
        await _find_all_memberships(email, teams, use_cache_only=True) if teams else []
    )
    # 匿名按邮箱查是有意保留的功能，但不能借它确认「某个邮箱是不是 Team 的 Owner」：
    # Owner 按普通已加入成员返回（无到期时间、expiry_state=permanent），和 /redeem 对
    # Owner 的回答一致；is_owner 字段为了兼容响应结构保留，恒为 False。
    if not hits:
        return {
            "status": "absent",
            "email": email,
            "message": "未找到记录",
            "memberships": [],
            "redemption_history": history,
            "cache_updated_at": None,
        }

    memberships = [
        {
            "status": "pending" if hit["kind"] == "invite" else "joined",
            "team_id": hit["team"]["id"],
            "team_name": hit["team"]["name"],
            "expires_at": None if hit.get("is_owner") else hit.get("expires_at"),
            "is_owner": False,
            "expiry_state": "permanent" if hit.get("is_owner") else _public_expiry_state(hit),
            "cache_updated_at": hit.get("cache_updated_at"),
        }
        for hit in hits
    ]

    # 顶层字段沿用第一个命中的 Team，老调用方不受影响；完整列表在 memberships。
    first = memberships[0]
    if len(memberships) > 1:
        message = f"在 {len(memberships)} 个 Team 中"
    else:
        message = "待接受邀请" if first["status"] == "pending" else "已加入"
    return {
        "status": first["status"],
        "email": email,
        "team_id": first["team_id"],
        "team_name": first["team_name"],
        "expires_at": first["expires_at"],
        "is_owner": first["is_owner"],
        "message": message,
        "memberships": memberships,
        "redemption_history": history,
        "cache_updated_at": first["cache_updated_at"],
    }


@public_router.post("/status", response_model=QueryMembershipResponse)
async def query_membership_status(req: QueryMembershipRequest, request: Request):
    # 限流检查：查询操作允许更高频率
    await _check_rate_limit(request, _limiter_query)

    email = _normalize_email(req.email)
    return await _query_membership_status(email, req.token)
