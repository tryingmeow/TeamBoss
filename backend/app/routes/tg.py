import secrets
import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_db, log_operation
from ..services.tg_member_bindings import (
    PAIRING_CODE_TTL_HOURS,
    build_member_copy_text,
    normalize_member_email,
)
from ..services.tg_commands import sync_all_commands_sync, sync_chat_commands_sync

router = APIRouter(prefix="/api/tg", tags=["tg"])

TG_API = "https://api.telegram.org/bot{token}/{method}"
_GETME_TIMEOUT = 8

# 生成配对码用的字符集：去掉容易混淆的 0/O/1/I/L
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 8
_CODE_TTL_HOURS = 24
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class TgConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    token: Optional[str] = None
    summary_enabled: Optional[bool] = None
    summary_interval_minutes: Optional[int] = None


class TgUserUpdate(BaseModel):
    disabled: Optional[bool] = None


class TgCodeCreate(BaseModel):
    note: Optional[str] = None


class TgMemberCodeCreate(BaseModel):
    email: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get_setting(key: str) -> Optional[str]:
    async with get_db() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
    if not row or row["value"] is None:
        return None
    value = str(row["value"]).strip()
    return value or None


async def _set_setting(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _now_iso()),
        )
        await db.commit()


def _fetch_bot_identity(token: str) -> Optional[dict]:
    try:
        resp = requests.get(TG_API.format(token=token, method="getMe"), timeout=_GETME_TIMEOUT)
        if not resp.ok:
            return None
        data = resp.json()
        if not data.get("ok"):
            return None
        result = data.get("result") or {}
        if result.get("id") is None or not result.get("username"):
            return None
        return {"id": str(result["id"]), "username": str(result["username"]).lstrip("@")}
    except Exception:
        return None


def _fetch_bot_username(token: str) -> Optional[str]:
    """best-effort lookup used by read-only config/member-code surfaces."""
    identity = _fetch_bot_identity(token)
    return identity["username"] if identity else None


async def _resolve_bot_username() -> Optional[str]:
    cached = await _get_setting("tg_bot_username")
    if cached:
        return cached.lstrip("@")
    token = await _get_setting("tg_bot_token")
    if not token:
        return None
    username = await asyncio.to_thread(_fetch_bot_username, token)
    if username:
        username = username.lstrip("@")
        await _set_setting("tg_bot_username", username)
    return username


def _safe_summary_interval(raw: Optional[str]) -> int:
    try:
        return int(raw or "15")
    except (TypeError, ValueError):
        return 15


# ---------------------------------------------------------------------------
# /api/tg/config
# ---------------------------------------------------------------------------

@router.get("/config")
async def get_config():
    enabled = (await _get_setting("tg_bot_enabled")) == "1"
    token = await _get_setting("tg_bot_token")
    token_set = bool(token)
    bot_username = await _resolve_bot_username() if token else None
    summary_interval = _safe_summary_interval(await _get_setting("tg_summary_interval_minutes"))
    from ..tg_bot import is_bot_thread_alive

    return {
        "enabled": enabled,
        "token_set": token_set,
        "bot_username": bot_username,
        "polling": is_bot_thread_alive(),
        "summary_enabled": (await _get_setting("tg_summary_enabled")) == "1",
        "summary_interval_minutes": summary_interval,
        "summary_last_sent_at": await _get_setting("tg_summary_last_sent_at"),
    }


@router.patch("/config")
async def update_config(req: TgConfigUpdate):
    # 先验证整份请求，再开始任何写入，避免“接口返回失败但部分配置已生效”。
    if req.summary_interval_minutes is not None and not 5 <= req.summary_interval_minutes <= 1440:
        await log_operation(None, "update_tg_config", None, f"summary_interval_minutes={req.summary_interval_minutes}", "failed", "Value out of range")
        raise HTTPException(status_code=400, detail="摘要间隔必须在 5–1440 分钟之间")

    token = req.token.strip() if req.token is not None else ""
    identity = None
    if token:
        identity = await asyncio.to_thread(_fetch_bot_identity, token)
        if not identity:
            await log_operation(None, "update_tg_config", None, "token_changed", "failed", "Invalid bot token")
            raise HTTPException(status_code=400, detail="无法验证机器人 Token，原配置未修改")

    should_sync_commands = req.enabled is True or identity is not None
    bot_changed = False
    admin_pairing_code = None
    admin_pairing_expires_at = None

    if identity:
        current_token = await _get_setting("tg_bot_token")
        current_bot_id = await _get_setting("tg_bot_id")
        current_username = await _get_setting("tg_bot_username")
        if current_token and not current_bot_id:
            current_identity = await asyncio.to_thread(_fetch_bot_identity, current_token)
            if current_identity:
                current_bot_id = current_identity["id"]

        if current_token:
            if current_bot_id:
                bot_changed = current_bot_id != identity["id"]
            elif current_username:
                bot_changed = current_username.lstrip("@").lower() != identity["username"].lower()

    now = _now_iso()
    async with get_db() as db:
        if req.enabled is not None:
            await db.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                ("tg_bot_enabled", "1" if req.enabled else "0", now),
            )

        if identity:
            if bot_changed:
                await db.execute("UPDATE tg_users SET disabled = 1 WHERE disabled = 0")
                await db.execute(
                    """UPDATE tg_member_bindings
                       SET disabled = 1, disabled_at = ?, updated_at = ?
                       WHERE disabled = 0""",
                    (now, now),
                )
                await db.execute("UPDATE tg_pairing_codes SET disabled = 1 WHERE disabled = 0")
                await db.execute("UPDATE tg_member_pairing_codes SET disabled = 1 WHERE disabled = 0")

                admin_pairing_expires_at = (
                    datetime.now(timezone.utc) + timedelta(hours=_CODE_TTL_HOURS)
                ).isoformat()
                for _ in range(10):
                    candidate = _generate_code()
                    operator_existing = await (
                        await db.execute(
                            "SELECT id FROM tg_pairing_codes WHERE code = ?", (candidate,)
                        )
                    ).fetchone()
                    member_existing = await (
                        await db.execute(
                            "SELECT id FROM tg_member_pairing_codes WHERE code = ?", (candidate,)
                        )
                    ).fetchone()
                    if not operator_existing and not member_existing:
                        admin_pairing_code = candidate
                        break
                if not admin_pairing_code:
                    raise HTTPException(status_code=500, detail="新机器人配对码生成失败，原配置未修改")
                await db.execute(
                    """INSERT INTO tg_pairing_codes
                       (code, note, expires_at, disabled, created_at)
                       VALUES (?, '更换机器人后重新配对', ?, 0, ?)""",
                    (admin_pairing_code, admin_pairing_expires_at, now),
                )

            for key, value in (
                ("tg_bot_token", token),
                ("tg_bot_id", identity["id"]),
                ("tg_bot_username", identity["username"]),
            ):
                await db.execute(
                    """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                      updated_at = excluded.updated_at""",
                    (key, value, now),
                )

        if req.summary_enabled is not None:
            await db.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                ("tg_summary_enabled", "1" if req.summary_enabled else "0", now),
            )
        if req.summary_interval_minutes is not None:
            await db.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                ("tg_summary_interval_minutes", str(req.summary_interval_minutes), now),
            )
        await db.commit()

    if bot_changed:
        from ..tg_bot import reset_conversation_state

        reset_conversation_state()

    if should_sync_commands:
        await asyncio.to_thread(sync_all_commands_sync)

    token_set = bool(await _get_setting("tg_bot_token"))
    detail_parts = []
    if req.enabled is not None:
        detail_parts.append(f"enabled={req.enabled}")
    if identity:
        detail_parts.append("token_changed=true")
        if bot_changed:
            detail_parts.append("bot_changed=true")
    if req.summary_enabled is not None:
        detail_parts.append(f"summary_enabled={req.summary_enabled}")
    if req.summary_interval_minutes is not None:
        detail_parts.append(f"summary_interval_minutes={req.summary_interval_minutes}")
    detail = ", ".join(detail_parts) if detail_parts else "no_changes"
    await log_operation(None, "update_tg_config", None, detail, "success")
    return {
        "status": "ok",
        "token_set": token_set,
        "bot_changed": bot_changed,
        "admin_pairing_code": admin_pairing_code,
        "admin_pairing_expires_at": admin_pairing_expires_at,
        "summary_enabled": (await _get_setting("tg_summary_enabled")) == "1",
        "summary_interval_minutes": _safe_summary_interval(
            await _get_setting("tg_summary_interval_minutes")
        ),
    }


@router.post("/summary")
async def send_summary_now():
    from ..services.tg_summary import maybe_send_summary_sync

    result = await asyncio.to_thread(maybe_send_summary_sync, force=True)
    if result.get("sent", 0) <= 0:
        reason = result.get("reason") or "unknown"
        raise HTTPException(status_code=503, detail=f"摘要未送达: {reason}")
    return result


# ---------------------------------------------------------------------------
# /api/tg/users
# ---------------------------------------------------------------------------

def _row_to_user(row) -> dict:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "username": row["username"],
        "note": row["note"],
        "disabled": bool(row["disabled"]),
        "paired_at": row["paired_at"],
    }


@router.get("/users")
async def list_users():
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM tg_users ORDER BY id DESC")
        rows = await cursor.fetchall()
    return {"users": [_row_to_user(row) for row in rows]}


@router.patch("/users/{user_id}")
async def update_user(user_id: int, req: TgUserUpdate):
    try:
        async with get_db() as db:
            row = await (
                await db.execute("SELECT id, chat_id FROM tg_users WHERE id = ?", (user_id,))
            ).fetchone()
            if not row:
                await log_operation(None, "update_tg_operator", None, f"user_id={user_id}", "failed", "User not found")
                raise HTTPException(status_code=404, detail="用户不存在")
            chat_id = str(row["chat_id"])

            if req.disabled is not None:
                await db.execute(
                    "UPDATE tg_users SET disabled = ? WHERE id = ?",
                    (1 if req.disabled else 0, user_id),
                )
                await db.commit()

            row = await (await db.execute("SELECT * FROM tg_users WHERE id = ?", (user_id,))).fetchone()
        await asyncio.to_thread(sync_chat_commands_sync, chat_id)
        detail = f"user_id={user_id}, disabled={req.disabled}" if req.disabled is not None else f"user_id={user_id}"
        await log_operation(None, "update_tg_operator", None, detail, "success")
        return _row_to_user(row)
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "update_tg_operator", None, f"user_id={user_id}", "failed", str(e))
        raise


@router.delete("/users/{user_id}")
async def delete_user(user_id: int):
    try:
        async with get_db() as db:
            row = await (
                await db.execute("SELECT id, chat_id FROM tg_users WHERE id = ?", (user_id,))
            ).fetchone()
            if not row:
                await log_operation(None, "delete_tg_operator", None, f"user_id={user_id}", "failed", "User not found")
                raise HTTPException(status_code=404, detail="用户不存在")
            chat_id = str(row["chat_id"])
            await db.execute("DELETE FROM tg_users WHERE id = ?", (user_id,))
            await db.commit()
        await asyncio.to_thread(sync_chat_commands_sync, chat_id)
        await log_operation(None, "delete_tg_operator", None, f"user_id={user_id}", "success")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "delete_tg_operator", None, f"user_id={user_id}", "failed", str(e))
        raise


# ---------------------------------------------------------------------------
# /api/tg/codes
# ---------------------------------------------------------------------------

def _row_to_code(row) -> dict:
    return {
        "id": row["id"],
        "code": row["code"],
        "note": row["note"],
        "expires_at": row["expires_at"],
        "used_by_chat_id": row["used_by_chat_id"],
        "used_at": row["used_at"],
        "disabled": bool(row["disabled"]),
        "created_at": row["created_at"],
    }


@router.get("/codes")
async def list_codes():
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM tg_pairing_codes ORDER BY id DESC")
        rows = await cursor.fetchall()
    return {"codes": [_row_to_code(row) for row in rows]}


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


@router.post("/codes")
async def create_code(req: TgCodeCreate):
    try:
        note = (req.note or "").strip()
        now = _now_iso()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=_CODE_TTL_HOURS)).isoformat()

        async with get_db() as db:
            code = None
            for _ in range(10):
                candidate = _generate_code()
                operator_existing = await (
                    await db.execute("SELECT id FROM tg_pairing_codes WHERE code = ?", (candidate,))
                ).fetchone()
                member_existing = await (
                    await db.execute(
                        "SELECT id FROM tg_member_pairing_codes WHERE code = ?", (candidate,)
                    )
                ).fetchone()
                if not operator_existing and not member_existing:
                    code = candidate
                    break
            if not code:
                await log_operation(None, "create_tg_pairing_code", None, None, "failed", "Failed to generate code")
                raise HTTPException(status_code=500, detail="生成配对码失败，请重试")

            cursor = await db.execute(
                """INSERT INTO tg_pairing_codes (code, note, expires_at, disabled, created_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (code, note, expires_at, now),
            )
            code_id = cursor.lastrowid
            await db.commit()

            row = await (await db.execute("SELECT * FROM tg_pairing_codes WHERE id = ?", (code_id,))).fetchone()
        detail = f"note={note}" if note else "no_note"
        await log_operation(None, "create_tg_pairing_code", None, detail, "success")
        return _row_to_code(row)
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "create_tg_pairing_code", None, None, "failed", str(e))
        raise



@router.delete("/codes/{code_id}")
async def delete_code(code_id: int):
    try:
        async with get_db() as db:
            row = await (
                await db.execute("SELECT id FROM tg_pairing_codes WHERE id = ?", (code_id,))
            ).fetchone()
            if not row:
                await log_operation(None, "delete_tg_pairing_code", None, f"code_id={code_id}", "failed", "Code not found")
                raise HTTPException(status_code=404, detail="配对码不存在")
            await db.execute("UPDATE tg_pairing_codes SET disabled = 1 WHERE id = ?", (code_id,))
            await db.commit()
        await log_operation(None, "delete_tg_pairing_code", None, f"code_id={code_id}", "success")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "delete_tg_pairing_code", None, f"code_id={code_id}", "failed", str(e))
        raise



# ---------------------------------------------------------------------------
# /api/tg/member-codes
# ---------------------------------------------------------------------------

@router.post("/member-codes")
async def create_member_code(req: TgMemberCodeCreate):
    try:
        email = normalize_member_email(req.email)
        if not email or not _EMAIL_RE.fullmatch(email):
            await log_operation(None, "create_tg_member_pairing_code", req.email, None, "failed", "Invalid email format")
            raise HTTPException(status_code=400, detail="成员邮箱格式无效")

        bot_username = await _resolve_bot_username()
        if not bot_username:
            await log_operation(None, "create_tg_member_pairing_code", email, None, "failed", "Bot username not available")
            raise HTTPException(status_code=503, detail="无法取得 TG 机器人地址，请先检查机器人 Token")

        now = _now_iso()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=PAIRING_CODE_TTL_HOURS)
        ).isoformat()

        async with get_db() as db:
            active = await (
                await db.execute(
                    """SELECT 1 FROM member_expiry
                       WHERE lower(trim(email)) = ? AND kicked = 0
                       LIMIT 1""",
                    (email,),
                )
            ).fetchone()
            if not active:
                await log_operation(None, "create_tg_member_pairing_code", email, None, "failed", "Email not in active members")
                raise HTTPException(status_code=409, detail="该邮箱当前不在待接受或已加入成员中")

            await db.execute(
                """UPDATE tg_member_pairing_codes
                   SET disabled = 1
                   WHERE email = ? COLLATE NOCASE
                     AND used_by_chat_id IS NULL
                     AND disabled = 0""",
                (email,),
            )

            code = None
            for _ in range(10):
                candidate = _generate_code()
                member_existing = await (
                    await db.execute(
                        "SELECT id FROM tg_member_pairing_codes WHERE code = ?",
                        (candidate,),
                    )
                ).fetchone()
                operator_existing = await (
                    await db.execute(
                        "SELECT id FROM tg_pairing_codes WHERE code = ?",
                        (candidate,),
                    )
                ).fetchone()
                if not member_existing and not operator_existing:
                    code = candidate
                    break
            if not code:
                await log_operation(None, "create_tg_member_pairing_code", email, None, "failed", "Failed to generate code")
                raise HTTPException(status_code=500, detail="生成成员配对码失败，请重试")

            cursor = await db.execute(
                """INSERT INTO tg_member_pairing_codes
                   (code, email, expires_at, disabled, created_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (code, email, expires_at, now),
            )
            code_id = cursor.lastrowid
            await db.commit()

            binding = await (
                await db.execute(
                    """SELECT username, paired_at FROM tg_member_bindings
                       WHERE email = ? COLLATE NOCASE AND disabled = 0""",
                    (email,),
                )
            ).fetchone()

        await log_operation(None, "create_tg_member_pairing_code", email, None, "success")
        return {
            "id": code_id,
            "email": email,
            "code": code,
            "expires_at": expires_at,
            "bot_username": bot_username,
            "bot_url": f"https://t.me/{bot_username}",
            "command": f"/pair {code}",
            "copy_text": build_member_copy_text(bot_username, code),
            "currently_bound": bool(binding),
        }
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "create_tg_member_pairing_code", req.email, None, "failed", str(e))
        raise
