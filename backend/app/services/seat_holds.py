"""本文件是「持久席位占用（seat_holds 表）」的正本。

一个邀请 / 切换已经发给 ChatGPT（成功、结果不明、还在待接受），但上游的空位数还不一定扣掉了它：
这时在库里占住这个 Team 的一个该类型席位，直到某次完整的成员快照证明它落在了哪里，或者
确认它没落地。进程内的 15 分钟预留（team_locks）重启就没了、到点就放，而 Premium 的待接受邀请
可能不带 seat_type、上游 available 也可能不扣它，所以 Premium 必须靠这里撑到对账。

规则：
- 一个邮箱在一个 Team 同时只占一份（主键 team_id + email），再占一次就刷新类型和时间。
- 一行的「版本」= 它存下的 ``(seat_type, created_at)``。``hold_seat`` 每次（新占或改占）
  都写一个带微秒的新 ``created_at`` 并把它返回。对账只删它选中的那个版本：选中之后、删除之前
  被重新占过（类型或时间变了）的行留着。
- 放掉只有两条路：上游明确拒绝了这次邀请（调用方 ``release_seat_hold``），或者对账
  （``reconcile_seat_holds*``）。结果不明的一律算占着。
- 对账只用完整的快照（成员 + 待接受邀请两份名单都分页拉全了，见 snapshot_pages），设这份
  快照的拉取开始时间为 S、一行占用为 (类型 T, 占下时间 C)：
  * C 或 S 读不出来，或者 C >= S → 留着（这次拉取开始之后才占的，快照不可能反映它）；
  * 名单里有这个邮箱（正式成员或待接受邀请），而且那一条的席位类型（规整后，缺失按
    ``default``）就是 T → 放掉：上游已经按这个类型计入了它；
  * 其他情况（名单里没有；或者在、但是别的类型，包括不带类型、按 ``default`` 记下的待接受
    邀请）→ 只有 S >= C + ``HOLD_ABSENT_GRACE_SECONDS`` 时才放掉（邀请 / 切换没落地、被撤、
    被拒，或者落成了别的类型）；还在宽限期内就继续占着。人「在」本身不是证据：一次超时的
    Codex → Premium 切换之后，名单里这个人可能还显示 Codex，而切换随时会生效、ChatGPT
    随时会加购。
- 规则对每一行都一样，不论类型（Premium 的占用、兑换结果不明时占的 ``default``）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..database import get_db
from ..seat_types import normalize_seat_type

# 占下之后，拉取开始得至少晚这么久的完整快照里仍看不见这个类型，才算「没落地」。
# 与进程内预留的 15 分钟一致。
HOLD_ABSENT_GRACE_SECONDS = 900

# 一行占用的版本：(email, seat_type, created_at)，与库里存的完全一致。
HoldVersion = tuple[str, str, str]


def _norm_team(team_id: Any) -> str:
    return str(team_id or "").strip()


def _norm_email(email: Any) -> str:
    return str(email or "").strip().lower()


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _snapshot_seat_types(members: Iterable[Any] | None, pending: Iterable[Any] | None) -> dict[str, set[str]]:
    """快照里每个邮箱（小写）出现时带的席位类型（规整后）。"""
    seen: dict[str, set[str]] = {}
    for items in (members or (), pending or ()):
        for item in items:
            if isinstance(item, dict):
                email = _norm_email(item.get("email") or item.get("email_address"))
                if email:
                    seen.setdefault(email, set()).add(normalize_seat_type(item.get("seat_type")))
    return seen


def _holds_to_release(
    rows: Iterable[HoldVersion],
    members: Any,
    pending: Any,
    fetch_started_at: Any,
) -> list[HoldVersion]:
    """rows = [(email, seat_type, created_at)]。返回这份完整快照可以放掉的行版本。"""
    started = _parse_ts(fetch_started_at)
    if started is None or not isinstance(members, list) or not isinstance(pending, list):
        return []
    seen = _snapshot_seat_types(members, pending)
    grace = timedelta(seconds=HOLD_ABSENT_GRACE_SECONDS)
    release: list[HoldVersion] = []
    for email, seat_type, created_at in rows:
        created = _parse_ts(created_at)
        if created is None or created >= started:
            # 这次拉取开始之后才占的：快照不可能反映它。
            continue
        shown_with_held_type = normalize_seat_type(seat_type) in seen.get(_norm_email(email), set())
        if shown_with_held_type or started >= created + grace:
            release.append((email, seat_type, created_at))
    return release


_SELECT_HOLDS_SQL = "SELECT email, seat_type, created_at FROM seat_holds WHERE team_id = ?"
# 只删选中的那个版本：选中之后被重新占过（类型或时间变了）的行不动。
_DELETE_HOLD_VERSION_SQL = (
    "DELETE FROM seat_holds WHERE team_id = ? AND email = ? AND seat_type = ? AND created_at = ?"
)


async def hold_seat(team_id: str, email: str, seat_type: str, *, source: str = "") -> Optional[str]:
    """在库里占住 ``team_id`` 的一个 ``seat_type`` 席位，直到对账或明确拒绝。

    返回这次写下的 ``created_at``（这一行的新版本）；邮箱或 Team 为空、什么都没写时返回 None。
    """
    team_key, email_key = _norm_team(team_id), _norm_email(email)
    if not team_key or not email_key:
        return None
    seat = str(seat_type or "default").strip() or "default"
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    async with get_db() as db:
        await db.execute(
            """INSERT INTO seat_holds (team_id, email, seat_type, source, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(team_id, email) DO UPDATE SET
                   seat_type = excluded.seat_type,
                   source = excluded.source,
                   created_at = excluded.created_at""",
            (team_key, email_key, seat, str(source or ""), now),
        )
        await db.commit()
    return now


async def release_seat_hold(team_id: str, email: str) -> None:
    """上游明确拒绝了这次邀请 / 切换：放掉它占的席位。"""
    team_key, email_key = _norm_team(team_id), _norm_email(email)
    if not team_key or not email_key:
        return
    async with get_db() as db:
        await db.execute(
            "DELETE FROM seat_holds WHERE team_id = ? AND email = ?", (team_key, email_key)
        )
        await db.commit()


async def seat_hold_exists(team_id: str, email: str) -> bool:
    """``team_id`` 上这个邮箱是不是已经占着一份（不论类型）。"""
    team_key, email_key = _norm_team(team_id), _norm_email(email)
    if not team_key or not email_key:
        return False
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT 1 FROM seat_holds WHERE team_id = ? AND email = ?", (team_key, email_key)
        )
        return await cursor.fetchone() is not None


async def held_seat_emails(team_id: str, seat_type: str, *, exclude_email: str = "") -> set[str]:
    """``team_id`` 上占着 ``seat_type`` 的邮箱（小写），不含 ``exclude_email``。"""
    team_key = _norm_team(team_id)
    excluded = _norm_email(exclude_email)
    seat = str(seat_type or "default").strip() or "default"
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT email FROM seat_holds WHERE team_id = ? AND seat_type = ?", (team_key, seat)
        )
        rows = await cursor.fetchall()
    return {row[0] for row in rows if row[0] and row[0] != excluded}


def reconcile_seat_holds_sync(
    conn: sqlite3.Connection,
    team_id: str,
    members: Any,
    pending: Any,
    fetch_started_at: Any,
) -> int:
    """用一份完整快照对账（同步连接版）。不提交事务，由调用方随快照一起提交。返回实际删掉的行数。"""
    team_key = _norm_team(team_id)
    rows = conn.execute(_SELECT_HOLDS_SQL, (team_key,)).fetchall()
    release = _holds_to_release(
        [(row[0], row[1], row[2]) for row in rows], members, pending, fetch_started_at
    )
    deleted = 0
    for email, seat_type, created_at in release:
        cursor = conn.execute(_DELETE_HOLD_VERSION_SQL, (team_key, email, seat_type, created_at))
        deleted += max(cursor.rowcount, 0)
    return deleted


async def reconcile_seat_holds(
    team_id: str,
    members: Any,
    pending: Any,
    fetch_started_at: Any,
) -> int:
    """用一份完整快照对账（异步版，自己提交）。返回实际删掉的行数。"""
    team_key = _norm_team(team_id)
    async with get_db() as db:
        cursor = await db.execute(_SELECT_HOLDS_SQL, (team_key,))
        rows = await cursor.fetchall()
        release = _holds_to_release(
            [(row[0], row[1], row[2]) for row in rows], members, pending, fetch_started_at
        )
        deleted = 0
        for email, seat_type, created_at in release:
            cursor = await db.execute(
                _DELETE_HOLD_VERSION_SQL, (team_key, email, seat_type, created_at)
            )
            deleted += max(cursor.rowcount, 0)
        if release:
            await db.commit()
    return deleted
