"""Telegram 自动同步摘要。

摘要只在一次 data_sync_job 完整走完后尝试发送。设置里的 interval 是最短间隔；
如果自动同步本身更慢，则自然以下一次完整同步为准，避免推送半旧数据。
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..database import get_db_path
from .seat_capacity import member_seat_usage_from_members
from .tg_notify import notify_admins_sync

# 向后兼容：模块级占位符，测试可能会 patch 它
DB_PATH = None

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440
DISPLAY_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_interval(value: Optional[str], default: int = 15) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, MIN_INTERVAL_MINUTES), MAX_INTERVAL_MINUTES)


def build_summary_sync(now: Optional[datetime] = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conn = _connect()
    try:
        settings = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('sync_interval_minutes', 'patrol_kick_enabled')"
            ).fetchall()
        }
        sync_interval = _safe_interval(settings.get("sync_interval_minutes"))
        freshness_cutoff = now - timedelta(minutes=max(sync_interval * 2, 15))
        rows = conn.execute(
            """SELECT t.id, t.name, t.status, t.is_codex_enabled, t.seats_entitled,
                      mc.members_json, mc.updated_at AS cache_updated_at
                 FROM teams t
                 LEFT JOIN member_cache mc ON mc.team_id = t.id
                ORDER BY t.name, t.id"""
        ).fetchall()
    finally:
        conn.close()

    total = len(rows)
    active_rows = [row for row in rows if row["status"] == "active"]
    online = 0
    stale_or_error = total - len(active_rows)
    total_active_gpt = 0
    total_entitled = 0
    idle_teams = 0
    over_teams = 0
    over_people = 0
    watch_teams = 0
    codex_on = 0
    over_names: list[str] = []

    for row in active_rows:
        cache_at = _parse_datetime(row["cache_updated_at"])
        if cache_at and cache_at >= freshness_cutoff:
            online += 1
        else:
            stale_or_error += 1

        try:
            members = json.loads(row["members_json"] or "[]")
        except Exception:
            members = []
        if not isinstance(members, list):
            members = []
        usage = member_seat_usage_from_members(members)
        active_gpt = usage.active_chatgpt if usage is not None else 0
        entitled = int(row["seats_entitled"] or 0)
        total_active_gpt += active_gpt
        total_entitled += entitled
        if active_gpt < entitled:
            idle_teams += 1

        if bool(row["is_codex_enabled"]):
            codex_on += 1
            continue
        if active_gpt > entitled:
            over_teams += 1
            over_people += active_gpt - entitled
            over_names.append(str(row["name"] or row["id"]))
        else:
            watch_teams += 1

    patrol_state = "开启 ✅" if settings.get("patrol_kick_enabled") == "1" else "关闭 ⏸️"
    local_now = now.astimezone(DISPLAY_TZ)
    lines = [
        "📊 TeamBoss 状态",
        "╭────────────────────",
        f"│ 🏢 Team 总数　　　 {total}",
        f"│ 🟢 在线 / 正常　　 {online}",
        f"│ 🔴 异常 / 过期　　 {stale_or_error}",
        "├────────────────────",
        f"│ 💺 GPT 席位　　　  {total_active_gpt} / {total_entitled}",
        f"│ 🈳 有空位 Team　　 {idle_teams}",
        f"│ 💻 Codex 已开启　　{codex_on}",
        "├────────────────────",
        f"│ 🛡️ 巡逻自动踢人：{patrol_state}",
        f"│ ⚠️ 超员 Team　　　{over_teams}",
        f"│ 👤 超员人数　　　　{over_people}",
        f"│ 👀 观察 Team　　　{watch_teams}",
        "╰────────────────────",
        f"🕐 {local_now.strftime('%Y-%m-%d %H:%M')}（北京时间）",
    ]
    if over_names:
        shown = "、".join(over_names[:5])
        suffix = f" 等 {len(over_names)} 个" if len(over_names) > 5 else ""
        lines.extend(("", f"🚨 超员 Team：{shown}{suffix}"))
    return "\n".join(lines)


def maybe_send_summary_sync(*, force: bool = False, now: Optional[datetime] = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('tg_bot_enabled', 'tg_summary_enabled', 'tg_summary_interval_minutes', "
            " 'tg_summary_last_sent_at')"
        ).fetchall()
        settings = {row["key"]: row["value"] for row in rows}
    finally:
        conn.close()

    if settings.get("tg_bot_enabled") != "1":
        return {"sent": 0, "reason": "bot_disabled"}
    if not force and settings.get("tg_summary_enabled") != "1":
        return {"sent": 0, "reason": "summary_disabled"}

    interval = _safe_interval(settings.get("tg_summary_interval_minutes"))
    last_sent = _parse_datetime(settings.get("tg_summary_last_sent_at"))
    if not force and last_sent and now - last_sent < timedelta(minutes=interval):
        return {"sent": 0, "reason": "not_due", "next_after_minutes": interval}

    sent = notify_admins_sync(build_summary_sync(now=now))
    if sent <= 0:
        return {"sent": 0, "reason": "no_admin_delivery"}

    conn = _connect()
    try:
        now_iso = now.isoformat()
        conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            ("tg_summary_last_sent_at", now_iso, now_iso),
        )
        conn.execute(
            """INSERT INTO operation_logs
               (team_id, action, target_email, detail, result, error_message, trigger_type, created_at)
               VALUES (NULL, 'tg_summary', NULL, ?, 'success', NULL, 'scheduler', ?)""",
            (f"delivered_to={sent}", now_iso),
        )
        conn.commit()
    finally:
        conn.close()
    return {"sent": sent, "reason": "sent", "sent_at": now.isoformat()}
