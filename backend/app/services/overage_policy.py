"""本文件是「超员策略怎么执行」的正本（服务端）。策略取值、标签的正本是 app/seat_types.py。

能加出一个计费席位（ChatGPT / Premium）的入口都用这里的同一套判断和 409 文案：
后台单个邀请、Telegram /invite（走同一个邀请接口）、切换席位、批量自动分配的超员那一步。
调用方必须在 ``team_invite_lock`` 与该成员的操作占用之内、发任何上游写请求之前调用。

规则（单个邀请、切换席位、Telegram）：
1. 只管计费类型；Codex 不查，未知类型由调用方先拒绝。
2. 策略在锁内从库里现读（``load_team_policy``）。
3. ``auto`` → 不读容量直接放行（可能加购）。
4. 否则现拉目标类型的空位，再扣预留；拉不到按没有空位处理（失败关闭）。
5. 有空位 → 放行，不碰确认。
6. 没有空位：``forbid`` 回 409 overage_forbidden；``confirm`` 从请求带的确认
   （``overage_confirmation``，记在 overage_confirmations 表）里原子地扣 1 个再放行，
   确认缺失、过期、对不上 Team / 类型或个数用完 → 409 require_overage_confirmation。
   旧前端的 ``allow_overage=True`` 不是确认。
7. 扣掉的那 1 个只在上游明确拒绝时还回去（``restore_overage_confirmation``）；
   结果不明按已经加购算。

批量自动分配的超员仍用 ``proceeds_without_capacity_check``（确认绑定在批量计划和个数上，
见 routes/gpt_members.py）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn

from fastapi import HTTPException, status

from ..database import get_db, log_operation
from ..seat_types import (
    DEFAULT_SEAT_TYPE,
    is_billed_seat_type,
    normalize_overage_policy,
    normalize_seat_type,
    seat_type_label,
)
from .pricing import seat_charge_text, seat_cost_totals, seat_price_info
from .seat_capacity import (
    fetch_live_chatgpt_seat_capacity,
    fetch_live_seat_type_capacity,
    update_capacity_cache,
)
from .team_locks import reserved_seats

logger = logging.getLogger(__name__)

CODE_FORBIDDEN = "overage_forbidden"
CODE_NEEDS_CONFIRMATION = "require_overage_confirmation"
# 拒绝时写进操作日志 detail 的 reason=…
REASON_FORBIDDEN = "overage_forbidden"
REASON_NEEDS_CONFIRMATION = "overage_needs_confirmation"
# 批量自动分配里找不到任何可去的 Team（没空位，也没有允许超员的 Team）。
NO_PLACE_ERROR = "没位置，未邀请"
CAPACITY_UNKNOWN_PREFIX = "暂时读不到空位，按已满处理："

OPERATION_INVITE = "invite"
OPERATION_SEAT_SWITCH = "seat_switch"
OPERATION_BATCH = "batch"

# 一次确认第一次用到之后多久作废。
CONFIRMATION_TTL = timedelta(hours=1)
CONFIRMATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
CONFIRMATION_MAX_SEATS = 100
# 确认没能用上的原因：409 detail 的 confirmation_status。
CONFIRMATION_MISSING = "missing"
CONFIRMATION_USED_UP = "used_up"
CONFIRMATION_EXPIRED = "expired"
CONFIRMATION_MISMATCH = "mismatch"
_CONFIRMATION_RETRY_NOTES = {
    CONFIRMATION_USED_UP: "你确认过的加购个数已经用完，需要重新确认。",
    CONFIRMATION_EXPIRED: "上次的确认已过期，需要重新确认。",
    CONFIRMATION_MISMATCH: "这次确认对不上这个 Team 或席位类型，需要重新确认。",
}


@dataclass(frozen=True)
class TeamPolicy:
    team_id: str
    team_name: str
    policy: str
    exists: bool


async def load_team_policy(team_id: str) -> TeamPolicy:
    """现读一个 Team 的超员策略。行不在了按 ``forbid``（最保守，不会加购）。"""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, overage_policy FROM teams WHERE id = ?", (team_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        return TeamPolicy(team_id=team_id, team_name=team_id, policy="forbid", exists=False)
    return TeamPolicy(
        team_id=row["id"],
        team_name=row["name"] or row["id"],
        policy=normalize_overage_policy(row["overage_policy"]),
        exists=True,
    )


def proceeds_without_capacity_check(policy: str, allow_overage: bool) -> bool:
    """批量自动分配的超员那一步：``auto``，或 ``confirm`` 且这个 Team 在管理员确认过的
    计划里 → 可以超员塞人（ChatGPT 自动加购扣费）。个数由批量路由的额度管。

    单个邀请 / 切换席位不用它：那里 confirm 必须带 ``overage_confirmation``，见 check_billed_seat。
    """
    policy = normalize_overage_policy(policy)
    return policy == "auto" or (policy == "confirm" and bool(allow_overage))


def refusal_reason(policy: str) -> str:
    return REASON_FORBIDDEN if normalize_overage_policy(policy) == "forbid" else REASON_NEEDS_CONFIRMATION


@dataclass(frozen=True)
class ConfirmationUse:
    """从一次确认里扣掉的 1 个：扣完后已用 ``used`` 个，共 ``seat_limit`` 个。"""

    confirmation_id: str
    used: int
    seat_limit: int

    def log_detail(self) -> str:
        return f"overage_confirmed={self.used}/{self.seat_limit}"


@dataclass(frozen=True)
class SeatCheck:
    team_id: str
    team_name: str
    seat_type: str
    policy: str
    allowed: bool
    # 放行但没确认过有空位（auto / confirm 扣了确认）：这次可能让 ChatGPT 加购扣费。
    overage: bool
    # 现拉到的容量（available 已扣预留）；None = auto 没读。
    capacity: dict[str, Any] | None = None
    capacity_unknown: bool = False
    # confirm 放行时扣掉的那 1 个；上游明确拒绝时调用方用它还回去。
    confirmation: ConfirmationUse | None = None
    # confirm 被拒时确认为什么没用上（CONFIRMATION_*）。
    confirmation_status: str | None = None

    @property
    def reason(self) -> str | None:
        return None if self.allowed else refusal_reason(self.policy)

    def log_detail(self) -> str:
        if self.allowed:
            text = f"seat_type={self.seat_type}, policy={self.policy}, overage={self.overage}"
            if self.confirmation is not None:
                text += f", {self.confirmation.log_detail()}"
            return text
        return f"seat_type={self.seat_type}, policy={self.policy}, reason={self.reason}"


def _confirmation_timestamp(value: datetime) -> str:
    # 定宽（带微秒）才能在 SQL 里直接按字符串比先后。
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _confirmation_fields(confirmation: Any) -> tuple[str, str, int] | None:
    """请求里的 ``overage_confirmation``（模型或 dict）→ (id, 席位类型, 个数)；不成形返回 None。"""
    if confirmation is None:
        return None
    if isinstance(confirmation, dict):
        raw_id = confirmation.get("confirmation_id")
        raw_type = confirmation.get("seat_type")
        raw_limit = confirmation.get("seat_limit")
    else:
        raw_id = getattr(confirmation, "confirmation_id", None)
        raw_type = getattr(confirmation, "seat_type", None)
        raw_limit = getattr(confirmation, "seat_limit", None)
    if not isinstance(raw_id, str) or not CONFIRMATION_ID_RE.match(raw_id):
        return None
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        return None
    if not 1 <= raw_limit <= CONFIRMATION_MAX_SEATS or not isinstance(raw_type, str):
        return None
    return raw_id, normalize_seat_type(raw_type), raw_limit


async def consume_overage_confirmation(
    confirmation: Any, *, team_id: str, seat_type: str
) -> tuple[ConfirmationUse | None, str]:
    """从确认里原子地扣 1 个（``used < seat_limit`` 时 ``used += 1``）。

    第一次用到某个 id 时登记 Team、席位类型、个数和 1 小时后的作废时间；之后只认登记的那份，
    请求里再带的个数不算数。返回 ``(扣到的那 1 个, "consumed")``，扣不到返回
    ``(None, CONFIRMATION_*)``。
    """
    fields = _confirmation_fields(confirmation)
    if fields is None:
        return None, CONFIRMATION_MISSING
    confirmation_id, confirmed_type, seat_limit = fields
    seat_type = normalize_seat_type(seat_type)
    if confirmed_type != seat_type:
        return None, CONFIRMATION_MISMATCH
    now = datetime.now(timezone.utc)
    now_text = _confirmation_timestamp(now)
    async with get_db() as db:
        await db.execute(
            """INSERT INTO overage_confirmations
                   (confirmation_id, team_id, seat_type, seat_limit, used, created_at, expires_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)
               ON CONFLICT(confirmation_id) DO NOTHING""",
            (
                confirmation_id, team_id, seat_type, seat_limit, now_text,
                _confirmation_timestamp(now + CONFIRMATION_TTL),
            ),
        )
        cursor = await db.execute(
            """UPDATE overage_confirmations SET used = used + 1
               WHERE confirmation_id = ? AND team_id = ? AND seat_type = ?
                 AND used < seat_limit AND expires_at > ?""",
            (confirmation_id, team_id, seat_type, now_text),
        )
        consumed = cursor.rowcount == 1
        cursor = await db.execute(
            "SELECT team_id, seat_type, seat_limit, used, expires_at FROM overage_confirmations "
            "WHERE confirmation_id = ?",
            (confirmation_id,),
        )
        row = await cursor.fetchone()
        await db.commit()
    if consumed and row is not None:
        return ConfirmationUse(confirmation_id, int(row["used"]), int(row["seat_limit"])), "consumed"
    if row is None:
        return None, CONFIRMATION_MISSING
    if row["team_id"] != team_id or row["seat_type"] != seat_type:
        return None, CONFIRMATION_MISMATCH
    if str(row["expires_at"]) <= now_text:
        return None, CONFIRMATION_EXPIRED
    return None, CONFIRMATION_USED_UP


async def restore_overage_confirmation(use: ConfirmationUse | None) -> None:
    """上游明确拒绝了这次邀请 / 切换：把扣掉的那 1 个还回去。结果不明时不要调用。"""
    if use is None:
        return
    try:
        async with get_db() as db:
            await db.execute(
                "UPDATE overage_confirmations SET used = used - 1 WHERE confirmation_id = ? AND used > 0",
                (use.confirmation_id,),
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - 还不回去只会让管理员多确认一次，不会多花钱
        logger.warning("overage confirmation restore failed: %s", type(exc).__name__)


def _unknown_capacity(seat_type: str, reserved: int) -> dict[str, Any]:
    capacity: dict[str, Any] = {
        "seat_type": seat_type,
        "available": 0,
        "capacity_unknown": True,
        "reserved": reserved,
    }
    if seat_type == DEFAULT_SEAT_TYPE:
        capacity.update({
            "seats_entitled": None,
            "seats_in_use_total": None,
            "codex_count": None,
            "active_chatgpt": None,
            "pending_default": None,
            "reserved_default": reserved,
        })
    else:
        capacity.update({"paid": None, "in_use": None, "pending": None})
    return capacity


async def live_free_seats(client: Any, team_id: str, seat_type: str, *, email: str = "") -> dict[str, Any]:
    """现拉计费类型 ``seat_type`` 的空位并扣掉进程内预留（不算 ``email`` 自己那份）。

    返回给 409 / 日志用的容量摘要，``available`` 是最终空位。任何一个读接口失败都返回
    ``capacity_unknown: True`` 且 ``available: 0``：拉不到就按没有空位处理，绝不放行加购。
    读成功时顺手刷新 Team 的容量缓存（界面的置灰判断用它）。
    """
    seat_type = normalize_seat_type(seat_type)
    reserved = await reserved_seats(team_id, seat_type, exclude_email=email)
    try:
        if seat_type == DEFAULT_SEAT_TYPE:
            cap, subscription, seat_counts, _pending = await fetch_live_chatgpt_seat_capacity(client)
            capacity: dict[str, Any] = {
                "seat_type": seat_type,
                "available": max(0, cap.available - reserved),
                "capacity_unknown": False,
                "reserved": reserved,
                "seats_entitled": cap.seats_entitled,
                "seats_in_use_total": cap.seats_in_use_total,
                "codex_count": cap.codex_count,
                "active_chatgpt": cap.active_chatgpt,
                "pending_default": cap.pending_default,
                "reserved_default": reserved,
                "legacy_available": cap.legacy_available,
                "per_type_available": cap.per_type_available,
            }
        else:
            cap, subscription, seat_counts, _pending = await fetch_live_seat_type_capacity(
                client, seat_type
            )
            capacity = {
                "seat_type": seat_type,
                "available": max(0, cap.available - reserved),
                "capacity_unknown": False,
                "reserved": reserved,
                "paid": cap.paid,
                "in_use": cap.in_use,
                "pending": cap.pending,
            }
    except Exception as exc:  # noqa: BLE001 - 任何读失败都按「没有空位」处理
        logger.warning(
            "overage check: live capacity read failed team=%s seat_type=%s: %s",
            team_id, seat_type, type(exc).__name__,
        )
        return _unknown_capacity(seat_type, reserved)

    await update_capacity_cache(team_id, subscription, seat_counts)
    return capacity


async def check_billed_seat(
    client: Any,
    team: TeamPolicy,
    seat_type: str,
    *,
    email: str = "",
    confirmation: Any = None,
) -> SeatCheck:
    """规则 3–6 的判断本身（不写日志、不抛 409）。``seat_type`` 必须是计费类型。

    ``confirmation`` 是请求带的 ``overage_confirmation``；只有 confirm 的 Team 现拉确认没有
    空位时才会从里面扣 1 个。放行结果里的 ``confirmation`` 不为空时，调用方在上游明确拒绝
    后要 ``restore_overage_confirmation``。
    """
    seat_type = normalize_seat_type(seat_type)
    if not is_billed_seat_type(seat_type):
        raise ValueError(f"overage check is only for billed seat types, got {seat_type!r}")
    base = {
        "team_id": team.team_id,
        "team_name": team.team_name,
        "seat_type": seat_type,
        "policy": team.policy,
    }
    if normalize_overage_policy(team.policy) == "auto":
        return SeatCheck(**base, allowed=True, overage=True)
    capacity = await live_free_seats(client, team.team_id, seat_type, email=email)
    capacity_unknown = bool(capacity.get("capacity_unknown"))
    if int(capacity.get("available") or 0) > 0:
        return SeatCheck(**base, allowed=True, overage=False, capacity=capacity)
    if normalize_overage_policy(team.policy) == "confirm":
        use, status = await consume_overage_confirmation(
            confirmation, team_id=team.team_id, seat_type=seat_type
        )
        if use is not None:
            return SeatCheck(
                **base, allowed=True, overage=True, capacity=capacity,
                capacity_unknown=capacity_unknown, confirmation=use,
            )
        return SeatCheck(
            **base, allowed=False, overage=False, capacity=capacity,
            capacity_unknown=capacity_unknown, confirmation_status=status,
        )
    return SeatCheck(
        **base, allowed=False, overage=False, capacity=capacity, capacity_unknown=capacity_unknown
    )


def refusal_message(
    *,
    policy: str,
    team_name: str,
    seat_type: str,
    operation: str = OPERATION_INVITE,
    capacity_unknown: bool = False,
    confirmation_status: str | None = None,
    charge: str | None = None,
) -> str:
    """给管理员看的一句话：为什么没加，以及继续会对钱发生什么。

    ``charge`` 是加购这 1 席每月多出的钱（``seat_charge_text``），给了就接在「扣费」后面。
    """
    label = seat_type_label(seat_type)
    money = f"，{charge}" if charge else ""
    if normalize_overage_policy(policy) == "forbid":
        text = (
            f"「{team_name}」设为禁止超员：{label} 席位已满，不会自动加购。"
            "要加人请先在 Team 设置里修改超员策略。"
        )
    elif operation == OPERATION_SEAT_SWITCH:
        text = f"切换到 {label} 会让 ChatGPT 自动加购 1 个 {label} 席位并扣费{money}。"
    else:
        text = f"「{team_name}」{label} 席位已满，继续会让 ChatGPT 自动加购 1 个 {label} 席位并扣费{money}。"
    if capacity_unknown:
        text = f"{CAPACITY_UNKNOWN_PREFIX}{text}"
    note = _CONFIRMATION_RETRY_NOTES.get(confirmation_status or "")
    return f"{note}{text}" if note and normalize_overage_policy(policy) == "confirm" else text


async def load_seat_price(team_id: str, seat_type: str) -> dict[str, Any] | None:
    """这个 Team 一席 ``seat_type`` 的价格（``seat_price_info``）；行不在、单价未知、计费周期未知都是 None。"""
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT billing_period, price_period, billing_currency, billing_symbol,
                      price_per_seat, premium_price_per_seat
               FROM teams WHERE id = ?""",
            (team_id,),
        )
        row = await cursor.fetchone()
    return seat_price_info(dict(row), seat_type) if row is not None else None


def refusal_detail(
    check: SeatCheck, *, operation: str, seat_price: dict[str, Any] | None = None
) -> dict[str, Any]:
    """单个 Team 被策略挡下时的 409 ``detail``（禁止超员 / 需要确认两种）。

    需要确认时带上 ``seat_price``（一席的月价，未知为 None），文案里也写上金额或「单价未知」。
    """
    forbid = normalize_overage_policy(check.policy) == "forbid"
    capacity = dict(check.capacity) if check.capacity else _unknown_capacity(check.seat_type, 0)
    detail = {
        "code": CODE_FORBIDDEN if forbid else CODE_NEEDS_CONFIRMATION,
        "message": refusal_message(
            policy=check.policy,
            team_name=check.team_name,
            seat_type=check.seat_type,
            operation=operation,
            capacity_unknown=check.capacity_unknown,
            confirmation_status=check.confirmation_status,
            charge=None if forbid else seat_charge_text(seat_price, 1),
        ),
        "team_id": check.team_id,
        "team_name": check.team_name,
        "seat_type": check.seat_type,
        "policy": check.policy,
        "operation": operation,
        "capacity": capacity,
    }
    if not forbid:
        detail["confirmation_status"] = check.confirmation_status or CONFIRMATION_MISSING
        detail["seat_price"] = seat_price
    return detail


async def refuse(check: SeatCheck, *, operation: str, action: str, email: str | None) -> NoReturn:
    """记一条 skipped 日志（与这次尝试的操作同一个 action），然后抛 409。"""
    seat_price = None
    if normalize_overage_policy(check.policy) != "forbid":
        seat_price = await load_seat_price(check.team_id, check.seat_type)
    detail = refusal_detail(check, operation=operation, seat_price=seat_price)
    await log_operation(check.team_id, action, email, check.log_detail(), "skipped", detail["message"])
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def batch_confirmation_detail(
    *,
    lead: str,
    plan: list[dict[str, Any]],
    capacity: dict[str, Any],
    added: list[dict[str, Any]],
    remaining_emails: list[str],
    failed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """批量自动分配要超员时问一次的 409 ``detail``：说清加购几个、加在哪些 Team、每月多花多少。

    ``plan`` 的每一项带 ``seat_price``（那个 Team 一席 ChatGPT 的月价，未知为 None）；
    ``cost_totals`` 按币种分开合计，不同币种绝不相加，单价未知的不计入。
    """
    extra_total = sum(int(item.get("extra_seats") or 0) for item in plan)
    targets = "；".join(
        f"「{item['team_name']}」{item['extra_seats']} 个，"
        f"{seat_charge_text(item.get('seat_price'), int(item.get('extra_seats') or 0))}"
        for item in plan
    )
    label = seat_type_label(DEFAULT_SEAT_TYPE)
    first = plan[0] if plan else {}
    return {
        "code": CODE_NEEDS_CONFIRMATION,
        "message": (
            f"{lead}继续会让 ChatGPT 自动加购 {extra_total} 个 {label} 席位并扣费：{targets}。"
        ),
        "team_id": first.get("team_id"),
        "team_name": first.get("team_name"),
        "seat_type": DEFAULT_SEAT_TYPE,
        "policy": "confirm",
        "operation": OPERATION_BATCH,
        "capacity": {**capacity, "seat_type": DEFAULT_SEAT_TYPE, "capacity_unknown": False},
        "added": added,
        "remaining_emails": remaining_emails,
        "failed": failed or [],
        "overage_plan": plan,
        "extra_seats_total": extra_total,
        "cost_totals": seat_cost_totals(
            [(item.get("seat_price"), int(item.get("extra_seats") or 0)) for item in plan]
        ),
    }
