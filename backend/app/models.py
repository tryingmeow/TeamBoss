from pydantic import BaseModel
from typing import Literal, Optional, List


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
    default_seat_type: Optional[Literal['default', 'usage_based']] = None
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
    # 'ok' | 'rejected'。rejected = 会话仍应答但交回的 token 已被上游吊销。
    auth_state: Literal['ok', 'rejected'] = 'ok'
    auth_state_since: Optional[str] = None
    # 定时同步挂起：连续失败满 24 小时后停止定时请求，只按低频探活。
    sync_failing_since: Optional[str] = None
    sync_suspended_at: Optional[str] = None
    cached_member_emails: List[str] = []


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


class InviteMemberRequest(BaseModel):
    email: str
    seat_type: str = "default"
    expires_in: Optional[str] = None
    allow_overage: bool = False


class ChangeSeatRequest(BaseModel):
    seat_type: str


class DefaultSeatTypeRequest(BaseModel):
    seat_type: Literal["default", "usage_based"]


class SetExpiryRequest(BaseModel):
    expires_in: Optional[str] = None
    expires_at: Optional[str] = None
    email: Optional[str] = None


class SettingsUpdate(BaseModel):
    sync_interval_minutes: Optional[int] = None
    api_concurrency: Optional[int] = None
    expiry_kick_mode: Optional[Literal["delay_hours", "day_end", "day_start"]] = None
    expiry_kick_delay_hours: Optional[int] = None
