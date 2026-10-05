"""Telegram command menus scoped to the current chat identity.

Telegram resolves a ``chat`` command scope before the default scope, which
lets the bot expose only the commands that the current private chat may use.
The backend remains the authorization boundary; menus are a matching UX layer.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

import requests

from .. import database as app_database


TG_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = 15
logger = logging.getLogger(__name__)


PUBLIC_COMMANDS = [
    {"command": "start", "description": "开始使用机器人"},
    {"command": "pair", "description": "使用配对码绑定身份"},
    {"command": "help", "description": "查看绑定帮助"},
]

MEMBER_COMMANDS = [
    {"command": "info", "description": "查询我的成员状态和到期时间"},
    {"command": "pair", "description": "继续绑定其他成员邮箱"},
    {"command": "help", "description": "查看成员命令"},
]

ADMIN_COMMANDS = [
    {"command": "status", "description": "列出 Team，可加 all/idle/busy"},
    {"command": "watch", "description": "查看超员风险和观察名单"},
    {"command": "billing", "description": "查看财务概览和告警"},
    {"command": "logs", "description": "查看最近操作日志"},
    {"command": "m_logs", "description": "查看最近人员日志"},
    {"command": "team", "description": "查看 Team 详情：/team 名字"},
    {"command": "info", "description": "查询成员信息和到期时间"},
    {"command": "members", "description": "成员列表和关键词查询"},
    {"command": "owners", "description": "车主列表和关键词查询"},
    {"command": "patrol", "description": "控制巡逻自动踢人"},
    {"command": "invite", "description": "邀请成员到指定 Team"},
    {"command": "kick", "description": "从成员列表选择并踢出"},
    {"command": "q", "description": "取消当前向导"},
    {"command": "pair", "description": "绑定成员邮箱或更新身份"},
    {"command": "help", "description": "查看管理员命令"},
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(app_database.get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _token_sync(conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    owned = conn is None
    db = conn or _connect()
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'tg_bot_token'"
        ).fetchone()
    except Exception:
        return None
    finally:
        if owned:
            db.close()
    if not row or row[0] is None:
        return None
    value = str(row[0]).strip()
    return value or None


def command_identity_sync(
    chat_id: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Return ``admin``, ``member`` or ``public`` for one private chat."""

    owned = conn is None
    db = conn or _connect()
    try:
        admin = db.execute(
            "SELECT 1 FROM tg_users WHERE chat_id = ? AND disabled = 0 LIMIT 1",
            (str(chat_id),),
        ).fetchone()
        if admin:
            return "admin"
        member = db.execute(
            """SELECT 1 FROM tg_member_bindings
               WHERE chat_id = ? AND disabled = 0 LIMIT 1""",
            (str(chat_id),),
        ).fetchone()
        return "member" if member else "public"
    except Exception:
        return "public"
    finally:
        if owned:
            db.close()


def commands_for_identity(identity: str) -> list[dict[str, str]]:
    if identity == "admin":
        return ADMIN_COMMANDS
    if identity == "member":
        return MEMBER_COMMANDS
    return PUBLIC_COMMANDS


def set_commands_sync(
    token: str,
    commands: list[dict[str, str]],
    *,
    scope: Optional[dict] = None,
) -> bool:
    payload: dict = {"commands": commands}
    if scope:
        payload["scope"] = scope
    try:
        response = requests.post(
            TG_API.format(token=token, method="setMyCommands"),
            json=payload,
            timeout=_TIMEOUT,
        )
        if not response.ok:
            logger.warning(
                "Telegram setMyCommands failed status=%s scope=%s",
                response.status_code,
                (scope or {}).get("type", "default"),
            )
        return response.ok
    except Exception as exc:
        logger.warning(
            "Telegram setMyCommands transport failure type=%s scope=%s",
            type(exc).__name__,
            (scope or {}).get("type", "default"),
        )
        return False


def sync_chat_commands_sync(
    chat_id: str,
    *,
    token: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    tok = token or _token_sync(conn)
    if not tok or not chat_id:
        return False
    identity = command_identity_sync(str(chat_id), conn=conn)
    return set_commands_sync(
        tok,
        commands_for_identity(identity),
        scope={"type": "chat", "chat_id": str(chat_id)},
    )


def sync_email_chat_commands_sync(
    email: str,
    *,
    token: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Refresh chats that have ever owned one member-email binding."""

    normalized = (email or "").strip().lower()
    if not normalized:
        return False
    owned = conn is None
    db = conn or _connect()
    try:
        tok = token or _token_sync(db)
        if not tok:
            return False
        rows = db.execute(
            "SELECT DISTINCT chat_id FROM tg_member_bindings WHERE lower(email) = ?",
            (normalized,),
        ).fetchall()
        ok = True
        for row in rows:
            if row and row[0]:
                ok = sync_chat_commands_sync(str(row[0]), token=tok, conn=db) and ok
        return ok
    finally:
        if owned:
            db.close()


def sync_all_commands_sync(*, token: Optional[str] = None) -> bool:
    """Set the public default and refresh every known private-chat scope."""

    conn = _connect()
    try:
        tok = token or _token_sync(conn)
        if not tok:
            return False
        default_ok = set_commands_sync(tok, PUBLIC_COMMANDS)
        rows = conn.execute(
            """SELECT chat_id FROM tg_users
               UNION
               SELECT chat_id FROM tg_member_bindings"""
        ).fetchall()
        chat_ids = [str(row[0]) for row in rows if row and row[0]]
        scoped_ok = True
        for chat_id in chat_ids:
            scoped_ok = sync_chat_commands_sync(chat_id, token=tok, conn=conn) and scoped_ok
        return default_ok and scoped_ok
    finally:
        conn.close()
