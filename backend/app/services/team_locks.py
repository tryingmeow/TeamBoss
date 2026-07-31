import asyncio
import secrets
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import AsyncIterator

from ..database import get_db


_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_reservations: dict[tuple[str, str], float] = {}
_reservations_guard = asyncio.Lock()
DEFAULT_RESERVATION_TTL_SECONDS = 900
MEMBER_OPERATION_TTL_SECONDS = 600


@asynccontextmanager
async def team_invite_lock(team_id: str) -> AsyncIterator[None]:
    """Serialize capacity check + invite for one team within this service process."""
    key = str(team_id or "").strip()
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock

    async with lock:
        yield


def _reservation_key(team_id: str, email: str) -> tuple[str, str]:
    return str(team_id or "").strip(), str(email or "").strip().lower()


def _drop_expired_reservations(now: float) -> None:
    expired = [key for key, expires_at in _reservations.items() if expires_at <= now]
    for key in expired:
        _reservations.pop(key, None)


async def reserve_default_seat(
    team_id: str,
    email: str,
    ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
) -> None:
    """Temporarily reserve one default seat while ChatGPT member caches catch up."""
    key = _reservation_key(team_id, email)
    if not key[0] or not key[1]:
        return
    async with _reservations_guard:
        now = monotonic()
        _drop_expired_reservations(now)
        _reservations[key] = now + max(1, ttl_seconds)


async def release_default_seat_reservation(team_id: str, email: str) -> None:
    key = _reservation_key(team_id, email)
    async with _reservations_guard:
        _drop_expired_reservations(monotonic())
        _reservations.pop(key, None)


async def reserved_default_seats(team_id: str, *, exclude_email: str = "") -> int:
    key_team = str(team_id or "").strip()
    excluded = str(exclude_email or "").strip().lower()
    async with _reservations_guard:
        _drop_expired_reservations(monotonic())
        return sum(
            1
            for reserved_team, reserved_email in _reservations
            if reserved_team == key_team and reserved_email != excluded
        )


def _member_operation_key(team_id: str, email: str = "", user_id: str = "") -> str:
    normalized_email = str(email or "").strip().lower()
    normalized_user_id = str(user_id or "").strip()
    identity = f"email:{normalized_email}" if normalized_email else f"user:{normalized_user_id}"
    return f"{str(team_id or '').strip()}|{identity}"


def try_acquire_member_operation_sync(
    conn,
    team_id: str,
    *,
    email: str = "",
    user_id: str = "",
    operation: str,
    ttl_seconds: int = MEMBER_OPERATION_TTL_SECONDS,
) -> str | None:
    """Try to claim one member before a destructive remote operation."""
    key = _member_operation_key(team_id, email, user_id)
    if key.endswith("email:") or key.endswith("user:"):
        return None
    now = datetime.now(timezone.utc)
    owner = secrets.token_hex(16)
    conn.execute(
        "DELETE FROM member_operation_claims WHERE expires_at <= ?",
        (now.isoformat(),),
    )
    cursor = conn.execute(
        """INSERT OR IGNORE INTO member_operation_claims
           (operation_key, team_id, email, user_id, operation, owner_token,
            expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            key,
            str(team_id or "").strip(),
            str(email or "").strip().lower(),
            str(user_id or "").strip(),
            operation,
            owner,
            (now + timedelta(seconds=max(1, ttl_seconds))).isoformat(),
            now.isoformat(),
        ),
    )
    conn.commit()
    return owner if cursor.rowcount == 1 else None


def release_member_operation_sync(conn, team_id: str, owner: str, *, email: str = "", user_id: str = "") -> None:
    key = _member_operation_key(team_id, email, user_id)
    conn.execute(
        "DELETE FROM member_operation_claims WHERE operation_key = ? AND owner_token = ?",
        (key, owner),
    )
    conn.commit()


@contextmanager
def member_operation_claim_sync(
    conn,
    team_id: str,
    *,
    email: str = "",
    user_id: str = "",
    operation: str,
    ttl_seconds: int = MEMBER_OPERATION_TTL_SECONDS,
):
    owner = try_acquire_member_operation_sync(
        conn,
        team_id,
        email=email,
        user_id=user_id,
        operation=operation,
        ttl_seconds=ttl_seconds,
    )
    try:
        yield owner is not None
    finally:
        if owner is not None:
            release_member_operation_sync(
                conn,
                team_id,
                owner,
                email=email,
                user_id=user_id,
            )


@asynccontextmanager
async def member_operation_claim(
    team_id: str,
    *,
    email: str = "",
    user_id: str = "",
    operation: str,
    ttl_seconds: int = MEMBER_OPERATION_TTL_SECONDS,
):
    key = _member_operation_key(team_id, email, user_id)
    owner = secrets.token_hex(16)
    now = datetime.now(timezone.utc)
    acquired = False
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "DELETE FROM member_operation_claims WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        cursor = await db.execute(
            """INSERT OR IGNORE INTO member_operation_claims
               (operation_key, team_id, email, user_id, operation, owner_token,
                expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                key,
                str(team_id or "").strip(),
                str(email or "").strip().lower(),
                str(user_id or "").strip(),
                operation,
                owner,
                (now + timedelta(seconds=max(1, ttl_seconds))).isoformat(),
                now.isoformat(),
            ),
        )
        acquired = cursor.rowcount == 1
        await db.commit()
    try:
        yield acquired
    finally:
        if acquired:
            async with get_db() as db:
                await db.execute(
                    """DELETE FROM member_operation_claims
                       WHERE operation_key = ? AND owner_token = ?""",
                    (key, owner),
                )
                await db.commit()
