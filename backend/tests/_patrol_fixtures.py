"""Patrol harness shared by the patrol / premium-patrol test modules (no test cases here).

PatrolCase gives each test a temp database, a RecordingClient in place of ChatGPTClient
(records calls, never touches the network) and captured patrol Telegram alerts.
RecordingClient.calls / live_members are class-level and reset in every setUp.
"""

import _isolation  # noqa: F401  must precede any app import
from _fixtures import direct_call, start_temp_db

import asyncio
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import member_cache_service
from app.services import patrol, team_health_alerts

OLD = "2020-01-01T00:00:00+00:00"
BASELINE = "2026-07-01T00:00:00+00:00"


def _member(email, user_id, *, seat_type="default", is_owner=False, source="detected",
            first_seen_at="2026-07-10T00:00:00+00:00", expires_at=None):
    return {
        "id": user_id,
        "email": email,
        "seat_type": seat_type,
        "is_owner": is_owner,
        "source": source,
        "first_seen_at": first_seen_at,
        "expires_at": expires_at,
        "created_time": None,
        "status": "active",
    }


def _pending(email, *, seat_type="default", source="detected",
             first_seen_at="2026-07-10T00:00:00+00:00"):
    return {
        "id": f"invite-{email}",
        "email": email,
        "seat_type": seat_type,
        "is_owner": False,
        "source": source,
        "first_seen_at": first_seen_at,
        "expires_at": None,
        "created_time": None,
        "status": "pending",
    }


class RecordingClient:
    """假 ChatGPT 客户端：只记录调用，绝不触网。"""

    calls: list = []
    live_members: list = []

    def __init__(self, *args, **kwargs):
        pass

    def remove_member(self, user_id):
        RecordingClient.calls.append(("remove_member", user_id))
        return {"status": "ok"}

    def revoke_invite(self, email):
        RecordingClient.calls.append(("revoke_invite", email))
        return {"status": "ok"}

    def get_members(self, offset=0, limit=100):
        RecordingClient.calls.append(("get_members", offset))
        items = list(RecordingClient.live_members) if offset == 0 else []
        return {"items": items, "total": len(RecordingClient.live_members)}

    def get_pending_invites(self, offset=0, limit=100):
        RecordingClient.calls.append(("get_pending_invites", offset))
        return {"items": [], "total": 0}


def _live(email, user_id, seat_type, *, owner=False):
    return {
        "id": user_id,
        "email": email,
        "role": "account-owner" if owner else "standard-user",
        "seat_type": seat_type,
    }


OWNER = _member("owner@example.com", "u-owner", seat_type="usage_based", is_owner=True, source=None)
# 生产形状：Owner 的成员条目没有席位类型，没有来源记录。
PROD_OWNER = _member("owner@example.com", "u-owner", seat_type=None, is_owner=True, source=None)
LIVE_PROD_OWNER = {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}


def _outsider(n, *, seat_type="default", day=10):
    return _member(f"out{n}@example.com", f"u-out{n}", seat_type=seat_type,
                   first_seen_at=f"2026-07-{day:02d}T00:00:00+00:00")


def _now():
    return datetime.now(timezone.utc).isoformat()


class PatrolCase(unittest.TestCase):
    """Temp DB + recording upstream + captured Telegram; helpers seed teams, caches and settings."""

    def setUp(self):
        self.db_path = start_temp_db(self)

        self.notify_calls: list[str] = []

        def fake_notify(text, **kwargs):
            self.notify_calls.append(text)
            return 1

        def forbidden_notify(text, **kwargs):  # pragma: no cover - 走到这里就是绕过了巡逻出口
            raise AssertionError("patrol alerts must go through patrol.notify_admins_sync")

        RecordingClient.calls = []
        RecordingClient.live_members = []
        for target, attr, value in (
            (patrol, "notify_admins_sync", fake_notify),
            (patrol, "ChatGPTClient", RecordingClient),
            (patrol, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)),
            (team_health_alerts, "notify_admins_sync", forbidden_notify),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    # ── helpers ──────────────────────────────────────────────────────────
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _team(self, team_id, *, seats_entitled=5, codex=0, name=None):
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, access_token, device_id, seats_entitled,
                                  is_codex_enabled, status, created_at, updated_at)
               VALUES (?, ?, 'fake-token', 'fake-device', ?, ?, 'active', '2026-01-01', '2026-01-01')""",
            (team_id, name or team_id, seats_entitled, codex),
        )
        conn.commit()
        conn.close()

    def _cache(self, team_id, members, pending=(), *, updated_at=None):
        # updated_at 同时当作这份快照的 fetch_started_at（巡逻的 Premium 否决比的是它）。
        snapshot_at = updated_at or datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at,
                                         fetch_started_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(team_id) DO UPDATE SET members_json = excluded.members_json,
                                                  pending_json = excluded.pending_json,
                                                  updated_at = excluded.updated_at,
                                                  fetch_started_at = excluded.fetch_started_at""",
            (team_id, json.dumps(members), json.dumps(list(pending)), snapshot_at, snapshot_at),
        )
        conn.commit()
        conn.close()

    def _expiry(self, team_id, email, user_id="", *, source="detected",
                first_seen_at="2026-07-10T00:00:00+00:00", expires_at=None, auto_kick=0):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, first_seen_at, source, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, '2026-01-01')""",
            (team_id, user_id, email, expires_at, auto_kick, first_seen_at, source),
        )
        conn.commit()
        conn.close()

    def _add(self, team_id, member):
        """成员快照之外再补一条对应来源的 member_expiry 行（和真实同步的结果一致）。"""
        if member.get("source"):
            self._expiry(team_id, member["email"], member.get("id") or "",
                         source=member["source"], first_seen_at=member.get("first_seen_at"))
        return member

    def _setting(self, key, value):
        conn = self._conn()
        conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, '2026-01-01')
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        conn.commit()
        conn.close()

    def _arm(self, *, kick_enabled="1"):
        self._setting("patrol_kick_enabled", kick_enabled)
        self._setting("patrol_baseline_at", BASELINE)

    def _baseline(self, team_id):
        conn = self._conn()
        conn.execute(
            "INSERT INTO patrol_team_baselines (team_id, baseline_at) VALUES (?, ?)",
            (team_id, BASELINE),
        )
        conn.commit()
        conn.close()

    def _patrol(self, *, dry_run=False, allow=None):
        if allow is None:
            conn = self._conn()
            allow = [r["id"] for r in conn.execute("SELECT id FROM teams WHERE status = 'active'")]
            conn.close()
        return patrol.run_patrol(dry_run=dry_run, allow_team_ids=allow)

    def _logs(self, action):
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM operation_logs WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _calls(self, name):
        return [c for c in RecordingClient.calls if c[0] == name]

    def _alerts(self):
        return [t for t in self.notify_calls if "席位提醒" in t or "席位类型提醒" in t]

    def _kicked(self, team_id, user_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT kicked, kick_source FROM member_expiry WHERE team_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (team_id, user_id),
        ).fetchone()
        conn.close()
        return (row["kicked"], row["kick_source"]) if row else None

    def _armed_team(self, team_id, members, pending=(), **team_kwargs):
        self._team(team_id, **team_kwargs)
        for m in list(members) + list(pending):
            self._add(team_id, m)
        self._cache(team_id, members, pending)
        self._arm()
        self._baseline(team_id)


class PatrolHistoryCase(PatrolCase):
    """PatrolCase plus member_expiry history rows, seat-change logs and a direct _patrol_kick."""

    def _closed_row(self, team_id, email, user_id, *, kick_source, source="system",
                    expires_at="2026-08-01T00:00:00+00:00"):
        """TeamBoss 以前管过这个人、后来关掉的 member_expiry 行。"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at, kick_source,
                first_seen_at, source, created_at)
               VALUES (?, ?, ?, ?, 1, 1, '2026-08-02T00:00:00+00:00', ?,
                       '2026-07-01T00:00:00+00:00', ?, '2026-07-01T00:00:00+00:00')""",
            (team_id, user_id, email, expires_at, kick_source, source),
        )
        conn.commit()
        conn.close()

    def _open_row(self, team_id, email, user_id, *, source="system"):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, first_seen_at, source, created_at)
               VALUES (?, ?, ?, '2026-12-01T00:00:00+00:00', 1, 0,
                       '2026-07-01T00:00:00+00:00', ?, '2026-07-01T00:00:00+00:00')""",
            (team_id, user_id, email, source),
        )
        conn.commit()
        conn.close()

    def _seat_log(self, team_id, action, target_email, detail, result, created_at):
        conn = self._conn()
        conn.execute(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, 'manual', ?)""",
            (team_id, action, target_email, detail, result, created_at),
        )
        conn.commit()
        conn.close()

    def _cache_row(self, team_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT members_json, pending_json, updated_at, fetch_started_at FROM member_cache "
            "WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def _kick(self, team_id, member, rule=patrol.KICK_RULE_OVER_QUOTA):
        conn = self._conn()
        try:
            return patrol._patrol_kick(conn, RecordingClient(), team_id, member, rule=rule)
        finally:
            conn.close()


class PatrolSnapshotCase(PatrolCase):
    """PatrolCase plus member_cache / seat_holds readers and an async member refresh."""

    def _cache_row(self, team_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT members_json, updated_at, fetch_started_at FROM member_cache WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def _cached_seat(self, team_id, user_id):
        row = self._cache_row(team_id)
        for m in json.loads(row["members_json"]):
            if m.get("id") == user_id:
                return m.get("seat_type")
        return None

    def _log(self, team_id, action, target_email, detail, result, created_at=None):
        conn = self._conn()
        conn.execute(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, 'manual', ?)""",
            (team_id, action, target_email, detail, result, created_at or _now()),
        )
        conn.commit()
        conn.close()

    def _team_row(self, team_id):
        conn = self._conn()
        row = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        conn.close()
        return row

    def _hold(self, team_id, email, *, age=timedelta(hours=1)):
        conn = self._conn()
        conn.execute(
            """INSERT INTO seat_holds (team_id, email, seat_type, source, created_at)
               VALUES (?, ?, 'prolite', 'test', ?)""",
            (team_id, email, (datetime.now(timezone.utc) - age).isoformat()),
        )
        conn.commit()
        conn.close()

    def _holds(self, team_id):
        conn = self._conn()
        rows = conn.execute("SELECT email FROM seat_holds WHERE team_id = ?", (team_id,)).fetchall()
        conn.close()
        return {r["email"] for r in rows}

    def _async_refresh(self, team_id, client):
        with patch.object(member_cache_service, "run_chatgpt_call", direct_call):
            return asyncio.run(member_cache_service._fetch_and_cache_members_impl(team_id, client))
