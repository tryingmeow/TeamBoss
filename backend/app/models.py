from pydantic import BaseModel, Field
from typing import Dict, Literal, Optional, List

from .seat_types import (
    OveragePolicyLiteral,
    SeatTypeLiteral,
    WorkspaceDefaultSeatTypeLiteral,
)


class TeamSession(BaseModel):
    user: dict
    expires: str
    account: dict
    accessToken: str
    sessionToken: str


class TeamResponse(BaseModel):
    id: str
    name: str
    remark: Optional[str] = None
    owner_email: str
    status: str
    seats_in_use: int
    seats_entitled: int
    codex_count: int
    chatgpt_count: int
    is_codex_enabled: bool = False
    default_seat_type: Optional[WorkspaceDefaultSeatTypeLiteral] = None
    billing_currency: str
    billing_symbol: Optional[str] = None
    billing_period: Optional[str] = None
    price_per_seat: Optional[float] = None
    discount_amount: float = 0.0
    discount_duration_num_periods: Optional[int] = None
    discount_expires_at: Optional[str] = None
    discount_quantity_off: Optional[int] = None
    promo_campaign_id: Optional[str] = None
    monthly_subtotal: Optional[float] = None
    monthly_total: Optional[float] = None
    balance: str
    active_start: Optional[str] = None
    active_until: Optional[str] = None
    will_renew: bool
    subscription_status: Literal['renewing', 'nonrenewing', 'expired', 'stale'] = 'renewing'
    card_last4: Optional[str] = None
    card_brand: Optional[str] = None
    days_remaining: Optional[int] = None
    proxy_id: Optional[int] = None
    # 同步健康度。scheduler 一直在写这两个字段，但过去没有出接口，前端因此看不出
    # 一个 Team 已经连续多久同步不上了。
    last_full_sync_at: Optional[str] = None
    last_sync_partial_failures: List[str] = []
    # 'ok' | 'rejected'。rejected = access token 已用不了（401 或 JWT 已过期），
    # 而会话端点仍应答却交不出新 token（同一个 token 或 RefreshAccessTokenError）。
    auth_state: Literal['ok', 'rejected'] = 'ok'
    auth_state_since: Optional[str] = None
    # 定时同步挂起：连续失败满 24 小时后停止定时请求，只按低频探活。
    sync_failing_since: Optional[str] = None
    sync_suspended_at: Optional[str] = None
    cached_member_emails: List[str] = []
    # 超员策略：forbid（禁止超员）/ confirm（超员需确认）/ auto（超员自动）。
    overage_policy: OveragePolicyLiteral = "confirm"
    # 缓存的分类型容量 {type: {paid, available}}；None = 未知（上游没给或结构不对）。
    seat_capacity: Optional[Dict[str, Dict[str, int]]] = None
    # 缓存的 seat_type_counts 原样计数（含未知类型）；{} = 未知。
    seat_type_counts: Dict[str, int] = {}
    # 成员缓存里待接受邀请按席位类型（上游原值，缺失按 default）的计数；{} = 无缓存或读不出。
    pending_invite_counts: Dict[str, int] = {}


class ProxyCreate(BaseModel):
    name: str
    url: str


class ProxyUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None


class ProxyResponse(BaseModel):
    id: int
    name: str
    url: str
    status: str
    last_check_at: Optional[str] = None
    created_at: Optional[str] = None


class TeamProxyUpdate(BaseModel):
    proxy_id: Optional[int] = None


class TeamRemarkUpdate(BaseModel):
    remark: Optional[str] = None


class MemberResponse(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    role: str
    seat_type: str
    is_owner: bool
    expires_at: Optional[str] = None
    created_time: Optional[str] = None


class PendingInvite(BaseModel):
    id: str
    email: str
    seat_type: str
    created_time: str
    expires_at: Optional[str] = None


class OverageConfirmation(BaseModel):
    """管理员点过的一次「确认加购」，绑定 Team、席位类型和他看到的加购个数。

    服务端按 ``confirmation_id`` 记账（overage_confirmations 表）：第一次用到时登记 Team、
    类型、个数，之后同一个 id 最多让 ChatGPT 加购 ``seat_limit`` 个，规则见
    services/overage_policy.py。
    """

    confirmation_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    seat_type: SeatTypeLiteral
    seat_limit: int = Field(ge=1, le=100)


class InviteMemberRequest(BaseModel):
    email: str
    seat_type: SeatTypeLiteral = "default"
    expires_in: Optional[str] = None
    # 超员策略为 confirm 的 Team 满了时，只有带了这个确认才会超员加购。
    overage_confirmation: Optional[OverageConfirmation] = None
    # 旧前端的确认标记：仍然接受，但在 confirm 的 Team 上不算确认（会回 409 要求确认）。
    allow_overage: bool = False


class ChangeSeatRequest(BaseModel):
    seat_type: SeatTypeLiteral
    # 同上：切到计费类型而该类型没有空位时，confirm 策略需要带确认（个数 1）。
    overage_confirmation: Optional[OverageConfirmation] = None
    allow_overage: bool = False


class DefaultSeatTypeRequest(BaseModel):
    seat_type: WorkspaceDefaultSeatTypeLiteral


class OveragePolicyRequest(BaseModel):
    overage_policy: OveragePolicyLiteral


class SetExpiryRequest(BaseModel):
    expires_in: Optional[str] = None
    expires_at: Optional[str] = None
    email: Optional[str] = None


class ExtendExpiryRequest(BaseModel):
    """A duration to append to a member's existing expiry.

    This is intentionally separate from ``SetExpiryRequest``: the latter
    overwrites the expiry with an absolute value (or a duration from now),
    whereas this request preserves unused time before adding the duration.
    """

    expires_in: str
    email: str = Field(min_length=1)
    request_id: str


class SettingsUpdate(BaseModel):
    sync_interval_minutes: Optional[int] = None
    api_concurrency: Optional[int] = None
    expiry_kick_mode: Optional[Literal["delay_hours", "day_end", "day_start"]] = None
    expiry_kick_delay_hours: Optional[int] = None
    skip_overage_confirmation: Optional[bool] = None
