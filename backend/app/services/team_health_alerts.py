"""Durable, de-duplicated Telegram alerts for Team health incidents.

Only final failures should enter this service.  Callers are responsible for
performing any automatic retry first.  Repeated failures update the incident
counter without repeatedly messaging admins; the first later success sends one
recovery message and closes the incident.
"""

import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from ..database import get_db_path
from ..tg_format import detail_card
from .tg_notify import notify_admins_sync

# 向后兼容：模块级占位符，测试可能会 patch 它
DB_PATH = None

_INCIDENT_LOCK = threading.Lock()
_MAX_ERROR_LENGTH = 500

_ALERT_LABELS = {
    "chatgpt_auth": "ChatGPT 鉴权",
    "team_sync": "Team 数据同步",
}


def is_auth_error(error: Any) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code == 401:
        return True
    detail = getattr(error, "detail", None)
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
) -> dict:
    """Open/update an incident and notify only when it has not been delivered."""
    if not team_id:
        return {"notified": 0, "reason": "missing_team_id"}

    now = _now_iso()
    error_text = str(getattr(error, "detail", error) or "unknown error")[:_MAX_ERROR_LENGTH]
    with _INCIDENT_LOCK:
        conn = _connect()
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM team_health_incidents WHERE team_id = ? AND alert_key = ?",
                (team_id, alert_key),
            ).fetchone()
            if row and row["status"] == "open":
                failure_count = int(row["failure_count"] or 0) + 1
                already_notified = bool(row["notified"])
                conn.execute(
                    """UPDATE team_health_incidents
                       SET last_failed_at = ?, last_error = ?, last_source = ?,
                           failure_count = ?, updated_at = ?
                       WHERE team_id = ? AND alert_key = ?""",
                    (now, error_text, source, failure_count, now, team_id, alert_key),
                )
            else:
                failure_count = 1
                already_notified = False
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

            label = _ALERT_LABELS.get(alert_key, alert_key)
            text = detail_card(
                "🚨 Team 异常",
                (
                    f"🏢 车队：{team_name}",
                    f"🏷️ 类型：{label}",
                    f"🔗 来源：{source}",
                    f"📝 错误：{error_text}",
                ),
            )
            sent = notify_admins_sync(text)
            if sent > 0:
                conn.execute(
                    """UPDATE team_health_incidents SET notified = 1, updated_at = ?
                       WHERE team_id = ? AND alert_key = ? AND status = 'open'""",
                    (now, team_id, alert_key),
                )
                conn.commit()
                _log_delivery(
                    conn,
                    team_id,
                    "team_health_alert",
                    f"key={alert_key}, source={source}, delivered_to={sent}",
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
                    f"🏢 车队：{team_name}",
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
