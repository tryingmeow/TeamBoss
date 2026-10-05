import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from ..chatgpt_limiter import run_chatgpt_call
from ..database import get_db
from ..utils.durations import utc_now

logger = logging.getLogger(__name__)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def positive_seat_count(value: Any) -> int | None:
    """上游 ``seats_entitled`` 的唯一合法形态：JSON 整数且 >= 1。其余一律 None（= 未知）。

    ``seats_entitled`` 是 patrol 算 over_by 的分母，也是邀请前的容量上限。null / 0 /
    负数落库后，patrol 会把每个默认席位都算成超员、一轮就开始踢 detected 成员；
    bool 是 int 的子类，必须单独排除；数字字符串（"25"）和浮点数（25.0）也不认——
    上游契约就是整数，类型变了说明响应结构变了，宁可这一轮当未知，也不去猜。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _describe_untrusted_value(value: Any) -> str:
    """给日志用：只说形态，不回显任意字符串内容。"""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    return f"<{type(value).__name__}>"


def subscription_column_updates(subscription: dict[str, Any], *, team_id: Any) -> dict[str, Any]:
    """一次成功的 ``get_subscription`` 响应里可以直接写进 ``teams`` 的列。

    所有写 teams 订阅字段的路径（定时同步、手动同步、导入会话、邀请前的容量缓存）
    共用这一份规则：

    * 缺字段就不写这一列，避免把原本正确的值覆盖成 NULL。
    * ``seats_entitled`` 只接受 ``positive_seat_count`` 认可的正整数；不合格（含缺失）
      就不写，保留上一次的合法值，并记一条 warning。
    """
    updates: dict[str, Any] = {}
    if not isinstance(subscription, dict):
        logger.warning(
            "subscription: unrecognized response shape %s for team=%s; nothing written",
            _describe_untrusted_value(subscription), team_id,
        )
        return updates
    for col in ("seats_in_use", "billing_currency", "active_start", "active_until"):
        if col in subscription:
            updates[col] = subscription.get(col)

    raw_entitled = subscription.get("seats_entitled")
    entitled = positive_seat_count(raw_entitled)
    if entitled is not None:
        updates["seats_entitled"] = entitled
    else:
        logger.warning(
            "subscription: ignoring seats_entitled=%s for team=%s; "
            "keeping the previously stored value",
            "<missing>" if "seats_entitled" not in subscription
            else _describe_untrusted_value(raw_entitled),
            team_id,
        )

    if "will_renew" in subscription:
        will_renew_raw = subscription.get("will_renew")
        updates["will_renew"] = None if will_renew_raw is None else (1 if will_renew_raw else 0)
    return updates


@dataclass(frozen=True)
class ChatGPTSeatCapacity:
    seats_entitled: int
    seats_in_use_total: int
    codex_count: int
    active_chatgpt: int
    pending_default: int
    available: int


@dataclass(frozen=True)
class MemberSeatUsage:
    seats_in_use_total: int
    codex_count: int
    active_chatgpt: int


def chatgpt_seat_capacity(
    *,
    seats_entitled: Any,
    seats_in_use: Any,
    codex_count: Any,
    active_chatgpt: Any = None,
    pending_default: Any = 0,
) -> ChatGPTSeatCapacity:
    entitled = safe_int(seats_entitled)
    total_in_use = safe_int(seats_in_use)
    codex = safe_int(codex_count)
    pending = safe_int(pending_default)
    resolved_chatgpt = (
        max(0, total_in_use - codex)
        if active_chatgpt is None
        else max(0, safe_int(active_chatgpt))
    )
    available = max(0, entitled - resolved_chatgpt - pending)
    return ChatGPTSeatCapacity(
        seats_entitled=entitled,
        seats_in_use_total=total_in_use,
        codex_count=codex,
        active_chatgpt=resolved_chatgpt,
        pending_default=pending,
        available=available,
    )


def seat_type_count_from_seat_counts(
    seat_counts: dict[str, Any],
    seat_type: str,
) -> int | None:
    counts = seat_counts.get("seat_type_counts", {}) if isinstance(seat_counts, dict) else {}
    if not isinstance(counts, dict) or seat_type not in counts or counts.get(seat_type) is None:
        return None
    try:
        return max(0, int(counts[seat_type]))
    except (TypeError, ValueError):
        return None


def codex_count_from_seat_counts(seat_counts: dict[str, Any]) -> int:
    value = seat_type_count_from_seat_counts(seat_counts, "usage_based")
    return value if value is not None else 0


def chatgpt_count_from_seat_counts(seat_counts: dict[str, Any]) -> int | None:
    """Return the official ChatGPT/default seat count when the API supplied it."""
    return seat_type_count_from_seat_counts(seat_counts, "default")


def member_seat_usage_from_members(members: Any) -> MemberSeatUsage | None:
    if not isinstance(members, list):
        return None

    active_members = [
        item for item in members
        if isinstance(item, dict) and item.get("status", "active") == "active"
    ]
    codex_count = sum(1 for item in active_members if item.get("seat_type") == "usage_based")
    chatgpt_count = sum(
        1 for item in active_members
        if (item.get("seat_type") or "default") == "default"
    )
    seats_in_use_total = len(active_members)
    return MemberSeatUsage(
        seats_in_use_total=seats_in_use_total,
        codex_count=codex_count,
        active_chatgpt=chatgpt_count,
    )


def member_seat_usage_from_members_data(members_data: Any) -> MemberSeatUsage | None:
    if not isinstance(members_data, dict):
        return None
    return member_seat_usage_from_members(members_data.get("members"))


async def update_member_seat_usage_cache(team_id: str, members: Any) -> MemberSeatUsage | None:
    usage = member_seat_usage_from_members(members)
    if usage is None:
        return None

    async with get_db() as db:
        await db.execute(
            """UPDATE teams SET
                 seats_in_use = ?,
                 codex_count = ?,
                 chatgpt_count = ?
               WHERE id = ?""",
            (usage.seats_in_use_total, usage.codex_count, usage.active_chatgpt, team_id),
        )
        await db.commit()
    return usage


def pending_default_count_from_api(pending_data: dict[str, Any]) -> int:
    items = pending_data.get("items") if isinstance(pending_data, dict) else None
    if not isinstance(items, list):
        items = pending_data.get("invites") if isinstance(pending_data, dict) else None
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if (item.get("seat_type") or "default") == "default")


def chatgpt_api_error(result: dict[str, Any]) -> str | None:
    return result.get("error") if isinstance(result, dict) else None


class SeatCapacityFetchError(Exception):
    pass


async def fetch_live_chatgpt_seat_capacity(client: Any, pending_limit: int = 100) -> tuple[
    ChatGPTSeatCapacity,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    subscription, seat_counts, pending = await asyncio.gather(
        run_chatgpt_call(client.get_subscription),
        run_chatgpt_call(client.get_seat_type_counts),
        run_chatgpt_call(client.get_pending_invites, 0, pending_limit),
    )

    for result in (subscription, seat_counts, pending):
        error = chatgpt_api_error(result)
        if error:
            raise SeatCapacityFetchError(error)

    # 拿不到合法的 seats_entitled 就没有可信的容量：按拉取失败处理（邀请不往这个
    # team 发、手动巡逻不把它算作刷新成功），而不是 safe_int 成 0 或者把
    # "25"/True 当成 25/1 个席位。
    entitled = (
        positive_seat_count(subscription.get("seats_entitled"))
        if isinstance(subscription, dict)
        else None
    )
    if entitled is None:
        raise SeatCapacityFetchError("subscription response has no valid seats_entitled")

    capacity = chatgpt_seat_capacity(
        seats_entitled=entitled,
        seats_in_use=subscription.get("seats_in_use"),
        codex_count=codex_count_from_seat_counts(seat_counts),
        active_chatgpt=chatgpt_count_from_seat_counts(seat_counts),
        pending_default=pending_default_count_from_api(pending),
    )
    return capacity, subscription, seat_counts, pending


async def update_capacity_cache(
    team_id: str,
    subscription: dict[str, Any],
    seat_counts: dict[str, Any],
) -> None:
    if chatgpt_api_error(subscription) or chatgpt_api_error(seat_counts):
        return

    now = utc_now().isoformat()
    async with get_db() as db:
        cursor = await db.execute("SELECT members_json FROM member_cache WHERE team_id = ?", (team_id,))
        row = await cursor.fetchone()
        member_usage = None
        if row:
            try:
                member_usage = member_seat_usage_from_members(json.loads(row["members_json"] or "[]"))
            except Exception:
                member_usage = None

        subscription_total = (
            safe_int(subscription.get("seats_in_use"))
            if subscription.get("seats_in_use") is not None
            else None
        )
        official_codex = seat_type_count_from_seat_counts(seat_counts, "usage_based")
        official_chatgpt = chatgpt_count_from_seat_counts(seat_counts)

        seats_in_use = (
            subscription_total
            if subscription_total is not None
            else member_usage.seats_in_use_total if member_usage is not None
            else 0
        )
        codex_count = (
            official_codex
            if official_codex is not None
            else member_usage.codex_count if member_usage is not None
            else 0
        )
        chatgpt_count = (
            official_chatgpt
            if official_chatgpt is not None
            else member_usage.active_chatgpt if member_usage is not None
            else max(0, seats_in_use - codex_count)
        )

        # Subscription columns follow the shared rule (absent → keep the DB value,
        # seats_entitled only when it is a positive integer). seats_in_use is
        # resolved above with member-cache fallbacks, so it overrides the raw one.
        updates: dict[str, Any] = subscription_column_updates(subscription, team_id=team_id)
        updates.update({
            "seats_in_use": seats_in_use,
            "codex_count": codex_count,
            "chatgpt_count": chatgpt_count,
            "updated_at": now,
        })

        set_clause = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [team_id]
        await db.execute(f"UPDATE teams SET {set_clause} WHERE id = ?", values)
        await db.commit()
