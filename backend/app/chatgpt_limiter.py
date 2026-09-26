import asyncio
import json
import logging
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
from .chatgpt_client import (
    REFRESH_DIAGNOSTICS_KEY,
    ChatGPTClient,
    access_token_summary,
)
from .services.team_health_alerts import (
    report_team_failure_sync,
    report_team_recovery_sync,
)


T = TypeVar("T")
logger = logging.getLogger(__name__)
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
    # 这次刷新把 Team 判成了 auth_state='rejected'：现有 access token 已不可用，
    # 会话端点又明确交不出新的。调用方据此给出"需重新导入"而不是笼统的 502。
    auth_rejected: bool = False

    @property
    def retry_original(self) -> bool:
        # superseded：刷新期间行上的 token 已被重新导入换掉，调用方已改用库里那个新
        # token，和 reused 一样值得用它重试一次。
        return self.status in {"refreshed", "reused", "superseded"}


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


def _session_answered_without_new_token(
    refresh_result: dict,
    current_access_token: str | None,
) -> bool:
    """会话端点正常应答（2xx），但明确没有交回新的 access token。

    典型是 NextAuth 的 ``{"error": "RefreshAccessTokenError"}``：HTTP 200，body 里
    带 error，不带新 token。超时、断网、429、5xx 要么没有 status_code，要么不是
    2xx，都不算——那些只说明这一次没问成，不说明会话换不出 token。
    """
    status_code = refresh_result.get("status_code")
    if not (isinstance(status_code, int) and 200 <= status_code < 300):
        return False
    returned = refresh_result.get("accessToken")
    return not returned or returned == current_access_token


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


def refresh_diagnostic_fields(refresh_result: Any, current_access_token: str | None) -> dict:
    """refresh_token() 附带的诊断摘要，加上需要库里现有 access token 才算得出的几项。

    全是哈希、长度、布尔值、时间和键名，可以原样进日志。
    """
    is_dict = isinstance(refresh_result, dict)
    diag = refresh_result.get(REFRESH_DIAGNOSTICS_KEY) if is_dict else None
    fields = dict(diag) if isinstance(diag, dict) else {"diag_missing": True}
    returned = refresh_result.get("accessToken") if is_dict else None
    fields["access_changed"] = bool(returned) and returned != current_access_token
    current = access_token_summary(current_access_token)
    fields["cur_access_exp"] = current["exp"]
    fields["cur_finalizer_claim"] = current["finalizer_claim"]
    return fields


def _diag_flag(value: Any) -> str:
    if value is None:
        return "-"
    return "1" if value else "0"


def _diag_fingerprint(fields: dict, prefix: str) -> str:
    length = fields.get(f"{prefix}_len")
    return f"{length}:{fields.get(f'{prefix}_sha8')}" if length else "-"


def format_refresh_diagnostics(fields: dict) -> str:
    """operation_logs.detail 里的一段紧凑摘要（access_changed 由调用方自己写在前缀里）。"""
    sc_source = fields.get("sc_session_source") or "none"
    if sc_source == "none":
        sc_session = "-"
    else:
        chunk_count = fields.get("sc_session_chunks") if sc_source == "chunks" else ""
        sc_session = f"{sc_source}{chunk_count}:{_diag_fingerprint(fields, 'sc_session')}"
    keys = fields.get("json_keys")
    cookie_names = fields.get("set_cookie_names")
    parts = [
        f"http={fields.get('http_status') or '-'}",
        f"keys={','.join(keys) if keys else '-'}",
        f"err={fields.get('error') or '-'}",
    ]
    if fields.get("exc_type"):
        parts.append(f"exc={fields['exc_type']}")
    parts += [
        f"access={'present' if fields.get('access_present') else 'absent'}",
        f"resp_iat={fields.get('access_iat') or '-'}",
        f"resp_exp={fields.get('access_exp') or '-'}",
        f"fin={_diag_flag(fields.get('access_finalizer_claim'))}",
        f"cur_exp={fields.get('cur_access_exp') or '-'}",
        f"cur_fin={_diag_flag(fields.get('cur_finalizer_claim'))}",
        f"set_cookies={','.join(cookie_names) if cookie_names else '-'}",
        f"sc_session={sc_session}",
        f"sc_cleared={_diag_flag(fields.get('sc_session_cleared'))}",
        f"json_session={_diag_fingerprint(fields, 'json_session')}",
        f"sent_session={_diag_fingerprint(fields, 'sent_session')}",
        f"sc_eq_json={_diag_flag(fields.get('sc_eq_json'))}",
        f"sc_eq_sent={_diag_flag(fields.get('sc_eq_sent'))}",
        f"json_eq_sent={_diag_flag(fields.get('json_eq_sent'))}",
    ]
    if fields.get("diag_missing"):
        parts.insert(0, "missing=1")
    return "diag: " + " ".join(parts)


def log_refresh_diagnostics(
    team_id: str,
    trigger: str,
    result: str,
    fields: dict,
    **extra: Any,
) -> None:
    """每次真正打到 /api/auth/session 的刷新写一行 JSON 到服务日志。

    用 WARNING 是因为 uvicorn 只给它自己的 logger 配了 handler，``app.*`` 的 INFO
    没有 handler 会被直接丢掉；WARNING 及以上经 logging.lastResort 落到 stderr，
    也就是 journald。Team 只记 id 前 8 位，完整对应关系看同一次写入的
    operation_logs 行。
    """
    record = {
        "team": str(team_id or "")[:8],
        "trigger": trigger,
        "result": result,
        **extra,
        **fields,
    }
    try:
        logger.warning(
            "auth_refresh_diag %s",
            json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str),
        )
    except Exception:
        pass


def _begin_refresh_write(
    conn: sqlite3.Connection,
    team_id: str,
    session_token: str | None,
    access_token: str | None,
) -> bool:
    """开写事务，并确认库里仍是发起刷新时读到的那对 token。

    刷新要在网络上等最多 60 秒，期间操作员可能重新导入了 session（导入路径不拿
    刷新锁）。旧会话换来的结果——新 token、rejected / token_expired 判定——只对旧
    会话成立，不能落到新导入的行上。BEGIN IMMEDIATE 先拿写锁，比较与随后所有写入
    之间不会再有别的写者插进来。
    """
    conn.execute("BEGIN IMMEDIATE")
    current = conn.execute(
        "SELECT session_token, access_token FROM teams WHERE id = ?",
        (team_id,),
    ).fetchone()
    return bool(
        current
        and current["session_token"] == session_token
        and current["access_token"] == access_token
    )


def _finish_superseded(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    trigger: str,
    action: str,
    trigger_type: str,
    diag_fields: dict,
    client: ChatGPTClient | None,
) -> AuthRefreshOutcome:
    """刷新结果已过时：只记一条 superseded 日志，不动 token、status、auth_state。

    superseded 不在 _NEGATIVE_REFRESH_RESULTS 里，不会触发冷却，也不算失败。
    调用方手上若有 client，就换成库里新导入的 access token。
    """
    current = conn.execute(
        "SELECT access_token FROM teams WHERE id = ?",
        (team_id,),
    ).fetchone()
    if current is None:
        # 刷新途中 Team 被删了：不能按 superseded 重试，否则拿旧 token 再撞一次 401，
        # 还会给一个已不存在的 Team 开健康告警。
        conn.commit()
        return AuthRefreshOutcome(status="not_found", error="Team not found")
    current_access_token = current["access_token"]
    _insert_operation_log(
        conn,
        team_id,
        action,
        f"trigger={trigger}; superseded=1; "
        f"access_changed={int(diag_fields['access_changed'])}; "
        f"{format_refresh_diagnostics(diag_fields)}",
        "superseded",
        None,
        trigger_type,
    )
    conn.commit()
    log_refresh_diagnostics(team_id, trigger, "superseded", diag_fields)
    if (
        client is not None
        and current_access_token
        and client.access_token != current_access_token
    ):
        client.update_access_token(current_access_token)
    return AuthRefreshOutcome(
        status="superseded",
        token_expires=(
            _decode_token_expires(current_access_token) if current_access_token else None
        ),
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
            diag_fields = refresh_diagnostic_fields(refresh_result, db_access_token)
            diag_detail = format_refresh_diagnostics(diag_fields)

            # 下面每一种写入（新 token、token_expired、rejected、清除 rejected）都只对
            # 发起刷新时那对 token 成立；行已被重新导入换掉就整体作废。
            if not _begin_refresh_write(
                conn, team_id, row["session_token"], db_access_token
            ):
                return _finish_superseded(
                    conn,
                    team_id=team_id,
                    trigger=trigger,
                    action=action,
                    trigger_type=trigger_type,
                    diag_fields=diag_fields,
                    client=client,
                )

            if "error" in refresh_result:
                error = str(refresh_result.get("error") or "Unknown auth refresh error")
                # A transient refresh/network failure does not prove that the
                # browser session is dead. A Team is only classified when the
                # current access token is already unusable (expired or rejected
                # by an API) AND the session endpoint gave an explicit answer:
                # - HTTP 401: the browser session itself is dead -> token_expired.
                # - 2xx carrying an error and no new access token (NextAuth's
                #   RefreshAccessTokenError): the session still answers but can
                #   no longer mint a token -> auth_state='rejected', status stays
                #   active so the scheduler keeps probing and can self-heal.
                session_rejected = refresh_result.get("status_code") == 401
                session_gave_no_token = _session_answered_without_new_token(
                    refresh_result,
                    db_access_token,
                )
                access_unusable = (
                    trigger == "api_401_retry"
                    or _access_token_expired(db_access_token, now)
                )
                auth_rejected = False
                marked_expired = False
                if access_unusable and session_rejected:
                    _mark_team_auth_expired(conn, team_id)
                    marked_expired = True
                elif access_unusable and session_gave_no_token:
                    _mark_team_auth_rejected(conn, team_id, now)
                    auth_rejected = True
                _insert_operation_log(
                    conn,
                    team_id,
                    action,
                    f"trigger={trigger}; "
                    f"access_changed={int(diag_fields['access_changed'])}; "
                    f"{diag_detail}",
                    "failed",
                    error,
                    trigger_type,
                )
                conn.commit()
                log_refresh_diagnostics(
                    team_id,
                    trigger,
                    "failed",
                    diag_fields,
                    auth_rejected=auth_rejected,
                    marked_token_expired=marked_expired,
                )
                return AuthRefreshOutcome(
                    status="failed",
                    error=error,
                    token_expires=_decode_token_expires(db_access_token),
                    cooldown_until=(now + AUTH_REFRESH_COOLDOWN).isoformat(),
                    auth_rejected=auth_rejected,
                )

            new_access_token = refresh_result.get("accessToken") or row["access_token"]
            new_session_token = refresh_result.get("sessionToken") or row["session_token"]
            session_has_access = bool(refresh_result.get("accessToken"))
            access_changed = new_access_token != row["access_token"]
            session_changed = new_session_token != row["session_token"]
            token_expires = _decode_token_expires(new_access_token)
            detail = (
                _refresh_log_fields(
                    trigger,
                    access_changed,
                    session_changed,
                    new_access_token,
                )
                + "; "
                + diag_detail
            )

            if access_changed or session_changed:
                # Compare-and-set on the session token this refresh started from.
                # _begin_refresh_write already holds the write lock after the same
                # check, so this cannot miss in practice; it keeps the write itself
                # safe if the surrounding guard is ever reshuffled.
                updated = conn.execute(
                    """UPDATE teams SET access_token = ?, session_token = ?,
                       token_expires = ?, updated_at = ?,
                       status = CASE WHEN ? THEN 'active' ELSE status END
                       WHERE id = ? AND session_token IS ?""",
                    (
                        new_access_token,
                        new_session_token,
                        token_expires,
                        now.isoformat(),
                        int(access_changed),
                        team_id,
                        row["session_token"],
                    ),
                ).rowcount
                if updated == 0:
                    return _finish_superseded(
                        conn,
                        team_id=team_id,
                        trigger=trigger,
                        action=action,
                        trigger_type=trigger_type,
                        diag_fields=diag_fields,
                        client=client,
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
            access_unusable = (
                trigger == "api_401_retry"
                or _access_token_expired(new_access_token, now)
            )
            marked_expired = False
            if not session_has_access and access_unusable:
                _mark_team_auth_expired(conn, team_id)
                marked_expired = True

            # 第三种情况：会话端点正常应答、也确实交回了 access token，但那个 token
            # 和库里那个一模一样，而它已经用不了（业务接口刚刚 401，或 JWT exp 已过）。
            # 401 时 token 往往还没到期，是上游把它吊销了；已过期时同样说明滚动会话
            # 已经换不出新的。既不是"会话还活着"也不是"会话已死"，单独记一个授权
            # 状态，status 保持 active 以免掉出 scheduler 的扫描范围。
            auth_rejected = False
            if access_unusable and session_has_access and not access_changed:
                _mark_team_auth_rejected(conn, team_id, now)
                auth_rejected = True
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

            # 会话文件在提交前、仍持有写锁时更新：重新导入是"先写库、后写文件"，
            # 它的库写入要等这里提交，于是它的文件写入一定排在这次之后，旧会话不会
            # 盖掉新导入的文件。失败日志走同一个连接——另开连接会被自己的写锁挡住。
            if access_changed or session_changed:
                try:
                    update_session_file_tokens(
                        team_id,
                        new_access_token,
                        new_session_token,
                    )
                except Exception as exc:
                    _insert_operation_log(
                        conn,
                        team_id,
                        "session_file_update",
                        None,
                        "failed",
                        str(exc),
                        trigger_type,
                    )
            conn.commit()
            log_refresh_diagnostics(
                team_id,
                trigger,
                result_status,
                diag_fields,
                session_changed=session_changed,
                auth_rejected=auth_rejected,
                marked_token_expired=marked_expired,
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
                auth_rejected=auth_rejected,
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
                        "superseded": "（刷新期间 Session 已被重新导入，改用新导入的 Token 重试仍失败）",
                    }
                    suffix = (
                        "（登录已失效，需重新导入）"
                        if refresh_outcome.auth_rejected and refresh_outcome.status == "failed"
                        else suffix_by_status.get(
                            refresh_outcome.status,
                            "（无法自动刷新 Token）",
                        )
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
