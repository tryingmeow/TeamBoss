from fastapi import HTTPException

from ..chatgpt_client import ChatGPTClient
from ..database import get_db


async def get_proxy_url(proxy_id: int | None) -> str | None:
    if not proxy_id:
        return None
    async with get_db() as db:
        cursor = await db.execute("SELECT url FROM proxies WHERE id = ?", (proxy_id,))
        row = await cursor.fetchone()
    return row["url"] if row else None


async def get_team_client(team_id: str) -> ChatGPTClient:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT access_token, device_id, status, proxy_id FROM teams WHERE id = ?",
            (team_id,),
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Team not found")
    if row["status"] == "token_expired":
        raise HTTPException(status_code=401, detail="Team token expired, please refresh")

    proxy_url = await get_proxy_url(row["proxy_id"])
    return ChatGPTClient(row["access_token"], team_id, row["device_id"], proxy_url=proxy_url)


async def load_active_teams() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, name, access_token, device_id, proxy_id,
                      seats_in_use, seats_entitled, codex_count, chatgpt_count, created_at
               FROM teams
               WHERE status = 'active'
               ORDER BY (
                   COALESCE(seats_entitled, 0)
                   - COALESCE(
                       chatgpt_count,
                       MAX(0, COALESCE(seats_in_use, 0) - COALESCE(codex_count, 0))
                   )
               ) DESC, created_at ASC"""
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]
