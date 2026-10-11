from fastapi import HTTPException

from ..chatgpt_client import ChatGPTClient
from ..database import get_db
from ..proxy_resolve import ProxyUnavailableError, resolve_proxy_url
from .subscription_status import subscription_status


# auth_state='rejected' 时，同步/续期等必须实时访问上游的操作只会一直 401。
# 用 409 而不是 502：这不是上游偶发故障，重试没用，只有重新导入 session 才能恢复。
# 也不能用 401——前端把任何 401 当作管理员登录失效，会直接把人踢回登录页。
TEAM_AUTH_REJECTED_DETAIL = "登录已失效，请重新导入"


def team_auth_rejected_error() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "team_auth_rejected", "message": TEAM_AUTH_REJECTED_DETAIL},
    )


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


# 绑了代理但解析不出来：抛 ProxyUnavailableError，调用方要么转成下面的 503，
# 要么跳过这个 Team。任何情况下都不能退化成不带代理的直连。
get_proxy_url = resolve_proxy_url


def proxy_unavailable_error(exc: ProxyUnavailableError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": "team_proxy_unavailable", "message": f"该 Team 的代理不可用：{exc}"},
    )


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
        # session 已死（status=token_expired），只有重新导入才能恢复；
        # 复用 team_auth_rejected 的 409，避免前端把 401 当成管理员 key 失效。
        raise team_auth_rejected_error()

    try:
        proxy_url = await get_proxy_url(row["proxy_id"])
    except ProxyUnavailableError as exc:
        raise proxy_unavailable_error(exc) from exc
    return ChatGPTClient(row["access_token"], team_id, row["device_id"], proxy_url=proxy_url)


async def load_active_teams() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, name, access_token, device_id, proxy_id,
                      seats_in_use, seats_entitled, codex_count, chatgpt_count, created_at,
                      active_until, will_renew, auth_state
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


def team_login_rejected(team: dict) -> bool:
    """这个 Team 的登录已被上游明确拒绝（``auth_state='rejected'``）。

    这是"现在调用一定 401"的权威信号：只在刷新接口明确交不出新 token 时写入，换到新
    token 或重新导入时清掉（见 chatgpt_limiter._mark_team_auth_rejected / team_service）。
    status 刻意保持 'active'，所以 load_active_teams 仍会返回它。
    """
    return team.get("auth_state") == "rejected"


def subscription_lapsed(team: dict) -> bool:
    """订阅已到期，判定与管理员邀请一致（services/gpt_invites.py）。

    用原始判定，不做展示层的 stale 区分：active_until 已过就算到期，哪怕快照是
    陈旧的——发新邀请这种行为路径宁可错过一个 Team（fail-closed）。
    """
    return subscription_status(team.get("active_until"), bool(team.get("will_renew"))) == "expired"
