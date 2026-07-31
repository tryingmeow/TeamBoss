from datetime import datetime, timezone
from typing import Optional

from ..database import get_db


def normalize_email(value: Optional[str]) -> str:
    return (value or "").strip().lower()


async def load_display_name_map() -> dict[str, str]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT email, system_display_name FROM user_display_names"
        )
        rows = await cursor.fetchall()
    return {
        normalize_email(row["email"]): (row["system_display_name"] or "").strip()
        for row in rows
        if (row["system_display_name"] or "").strip()
    }


async def get_display_name(email: Optional[str]) -> Optional[str]:
    key = normalize_email(email)
    if not key:
        return None
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT system_display_name FROM user_display_names WHERE email = ?",
            (key,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    value = (row["system_display_name"] or "").strip()
    return value or None


async def set_display_name(email: Optional[str], system_display_name: Optional[str]) -> Optional[str]:
    key = normalize_email(email)
    if not key:
        raise ValueError("email is required")

    value = (system_display_name or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        if value:
            await db.execute(
                """
                INSERT INTO user_display_names (email, system_display_name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    system_display_name = excluded.system_display_name,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
        else:
            await db.execute("DELETE FROM user_display_names WHERE email = ?", (key,))
        await db.commit()
    return value or None
