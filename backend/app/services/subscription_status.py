"""Derived subscription lifecycle state shared by API surfaces."""

from datetime import datetime, timezone
from typing import Optional


def parse_active_until(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def subscription_status(
    active_until: Optional[str],
    will_renew: bool,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Return ``renewing``, ``nonrenewing`` or ``expired``."""
    until = parse_active_until(active_until)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    if until is not None and until <= current:
        return "expired"
    return "renewing" if will_renew else "nonrenewing"


# 快照连续这么久没有一次全量成功同步，就视为陈旧：展示层不再拿旧的
# active_until 判「已到期」，改报 stale（数据未同步），避免误导。
SNAPSHOT_STALE_HOURS = 48


def snapshot_is_stale(
    last_full_sync_at: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> bool:
    synced = parse_active_until(last_full_sync_at)
    if synced is None:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return (current - synced).total_seconds() >= SNAPSHOT_STALE_HOURS * 3600


def subscription_status_display(
    active_until: Optional[str],
    will_renew: bool,
    last_full_sync_at: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> str:
    """展示层状态：陈旧快照不判到期，返回 ``stale``。

    仅供 UI/通知使用；邀请派发等行为路径仍用 :func:`subscription_status`
    的原始判定（陈旧时按已到期处理，fail-closed）。
    """
    state = subscription_status(active_until, will_renew, now=now)
    if state == "expired" and snapshot_is_stale(last_full_sync_at, now=now):
        return "stale"
    return state
