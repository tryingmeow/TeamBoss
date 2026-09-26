from fastapi import HTTPException

from ..chatgpt_client import ChatGPTClient
from ..database import get_db


# auth_state='rejected' 时，同步/续期等必须实时访问上游的操作只会一直 401。
# 用 409 而不是 502：这不是上游偶发故障，重试没用，只有重新导入 session 才能恢复。
# 也不能用 401——前端把任何 401 当作管理员登录失效，会直接把人踢回登录页。
TEAM_AUTH_REJECTED_DETAIL = (
    "该 Team 的 ChatGPT 登录已失效：access token 已过期或被拒绝，自动刷新也拿不到新的 token。"
    "请重新导入该 Team 的 session 后再试。"
)


def team_auth_rejected_error() -> HTTPException:
    return HTTPException(status_code=409, detail=TEAM_AUTH_REJECTED_DETAIL)


async def is_team_auth_rejected(team_id: str) -> bool:
    try:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT auth_state FROM teams WHERE id = ?",
                (team_id,),
            )
            row = await cursor.fetchone()
    except Exception:
        # 只用来改善报错文案，查询失败时按原样报错即可。
        return False
    return bool(row) and row["auth_state"] == "rejected"


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
