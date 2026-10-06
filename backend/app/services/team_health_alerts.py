"""Durable, de-duplicated Telegram alerts for Team health incidents.

Only final failures should enter this service.  Callers are responsible for
performing any automatic retry first.  Repeated failures update the incident
counter without repeatedly messaging admins; the first later success sends one
recovery message and closes the incident.
"""

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from ..database import get_db_path
from ..tg_format import detail_card
from .tg_notify import notify_admins_sync

# 向后兼容：模块级占位符，测试可能会 patch 它
DB_PATH = None

_INCIDENT_LOCK = threading.Lock()
_MAX_ERROR_LENGTH = 500

# (team_id, alert_key) pairs for which a thread has claimed the right to send
# the Telegram notification and is currently doing so (outside _INCIDENT_LOCK,
# since notify_admins_sync is a blocking network call). Guarded by
# _INCIDENT_LOCK just like the DB row itself, so "claim, then notify, then
# release" still gives exactly one sender per open incident even though the
# lock is no longer held for the whole duration of the send.
_SENDING_INCIDENTS: set[tuple[str, str]] = set()

# 一个未恢复的故障每隔多久重新提醒一次。只发一次的话，故障发生在深夜就等于没发：
# 消息被后来的通知顶走，第二天没人知道还坏着。
REPEAT_ALERT_INTERVAL = timedelta(hours=6)

_ALERT_LABELS = {
    "chatgpt_auth": "ChatGPT 鉴权",
    "team_sync": "Team 数据同步",
    "seats_entitled": "席位数（seats_entitled）",
}


def is_auth_error(error: Any) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code == 401:
        return True
    detail = getattr(error, "detail", None)
    if isinstance(detail, dict) and detail.get("code") == "team_auth_rejected":
        return True
    text = str(detail if detail is not None else error).lower()
    return "401" in text or "unauthorized" in text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    # Defensive for hot-deployed workers that start before init_database has
    # applied the schema.  CREATE IF NOT EXISTS is idempotent.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS team_health_incidents (
               team_id TEXT NOT NULL,
               alert_key TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'open',
               first_failed_at TEXT,
               last_failed_at TEXT,
               last_error TEXT,
               last_source TEXT,
               failure_count INTEGER NOT NULL DEFAULT 1,
               notified INTEGER NOT NULL DEFAULT 0,
               resolved_at TEXT,
               updated_at TEXT,
               PRIMARY KEY (team_id, alert_key)
           )"""
    )
    # 最近一次向管理员发出提醒的时间，用于按固定间隔重复提醒。
    try:
        conn.execute(
            "ALTER TABLE team_health_incidents ADD COLUMN last_notified_at TEXT"
        )
    except sqlite3.OperationalError:
        pass


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _humanize_since(started_at: Optional[datetime], now: datetime) -> str:
    if not started_at:
        return ""
    minutes = int((now - started_at).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} 分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时"
    return f"{hours // 24} 天"


def _team_name(conn: sqlite3.Connection, team_id: str) -> str:
    try:
        row = conn.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
        if row and row["name"]:
            return str(row["name"])
    except Exception:
        pass
    return team_id


def _log_delivery(
    conn: sqlite3.Connection,
    team_id: str,
    action: str,
    detail: str,
    result: str,
    error_message: Optional[str] = None,
) -> None:
    try:
        conn.execute(
            """INSERT INTO operation_logs
               (team_id, action, target_email, detail, result, error_message,
                trigger_type, created_at)
               VALUES (?, ?, NULL, ?, ?, ?, 'team_health', ?)""",
            (team_id, action, detail, result, error_message, _now_iso()),
        )
        conn.commit()
    except Exception:
        pass


def report_team_failure_sync(
    team_id: str,
    alert_key: str,
    error: Any,
    *,
    source: str,
    notify_interval: Optional[timedelta] = None,
    render: Optional[Callable[[bool], str]] = None,
    notify: Optional[Callable[[str], int]] = None,
) -> dict:
    """Open/update an incident and notify only when it has not been delivered.

    ``notify_interval``: for conditions that can flap between rounds. Admins hear
    about this (team, alert_key) at most once per interval, counted from the last
    delivered alert, and a recovery in between does not reopen the window. It also
    replaces REPEAT_ALERT_INTERVAL as the reminder interval for an open incident.

    ``render(is_reminder) -> text`` replaces the generic "Team 异常" card for alert
    families that are not failures (e.g. patrol's Premium seat alerts); ``notify``
    replaces notify_admins_sync and must return the number of admins reached.
    The incident / throttle bookkeeping is the same either way.
    """
    if not team_id:
        return {"notified": 0, "reason": "missing_team_id"}

    now = _now_iso()
    error_text = str(getattr(error, "detail", error) or "unknown error")[:_MAX_ERROR_LENGTH]
    incident_key = (team_id, alert_key)

    # ── Critical section: decide whether this call is the one that gets to
    # notify, and persist the failure-counter update. Kept short and non-
    # blocking so the lock is never held across the Telegram call below. ──
    with _INCIDENT_LOCK:
        conn = _connect()
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM team_health_incidents WHERE team_id = ? AND alert_key = ?",
                (team_id, alert_key),
            ).fetchone()
            now_dt = _parse_iso(now) or datetime.now(timezone.utc)
            first_failed_at = None
            is_reminder = False
            # 上一次真正发出去的时间。事件恢复后再开新事件时这一列不会被清掉，
            # notify_interval 靠它跨事件限频。
            last_notified_at = None
            if row is not None and "last_notified_at" in row.keys():
                last_notified_at = _parse_iso(row["last_notified_at"])
            within_notify_interval = bool(
                notify_interval is not None
                and last_notified_at is not None
                and now_dt - last_notified_at < notify_interval
            )
            if row and row["status"] == "open":
                failure_count = int(row["failure_count"] or 0) + 1
                first_failed_at = _parse_iso(row["first_failed_at"])
                # 已经通知过的故障默认静音，但静音有期限：超过 REPEAT_ALERT_INTERVAL
                # 仍未恢复就再提醒一次，免得一条深夜的告警被顶走后再无人知晓。
                already_notified = bool(row["notified"])
                if already_notified:
                    reference = last_notified_at or first_failed_at
                    interval = notify_interval or REPEAT_ALERT_INTERVAL
                    if reference and now_dt - reference >= interval:
                        already_notified = False
                        is_reminder = True
                elif within_notify_interval:
                    # 这次事件开在限频窗口里（中间恢复过又坏了），还没提醒过，继续静音。
                    already_notified = True
                conn.execute(
                    """UPDATE team_health_incidents
                       SET last_failed_at = ?, last_error = ?, last_source = ?,
                           failure_count = ?, updated_at = ?
                       WHERE team_id = ? AND alert_key = ?""",
                    (now, error_text, source, failure_count, now, team_id, alert_key),
                )
            else:
                failure_count = 1
                # 新事件：notify_interval 窗口内恢复过又坏了，就不再提醒。新事件的
                # notified 仍记 0，于是它恢复时也不会再发一条"恢复"。
                already_notified = within_notify_interval
                conn.execute(
                    """INSERT INTO team_health_incidents
                       (team_id, alert_key, status, first_failed_at, last_failed_at,
                        last_error, last_source, failure_count, notified, resolved_at, updated_at)
                       VALUES (?, ?, 'open', ?, ?, ?, ?, 1, 0, NULL, ?)
                       ON CONFLICT(team_id, alert_key) DO UPDATE SET
                           status = 'open', first_failed_at = excluded.first_failed_at,
                           last_failed_at = excluded.last_failed_at,
                           last_error = excluded.last_error, last_source = excluded.last_source,
                           failure_count = 1, notified = 0, resolved_at = NULL,
                           updated_at = excluded.updated_at""",
                    (team_id, alert_key, now, now, error_text, source, now),
                )
            conn.commit()
            team_name = _team_name(conn, team_id)

            if already_notified:
                return {
                    "notified": 0,
                    "reason": "deduplicated",
                    "failure_count": failure_count,
                }

            # Another thread is already mid-send for this exact incident (DB
            # still shows notified=0 until that send finishes) — don't send
            # twice. This is the in-memory equivalent of the "already_notified"
            # check above, for the window between claiming and persisting it.
            if incident_key in _SENDING_INCIDENTS:
                return {
                    "notified": 0,
                    "reason": "deduplicated",
                    "failure_count": failure_count,
                }
            _SENDING_INCIDENTS.add(incident_key)
        except Exception as exc:
            return {"notified": 0, "reason": "internal_error", "error": str(exc)}
        finally:
            conn.close()

    # ── Outside the lock: the blocking Telegram call, then a short second
    # critical section to persist the outcome and release the claim. ──
    try:
        if render is not None:
            text = render(is_reminder)
        else:
            label = _ALERT_LABELS.get(alert_key, alert_key)
            lines = [
                f"🏢 Team：{team_name}",
                f"🏷️ 类型：{label}",
                f"🔗 来源：{source}",
                f"📝 错误：{error_text}",
            ]
            if is_reminder:
                duration = _humanize_since(first_failed_at, now_dt)
                lines.insert(1, f"⏰ 仍未恢复{f'，已持续 {duration}' if duration else ''}")
            text = detail_card(
                "🔁 Team 异常仍未恢复" if is_reminder else "🚨 Team 异常",
                tuple(lines),
            )
        sent = (notify or notify_admins_sync)(text)

        with _INCIDENT_LOCK:
            conn = _connect()
            try:
                if sent > 0:
                    conn.execute(
                        """UPDATE team_health_incidents
                           SET notified = 1, last_notified_at = ?, updated_at = ?
                           WHERE team_id = ? AND alert_key = ? AND status = 'open'""",
                        (now, now, team_id, alert_key),
                    )
                    conn.commit()
                    _log_delivery(
                        conn,
                        team_id,
                        "team_health_alert",
                        f"key={alert_key}, source={source}, delivered_to={sent}"
                        + (", reminder=1" if is_reminder else ""),
                        "success",
                    )
                    return {"notified": sent, "reason": "sent", "failure_count": failure_count}

                _log_delivery(
                    conn,
                    team_id,
                    "team_health_alert",
                    f"key={alert_key}, source={source}, delivered_to=0",
                    "failed",
                    "Telegram alert was not delivered",
                )
                return {"notified": 0, "reason": "not_delivered", "failure_count": failure_count}
            except Exception as exc:
                return {"notified": 0, "reason": "internal_error", "error": str(exc)}
            finally:
                conn.close()
    finally:
        with _INCIDENT_LOCK:
            _SENDING_INCIDENTS.discard(incident_key)


def close_incident_family_sync(
    team_id: str,
    alert_key_prefix: str,
    *,
    keep_alert_key: Optional[str] = None,
    forget_after: Optional[timedelta] = None,
) -> int:
    """Silently resolve open incidents whose key starts with ``alert_key_prefix``.

    For alert families keyed by *what* was found (one key per distinct finding set)
    rather than by a failure that later recovers: when the set changes or empties,
    the old keys are closed without a "恢复" message, because the condition going
    away is not news. ``keep_alert_key`` stays open. Resolved rows of the family
    whose last notification is older than ``forget_after`` are deleted: they can no
    longer throttle anything. Returns the number of incidents resolved.
    """
    if not team_id or not alert_key_prefix:
        return 0
    now = _now_iso()
    prefix_len = len(alert_key_prefix)
    keep = keep_alert_key or ""
    with _INCIDENT_LOCK:
        conn = _connect()
        try:
            _ensure_table(conn)
            # 常态是这一族一行都没有：先读一下，免得每轮每个 Team 都开一次写事务。
            if conn.execute(
                """SELECT 1 FROM team_health_incidents
                   WHERE team_id = ? AND substr(alert_key, 1, ?) = ? AND alert_key != ?
                   LIMIT 1""",
                (team_id, prefix_len, alert_key_prefix, keep),
            ).fetchone() is None:
                return 0
            cursor = conn.execute(
                """UPDATE team_health_incidents
                   SET status = 'resolved', resolved_at = ?, updated_at = ?
                   WHERE team_id = ? AND status = 'open'
                     AND substr(alert_key, 1, ?) = ? AND alert_key != ?""",
                (now, now, team_id, prefix_len, alert_key_prefix, keep),
            )
            resolved = cursor.rowcount
            if forget_after is not None:
                cutoff = (datetime.now(timezone.utc) - forget_after).isoformat()
                conn.execute(
                    """DELETE FROM team_health_incidents
                       WHERE team_id = ? AND status = 'resolved'
                         AND substr(alert_key, 1, ?) = ? AND alert_key != ?
                         AND COALESCE(last_notified_at, updated_at, '') < ?""",
                    (team_id, prefix_len, alert_key_prefix, keep, cutoff),
                )
            conn.commit()
            return resolved
        finally:
            conn.close()


def report_team_recovery_sync(team_id: str, alert_key: str, *, source: str) -> dict:
    """Resolve one open incident and send at most one recovery notification."""
    if not team_id:
        return {"notified": 0, "reason": "missing_team_id"}

    now = _now_iso()
    with _INCIDENT_LOCK:
        conn = _connect()
        try:
            _ensure_table(conn)
            row = conn.execute(
                """SELECT * FROM team_health_incidents
                   WHERE team_id = ? AND alert_key = ? AND status = 'open'""",
                (team_id, alert_key),
            ).fetchone()
            if not row:
                return {"notified": 0, "reason": "no_open_incident"}

            if not bool(row["notified"]):
                conn.execute(
                    """UPDATE team_health_incidents
                       SET status = 'resolved', resolved_at = ?, updated_at = ?
                       WHERE team_id = ? AND alert_key = ?""",
                    (now, now, team_id, alert_key),
                )
                conn.commit()
                return {"notified": 0, "reason": "resolved_without_prior_delivery"}

            team_name = _team_name(conn, team_id)
            label = _ALERT_LABELS.get(alert_key, alert_key)
            text = detail_card(
                "✅ Team 恢复",
                (
                    f"🏢 Team：{team_name}",
                    f"🏷️ 类型：{label}",
                    f"🔗 来源：{source}",
                    f"📈 状态：连续失败 {int(row['failure_count'] or 1)} 次后恢复",
                ),
            )
            sent = notify_admins_sync(text)
            if sent > 0:
                conn.execute(
                    """UPDATE team_health_incidents
                       SET status = 'resolved', resolved_at = ?, updated_at = ?
                       WHERE team_id = ? AND alert_key = ? AND status = 'open'""",
                    (now, now, team_id, alert_key),
                )
                conn.commit()
            _log_delivery(
                conn,
                team_id,
                "team_health_recovery",
                f"key={alert_key}, source={source}, delivered_to={sent}",
                "success" if sent > 0 else "failed",
                None if sent > 0 else "Telegram recovery was not delivered",
            )
            return {"notified": sent, "reason": "sent" if sent > 0 else "not_delivered"}
        except Exception as exc:
            return {"notified": 0, "reason": "internal_error", "error": str(exc)}
        finally:
            conn.close()


async def report_team_failure(
    team_id: str,
    alert_key: str,
    error: Any,
    *,
    source: str,
) -> dict:
    return await asyncio.to_thread(
        report_team_failure_sync,
        team_id,
        alert_key,
        error,
        source=source,
    )


async def report_team_recovery(team_id: str, alert_key: str, *, source: str) -> dict:
    return await asyncio.to_thread(
        report_team_recovery_sync,
        team_id,
        alert_key,
        source=source,
    )
