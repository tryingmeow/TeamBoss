"""本文件是「持久席位占用（seat_holds 表）」的正本。

一个邀请 / 切换已经发给 ChatGPT（成功、结果不明、还在待接受），但上游的空位数还不一定扣掉了它：
这时在库里占住这个 Team 的一个该类型席位，直到某次完整的成员快照「看见」了这个人，或者
确认他不在。进程内的 15 分钟预留（team_locks）重启就没了、到点就放，而 Premium 的待接受邀请
可能不带 seat_type、上游 available 也可能不扣它，所以 Premium 必须靠这里撑到对账。

规则：
- 一个邮箱在一个 Team 同时只占一份（主键 team_id + email），再占一次就刷新类型和时间。
- 放掉只有两条路：上游明确拒绝了这次邀请（调用方 ``release_seat_hold``），或者对账
  （``reconcile_seat_holds*``）。结果不明的一律算占着。
- 对账只用完整的快照（成员 + 待接受邀请两份名单都拉全了），并且只处理在这次拉取开始之前
  就占下的行：
  * 名单里有这个人（正式成员或待接受邀请，不论席位类型）→ 放掉，他已经按上游的类型计入
    那个类型的占用；
  * 名单里没有，而且占下已经超过 ``HOLD_ABSENT_GRACE_SECONDS`` → 放掉（邀请没落地、
    被撤或被拒）；
  * 名单里没有、但还在宽限期内 → 继续占着（上游名单可能还没反映出来）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..database import get_db

# 占下之后多久，完整快照里仍看不见这个人才算「没落地」。与进程内预留的 15 分钟一致。
HOLD_ABSENT_GRACE_SECONDS = 900


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


def _snapshot_emails(members: Iterable[Any] | None, pending: Iterable[Any] | None) -> set[str]:
    seen: set[str] = set()
    for items in (members or (), pending or ()):
        for item in items:
            if isinstance(item, dict):
                email = _norm_email(item.get("email") or item.get("email_address"))
                if email:
                    seen.add(email)
    return seen


def _holds_to_release(
    rows: Iterable[tuple[str, str]],
    members: Any,
    pending: Any,
    fetch_started_at: Any,
) -> list[str]:
    """rows = [(email, created_at)]。返回这份快照可以放掉的邮箱。"""
    started = _parse_ts(fetch_started_at)
    if started is None or not isinstance(members, list) or not isinstance(pending, list):
        return []
    seen = _snapshot_emails(members, pending)
    grace = timedelta(seconds=HOLD_ABSENT_GRACE_SECONDS)
    release: list[str] = []
    for email, created_at in rows:
        created = _parse_ts(created_at)
        if created is None or created >= started:
            # 这次拉取开始之后才占的：快照不可能反映它。
            continue
        if email in seen or created <= started - grace:
            release.append(email)
    return release


async def hold_seat(team_id: str, email: str, seat_type: str, *, source: str = "") -> None:
    """在库里占住 ``team_id`` 的一个 ``seat_type`` 席位，直到对账或明确拒绝。"""
    team_key, email_key = _norm_team(team_id), _norm_email(email)
    if not team_key or not email_key:
        return
    seat = str(seat_type or "default").strip() or "default"
    now = datetime.now(timezone.utc).isoformat()
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
    """用一份完整快照对账（同步连接版）。不提交事务，由调用方随快照一起提交。返回放掉的行数。"""
    team_key = _norm_team(team_id)
    rows = conn.execute(
        "SELECT email, created_at FROM seat_holds WHERE team_id = ?", (team_key,)
    ).fetchall()
    release = _holds_to_release(
        [(row[0], row[1]) for row in rows], members, pending, fetch_started_at
    )
    for email in release:
        conn.execute(
            "DELETE FROM seat_holds WHERE team_id = ? AND email = ?", (team_key, email)
        )
    return len(release)


async def reconcile_seat_holds(
    team_id: str,
    members: Any,
    pending: Any,
    fetch_started_at: Any,
) -> int:
    """用一份完整快照对账（异步版，自己提交）。返回放掉的行数。"""
    team_key = _norm_team(team_id)
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT email, created_at FROM seat_holds WHERE team_id = ?", (team_key,)
        )
        rows = await cursor.fetchall()
        release = _holds_to_release(
            [(row[0], row[1]) for row in rows], members, pending, fetch_started_at
        )
        for email in release:
            await db.execute(
                "DELETE FROM seat_holds WHERE team_id = ? AND email = ?", (team_key, email)
            )
        if release:
            await db.commit()
    return len(release)
