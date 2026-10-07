"""本文件是「续费前还有没人用的计费席位」判定与 Telegram 提醒的正本（后端）。
前端演示数据镜像同一算法：frontend/src/demo/views.ts（renewalIdleSeats）。

为什么要提醒：OpenAI 在每个计费周期开始时按已付席位扣费，席位分没分配都收；移除成员
不会减少已付席位，只有 Owner 在 ChatGPT 后台把席位改小，下个计费周期才生效。所以续费时
还空着的计费席位，会被再收一个周期的钱。

只读已经同步到本地的数据（teams.seat_capacity_json / seat_type_counts_json、
member_cache.pending_json），不打 ChatGPT、不改账单。任何一块缺失或结构不对就不报：
宁可漏一次提醒，也不拿残缺数据去猜空闲席位。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from ..database import get_db_path
from ..seat_types import BILLED_SEAT_TYPES, DEFAULT_SEAT_TYPE, PREMIUM_SEAT_TYPE, seat_type_label
from ..tg_format import detail_card
from .pricing import (
    MONTHS_PER_PERIOD,
    priced_period,
    seat_price_per_month,
)
from .seat_capacity import cached_seat_capacity, cached_seat_type_counts, pending_count_from_api
from .subscription_status import parse_active_until
from .team_health_alerts import close_incident_family_sync, report_team_failure_sync
from .tg_notify import mask_email_for_notice, notify_admins_sync

logger = logging.getLogger(__name__)

DISPLAY_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 续费前多久开始提醒（卡片上的「续费前可减 K 席」也只在这个窗口里出现）。
RENEWAL_REMINDER_WINDOW = timedelta(days=3)

# 去重走 team_health_incidents：key = 前缀 + 规整后的 active_until，每个 Team 每个计费周期一个 key，
# 重启后照样认得。限频间隔必须比提醒窗口长，这一期发出去之后窗口内就不会再发；中间空闲变 0
# 又变回来也不重发（notify_interval 跨事件生效）。过了续费时间这一族静默关掉，旧行过了间隔删除。
RENEWAL_ALERT_KEY_PREFIX = "renewal_idle_seats:"
RENEWAL_ALERT_INTERVAL = timedelta(days=7)

LOG_ACTION = "renewal_idle_seat_reminder"

_SEAT_ICONS = {DEFAULT_SEAT_TYPE: "💺", PREMIUM_SEAT_TYPE: "💎"}

ACTION_LINE = "👉 不需要的话，请在 ChatGPT 后台「管理席位」里减少席位，下个计费周期生效；TeamBoss 不会替你改账单。"
WHY_LINE = "ℹ️ 续费时所有已付席位都扣费，没人用也收；移除成员不会减少已付席位。"


@dataclass(frozen=True)
class IdleSeatLine:
    seat_type: str
    paid: int
    # 下个计费周期要续费的席位数：上游 renewal_requested，没给时等于 paid。
    renewing: int
    in_use: int
    pending: int
    idle: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "seat_type": self.seat_type,
            "paid": self.paid,
            "renewing": self.renewing,
            "in_use": self.in_use,
            "pending": self.pending,
            "idle": self.idle,
        }


@dataclass(frozen=True)
class RenewalIdleSeats:
    # 规整成 UTC 秒级的 active_until，也是去重 key 的一部分。
    renews_at: str
    lines: tuple[IdleSeatLine, ...]

    @property
    def total_idle(self) -> int:
        return sum(line.idle for line in self.lines)

    def idle_of(self, seat_type: str) -> int:
        return sum(line.idle for line in self.lines if line.seat_type == seat_type)

    def as_dict(self) -> dict[str, Any]:
        return {
            "renews_at": self.renews_at,
            "total_idle": self.total_idle,
            "lines": [line.as_dict() for line in self.lines],
        }


def cached_pending_items(raw: Any) -> list[dict] | None:
    """member_cache.pending_json → 待接受邀请列表。没有缓存行（None）、读不出、不是对象列表 = 未知。"""
    if raw is None:
        return None
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None
    return items


def _renewal_instant(team: Mapping[str, Any], now: datetime) -> datetime | None:
    """会续费、且续费时间落在 (now, now + 窗口] 里才返回续费时间。"""
    will_renew = team.get("will_renew")
    if isinstance(will_renew, bool):
        will_renew = int(will_renew)
    if will_renew != 1:
        return None
    # 定时同步已挂起 = 快照冻住了，不拿它判断。
    if team.get("sync_suspended_at"):
        return None
    until = parse_active_until(team.get("active_until"))
    if until is None or not (now < until <= now + RENEWAL_REMINDER_WINDOW):
        return None
    return until


def renewal_idle_seats(
    team: Mapping[str, Any],
    pending_json: Any,
    *,
    now: Optional[datetime] = None,
) -> RenewalIdleSeats | None:
    """续费窗口里的空闲计费席位。``team`` 是 teams 表的一行（原始列），``pending_json`` 是
    member_cache.pending_json 原值（没有缓存行传 None）。

    每个计费类型：空闲 = 续费席位（renewal_requested，没给按 paid）− 在用（seat_type_counts）
    − 待接受邀请（没带类型的邀请对每个计费类型都算一份），不低于 0。某个类型的容量、
    在用数缺失或 renewal_requested 不可信，这个类型不算；待接受邀请名单未知，整个 Team 不算。
    不在窗口、不续费、或者合计没有空闲，返回 None。
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    until = _renewal_instant(team, current)
    if until is None:
        return None
    capacity = cached_seat_capacity(team.get("seat_capacity_json"))
    counts = cached_seat_type_counts(team.get("seat_type_counts_json"))
    pending_items = cached_pending_items(pending_json)
    if not capacity or not counts or pending_items is None:
        return None

    pending_data = {"items": pending_items}
    lines: list[IdleSeatLine] = []
    for seat_type in BILLED_SEAT_TYPES:
        entry = capacity.get(seat_type)
        in_use = counts.get(seat_type)
        if entry is None or in_use is None:
            continue
        if "renewal_requested" in entry:
            renewing = entry["renewal_requested"]
            if renewing is None:
                continue
        else:
            renewing = entry["paid"]
        if renewing == 0 and entry["paid"] == 0:
            continue
        pending = pending_count_from_api(pending_data, seat_type)
        lines.append(
            IdleSeatLine(
                seat_type=seat_type,
                paid=entry["paid"],
                renewing=renewing,
                in_use=in_use,
                pending=pending,
                idle=max(0, renewing - in_use - pending),
            )
        )
    result = RenewalIdleSeats(
        renews_at=until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        lines=tuple(lines),
    )
    return result if result.total_idle > 0 else None


# ── Telegram ─────────────────────────────────────────────────────────────

def _format_amount(amount: float, currency: str) -> str:
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {currency}"


def _seat_line(line: IdleSeatLine) -> str:
    icon = _SEAT_ICONS.get(line.seat_type, "💺")
    paid = f"已付 {line.paid}"
    if line.renewing != line.paid:
        paid += f" · 续费 {line.renewing}"
    return (
        f"{icon} {seat_type_label(line.seat_type)}：{paid} · 在用 {line.in_use} · "
        f"待接受 {line.pending} · 空闲 {line.idle}"
    )


def _cost_line(team: Mapping[str, Any], result: RenewalIdleSeats) -> str:
    """空闲席位续费后多花的钱，按这个 Team 每种席位的真实单价和币种（不含税）。月付写每月；
    年付写月均和一年的钱（一席一年 = 年付月价 × 12，按公开定价推断，以账单为准）。计费周期
    未知或单价对不上就说未知，不估算缺失的价格。"""
    parts: list[str] = []
    currency = str(team.get("billing_currency") or "").strip().upper()
    period = priced_period(team)
    upstream_priced = False
    for seat_type in (DEFAULT_SEAT_TYPE, PREMIUM_SEAT_TYPE):
        idle = result.idle_of(seat_type)
        if idle <= 0:
            continue
        label = seat_type_label(seat_type)
        price = seat_price_per_month(team, seat_type)
        if price is not None and currency:
            monthly = idle * price
            if period == "yearly":
                annual = _format_amount(monthly * MONTHS_PER_PERIOD["yearly"], currency)
                parts.append(f"{_format_amount(monthly, currency)}/月（{label}，年付，一年 {annual}）")
            else:
                parts.append(f"{_format_amount(monthly, currency)}/月（{label}）")
            upstream_priced = True
        else:
            parts.append(f"{label} 单价未知")
    line = "💸 续费后约多花：" + " + ".join(parts)
    # 上游给的单价都不含税（ChatGPT 后台写的是「+ 税费/月」）。
    return f"{line}，税费另计" if upstream_priced else line


def render_reminder_text(team: Mapping[str, Any], result: RenewalIdleSeats) -> str:
    name = str(team.get("name") or team.get("id") or "未知 Team")
    renews_at = parse_active_until(result.renews_at)
    when = renews_at.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M") if renews_at else result.renews_at
    rows = [
        f"👤 Owner：{mask_email_for_notice(team.get('owner_email'))}",
        f"📅 续费：{when}（北京时间）",
        *(_seat_line(line) for line in result.lines),
        _cost_line(team, result),
    ]
    card = detail_card(f"⏰ 续费前有空闲席位 · {name}", rows)
    return "\n".join([card, "", WHY_LINE, ACTION_LINE])


def _sent_count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    return value if isinstance(value, int) else 0


def _log_reminder_sync(team_id: str, detail: str, result: str, error_message: Optional[str]) -> None:
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            conn.execute(
                """INSERT INTO operation_logs
                   (team_id, action, target_email, detail, result, error_message, trigger_type, created_at)
                   VALUES (?, ?, NULL, ?, ?, ?, 'scheduler', ?)""",
                (team_id, LOG_ACTION, detail, result, error_message,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("renewal reminder: writing the operation log failed", exc_info=True)


def remind_team_sync(
    team: Mapping[str, Any],
    pending_json: Any,
    *,
    now: Optional[datetime] = None,
) -> dict | None:
    """判定一个 Team 并在需要时提醒。不该提醒时静默关掉这一族旧事件，返回 None。

    日志 ``renewal_idle_seat_reminder``：真送达时写一行；这一期第一次判定到却没送达
    （Telegram 没开、没有管理员、网络失败）也写一行，之后每轮静默重试，不重复写。
    """
    team_id = str(team.get("id") or "")
    if not team_id:
        return None
    result = renewal_idle_seats(team, pending_json, now=now)
    if result is None:
        close_incident_family_sync(
            team_id, RENEWAL_ALERT_KEY_PREFIX, forget_after=RENEWAL_ALERT_INTERVAL
        )
        return None

    alert_key = RENEWAL_ALERT_KEY_PREFIX + result.renews_at
    close_incident_family_sync(
        team_id,
        RENEWAL_ALERT_KEY_PREFIX,
        keep_alert_key=alert_key,
        forget_after=RENEWAL_ALERT_INTERVAL,
    )
    idle_pairs = ", ".join(f"idle_{line.seat_type}={line.idle}" for line in result.lines)
    text = render_reminder_text(team, result)
    outcome = report_team_failure_sync(
        team_id,
        alert_key,
        idle_pairs,
        source="renewal_reminder",
        notify_interval=RENEWAL_ALERT_INTERVAL,
        render=lambda _is_reminder: text,
        # 运行时再取模块里的 notify_admins_sync，测试可以替换。
        notify=lambda message: _sent_count(notify_admins_sync(message)),
        log_delivery=False,
    )
    delivered = int(outcome.get("notified") or 0)
    detail = f"renews_at={result.renews_at}, {idle_pairs}, delivered_to={delivered}"
    if delivered > 0:
        _log_reminder_sync(team_id, detail, "success", None)
    elif outcome.get("reason") == "not_delivered" and outcome.get("failure_count") == 1:
        _log_reminder_sync(team_id, detail, "skipped", "Telegram 未送达，下一轮重试")
    return {**outcome, "renews_at": result.renews_at, "total_idle": result.total_idle}


def run_renewal_idle_seat_reminders_sync(*, now: Optional[datetime] = None) -> dict:
    """定时任务入口：所有 Team 判一遍。返回 {"due": 在窗口里有空闲的 Team 数, "sent": 本轮送达数}。"""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT t.*, mc.pending_json AS _pending_json, mc.team_id IS NOT NULL AS _has_cache
               FROM teams t LEFT JOIN member_cache mc ON mc.team_id = t.id"""
        ).fetchall()
    finally:
        conn.close()

    due = 0
    sent = 0
    for row in rows:
        team = dict(row)
        has_cache = team.pop("_has_cache", 0)
        pending_raw = team.pop("_pending_json", None)
        # 没有缓存行 = 待接受邀请未知（None）；有行但 pending_json 是 NULL 也按未知。
        pending_json = pending_raw if has_cache else None
        try:
            outcome = remind_team_sync(team, pending_json, now=now)
        except Exception:
            logger.warning("renewal reminder failed for team=%s", team.get("id"), exc_info=True)
            continue
        if outcome is not None:
            due += 1
            if int(outcome.get("notified") or 0) > 0:
                sent += 1
    return {"due": due, "sent": sent}
