"""
成员缓存服务

职责：
- 读写 member_cache 表（成员列表快照）
- 写入 member_watch 表（变动监视任务）

缓存策略（懒加载）：
  首次打开成员面板时写入缓存。
  邀请/踢人/撤邀请操作后创建监视任务，由 scheduler.member_watch_job 轮询刷新。
  日常不主动拉取，零额外 API 开销。
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import HTTPException, status

from .database import get_db
from .chatgpt_client import ChatGPTClient
from .chatgpt_limiter import run_chatgpt_call
from .services.seat_capacity import update_member_seat_usage_cache
from .services.seat_holds import reconcile_seat_holds, reconcile_seat_holds_sync
from .services.snapshot_pages import SnapshotPageAccumulator, SnapshotPageError

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


def snapshot_fetch_started_now() -> str:
    """一份完整快照的 fetch_started_at：在这次刷新的第一个上游列表请求（成员或邀请）发出之前取。

    固定带微秒，库里的字符串按字典序比较就是按时间比较。
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# 完整快照（成员 + 待接受邀请两份名单都拉全了）的唯一写法。只允许拉取开始得更晚（或同时）的
# 快照覆盖已有行：一次卡在半路的旧刷新晚些写回时，不能盖掉中途另一次刷新拿到的新名单（比如
# 管理员刚把人切回 ChatGPT）。旧行 fetch_started_at 为 NULL = 不知道，按最旧处理。
# 只改一行里成员字段的原地修改（到期时间、巡逻建基线）不走这里，也不动 fetch_started_at。
_SNAPSHOT_UPSERT_SQL = """
    INSERT INTO member_cache (team_id, members_json, pending_json, updated_at, fetch_started_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(team_id) DO UPDATE SET
        members_json     = excluded.members_json,
        pending_json     = excluded.pending_json,
        updated_at       = excluded.updated_at,
        fetch_started_at = excluded.fetch_started_at
    WHERE member_cache.fetch_started_at IS NULL
       OR excluded.fetch_started_at >= member_cache.fetch_started_at
"""


def _snapshot_params(team_id: str, members: list, pending_invites: list,
                     updated_at: str, fetch_started_at: Optional[str]) -> tuple:
    return (
        team_id,
        json.dumps(members, ensure_ascii=False),
        json.dumps(pending_invites, ensure_ascii=False),
        updated_at,
        fetch_started_at,
    )


def store_member_snapshot_sync(
    conn: sqlite3.Connection,
    team_id: str,
    members: list,
    pending_invites: list,
    fetch_started_at: str,
    *,
    updated_at: Optional[str] = None,
) -> bool:
    """同步连接写一份完整快照，再拿它给持久席位占用对账。不提交，由调用方随快照一起提交。

    返回快照有没有写进缓存（库里已有一份开始得更晚的快照时为 False）。对账出错只记日志，
    不影响刷新本身。
    """
    now = updated_at or datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        _SNAPSHOT_UPSERT_SQL,
        _snapshot_params(team_id, members, pending_invites, now, fetch_started_at),
    )
    written = cursor.rowcount > 0
    try:
        reconcile_seat_holds_sync(conn, team_id, members, pending_invites, fetch_started_at)
    except Exception:
        logger.exception("seat hold reconciliation failed for team %s", team_id)
    return written


async def write_member_cache(
    team_id: str,
    members: list,
    pending_invites: list,
    *,
    fetch_started_at: Optional[str],
) -> tuple[str, bool]:
    """将从 API 获取的完整成员快照写入缓存，返回 (写入时间, 是否写进去了)。

    库里已有一份拉取开始得更晚的快照时不覆盖（见 _SNAPSHOT_UPSERT_SQL）。
    """
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            _SNAPSHOT_UPSERT_SQL,
            _snapshot_params(team_id, members, pending_invites, now, fetch_started_at),
        )
        written = cursor.rowcount > 0
        await db.commit()
    return now, written


def _apply_cached_expiry(
    members: list,
    pending_invites: list,
    *,
    user_id: str,
    email: str,
    expires_at: Optional[str],
) -> bool:
    """把 ``expires_at`` 写进缓存名单里对应的那一条（就地改）。返回有没有改到。"""
    changed = False
    for member in members:
        member_id = (member.get("id") or member.get("user_id") or "").strip()
        member_email = (member.get("email") or "").strip().lower()
        # A caller with a user id has already resolved the canonical
        # Team member.  Do not OR-match its email against another row.
        member_matches = (
            member_id == user_id
            and (not email or member_email == email)
        ) if user_id else (email and member_email == email)
        if member_matches:
            member["expires_at"] = expires_at
            changed = True

    for invite in pending_invites:
        invite_email = (invite.get("email") or "").strip().lower()
        if not user_id and email and invite_email == email:
            invite["expires_at"] = expires_at
            changed = True
    return changed


# 读到写之间另一次刷新写进来的快照不能被这里盖掉：写回只在这一行仍是读到的那一版时生效
# （拉取开始时间和两份名单原文都没变），否则重读、在新的那一版上再改一次。
_EXPIRY_EDIT_ATTEMPTS = 5


async def update_cached_member_expiry(
    team_id: str,
    *,
    user_id: str = "",
    email: str = "",
    expires_at: Optional[str],
) -> None:
    """Update local member cache after an expiry-only change.

    Expiry is managed locally, so changing it should not require a live
    ChatGPT members refresh.  The edit is a compare-and-swap on the row it
    read: a snapshot written between the read and the write is kept and the
    edit is re-applied on top of it.
    """
    normalized_user_id = user_id or ""
    normalized_email = (email or "").strip().lower()

    for _attempt in range(_EXPIRY_EDIT_ATTEMPTS):
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT members_json, pending_json, fetch_started_at FROM member_cache WHERE team_id = ?",
                (team_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if not row:
                return
            members_json, pending_json = row["members_json"], row["pending_json"]
            fetch_started_at = row["fetch_started_at"]

            members = json.loads(members_json or "[]")
            pending_invites = json.loads(pending_json or "[]")
            if not _apply_cached_expiry(
                members,
                pending_invites,
                user_id=normalized_user_id,
                email=normalized_email,
                expires_at=expires_at,
            ):
                return

            cursor = await db.execute(
                """UPDATE member_cache SET members_json = ?, pending_json = ?
                   WHERE team_id = ?
                     AND fetch_started_at IS ?
                     AND members_json IS ?
                     AND pending_json IS ?""",
                (
                    json.dumps(members, ensure_ascii=False),
                    json.dumps(pending_invites, ensure_ascii=False),
                    team_id,
                    fetch_started_at,
                    members_json,
                    pending_json,
                ),
            )
            swapped = cursor.rowcount > 0
            await db.commit()
        if swapped:
            return
    logger.warning(
        "member cache expiry edit for team %s skipped: the cached snapshot kept changing", team_id
    )


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


# ── 实时拉取并写缓存（供 GET /members 首次调用） ─────────────────────────────

def _api_items(data: dict, *fallback_keys: str) -> list:
    """取出上游返回里的列表字段，取不到就失败关闭。

    空 Team 返回的是 ``{"items": []}``——列表存在、只是没有元素。而 ``{}``、
    ``{"items": null}``、被截断的 JSON 里根本没有这个字段：那是"没拿到名单"，
    不是"名单为空"。以前这里返回 ``[]``，上层据此判定"这个邮箱不在任何 Team"，
    可能拿用户花钱的兑换码把人邀请到另一个 Team 去。宁可 502 让他重试。
    """
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream returned a non-object response",
        )
    for key in ("items",) + fallback_keys:
        items = data.get(key)
        if isinstance(items, list):
            return items
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            "Upstream response has no usable list field "
            f"(expected one of {('items',) + fallback_keys})"
        ),
    )


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
    """分页拉完整份名单；何时算完整只按 ``SnapshotPageAccumulator``（snapshot_pages 正本）。

    拉不全、读不懂一律 502（上一份缓存保留，这次刷新算失败）。上游报错页沿用原来的
    ``Failed to fetch <kind>: <上游错误>``，``is_auth_error`` 靠这段文字认 401。
    """
    pages = SnapshotPageAccumulator(*item_keys, limit=limit)

    for _ in range(MAX_FETCH_PAGES):
        data = await run_chatgpt_call(method, pages.next_offset, pages.limit)
        try:
            if pages.add(data):
                return pages.items
        except SnapshotPageError as exc:
            if isinstance(data, dict) and "error" in data:
                reason = exc.upstream_error
            else:
                reason = str(exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to fetch {kind}: {reason}",
            ) from exc

    # 翻到硬上限还没结束 = 这份名单是截断的。返回它等于对上层撒谎说"就这些人"，
    # 而"人不在名单里"会被当作可以另外发邀请的依据。同样失败关闭。
    logger.error(
        "%s 分页在 %d 页后仍未结束，判定为不完整名单（已取 %d 条）",
        kind,
        MAX_FETCH_PAGES,
        len(pages.items),
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Failed to fetch {kind}: pagination did not terminate",
    )

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

    fetch_started_at = snapshot_fetch_started_now()
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
    updated_at, written = await write_member_cache(
        team_id, members, pending, fetch_started_at=fetch_started_at
    )
    if written:
        await update_member_seat_usage_cache(team_id, members)
    try:
        await reconcile_seat_holds(team_id, members, pending, fetch_started_at)
    except Exception:
        logger.exception("seat hold reconciliation failed for team %s", team_id)

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
