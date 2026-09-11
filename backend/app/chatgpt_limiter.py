import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Callable, TypeVar

import jwt

from .database import get_db_path
from .session_store import update_session_file_tokens
from .chatgpt_client import ChatGPTClient
from .services.team_health_alerts import (
    report_team_failure_sync,
    report_team_recovery_sync,
)


T = TypeVar("T")
DEFAULT_API_CONCURRENCY = 4
AUTH_REFRESH_COOLDOWN = timedelta(minutes=10)
PROACTIVE_REFRESH_WINDOW = timedelta(hours=24)
PROACTIVE_REFRESH_COOLDOWN = timedelta(hours=1)
_NEGATIVE_REFRESH_RESULTS = {"failed", "partial", "unchanged"}

class _ResizableSemaphore:
    """Counting semaphore whose capacity can change at runtime.

    A plain ``threading.BoundedSemaphore`` can't be resized in place, so the
    previous implementation replaced the object whenever the configured
    limit changed. Threads that already held a permit on the old object kept
    it while new callers acquired permits on the new object, so the two
    capacities briefly stacked (effectively ``old_limit + new_limit``
    concurrent calls). Adjusting the available-permit count in place instead
    of swapping the object keeps outstanding permits and the new capacity
    consistent at every point in time.
    """

    def __init__(self, value: int) -> None:
        self._cond = threading.Condition()
        self._value = value
        self._limit = value

    def acquire(self) -> None:
        with self._cond:
            while self._value <= 0:
                self._cond.wait()
            self._value -= 1

    def release(self) -> None:
        with self._cond:
            # BoundedSemaphore semantics: an unbalanced release is a bug in the
            # caller, and silently inflating capacity would raise the effective
            # concurrency limit for the rest of the process's life.
            if self._value >= self._limit:
                raise ValueError("semaphore released too many times")
            self._value += 1
            self._cond.notify()

    def resize(self, new_limit: int, current_limit: int) -> None:
        with self._cond:
            self._value += new_limit - current_limit
            self._limit = new_limit
            self._cond.notify_all()

    def __enter__(self) -> "_ResizableSemaphore":
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


_SETTINGS_CACHE_TTL_SECONDS = 5.0
_cached_api_concurrency_limit: int | None = None
_cached_api_concurrency_limit_at: float = 0.0
_settings_cache_lock = threading.Lock()

_semaphore: _ResizableSemaphore | None = None
_semaphore_limit: int | None = None
_semaphore_lock = threading.Lock()
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


@dataclass(frozen=True)
class AuthRefreshOutcome:
    status: str
    access_changed: bool = False
    session_changed: bool = False
    token_expires: str | None = None
    error: str | None = None
    cooldown_until: str | None = None

    @property
    def retry_original(self) -> bool:
        return self.status in {"refreshed", "reused"}


def _clamp_limit(value: int) -> int:
    return min(max(value, 1), 10)


def _read_api_concurrency_limit_sync() -> int:
    try:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM settings WHERE key = 'api_concurrency'").fetchone()
        conn.close()
        if row:
            return _clamp_limit(int(row["value"]))
    except Exception:
        pass
    return DEFAULT_API_CONCURRENCY


def _get_cached_api_concurrency_limit() -> int:
    """Read ``api_concurrency`` with a short TTL cache.

    ``_get_semaphore`` used to open and close a SQLite connection on every
    single ChatGPT API call just to re-read one setting row. A short TTL is
    enough since this only guards a concurrency limit, not correctness.
    """

    global _cached_api_concurrency_limit, _cached_api_concurrency_limit_at

    now = time.monotonic()
    with _settings_cache_lock:
        if (
            _cached_api_concurrency_limit is not None
            and (now - _cached_api_concurrency_limit_at) < _SETTINGS_CACHE_TTL_SECONDS
        ):
            return _cached_api_concurrency_limit

    limit = _read_api_concurrency_limit_sync()
    with _settings_cache_lock:
        _cached_api_concurrency_limit = limit
        _cached_api_concurrency_limit_at = now
    return limit


def _get_semaphore() -> _ResizableSemaphore:
    global _semaphore, _semaphore_limit

    limit = _get_cached_api_concurrency_limit()
    with _semaphore_lock:
        if _semaphore is None:
            _semaphore = _ResizableSemaphore(limit)
            _semaphore_limit = limit
        elif _semaphore_limit != limit:
            _semaphore.resize(limit, _semaphore_limit)
            _semaphore_limit = limit
        return _semaphore


def _get_refresh_lock(team_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        lock = _refresh_locks.get(team_id)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[team_id] = lock
        return lock


def _is_unauthorized_result(result: Any) -> bool:
    if not isinstance(result, dict) or "error" not in result:
        return False
    if result.get("status_code") == 401:
        return True
    message = str(result.get("error") or "").lower()
    return "401 client error" in message or "unauthorized" in message


def _bound_chatgpt_client(func: Callable[..., Any]) -> ChatGPTClient | None:
    client = getattr(func, "__self__", None)
    return client if isinstance(client, ChatGPTClient) else None


def _log_operation_sync(
    team_id: str,
    action: str,
    detail: str | None,
    result: str,
    error_message: str | None = None,
    trigger_type: str = "auto_refresh",
) -> None:
    try:
        conn = sqlite3.connect(get_db_path())
        _insert_operation_log(
            conn,
            team_id,
            action,
            detail,
            result,
            error_message,
            trigger_type,
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _insert_operation_log(
    conn: sqlite3.Connection,
    team_id: str,
    action: str,
    detail: str | None,
    result: str,
    error_message: str | None,
    trigger_type: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO operation_logs
           (team_id, action, target_email, detail, result, error_message, trigger_type, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (team_id, action, None, detail, result, error_message, trigger_type, now),
    )


def _proxy_url_sync(conn: sqlite3.Connection, proxy_id: int | None) -> str | None:
    if not proxy_id:
        return None
    row = conn.execute("SELECT url FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
    return row["url"] if row else None


def _decode_token_expires(access_token: str) -> str | None:
    return _decode_token_times(access_token)[1]


def _decode_token_times(access_token: str) -> tuple[str | None, str | None]:
    try:
        payload = jwt.decode(access_token, options={"verify_signature": False})
        issued_at = payload.get("iat")
        exp = payload.get("exp")
        issued_at_iso = (
            datetime.fromtimestamp(issued_at, tz=timezone.utc).isoformat()
            if issued_at
            else None
        )
        expires_iso = (
            datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
            if exp
            else None
        )
        return issued_at_iso, expires_iso
    except Exception:
        return None, None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _access_token_expired(access_token: str, now: datetime) -> bool:
    expires_at = _parse_iso(_decode_token_expires(access_token))
    return bool(expires_at and expires_at <= now)


def _access_token_due_for_refresh(access_token: str, now: datetime) -> bool:
    expires_at = _parse_iso(_decode_token_expires(access_token))
    return bool(expires_at and expires_at <= now + PROACTIVE_REFRESH_WINDOW)


def _cooldown_until(
    conn: sqlite3.Connection,
    team_id: str,
    now: datetime,
    trigger: str,
) -> datetime | None:
    """冷却时长由**本次**尝试的 trigger 决定，不看上一条日志是什么。

    主动刷新（``scheduled_expiry_refresh``）是机会性的，失败一次冷却 1 小时没问
    题；但业务接口真的 401 时必须尽快救回来。按历史 action 取冷却时长会让一次
    ``partial``（session 轮换、access token 未变，这是主动刷新的常见结果）把随后
    一小时内的所有 401 重试一并挡掉，Team 会持续 401 且不再尝试自救。
    """
    row = conn.execute(
        """SELECT action, result, created_at
           FROM operation_logs
           WHERE team_id = ?
             AND action IN (
                 'token_auto_refresh', 'token_proactive_refresh',
                 'refresh_token', 'token_refresh'
             )
           ORDER BY id DESC
           LIMIT 1""",
        (team_id,),
    ).fetchone()
    if not row or row["result"] not in _NEGATIVE_REFRESH_RESULTS:
        return None
    attempted_at = _parse_iso(row["created_at"])
    if not attempted_at:
        return None
    cooldown = (
        PROACTIVE_REFRESH_COOLDOWN
        if trigger == "scheduled_expiry_refresh"
        else AUTH_REFRESH_COOLDOWN
    )
    until = attempted_at + cooldown
    return until if until > now else None


def _refresh_log_fields(
    trigger: str,
    access_changed: bool,
    session_changed: bool,
    access_token: str,
) -> str:
    issued_at, expires_at = _decode_token_times(access_token)
    return "; ".join(
        (
            f"trigger={trigger}",
            f"access_changed={int(access_changed)}",
            f"session_changed={int(session_changed)}",
            f"access_iat={issued_at or 'unknown'}",
            f"access_exp={expires_at or 'unknown'}",
        )
    )


def _refresh_log_identity(trigger: str) -> tuple[str, str]:
    if trigger == "api_401_retry":
        return "token_auto_refresh", "auto_refresh"
    if trigger == "scheduled_expiry_refresh":
        return "token_proactive_refresh", "auto_refresh"
    return "refresh_token", "manual"


def _mark_team_auth_expired(conn: sqlite3.Connection, team_id: str) -> None:
    conn.execute("UPDATE teams SET status = 'token_expired' WHERE id = ?", (team_id,))
    try:
        conn.execute("DELETE FROM patrol_team_baselines WHERE team_id = ?", (team_id,))
    except sqlite3.OperationalError:
        pass


def _mark_team_auth_rejected(
    conn: sqlite3.Connection,
    team_id: str,
    now: datetime,
) -> None:
    """标记「token 被上游吊销」。刻意不动 status，见 database.py 的迁移注释。

    ``auth_state_since`` 只在第一次进入 rejected 时写入，后续重复检测不刷新它，
    界面上的「已持续 N 小时」才是从头算起而不是每轮归零。
    """
    try:
        conn.execute(
            """UPDATE teams
               SET auth_state = 'rejected',
                   auth_state_since = COALESCE(auth_state_since, ?)
               WHERE id = ? AND COALESCE(auth_state, 'ok') != 'rejected'""",
            (now.isoformat(), team_id),
        )
        # 已经是 rejected 的行上面那条不会命中，补一次纯状态写入保证幂等。
        conn.execute(
            "UPDATE teams SET auth_state = 'rejected' WHERE id = ?",
            (team_id,),
        )
    except sqlite3.OperationalError:
        # 迁移尚未跑到的旧库：授权状态只是观测信息，不该让刷新流程失败。
        pass


def _clear_team_auth_rejected(conn: sqlite3.Connection, team_id: str) -> None:
    """换到新的 access token 即视为恢复。"""
    try:
        conn.execute(
            "UPDATE teams SET auth_state = 'ok', auth_state_since = NULL WHERE id = ?",
            (team_id,),
        )
    except sqlite3.OperationalError:
        pass


def refresh_team_auth_sync(
    team_id: str,
    *,
    trigger: str,
    force: bool = False,
    client: ChatGPTClient | None = None,
    stale_access_token: str | None = None,
) -> AuthRefreshOutcome:
    """Refresh a Team once, with token-change detection and durable cooldown."""
    with _get_refresh_lock(team_id):
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT access_token, session_token, proxy_id FROM teams WHERE id = ?",
                (team_id,),
            ).fetchone()
            if not row:
                return AuthRefreshOutcome(status="not_found", error="Team not found")

            db_access_token = row["access_token"]
            if client is not None:
                if stale_access_token and client.access_token != stale_access_token:
                    return AuthRefreshOutcome(
                        status="reused",
                        token_expires=_decode_token_expires(client.access_token),
                    )
                compared_token = stale_access_token or client.access_token
                if db_access_token and db_access_token != compared_token:
                    client.update_access_token(db_access_token)
                    return AuthRefreshOutcome(
                        status="reused",
                        token_expires=_decode_token_expires(db_access_token),
                    )

            now = datetime.now(timezone.utc)
            if not force:
                cooldown_until = _cooldown_until(conn, team_id, now, trigger)
                if cooldown_until:
                    return AuthRefreshOutcome(
                        status="cooldown",
                        error="Auth refresh is cooling down after a recent unsuccessful attempt",
                        cooldown_until=cooldown_until.isoformat(),
                        token_expires=_decode_token_expires(db_access_token),
                    )

            proxy_url = _proxy_url_sync(conn, row["proxy_id"])
            try:
                refresh_result = ChatGPTClient.refresh_token(
                    row["session_token"],
                    proxy_url,
                )
            except Exception as exc:
                refresh_result = {"error": str(exc)}

            action, trigger_type = _refresh_log_identity(trigger)
            if not isinstance(refresh_result, dict):
                refresh_result = {"error": "Invalid auth session response"}

            if "error" in refresh_result:
                error = str(refresh_result.get("error") or "Unknown auth refresh error")
                # A transient refresh/network failure does not prove that the
                # browser session is dead. Only an explicit session-endpoint
                # 401 may invalidate a Team, and only when the current access
                # token is already unusable (expired or rejected by an API).
                session_rejected = refresh_result.get("status_code") == 401
                access_unusable = (
                    trigger == "api_401_retry"
                    or _access_token_expired(db_access_token, now)
                )
                if session_rejected and access_unusable:
                    _mark_team_auth_expired(conn, team_id)
                _insert_operation_log(
                    conn,
                    team_id,
                    action,
                    f"trigger={trigger}",
                    "failed",
                    error,
                    trigger_type,
                )
                conn.commit()
                return AuthRefreshOutcome(
                    status="failed",
                    error=error,
                    token_expires=_decode_token_expires(db_access_token),
                    cooldown_until=(now + AUTH_REFRESH_COOLDOWN).isoformat(),
                )

            new_access_token = refresh_result.get("accessToken") or row["access_token"]
            new_session_token = refresh_result.get("sessionToken") or row["session_token"]
            session_has_access = bool(refresh_result.get("accessToken"))
            access_changed = new_access_token != row["access_token"]
            session_changed = new_session_token != row["session_token"]
            token_expires = _decode_token_expires(new_access_token)
            detail = _refresh_log_fields(
                trigger,
                access_changed,
                session_changed,
                new_access_token,
            )

            if access_changed or session_changed:
                conn.execute(
                    """UPDATE teams SET access_token = ?, session_token = ?,
                       token_expires = ?, updated_at = ?,
                       status = CASE WHEN ? THEN 'active' ELSE status END
                       WHERE id = ?""",
                    (
                        new_access_token,
                        new_session_token,
                        token_expires,
                        now.isoformat(),
                        int(access_changed),
                        team_id,
                    ),
                )

            if access_changed:
                result_status = "success"
                outcome_status = "refreshed"
            elif session_changed:
                result_status = "partial"
                outcome_status = "session_rotated"
            else:
                result_status = "unchanged"
                outcome_status = "unchanged"

            # A successful /api/auth/session response that returns the same
            # access token is still a live rolling browser session. Do not
            # confuse "no new short-lived token yet" with session expiry.
            # An empty auth-session response is conclusive only after the
            # current access token is unusable.
            if not session_has_access and (
                trigger == "api_401_retry"
                or _access_token_expired(new_access_token, now)
            ):
                _mark_team_auth_expired(conn, team_id)

            # 第三种情况：会话端点正常应答、也确实交回了 access token，但那个 token
            # 和库里那个一模一样，而业务接口刚刚 401。此时 token 本身往往还没到
            # 期（JWT exp 还在未来），是上游把它吊销了，滚动会话已经换不出新的。
            # 既不是"会话还活着"也不是"会话已死"，单独记一个授权状态，
            # status 保持 active 以免掉出 scheduler 的扫描范围。
            if trigger == "api_401_retry" and session_has_access and not access_changed:
                _mark_team_auth_rejected(conn, team_id, now)
            elif access_changed:
                _clear_team_auth_rejected(conn, team_id)

            _insert_operation_log(
                conn,
                team_id,
                action,
                detail,
                result_status,
                None,
                trigger_type,
            )
            conn.commit()

            if access_changed or session_changed:
                try:
                    update_session_file_tokens(
                        team_id,
                        new_access_token,
                        new_session_token,
                    )
                except Exception as exc:
                    _log_operation_sync(
                        team_id,
                        "session_file_update",
                        None,
                        "failed",
                        str(exc),
                        trigger_type,
                    )
            if access_changed and client is not None:
                client.update_access_token(new_access_token)

            cooldown_until = None
            if not access_changed:
                cooldown_until = (now + AUTH_REFRESH_COOLDOWN).isoformat()
            return AuthRefreshOutcome(
                status=outcome_status,
                access_changed=access_changed,
                session_changed=session_changed,
                token_expires=token_expires,
                cooldown_until=cooldown_until,
            )
        finally:
            conn.close()


async def refresh_team_auth(
    team_id: str,
    *,
    trigger: str,
    force: bool = False,
) -> AuthRefreshOutcome:
    loop = asyncio.get_running_loop()
    bound = partial(
        refresh_team_auth_sync,
        team_id,
        trigger=trigger,
        force=force,
    )
    return await loop.run_in_executor(None, bound)


def run_chatgpt_call_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    semaphore = _get_semaphore()
    with semaphore:
        client = _bound_chatgpt_client(func)
        stale_access_token = client.access_token if client is not None else None

        # The access token is short-lived while the browser session can roll
        # for months. Refresh from that long-lived session before access-token
        # expiry instead of waiting for a business API to return 401.
        if (
            client is not None
            and stale_access_token
            and _access_token_due_for_refresh(
                stale_access_token,
                datetime.now(timezone.utc),
            )
        ):
            refresh_team_auth_sync(
                client.team_id,
                trigger="scheduled_expiry_refresh",
                client=client,
                stale_access_token=stale_access_token,
            )
            stale_access_token = client.access_token

        result = func(*args, **kwargs)
        if _is_unauthorized_result(result):
            refresh_outcome = (
                refresh_team_auth_sync(
                    client.team_id,
                    trigger="api_401_retry",
                    client=client,
                    stale_access_token=stale_access_token,
                )
                if client is not None
                else AuthRefreshOutcome(status="not_available")
            )
            if refresh_outcome.retry_original:
                result = func(*args, **kwargs)
            if client is not None:
                if _is_unauthorized_result(result):
                    suffix_by_status = {
                        "refreshed": "（自动刷新 Token 后重试仍失败）",
                        "reused": "（复用其他请求刷新的 Token 后重试仍失败）",
                        "cooldown": "（近期刷新未成功，冷却期内未重复刷新）",
                        "unchanged": "（Session 接口未返回新的 access token）",
                        "session_rotated": "（仅轮换 session token，access token 未更新）",
                        "failed": "（自动刷新 Token 失败）",
                    }
                    suffix = suffix_by_status.get(
                        refresh_outcome.status,
                        "（无法自动刷新 Token）",
                    )
                    report_team_failure_sync(
                        client.team_id,
                        "chatgpt_auth",
                        f"{result.get('error') or '401 Unauthorized'}{suffix}",
                        source="api_401_retry",
                    )
                else:
                    report_team_recovery_sync(
                        client.team_id,
                        "chatgpt_auth",
                        source="api_401_retry",
                    )
        return result


async def run_chatgpt_call(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    loop = asyncio.get_running_loop()
    bound = partial(run_chatgpt_call_sync, func, *args, **kwargs)
    return await loop.run_in_executor(None, bound)
