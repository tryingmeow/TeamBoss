"""
成员缓存服务

职责：
- 读写 member_cache 表（成员列表快照）
- 写入 member_watch 表（变动监视任务）
- 为搜索提供成员邮件列表

缓存策略（懒加载）：
  首次打开成员面板时写入缓存。
  邀请/踢人/撤邀请操作后创建监视任务，由 scheduler.member_watch_job 轮询刷新。
  日常不主动拉取，零额外 API 开销。
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import HTTPException, status

from .database import get_db
from .chatgpt_client import ChatGPTClient
from .chatgpt_limiter import run_chatgpt_call
from .services.seat_capacity import update_member_seat_usage_cache

logger = logging.getLogger(__name__)


# ── 缓存读写 ────────────────────────────────────────────────────────────────

async def get_cached_members(team_id: str) -> Optional[dict]:
    """
    返回缓存的成员数据，格式：
      {"members": [...], "pending_invites": [...], "updated_at": "..."}
    若无缓存则返回 None。
    """
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT members_json, pending_json, updated_at FROM member_cache WHERE team_id = ?",
            (team_id,)
        )
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "members": json.loads(row["members_json"] or "[]"),
        "pending_invites": json.loads(row["pending_json"] or "[]"),
        "updated_at": row["updated_at"],
    }


async def write_member_cache(team_id: str, members: list, pending_invites: list) -> str:
    """将从 API 获取的成员数据写入缓存。"""
    now = datetime.now(timezone.utc).isoformat()
    members_json = json.dumps(members, ensure_ascii=False)
    pending_json = json.dumps(pending_invites, ensure_ascii=False)
    async with get_db() as db:
        await db.execute("""
            INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                members_json = excluded.members_json,
                pending_json = excluded.pending_json,
                updated_at   = excluded.updated_at
        """, (team_id, members_json, pending_json, now))
        await db.commit()
    return now


async def update_cached_member_expiry(
    team_id: str,
    *,
    user_id: str = "",
    email: str = "",
    expires_at: Optional[str],
) -> None:
    """Update local member cache after an expiry-only change.

    Expiry is managed locally, so changing it should not require a live
    ChatGPT members refresh.
    """
    normalized_user_id = user_id or ""
    normalized_email = (email or "").strip().lower()

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT members_json, pending_json FROM member_cache WHERE team_id = ?",
            (team_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return

        members = json.loads(row["members_json"] or "[]")
        pending_invites = json.loads(row["pending_json"] or "[]")

        changed = False
        for member in members:
            member_email = (member.get("email") or "").strip().lower()
            if (
                normalized_user_id
                and member.get("id") == normalized_user_id
            ) or (
                normalized_email
                and member_email == normalized_email
            ):
                member["expires_at"] = expires_at
                changed = True

        for invite in pending_invites:
            invite_email = (invite.get("email") or "").strip().lower()
            if normalized_email and invite_email == normalized_email:
                invite["expires_at"] = expires_at
                changed = True

        if not changed:
            return

        await db.execute(
            "UPDATE member_cache SET members_json = ?, pending_json = ? WHERE team_id = ?",
            (
                json.dumps(members, ensure_ascii=False),
                json.dumps(pending_invites, ensure_ascii=False),
                team_id,
            ),
        )
        await db.commit()


async def get_cached_member_emails(team_id: str) -> list[str]:
    """
    返回该 team 缓存中所有成员（active + pending）的邮件列表，供搜索使用。
    若无缓存返回空列表。
    """
    cached = await get_cached_members(team_id)
    if not cached:
        return []
    emails = set()
    for m in cached["members"]:
        if m.get("email"):
            emails.add(m["email"].lower())
    for p in cached["pending_invites"]:
        if p.get("email"):
            emails.add(p["email"].lower())
    return list(emails)


# ── 监视任务 ─────────────────────────────────────────────────────────────────

WATCH_TIMEOUT_MINUTES = 30  # 监视最长时间，超时后强制刷新缓存并退出


async def add_member_watch(
    team_id: str,
    reason: str,           # 'invite' | 'kick'
    target_email: Optional[str] = None,
    target_user_id: Optional[str] = None,
    tg_chat_id: Optional[str] = None,
    tg_message_id: Optional[int] = None,
):
    """
    创建成员变动监视任务。
    - invite：邀请后监视，直到邀请对象出现在 members 中（已接受）
    - kick：踢人/撤邀请后监视，直到目标不再出现在 members / pending_invites 中
    tg_chat_id / tg_message_id 非空时，条件满足后自动编辑对应 TG 消息。
    """
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=WATCH_TIMEOUT_MINUTES)).isoformat()
    now_iso = now.isoformat()

    async with get_db() as db:
        # 若同一 team + 同一 target 已有进行中的监视，先关闭旧的再插新的
        if target_email:
            await db.execute("""
                UPDATE member_watch SET done = 1
                WHERE team_id = ? AND target_email = ? AND done = 0
            """, (team_id, target_email))
        elif target_user_id:
            await db.execute("""
                UPDATE member_watch SET done = 1
                WHERE team_id = ? AND target_user_id = ? AND done = 0
            """, (team_id, target_user_id))

        await db.execute("""
            INSERT INTO member_watch
                (team_id, reason, target_email, target_user_id, started_at, expires_at, done,
                 tg_chat_id, tg_message_id)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (team_id, reason, target_email, target_user_id, now_iso, expires_at,
              tg_chat_id, tg_message_id))
        await db.commit()


async def mark_watch_done(watch_id: int):
    async with get_db() as db:
        await db.execute("UPDATE member_watch SET done = 1 WHERE id = ?", (watch_id,))
        await db.commit()


async def get_pending_watches() -> list[dict]:
    """获取所有尚未完成的监视任务。"""
    async with get_db() as db:
        cursor = await db.execute("""
            SELECT mw.*, t.access_token, t.device_id
            FROM member_watch mw
            JOIN teams t ON mw.team_id = t.id
            WHERE mw.done = 0 AND t.status = 'active'
        """)
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── 实时拉取并写缓存（供 GET /members 首次调用） ─────────────────────────────

def _api_items(data: dict, *fallback_keys: str) -> list:
    for key in ("items",) + fallback_keys:
        items = data.get(key)
        if isinstance(items, list):
            return items
    return []


def _raise_fetch_error(kind: str, data: dict) -> None:
    if isinstance(data, dict) and "error" in data:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch {kind}: {data['error']}",
        )


# 上游返回满页且不给 total 时，翻页循环没有自然终点。给一个硬上限兜底，避免单次
# 同步一直占着刷新信号量、items 无限增长。10000 条远超任何真实 Team 的规模。
MAX_FETCH_PAGES = 100


async def _fetch_all_pages(method, kind: str, *item_keys: str, limit: int = 100) -> list:
    items: list = []
    offset = 0

    for _ in range(MAX_FETCH_PAGES):
        data = await run_chatgpt_call(method, offset, limit)
        _raise_fetch_error(kind, data)

        page_items = _api_items(data, *item_keys)
        items.extend(page_items)

        total = data.get("total")
        if len(page_items) < limit:
            break
        if isinstance(total, int) and len(items) >= total:
            break

        offset += limit
    else:
        logger.warning(
            "%s 分页在 %d 页后仍未结束，已停止翻页（已取 %d 条）",
            kind,
            MAX_FETCH_PAGES,
            len(items),
        )

    return items

def _build_members_list(members_data: dict, pending_data: dict, expiry_map: dict) -> tuple[list, list]:
    """从 API 返回值构建 members / pending_invites 两个列表（复用 routes/members.py 的逻辑）。"""
    members = []
    _raise_fetch_error("members", members_data)
    for m in _api_items(members_data, "users"):
        user_id = m.get("id", m.get("user_id", ""))
        email = m.get("email", "")
        expiry_info = expiry_map.get(user_id) or expiry_map.get(email.lower())
        members.append({
            "id": user_id,
            "email": email,
            "name": m.get("name"),
            "role": m.get("role", "standard-user"),
            "seat_type": m.get("seat_type", "default"),
            "is_owner": m.get("role") == "account-owner",
            "expires_at": expiry_info["expires_at"] if expiry_info else None,
            "first_seen_at": expiry_info.get("first_seen_at") if expiry_info else None,
            "source": expiry_info.get("source") if expiry_info else None,
            "created_time": m.get("created_time", m.get("created")),
            "status": "active",
        })

    pending = []
    _raise_fetch_error("pending invites", pending_data)
    for inv in _api_items(pending_data, "invites"):
        email = inv.get("email_address", inv.get("email", ""))
        expiry_info = expiry_map.get(email.lower())
        pending.append({
            "id": inv.get("id", ""),
            "email": email,
            "name": None,
            "role": inv.get("role", "standard-user"),
            "seat_type": inv.get("seat_type", "default"),
            "is_owner": False,
            "expires_at": expiry_info["expires_at"] if expiry_info else None,
            "first_seen_at": expiry_info.get("first_seen_at") if expiry_info else None,
            "source": expiry_info.get("source") if expiry_info else None,
            "created_time": inv.get("created_time", inv.get("created")),
            "status": "pending",
        })
    return members, pending


async def _fetch_and_cache_members_impl(team_id: str, client: ChatGPTClient) -> dict:
    """
    实时从 API 拉取成员数据，写入缓存，并返回结果。
    同时读取 member_expiry 表补充 expires_at 信息。
    """
    import asyncio

    member_items, pending_items = await asyncio.gather(
        _fetch_all_pages(client.get_members, "members", "users"),
        _fetch_all_pages(client.get_pending_invites, "pending invites", "invites"),
    )
    members_data = {"items": member_items, "total": len(member_items)}
    pending_data = {"items": pending_items, "total": len(pending_items)}

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM member_expiry WHERE team_id = ? AND kicked = 0", (team_id,)
        )
        expiry_rows = await cursor.fetchall()

    expiry_map: dict = {}
    for row in expiry_rows:
        row_dict = dict(row)
        if row["user_id"]:
            expiry_map[row["user_id"]] = row_dict
        if row["email"]:
            expiry_map[row["email"].lower()] = row_dict

    members, pending = _build_members_list(members_data, pending_data, expiry_map)
    updated_at = await write_member_cache(team_id, members, pending)
    await update_member_seat_usage_cache(team_id, members)

    return {
        "members": members,
        "pending_invites": pending,
        "total": len(members) + len(pending),
        "cached": False,
        "cached_at": updated_at,
    }


async def fetch_and_cache_members(team_id: str, client: ChatGPTClient) -> dict:
    """Refresh members and maintain authentication incident state.

    A member refresh is only one branch of a full Team sync, so it must not
    resolve the broader ``team_sync`` incident on its own.
    """
    from .services.team_health_alerts import (
        is_auth_error,
        report_team_failure,
        report_team_recovery,
    )

    try:
        result = await _fetch_and_cache_members_impl(team_id, client)
    except Exception as exc:
        await report_team_failure(
            team_id,
            "chatgpt_auth" if is_auth_error(exc) else "team_sync",
            exc,
            source="member_refresh",
        )
        try:
            setattr(exc, "team_health_reported", True)
        except Exception:
            pass
        raise

    await report_team_recovery(team_id, "chatgpt_auth", source="member_refresh")
    return result
