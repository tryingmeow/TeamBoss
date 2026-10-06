"""本文件是「超员策略怎么执行」的正本（服务端）。策略取值、标签的正本是 app/seat_types.py。

能加出一个计费席位（ChatGPT / Premium）的入口都用这里的同一套判断和 409 文案：
后台单个邀请、Telegram /invite（走同一个邀请接口）、切换席位、批量自动分配的超员那一步。
调用方必须在 ``team_invite_lock`` 与该成员的操作占用之内、发任何上游写请求之前调用。

规则：
1. 只管计费类型；Codex 不查，未知类型由调用方先拒绝。
2. 策略在锁内从库里现读（``load_team_policy``）。
3. ``auto``，或 ``confirm`` 且请求带了确认标记 → 不读容量直接放行（即旧的 allow_overage=True）。
4. 否则现拉目标类型的空位，再扣进程内预留；拉不到按没有空位处理（失败关闭）。
5. 有空位 → 放行；没有 → ``forbid`` 回 409 overage_forbidden，``confirm`` 回 409
   require_overage_confirmation。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
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
    """规则 3：``auto``，或 ``confirm`` 且管理员已确认 → 满了也加（ChatGPT 自动加购扣费）。

    批量自动分配的超员那一步用的也是这个判断：返回 True 的 Team 才能被超员塞人。
    """
    policy = normalize_overage_policy(policy)
    return policy == "auto" or (policy == "confirm" and bool(allow_overage))


def refusal_reason(policy: str) -> str:
    return REASON_FORBIDDEN if normalize_overage_policy(policy) == "forbid" else REASON_NEEDS_CONFIRMATION


@dataclass(frozen=True)
class SeatCheck:
    team_id: str
    team_name: str
    seat_type: str
    policy: str
    allowed: bool
    # 放行但没确认过有空位（auto / confirm+确认）：这次可能让 ChatGPT 加购扣费。
    overage: bool
    # 现拉到的容量（available 已扣进程内预留）；None = 按规则 3 没读。
    capacity: dict[str, Any] | None = None
    capacity_unknown: bool = False

    @property
    def reason(self) -> str | None:
        return None if self.allowed else refusal_reason(self.policy)

    def log_detail(self) -> str:
        if self.allowed:
            return f"seat_type={self.seat_type}, policy={self.policy}, overage={self.overage}"
        return f"seat_type={self.seat_type}, policy={self.policy}, reason={self.reason}"


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
    allow_overage: bool = False,
) -> SeatCheck:
    """规则 3–5 的判断本身（不写日志、不抛 409）。``seat_type`` 必须是计费类型。"""
    seat_type = normalize_seat_type(seat_type)
    if not is_billed_seat_type(seat_type):
        raise ValueError(f"overage check is only for billed seat types, got {seat_type!r}")
    if proceeds_without_capacity_check(team.policy, allow_overage):
        return SeatCheck(
            team_id=team.team_id,
            team_name=team.team_name,
            seat_type=seat_type,
            policy=team.policy,
            allowed=True,
            overage=True,
        )
    capacity = await live_free_seats(client, team.team_id, seat_type, email=email)
    return SeatCheck(
        team_id=team.team_id,
        team_name=team.team_name,
        seat_type=seat_type,
        policy=team.policy,
        allowed=int(capacity.get("available") or 0) > 0,
        overage=False,
        capacity=capacity,
        capacity_unknown=bool(capacity.get("capacity_unknown")),
    )


def refusal_message(
    *,
    policy: str,
    team_name: str,
    seat_type: str,
    operation: str = OPERATION_INVITE,
    capacity_unknown: bool = False,
) -> str:
    """给管理员看的一句话：为什么没加，以及继续会对钱发生什么。"""
    label = seat_type_label(seat_type)
    if normalize_overage_policy(policy) == "forbid":
        text = (
            f"「{team_name}」设为禁止超员：{label} 席位已满，不会自动加购。"
            "要加人请先在 Team 设置里修改超员策略。"
        )
    elif operation == OPERATION_SEAT_SWITCH:
        text = f"切换到 {label} 会让 ChatGPT 自动加购 1 个 {label} 席位并扣费。"
    else:
        text = f"「{team_name}」{label} 席位已满，继续会让 ChatGPT 自动加购 1 个 {label} 席位并扣费。"
    return f"{CAPACITY_UNKNOWN_PREFIX}{text}" if capacity_unknown else text


def refusal_detail(check: SeatCheck, *, operation: str) -> dict[str, Any]:
    """单个 Team 被策略挡下时的 409 ``detail``（禁止超员 / 需要确认两种）。"""
    forbid = normalize_overage_policy(check.policy) == "forbid"
    capacity = dict(check.capacity) if check.capacity else _unknown_capacity(check.seat_type, 0)
    return {
        "code": CODE_FORBIDDEN if forbid else CODE_NEEDS_CONFIRMATION,
        "message": refusal_message(
            policy=check.policy,
            team_name=check.team_name,
            seat_type=check.seat_type,
            operation=operation,
            capacity_unknown=check.capacity_unknown,
        ),
        "team_id": check.team_id,
        "team_name": check.team_name,
        "seat_type": check.seat_type,
        "policy": check.policy,
        "operation": operation,
        "capacity": capacity,
    }


async def refuse(check: SeatCheck, *, operation: str, action: str, email: str | None) -> NoReturn:
    """记一条 skipped 日志（与这次尝试的操作同一个 action），然后抛 409。"""
    detail = refusal_detail(check, operation=operation)
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
    """批量自动分配要超员时问一次的 409 ``detail``：说清加购几个、加在哪些 Team。"""
    extra_total = sum(int(item.get("extra_seats") or 0) for item in plan)
    targets = "、".join(f"「{item['team_name']}」{item['extra_seats']} 个" for item in plan)
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
    }
