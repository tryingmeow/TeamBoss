"""Telegram 主动推送（best-effort）。

设计要点：
- 自包含：只依赖 requests + sqlite3，直接读 settings / tg_users，不 import 业务模块，避免循环依赖。
- 容错：任何异常都吞掉并返回 False，Telegram 挂了绝不能拖垮巡逻/同步。
- 双入口：scheduler 线程用 *_sync；async 代码用 await notify_admins(...)（内部走线程池）。

推送对象 = tg_users 里 disabled=0 的所有已配对管理员。
"""

import asyncio
import logging
import sqlite3
import time
from typing import Optional

import requests

from ..database import get_db_path
from ..tg_format import detail_card

TG_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = 15
_MAX_ERROR_DESCRIPTION = 240
logger = logging.getLogger(__name__)


def _get_setting_sync(key: str) -> Optional[str]:
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
        finally:
            conn.close()
        if row and row[0] is not None:
            value = str(row[0]).strip()
            return value or None
    except Exception:
        pass
    return None


def get_bot_token_sync() -> Optional[str]:
    return _get_setting_sync("tg_bot_token")


def _bot_enabled_sync() -> bool:
    return _get_setting_sync("tg_bot_enabled") == "1"


def _admin_chat_ids_sync() -> list[str]:
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            cur = conn.execute(
                "SELECT chat_id FROM tg_users WHERE disabled = 0"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def send_message_sync(
    chat_id: str,
    text: str,
    *,
    token: Optional[str] = None,
    parse_mode: Optional[str] = None,
    disable_preview: bool = True,
) -> bool:
    """给单个 chat 发消息。失败返回 False，绝不抛。"""
    tok = token or get_bot_token_sync()
    if not tok or not chat_id or not text:
        return False
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    for attempt in range(2):
        try:
            resp = requests.post(
                TG_API.format(token=tok, method="sendMessage"),
                json=payload,
                timeout=_TIMEOUT,
            )
            if resp.ok:
                return True

            description = ""
            retry_after = 0
            try:
                body = resp.json()
                description = str(body.get("description") or "")[:_MAX_ERROR_DESCRIPTION]
                retry_after = int((body.get("parameters") or {}).get("retry_after") or 0)
            except Exception:
                pass
            logger.warning(
                "Telegram sendMessage failed status=%s description=%s",
                resp.status_code,
                description or "<empty>",
            )
            if attempt == 0 and (resp.status_code == 429 or resp.status_code >= 500):
                time.sleep(min(max(retry_after, 1), 5))
                continue
            return False
        except Exception as exc:
            logger.warning("Telegram sendMessage transport failure type=%s", type(exc).__name__)
            if attempt == 0:
                time.sleep(1)
                continue
            return False
    return False


def edit_message_sync(
    chat_id: str,
    message_id: int,
    text: str,
    *,
    token: Optional[str] = None,
) -> bool:
    """原地编辑已发出的消息。失败返回 False，绝不抛。"""
    tok = token or get_bot_token_sync()
    if not tok or not chat_id or message_id is None or not text:
        return False
    try:
        resp = requests.post(
            TG_API.format(token=tok, method="editMessageText"),
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            logger.warning("Telegram editMessageText failed status=%s", resp.status_code)
        return resp.ok
    except Exception as exc:
        logger.warning("Telegram editMessageText transport failure type=%s", type(exc).__name__)
        return False


def notify_admins_sync(text: str, *, parse_mode: Optional[str] = None) -> int:
    """推送给所有 admin 角色的已配对用户。返回成功条数。"""
    if not _bot_enabled_sync():
        return 0
    tok = get_bot_token_sync()
    if not tok:
        return 0
    sent = 0
    for chat_id in _admin_chat_ids_sync():
        if send_message_sync(chat_id, text, token=tok, parse_mode=parse_mode):
            sent += 1
    return sent


def _team_name_sync(team_id: Optional[str]) -> str:
    if not team_id:
        return "未知 Team"
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            row = conn.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return str(team_id)


def notify_member_event_sync(
    action: str,
    team_id: Optional[str],
    *,
    email: Optional[str] = None,
    result: str = "success",
    source: str = "manual",
    detail: Optional[str] = None,
) -> int:
    """统一的成员变更 TG 留痕。仅包含必要业务字段，不携带 token/user_id。"""
    ok = result == "success"
    icon = "✅" if ok else "❌"
    target = (email or "未知成员").strip()
    rows = [
        f"🏢 Team：{_team_name_sync(team_id)}",
        f"👤 成员：{target}",
        f"🔗 来源：{source}",
    ]
    if detail:
        rows.append(f"📝 说明：{str(detail)[:300]}")
    text = detail_card(f"{icon} {action} · {'成功' if ok else '失败'}", rows)
    return notify_admins_sync(text)


async def notify_admins(text: str, *, parse_mode: Optional[str] = None) -> int:
    """async 入口：把同步发送丢到线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(notify_admins_sync, text, parse_mode=parse_mode)


async def notify_member_event(
    action: str,
    team_id: Optional[str],
    *,
    email: Optional[str] = None,
    result: str = "success",
    source: str = "manual",
    detail: Optional[str] = None,
) -> int:
    return await asyncio.to_thread(
        notify_member_event_sync,
        action,
        team_id,
        email=email,
        result=result,
        source=source,
        detail=detail,
    )
