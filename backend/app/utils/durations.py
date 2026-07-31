import re
from datetime import datetime, timedelta, timezone
from typing import Optional


DURATION_RE = re.compile(r"^\s*(\d+)\s*([mhd])\s*$", re.IGNORECASE)
NEVER_VALUES = {"never", "none", "null", "infinite", "infinity", "forever", "永久", "∞"}


class DurationError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_duration(raw: str, *, allow_never: bool = False) -> str:
    value = (raw or "").strip().lower()
    if allow_never and value in NEVER_VALUES:
        return "never"

    match = DURATION_RE.match(value)
    if not match:
        suffix = " or never" if allow_never else ""
        raise DurationError(f"Invalid duration: {raw}. Use 3m, 12h, 7d, 30d{suffix}")

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        raise DurationError("Duration must be greater than 0")
    return f"{amount}{unit}"


def duration_to_timedelta(duration: str) -> timedelta:
    match = DURATION_RE.match(duration)
    if not match:
        raise DurationError(f"Invalid duration: {duration}")

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    raise DurationError(f"Unsupported duration unit: {unit}")


def expiry_from_duration(duration: str, *, base: Optional[datetime] = None) -> Optional[datetime]:
    if duration == "never":
        return None
    return (base or utc_now()) + duration_to_timedelta(duration)


def parse_optional_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
