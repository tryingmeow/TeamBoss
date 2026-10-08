"""本文件是席位类型注册表与超员策略取值的正本（后端）。前端正本：frontend/src/lib/seatType.ts。

席位类型来自 ChatGPT Business 工作区（``seat_type_counts`` / ``subscriptions.seat_capacity``）。
TeamBoss 只对注册表里的类型动手：不在表里的类型（例如 ``automation``）只显示为
「其他（<原值>）」，不算空位、不邀请、不切换、不踢。

``billed`` = 没有已付空位时邀请或切换到这个类型，ChatGPT 会自动加购一个席位并扣费。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args


SeatTypeLiteral = Literal["default", "usage_based", "prolite"]
# 工作区「默认邀请席位」只允许这两种：Premium 很贵，不能成为默认值。
WorkspaceDefaultSeatTypeLiteral = Literal["default", "usage_based"]
# 兑换码可选的席位类型（Codex 按量计费，不发兑换码）。
CodeSeatTypeLiteral = Literal["default", "prolite"]

DEFAULT_SEAT_TYPE = "default"
CODEX_SEAT_TYPE = "usage_based"
PREMIUM_SEAT_TYPE = "prolite"


@dataclass(frozen=True)
class SeatTypeInfo:
    value: str
    label: str
    billed: bool


SEAT_TYPES: dict[str, SeatTypeInfo] = {
    DEFAULT_SEAT_TYPE: SeatTypeInfo(DEFAULT_SEAT_TYPE, "ChatGPT", True),
    CODEX_SEAT_TYPE: SeatTypeInfo(CODEX_SEAT_TYPE, "Codex", False),
    PREMIUM_SEAT_TYPE: SeatTypeInfo(PREMIUM_SEAT_TYPE, "Premium", True),
}

WORKSPACE_DEFAULT_SEAT_TYPES: tuple[str, ...] = get_args(WorkspaceDefaultSeatTypeLiteral)
CODE_SEAT_TYPES: tuple[str, ...] = get_args(CodeSeatTypeLiteral)
BILLED_SEAT_TYPES: tuple[str, ...] = tuple(v for v, info in SEAT_TYPES.items() if info.billed)

if set(get_args(SeatTypeLiteral)) != set(SEAT_TYPES):  # pragma: no cover - import-time guard
    raise RuntimeError("SeatTypeLiteral and SEAT_TYPES disagree")
if not set(WORKSPACE_DEFAULT_SEAT_TYPES) <= set(SEAT_TYPES) or not set(CODE_SEAT_TYPES) <= set(SEAT_TYPES):
    raise RuntimeError("seat type subsets must be registry types")  # pragma: no cover


def normalize_seat_type(value: Any) -> str:
    """上游原值规整成字符串：缺失 / None / 空串按 ``default``（与上游、旧代码一致）。

    不认识的值原样返回（去空白），**不会**被当成 ``default``。调用方用
    ``is_known_seat_type`` 判断能不能动手。
    """
    if value is None:
        return DEFAULT_SEAT_TYPE
    text = str(value).strip()
    return text or DEFAULT_SEAT_TYPE


def is_known_seat_type(value: Any) -> bool:
    return normalize_seat_type(value) in SEAT_TYPES


def is_billed_seat_type(value: Any) -> bool:
    """注册表里标为计费的类型。未知类型返回 False，但调用方必须先拒绝未知类型。"""
    info = SEAT_TYPES.get(normalize_seat_type(value))
    return bool(info and info.billed)


def seat_type_label(value: Any) -> str:
    seat_type = normalize_seat_type(value)
    info = SEAT_TYPES.get(seat_type)
    return info.label if info else f"其他（{seat_type}）"


# ---- 每个 Team 的超员策略 ---------------------------------------------------------

OveragePolicyLiteral = Literal["forbid", "confirm", "auto"]
OVERAGE_POLICIES: tuple[str, ...] = get_args(OveragePolicyLiteral)
DEFAULT_OVERAGE_POLICY = "confirm"

OVERAGE_POLICY_LABELS: dict[str, str] = {
    "forbid": "禁止超员",
    "confirm": "超员需确认",
    "auto": "超员自动",
}
OVERAGE_POLICY_HINTS: dict[str, str] = {
    "forbid": "满了就拒绝，不会自动加购",
    "confirm": "满了先问你，确认后 ChatGPT 自动加购并扣费",
    "auto": "满了直接加，ChatGPT 自动加购并扣费",
}


def normalize_overage_policy(value: Any) -> str:
    """库里的策略值：NULL / 空 = 默认 ``confirm``；其他不认识的值按最保守的 ``forbid``。"""
    if value is None:
        return DEFAULT_OVERAGE_POLICY
    text = str(value).strip().lower()
    if not text:
        return DEFAULT_OVERAGE_POLICY
    return text if text in OVERAGE_POLICIES else "forbid"
