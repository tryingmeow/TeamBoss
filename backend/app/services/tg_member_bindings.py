"""Telegram member-email binding and expiry reminders.

Administrator authorization continues to live in ``tg_users``.  This module
owns a separate member identity so pairing a ChatGPT member can never grant
access to administrator commands.
"""

from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..database import get_db_path
from ..tg_format import detail_card
from .tg_commands import sync_chat_commands_sync

# 向后兼容：模块级占位符，测试可能会 patch 它
DB_PATH = None

DISPLAY_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
PAIRING_CODE_TTL_HOURS = 24
TG_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

_REMINDER_WINDOWS = (
    ("before_7d", timedelta(days=7), timedelta(days=3), "7 天"),
    ("before_3d", timedelta(days=3), timedelta(days=1), "3 天"),
    ("before_1d", timedelta(days=1), timedelta(hours=5), "1 天"),
    ("before_5h", timedelta(hours=5), timedelta(0), "5 小时"),
)


def normalize_member_email(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def build_member_copy_text(bot_username: str, code: str) -> str:
    username = (bot_username or "").strip().lstrip("@")
    normalized_code = (code or "").strip().upper()
    if not username or not normalized_code:
        raise ValueError("机器人用户名和配对码不能为空")
    return (
        f"TG 机器人：https://t.me/{username}\n"
        f"绑定指令：/pair {normalized_code}"
    )


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


def _format_local(value: datetime) -> str:
    return value.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH or get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def member_emails_for_chat_sync(chat_id: str) -> list[str]:
    try:
        conn = _connect()
        previous_chat_id: Optional[str] = None
        try:
            rows = conn.execute(
                """SELECT email FROM tg_member_bindings
                   WHERE chat_id = ? AND disabled = 0
                   ORDER BY email""",
                (str(chat_id),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [normalize_member_email(row["email"]) for row in rows if row["email"]]


def claim_member_pairing_code_sync(
    chat_id: str,
    username: Optional[str],
    code: str,
) -> Optional[str]:
    """Claim a member pairing code.

    ``None`` means the code does not belong to the member-code table, so the
    caller may continue checking the legacy operator pairing codes.
    """

    normalized_code = (code or "").strip().upper()
    if not normalized_code:
        return None

    previous_chat_id: Optional[str] = None
    try:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tg_member_pairing_codes WHERE code = ?",
                (normalized_code,),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            if row["disabled"]:
                conn.rollback()
                return "配对码已被吊销。"
            if row["used_by_chat_id"]:
                conn.rollback()
                return "配对码已被使用过。"

            expires_at = _parse_datetime(row["expires_at"])
            if expires_at and datetime.now(timezone.utc) > expires_at:
                conn.rollback()
                return "配对码已过期，请向管理员索取新的配对码。"

            email = normalize_member_email(row["email"])
            if not email:
                conn.rollback()
                return "配对码缺少成员邮箱，请让管理员重新生成。"

            previous_binding = conn.execute(
                "SELECT chat_id FROM tg_member_bindings WHERE email = ? COLLATE NOCASE",
                (email,),
            ).fetchone()
            if previous_binding and previous_binding["chat_id"]:
                previous_chat_id = str(previous_binding["chat_id"])

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO tg_member_bindings
                   (email, chat_id, username, disabled, paired_at, disabled_at, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, NULL, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET
                     chat_id = excluded.chat_id,
                     username = excluded.username,
                     disabled = 0,
                     paired_at = excluded.paired_at,
                     disabled_at = NULL,
                     updated_at = excluded.updated_at""",
                (email, str(chat_id), username or "", now, now, now),
            )
            conn.execute(
                """UPDATE tg_member_pairing_codes
                   SET used_by_chat_id = ?, used_at = ?
                   WHERE id = ? AND used_by_chat_id IS NULL""",
                (str(chat_id), now, row["id"]),
            )
            conn.commit()
            sync_chat_commands_sync(str(chat_id), conn=conn)
            if previous_chat_id and previous_chat_id != str(chat_id):
                sync_chat_commands_sync(previous_chat_id, conn=conn)
        finally:
            conn.close()
    except Exception as exc:
        return f"绑定失败：{exc}"

    return f"✅ 绑定成功：{email}\n发送 /info 查询自己的成员状态和服务到期时间。"


def deactivate_member_binding_if_inactive_sync(
    conn: sqlite3.Connection,
    email: Optional[str],
    *,
    now_iso: Optional[str] = None,
) -> bool:
    """Disable an email binding once no active/pending membership remains."""

    normalized = normalize_member_email(email)
    if not normalized:
        return False
    active = conn.execute(
        """SELECT 1 FROM member_expiry
           WHERE lower(trim(email)) = ? AND kicked = 0
           LIMIT 1""",
        (normalized,),
    ).fetchone()
    if active:
        return False

    now = now_iso or datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """UPDATE tg_member_bindings
           SET disabled = 1, disabled_at = ?, updated_at = ?
           WHERE email = ? COLLATE NOCASE AND disabled = 0""",
        (now, now, normalized),
    )
    conn.execute(
        """UPDATE tg_member_pairing_codes
           SET disabled = 1
           WHERE email = ? COLLATE NOCASE
             AND used_by_chat_id IS NULL
             AND disabled = 0""",
        (normalized,),
    )
    return cursor.rowcount > 0


async def deactivate_member_binding_if_inactive(
    db,
    email: Optional[str],
    *,
    now_iso: Optional[str] = None,
) -> bool:
    normalized = normalize_member_email(email)
    if not normalized:
        return False
    active = await (
        await db.execute(
            """SELECT 1 FROM member_expiry
               WHERE lower(trim(email)) = ? AND kicked = 0
               LIMIT 1""",
            (normalized,),
        )
    ).fetchone()
    if active:
        return False

    now = now_iso or datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        """UPDATE tg_member_bindings
           SET disabled = 1, disabled_at = ?, updated_at = ?
           WHERE email = ? COLLATE NOCASE AND disabled = 0""",
        (now, now, normalized),
    )
    await db.execute(
        """UPDATE tg_member_pairing_codes
           SET disabled = 1
           WHERE email = ? COLLATE NOCASE
             AND used_by_chat_id IS NULL
             AND disabled = 0""",
        (normalized,),
    )
    return cursor.rowcount > 0


def _kick_policy(conn: sqlite3.Connection) -> tuple[str, int]:
    rows = conn.execute(
        """SELECT key, value FROM settings
           WHERE key IN ('expiry_kick_mode', 'expiry_kick_delay_hours')"""
    ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    mode = values.get("expiry_kick_mode") or "delay_hours"
    if mode == "day_start":
        mode = "day_end"
    if mode not in {"delay_hours", "day_end"}:
        mode = "delay_hours"
    try:
        delay_hours = int(values.get("expiry_kick_delay_hours") or 0)
    except (TypeError, ValueError):
        delay_hours = 0
    return mode, min(max(delay_hours, 0), 720)


def _effective_kick_at(expires_at: datetime, mode: str, delay_hours: int) -> datetime:
    if mode == "day_end":
        local = expires_at.astimezone(DISPLAY_TZ)
        day_end = local.replace(hour=23, minute=59, second=0, microsecond=0)
        return max(day_end.astimezone(timezone.utc), expires_at)
    return expires_at + timedelta(hours=delay_hours)


def _admin_contact(conn: sqlite3.Connection) -> Optional[str]:
    """Return the most recently paired contactable admin's public TG handle."""

    rows = conn.execute(
        """SELECT username FROM tg_users
           WHERE disabled = 0 AND trim(COALESCE(username, '')) != ''
           ORDER BY COALESCE(NULLIF(paired_at, ''), created_at, '') DESC, id DESC"""
    ).fetchall()
    for row in rows:
        username = str(row["username"] or "").strip().lstrip("@")
        if TG_USERNAME_RE.fullmatch(username):
            return f"@{username}"
    return None


def _reminder_stage(
    now: datetime,
    expires_at: datetime,
    effective_kick_at: datetime,
) -> Optional[tuple[str, str]]:
    remaining = expires_at - now
    if remaining > timedelta(0):
        for key, upper, lower, label in _REMINDER_WINDOWS:
            if lower < remaining <= upper:
                return key, label
        return None
    if expires_at <= now < effective_kick_at and effective_kick_at > expires_at:
        return "grace", "宽限期"
    return None


def _reminder_text(
    *,
    email: str,
    team_name: str,
    expires_at: datetime,
    effective_kick_at: datetime,
    reminder_key: str,
    stage_label: str,
    admin_contact: Optional[str] = None,
) -> str:
    if reminder_key == "grace":
        title = "⚠️ 已进入系统宽限期"
        rows = (
            f"👤 邮箱：{email}",
            f"🏢 车队：{team_name}",
            f"📅 服务到期：{_format_local(expires_at)}（北京时间）",
            f"🗑️ 预计移除：{_format_local(effective_kick_at)}（北京时间）",
        )
        footer = "⚠️ 请尽快续期；宽限期是系统缓冲，不计入购买时长。"
    else:
        title = f"⏰ 服务将在 {stage_label}内到期"
        rows = (
            f"👤 邮箱：{email}",
            f"🏢 车队：{team_name}",
            f"📅 服务到期：{_format_local(expires_at)}（北京时间）",
        )
        footer = "ℹ️ 系统宽限期不计入购买时长，请及时续期。"
    lines = [detail_card(title, rows), "", footer]
    if admin_contact:
        lines.extend(("", f"💬 管理员：{admin_contact}"))
    return "\n".join(lines)


def run_member_expiry_reminders_sync(
    *,
    now: Optional[datetime] = None,
    send_func: Optional[Callable[[str, str], bool]] = None,
) -> dict:
    """Send due member reminders once per membership/expiry/stage."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conn = _connect()
    try:
        if send_func is None:
            settings = {
                row["key"]: row["value"]
                for row in conn.execute(
                    """SELECT key, value FROM settings
                       WHERE key IN ('tg_bot_enabled', 'tg_bot_token')"""
                ).fetchall()
            }
            if settings.get("tg_bot_enabled") != "1" or not settings.get("tg_bot_token"):
                return {"sent": 0, "due": 0, "reason": "bot_disabled"}
            from .tg_notify import send_message_sync

            token = settings["tg_bot_token"]

            def send_func(chat_id: str, text: str) -> bool:
                return send_message_sync(chat_id, text, token=token)

        mode, delay_hours = _kick_policy(conn)
        admin_contact = _admin_contact(conn)
        rows = conn.execute(
            """SELECT me.id, me.team_id, lower(trim(me.email)) AS email,
                      me.expires_at, b.chat_id, COALESCE(t.name, me.team_id) AS team_name
               FROM member_expiry me
               JOIN tg_member_bindings b
                 ON b.email = lower(trim(me.email)) COLLATE NOCASE
                AND b.disabled = 0
               LEFT JOIN teams t ON t.id = me.team_id
              WHERE me.kicked = 0
                AND me.expires_at IS NOT NULL
                AND trim(me.email) != ''
              ORDER BY me.id DESC"""
        ).fetchall()

        sent = 0
        due = 0
        seen: set[tuple[str, str]] = set()
        for row in rows:
            email = normalize_member_email(row["email"])
            membership_key = (str(row["team_id"] or ""), email)
            if membership_key in seen:
                continue
            seen.add(membership_key)

            expires_at = _parse_datetime(row["expires_at"])
            if not expires_at:
                continue
            effective_kick_at = _effective_kick_at(expires_at, mode, delay_hours)
            stage = _reminder_stage(now, expires_at, effective_kick_at)
            if not stage:
                continue
            reminder_key, stage_label = stage
            due += 1

            already_sent = conn.execute(
                """SELECT 1 FROM tg_member_reminders
                   WHERE email = ? COLLATE NOCASE
                     AND team_id = ?
                     AND expires_at = ?
                     AND reminder_key = ?""",
                (email, row["team_id"], row["expires_at"], reminder_key),
            ).fetchone()
            if already_sent:
                continue

            text = _reminder_text(
                email=email,
                team_name=str(row["team_name"] or row["team_id"] or "未知车队"),
                expires_at=expires_at,
                effective_kick_at=effective_kick_at,
                reminder_key=reminder_key,
                stage_label=stage_label,
                admin_contact=admin_contact,
            )
            if not send_func(str(row["chat_id"]), text):
                continue

            conn.execute(
                """INSERT OR IGNORE INTO tg_member_reminders
                   (email, team_id, expires_at, reminder_key, chat_id, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    email,
                    row["team_id"],
                    row["expires_at"],
                    reminder_key,
                    str(row["chat_id"]),
                    now.isoformat(),
                ),
            )
            conn.commit()
            sent += 1
        return {"sent": sent, "due": due, "reason": "ok"}
    finally:
        conn.close()
