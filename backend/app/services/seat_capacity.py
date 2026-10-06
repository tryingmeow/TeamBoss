import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from ..chatgpt_limiter import run_chatgpt_call
from ..database import get_db
from ..seat_types import DEFAULT_SEAT_TYPE, is_billed_seat_type, normalize_seat_type
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

    # 分类型席位容量。缺字段不写（保留上一次的值）；字段在但结构不对写 NULL（= 未知，
    # 读的一方按「没有可信的分类型容量」处理：default 回落旧公式，其他计费类型空位 = 0）。
    if "seat_capacity" in subscription:
        entries = parse_seat_capacity(subscription)
        updates["seat_capacity_json"] = (
            json.dumps(entries, sort_keys=True) if entries is not None else None
        )
    return updates


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def parse_seat_capacity(subscription: Any) -> dict[str, dict[str, int]] | None:
    """``subscription.seat_capacity`` → ``{type: {"paid": int, "available": int}}``。

    字段缺失或不是列表 → None（整体未知）。单个条目结构不对（type 不是非空字符串、
    paid / available 不是非负整数）就丢掉那一条，那个类型按未知处理。
    同一类型出现多次时取 available 更小的那条（宁可少卖）。
    """
    if not isinstance(subscription, dict):
        return None
    raw = subscription.get("seat_capacity")
    if not isinstance(raw, list):
        return None
    entries: dict[str, dict[str, int]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        seat_type = item.get("type")
        if not isinstance(seat_type, str) or not seat_type.strip():
            continue
        paid = _non_negative_int(item.get("paid"))
        available = _non_negative_int(item.get("available"))
        if paid is None or available is None:
            continue
        key = seat_type.strip()
        previous = entries.get(key)
        if previous is None or available < previous["available"]:
            entries[key] = {"paid": paid, "available": available}
    return entries


def cached_seat_capacity(raw: Any) -> dict[str, dict[str, int]] | None:
    """读 ``teams.seat_capacity_json``：结构不对一律 None（未知）。"""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entries: dict[str, dict[str, int]] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        paid = _non_negative_int(value.get("paid"))
        available = _non_negative_int(value.get("available"))
        if paid is None or available is None:
            continue
        entries[key] = {"paid": paid, "available": available}
    return entries


def seat_counts_column_updates(seat_counts: Any) -> dict[str, Any]:
    """一次成功的 ``get_seat_type_counts`` 响应 → ``teams.seat_type_counts_json``。

    保留所有上游给出的类型（含未知类型，界面要显示「其他（<原值>）」），只收非负整数。
    响应结构不对就不写这一列。
    """
    if not isinstance(seat_counts, dict) or chatgpt_api_error(seat_counts):
        return {}
    counts = seat_counts.get("seat_type_counts")
    if not isinstance(counts, dict):
        return {}
    clean = {
        str(key): value
        for key, value in counts.items()
        if isinstance(key, str) and _non_negative_int(value) is not None
    }
    return {"seat_type_counts_json": json.dumps(clean, sort_keys=True)}


def cached_seat_type_counts(raw: Any) -> dict[str, int]:
    """读 ``teams.seat_type_counts_json``：未知 / 结构不对返回空 dict。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): value
        for key, value in data.items()
        if isinstance(key, str) and _non_negative_int(value) is not None
    }


def billed_free_seats(
    seat_type: str,
    *,
    entries: dict[str, dict[str, int]] | None,
    pending: Any = 0,
    legacy_available: Any = None,
) -> int:
    """只看 ``seat_capacity`` 时计费类型 T 的空位数（尚未扣预留）。

    * 分类型值 = ``seat_capacity[T].available − 待接受的 T 类邀请``。
    * ``default``：分类型值和旧公式（``legacy_available`` = seats_entitled − 在用 default
      − 待接受 default）都有时取**更小**的；只有一个时用那一个；都没有时 0。
    * 其他计费类型（Premium）：没有可信的分类型值时 0。这里只用于缓存预筛；现拉时
      再用已付 − 在用 − 待接受压一道（见 ``occupancy_bounded_free_seats``）。
    * 非计费 / 未知类型：0（调用方本来就不该为它们问空位）。
    """
    seat_type = normalize_seat_type(seat_type)
    if not is_billed_seat_type(seat_type):
        return 0
    per_type: int | None = None
    entry = (entries or {}).get(seat_type)
    if entry is not None:
        per_type = max(0, safe_int(entry.get("available")) - max(0, safe_int(pending)))
    legacy: int | None = None
    if seat_type == DEFAULT_SEAT_TYPE and legacy_available is not None:
        legacy = max(0, safe_int(legacy_available))
    candidates = [value for value in (per_type, legacy) if value is not None]
    return min(candidates) if candidates else 0


def occupancy_bounded_free_seats(
    entry: dict[str, int] | None,
    *,
    in_use: int | None,
    pending: int,
) -> int:
    """非 default 计费类型（Premium）的现拉空位（尚未扣预留）。

    ``pending`` = 待接受的 T 类邀请加上没带类型的邀请。取两个值里更小的：
    ``seat_capacity[T].available − pending``，和按占用自己算的
    ``paid − 在用 T（seat_type_counts） − pending``。只信 available 一个上游字段的话，
    它没扣到的占用就会被再卖一次。任何一块缺失（没有 T 条目、seat_type_counts 里
    没有 T）= 0。
    """
    if entry is None or in_use is None:
        return 0
    occupied_by_invites = max(0, safe_int(pending))
    by_upstream = safe_int(entry.get("available")) - occupied_by_invites
    by_occupancy = safe_int(entry.get("paid")) - in_use - occupied_by_invites
    return max(0, min(by_upstream, by_occupancy))


@dataclass(frozen=True)
class ChatGPTSeatCapacity:
    seats_entitled: int
    seats_in_use_total: int
    codex_count: int
    active_chatgpt: int
    pending_default: int
    # 最终的 ChatGPT 空位（未扣进程内预留）：旧公式与分类型值都有时取更小的。
    available: int
    # 旧公式 seats_entitled − active_chatgpt − pending_default。
    legacy_available: int = 0
    # seat_capacity.default.available − pending_default；没有可信的分类型值时 None。
    per_type_available: int | None = None


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
    seat_capacity: dict[str, dict[str, int]] | None = None,
) -> ChatGPTSeatCapacity:
    """``seat_capacity`` 是 ``parse_seat_capacity`` / ``cached_seat_capacity`` 的结果；
    给了且含 default 条目时，空位取旧公式与分类型值中更小的一个（见 billed_free_seats）。"""
    entitled = safe_int(seats_entitled)
    total_in_use = safe_int(seats_in_use)
    codex = safe_int(codex_count)
    pending = safe_int(pending_default)
    resolved_chatgpt = (
        max(0, total_in_use - codex)
        if active_chatgpt is None
        else max(0, safe_int(active_chatgpt))
    )
    legacy_available = max(0, entitled - resolved_chatgpt - pending)
    entry = (seat_capacity or {}).get(DEFAULT_SEAT_TYPE)
    per_type_available = (
        max(0, safe_int(entry.get("available")) - pending) if entry is not None else None
    )
    available = billed_free_seats(
        DEFAULT_SEAT_TYPE,
        entries=seat_capacity,
        pending=pending,
        legacy_available=legacy_available,
    )
    return ChatGPTSeatCapacity(
        seats_entitled=entitled,
        seats_in_use_total=total_in_use,
        codex_count=codex,
        active_chatgpt=resolved_chatgpt,
        pending_default=pending,
        available=available,
        legacy_available=legacy_available,
        per_type_available=per_type_available,
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


class SeatCapacityFetchError(Exception):
    pass


# 待接受邀请每页条数和翻页上限：与成员快照的拉取一致（100 页 × 100 条）。
PENDING_PAGE_LIMIT = 100
MAX_PENDING_PAGES = 100


def _pending_items(pending_data: Any) -> list[dict[str, Any]]:
    """取出待接受邀请列表。没有可用的列表、条目不是对象 = 占用未知，抛 SeatCapacityFetchError。

    ``{"items": []}`` 是空名单；``{}``、``{"items": null}``、非对象响应是「没拿到名单」，
    绝不能当成 0 个待接受邀请——那会把已经被邀请占着的空位再卖一次。
    """
    if not isinstance(pending_data, dict):
        raise SeatCapacityFetchError("pending invites response is not an object")
    for key in ("items", "invites"):
        items = pending_data.get(key)
        if isinstance(items, list):
            if not all(isinstance(item, dict) for item in items):
                raise SeatCapacityFetchError("pending invites response has a non-object entry")
            return items
    raise SeatCapacityFetchError("pending invites response has no usable list")


def pending_invite_seat_type(item: dict[str, Any]) -> str | None:
    """待接受邀请的席位类型；没带（缺失 / null / 空串 / 不是字符串）返回 None。"""
    raw = item.get("seat_type")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def pending_count_from_api(pending_data: dict[str, Any], seat_type: str) -> int:
    """占着 ``seat_type`` 的待接受邀请个数：类型就是它的，加上没带类型的。

    没带类型的邀请不知道占的是哪一种，对**每个**计费类型都算一份（宁可少卖）。
    名单缺失或结构不对抛 SeatCapacityFetchError。
    """
    items = _pending_items(pending_data)
    wanted = normalize_seat_type(seat_type)
    count_untyped = is_billed_seat_type(wanted)
    count = 0
    for item in items:
        item_type = pending_invite_seat_type(item)
        if item_type == wanted or (item_type is None and count_untyped):
            count += 1
    return count


def untyped_pending_count_from_api(pending_data: dict[str, Any]) -> int:
    return sum(1 for item in _pending_items(pending_data) if pending_invite_seat_type(item) is None)


def pending_default_count_from_api(pending_data: dict[str, Any]) -> int:
    return pending_count_from_api(pending_data, DEFAULT_SEAT_TYPE)


async def fetch_all_pending_invites(
    client: Any, limit: int = PENDING_PAGE_LIMIT
) -> dict[str, Any]:
    """分页拉完整份待接受邀请，返回 ``{"items": [...], "total": n}``。

    拿不到完整名单一律抛 SeatCapacityFetchError（占用未知 = 没有空位）：
    任何一页报错、没有可用列表、条目不是对象；给了 ``total`` 却在凑够之前收到短页；
    翻到 ``MAX_PENDING_PAGES`` 还没结束。没有 ``total`` 时，短页（不足 ``limit`` 条）就是最后一页。
    """
    page_size = max(1, int(limit))
    items: list[dict[str, Any]] = []
    offset = 0
    for _ in range(MAX_PENDING_PAGES):
        data = await run_chatgpt_call(client.get_pending_invites, offset, page_size)
        error = chatgpt_api_error(data)
        if error:
            raise SeatCapacityFetchError(error)
        page_items = _pending_items(data)
        items.extend(page_items)
        total = data.get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            total = None
        short_page = len(page_items) < page_size
        if total is not None:
            if len(items) >= total:
                break
            if short_page:
                raise SeatCapacityFetchError(
                    "pending invites truncated before the reported total"
                )
        elif short_page:
            break
        offset += page_size
    else:
        raise SeatCapacityFetchError("pending invites exceed the paging limit")
    return {"items": items, "total": len(items)}


def chatgpt_api_error(result: dict[str, Any]) -> str | None:
    return result.get("error") if isinstance(result, dict) else None


async def _live_capacity_reads(
    client: Any, pending_limit: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """并发拉订阅、分类型人数和完整的待接受邀请；任何一份报错都抛 SeatCapacityFetchError。"""
    subscription, seat_counts, pending = await asyncio.gather(
        run_chatgpt_call(client.get_subscription),
        run_chatgpt_call(client.get_seat_type_counts),
        fetch_all_pending_invites(client, pending_limit),
    )
    for result in (subscription, seat_counts):
        error = chatgpt_api_error(result)
        if error:
            raise SeatCapacityFetchError(error)
    return subscription, seat_counts, pending


async def fetch_live_chatgpt_seat_capacity(
    client: Any, pending_limit: int = PENDING_PAGE_LIMIT
) -> tuple[
    ChatGPTSeatCapacity,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """现拉 ChatGPT（default）空位。``pending`` 是完整的待接受邀请
    （``{"items": [...], "total": n}``）；没带类型的邀请算作 default。"""
    subscription, seat_counts, pending = await _live_capacity_reads(client, pending_limit)

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
        seat_capacity=parse_seat_capacity(subscription),
    )
    return capacity, subscription, seat_counts, pending


@dataclass(frozen=True)
class SeatTypeCapacity:
    """一个计费席位类型的现拉容量（未扣进程内预留）。"""
    seat_type: str
    available: int
    paid: int | None
    in_use: int | None
    # 占着这个类型的待接受邀请，含没带类型的那些（pending_untyped 是其中的个数）。
    pending: int
    pending_untyped: int = 0

    def describe(self) -> str:
        """给日志 / 409 文案用的一行摘要，不含邮箱。"""
        return (
            f"seat_type={self.seat_type}, paid={self.paid}, in_use={self.in_use}, "
            f"pending={self.pending}, pending_untyped={self.pending_untyped}, "
            f"available={self.available}"
        )


async def fetch_live_seat_type_capacity(
    client: Any,
    seat_type: str,
    pending_limit: int = PENDING_PAGE_LIMIT,
) -> tuple[SeatTypeCapacity, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """现拉计费类型 ``seat_type`` 的空位。任何一个读接口失败、待接受邀请没拉全，都抛
    SeatCapacityFetchError，调用方按「没有空位」处理（失败关闭）。

    ``default`` 走 ``fetch_live_chatgpt_seat_capacity``（含 seats_entitled 校验和取小规则）；
    其他计费类型按 ``occupancy_bounded_free_seats``，缺任何一块 = 0 空位。没带类型的
    待接受邀请对每个计费类型都扣一份。非计费 / 未知类型是调用方的编程错误，抛 ValueError。
    """
    seat_type = normalize_seat_type(seat_type)
    if not is_billed_seat_type(seat_type):
        raise ValueError(f"not a billed seat type: {seat_type!r}")
    if seat_type == DEFAULT_SEAT_TYPE:
        capacity, subscription, seat_counts, pending = await fetch_live_chatgpt_seat_capacity(
            client, pending_limit
        )
        entry = (parse_seat_capacity(subscription) or {}).get(DEFAULT_SEAT_TYPE)
        return (
            SeatTypeCapacity(
                seat_type=seat_type,
                available=capacity.available,
                paid=entry["paid"] if entry else capacity.seats_entitled,
                in_use=capacity.active_chatgpt,
                pending=capacity.pending_default,
                pending_untyped=untyped_pending_count_from_api(pending),
            ),
            subscription,
            seat_counts,
            pending,
        )

    subscription, seat_counts, pending = await _live_capacity_reads(client, pending_limit)
    if not isinstance(subscription, dict):
        raise SeatCapacityFetchError("subscription response is not an object")

    entry = (parse_seat_capacity(subscription) or {}).get(seat_type)
    in_use = seat_type_count_from_seat_counts(seat_counts, seat_type)
    pending_count = pending_count_from_api(pending, seat_type)
    return (
        SeatTypeCapacity(
            seat_type=seat_type,
            available=occupancy_bounded_free_seats(entry, in_use=in_use, pending=pending_count),
            paid=entry["paid"] if entry else None,
            in_use=in_use,
            pending=pending_count,
            pending_untyped=untyped_pending_count_from_api(pending),
        ),
        subscription,
        seat_counts,
        pending,
    )


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
        updates.update(seat_counts_column_updates(seat_counts))
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
