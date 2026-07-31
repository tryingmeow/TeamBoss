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
