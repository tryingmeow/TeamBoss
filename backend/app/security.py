import asyncio
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException, status

from .database import get_db


ADMIN_PASSWORD_ENV = "AUTO_TEAM_ADMIN_PASSWORD"
ADMIN_API_KEY_ENV = "AUTO_TEAM_API_KEY"
ADMIN_PASSWORD_HASH_SETTING = "admin_password_hash"
ADMIN_API_KEY_SETTING = "admin_api_key"
ADMIN_PREVIOUS_API_KEY_SETTING = "admin_api_key_previous"
ADMIN_PREVIOUS_API_KEY_EXPIRES_SETTING = "admin_api_key_previous_expires_at"
ADMIN_API_KEY_GRACE_PERIOD = timedelta(minutes=10)
PASSWORD_HASH_ITERATIONS = 210_000
INITIAL_PASSWORD_MIN_LENGTH = 8
INITIAL_API_KEY_MIN_LENGTH = 12


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _initial_password_error(password: str) -> Optional[str]:
    if len(password) < INITIAL_PASSWORD_MIN_LENGTH:
        return f"至少 {INITIAL_PASSWORD_MIN_LENGTH} 位"
    if password.lower().startswith("change_me"):
        return "不能使用 .env.example 的公开占位值"
    return None


def _initial_api_key_error(api_key: str) -> Optional[str]:
    if not api_key.startswith("atk_"):
        return "必须以 atk_ 开头"
    if len(api_key) < INITIAL_API_KEY_MIN_LENGTH:
        return f"至少 {INITIAL_API_KEY_MIN_LENGTH} 位"
    if api_key.lower().startswith("atk_change_me"):
        return "不能使用 .env.example 的公开占位值"
    return None


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def safe_compare_digest(supplied: str, expected: str) -> bool:
    """secrets.compare_digest 对非 ASCII 的 str 会抛 TypeError（Uvicorn 把请求头按
    latin-1 解码，所以 `X-API-Key: <非 ASCII>` 能一路走到这里）。非 ASCII 输入按
    鉴权失败处理，而不是让异常向上冒泡成 500；ASCII 路径仍然是常数时间比较。"""
    try:
        supplied.encode("ascii")
        expected.encode("ascii")
    except (UnicodeEncodeError, AttributeError):
        return False
    return secrets.compare_digest(supplied, expected)


def _password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt}${digest}"


def _password_hash_matches(password: str, encoded: str) -> bool:
    """Synchronous password hash comparison. DO NOT call directly from event loop.
    Use _password_hash_matches_async instead to avoid blocking."""
    try:
        algorithm, iterations_raw, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("ascii"),
            iterations,
        ).hex()
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


async def _password_hash_matches_async(password: str, encoded: str) -> bool:
    """Async password hash comparison that offloads PBKDF2 to thread pool.

    PBKDF2 with 210000 iterations takes ~100-200ms per call. Running this
    directly in the event loop blocks all other requests. asyncio.to_thread
    offloads it to the default executor thread pool, preventing DoS.
    """
    return await asyncio.to_thread(_password_hash_matches, password, encoded)


async def _read_setting(key: str) -> Optional[str]:
    async with get_db() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
    if not row:
        return None
    value = (row["value"] or "").strip()
    return value or None


async def _write_setting(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _now_iso()),
        )
        await db.commit()


async def ensure_admin_credentials_initialized() -> None:
    password_hash = await _read_setting(ADMIN_PASSWORD_HASH_SETTING)
    api_key = await _read_setting(ADMIN_API_KEY_SETTING)
    first_admin_start = not password_hash

    initial_password = _env(ADMIN_PASSWORD_ENV)
    initial_api_key = _env(ADMIN_API_KEY_ENV)
    missing: list[str] = []
    if not password_hash and not initial_password:
        missing.append(ADMIN_PASSWORD_ENV)
    if (first_admin_start or not api_key) and not initial_api_key:
        missing.append(ADMIN_API_KEY_ENV)
    if missing:
        raise RuntimeError(f"首次启动缺少环境变量: {', '.join(missing)}")

    invalid: list[str] = []
    if not password_hash:
        password_error = _initial_password_error(initial_password)
        if password_error:
            invalid.append(f"{ADMIN_PASSWORD_ENV}（{password_error}）")
    if first_admin_start or not api_key:
        api_key_error = _initial_api_key_error(initial_api_key)
        if api_key_error:
            invalid.append(f"{ADMIN_API_KEY_ENV}（{api_key_error}）")
    if invalid:
        raise RuntimeError(f"首次启动环境变量不安全: {', '.join(invalid)}")

    if not password_hash:
        await _write_setting(ADMIN_PASSWORD_HASH_SETTING, _password_hash(initial_password))
    if first_admin_start or not api_key:
        await _write_setting(ADMIN_API_KEY_SETTING, initial_api_key)


async def get_admin_api_key() -> Optional[str]:
    return await _read_setting(ADMIN_API_KEY_SETTING)


async def verify_admin_password(password: str) -> bool:
    password = (password or "").strip()
    if not password:
        return False
    password_hash = await _read_setting(ADMIN_PASSWORD_HASH_SETTING)
    if not password_hash:
        return False
    return await _password_hash_matches_async(password, password_hash)


async def change_admin_password(current_password: str, new_password: str) -> None:
    # 不能用 401：前端 client 对带管理员 key 的 401 会清 key 并跳登录页，改密码填错
    # 当前密码会把管理员踢出登录。这是表单校验失败，用 400。
    if not await verify_admin_password(current_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码不正确")

    new_password = (new_password or "").strip()
    if len(new_password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码至少 8 位")

    await _write_setting(ADMIN_PASSWORD_HASH_SETTING, _password_hash(new_password))


async def rotate_admin_api_key() -> str:
    new_api_key = "atk_" + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = (now + ADMIN_API_KEY_GRACE_PERIOD).isoformat()

    # 当前 key、旧 key 和宽限截止时间必须一次提交。这样即使响应在新 key
    # 返回前中断，调用方仍能用旧 key 访问 /api/admin/account 取回新 key。
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (ADMIN_API_KEY_SETTING,),
        )
        row = await cursor.fetchone()
        previous_api_key = (row["value"] or "").strip() if row else ""
        if not previous_api_key:
            await db.rollback()
            raise RuntimeError("API Key 未初始化")

        for key, value in (
            (ADMIN_PREVIOUS_API_KEY_SETTING, previous_api_key),
            (ADMIN_PREVIOUS_API_KEY_EXPIRES_SETTING, expires_at),
            (ADMIN_API_KEY_SETTING, new_api_key),
        ):
            await db.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (key, value, now.isoformat()),
            )
        await db.commit()
    return new_api_key


async def _get_valid_admin_api_keys() -> tuple[Optional[str], Optional[str]]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT key, value FROM settings
               WHERE key IN (?, ?, ?)""",
            (
                ADMIN_API_KEY_SETTING,
                ADMIN_PREVIOUS_API_KEY_SETTING,
                ADMIN_PREVIOUS_API_KEY_EXPIRES_SETTING,
            ),
        )
        values = {row["key"]: (row["value"] or "").strip() for row in await cursor.fetchall()}

    current = values.get(ADMIN_API_KEY_SETTING) or None
    previous = values.get(ADMIN_PREVIOUS_API_KEY_SETTING) or None
    previous_expires = values.get(ADMIN_PREVIOUS_API_KEY_EXPIRES_SETTING) or None
    if not previous or not previous_expires:
        return current, None
    try:
        expires_at = datetime.fromisoformat(previous_expires.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            return current, None
    except (TypeError, ValueError):
        return current, None
    return current, previous


async def require_admin(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> None:
    expected, previous = await _get_valid_admin_api_keys()
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API Key 未初始化")

    supplied = (x_api_key or "").strip() or (_bearer_token(authorization) or "").strip()
    current_matches = bool(supplied) and safe_compare_digest(supplied, expected)
    previous_matches = bool(supplied and previous) and safe_compare_digest(supplied, previous)
    if not current_matches and not previous_matches:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API Key 无效")


def get_cors_origins() -> list[str]:
    raw = os.getenv("AUTO_TEAM_CORS_ORIGINS") or os.getenv("CORS_ORIGINS")
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]
