"""Harnesses shared by the redemption, fallback-credit and member-expiry tests.

Not a test module (the name does not match test*.py), so neither pytest nor
unittest discovery collects it.

- RedeemFlowCase: the real public redemption flow (redeem_access_token,
  reconcile_pending_redemptions, the public queries) on a fresh database, with
  every upstream call faked.
- RedemptionLedgerCase: a fresh database holding Team 'team-1' without
  credentials, plus a helper that seeds one spent code and its redemption row.
- ConnectedTeamCase: a fresh database holding Team 'team-1' with stub
  credentials, for jobs that build a ChatGPT client for it (data sync,
  auto-kick, the redemption reconciler). The test patches that client.
"""

import _isolation  # noqa: F401  must precede any app import
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from _fixtures import start_temp_db

from app import database as app_database
from app.routes import access_tokens

EMAIL = "redeemer@example.com"
CREATED = "2026-09-01T00:00:00+00:00"
FUTURE = "2027-01-01T00:00:00+00:00"


class RedeemFlowCase(unittest.IsolatedAsyncioTestCase):
    """Runs the real redemption flow against fake upstream calls.

    ``self.live[team_id]`` is that Team's live member snapshot; a live fetch of a
    Team in ``self.broken`` fails (upstream 401 / network down). ``self.fetched``
    records which Teams were fetched live, ``self.invites`` which Teams received
    an invite, and ``self.invite_result`` is what every invite call returns.
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()

        self.live: dict[str, dict] = {}
        self.broken: set[str] = set()
        self.fetched: list[str] = []
        self.invites: list[str] = []
        self.invite_result: dict = {}
        self.budget = access_tokens._RedeemLookupBudget(
            per_code=10, per_code_window=3600, global_limit=60, global_window=600
        )

        async def fake_fetch(team_id, client):
            self.fetched.append(team_id)
            if team_id in self.broken:
                raise HTTPException(status_code=502, detail="Failed to fetch members: 401")
            return self.live.get(team_id, {"members": [], "pending_invites": []})

        async def fake_run(func, *args, **kwargs):
            self.invites.append(func.__self__.team_id)
            return dict(self.invite_result)

        for target, value in (
            ("_check_rate_limit", AsyncMock()),
            ("log_operation", AsyncMock()),
            ("notify_member_event", AsyncMock()),
            ("add_member_watch", AsyncMock()),
            ("reserve_default_seat", AsyncMock()),
            ("_get_proxy_url", AsyncMock(return_value=None)),
            ("_chatgpt_available", AsyncMock(return_value=(True, "available=1"))),
            ("fetch_and_cache_members", fake_fetch),
            ("run_chatgpt_call", fake_run),
            ("_redeem_lookup_budget", self.budget),
        ):
            p = patch.object(access_tokens, target, new=value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _exec(self, sql, params=()):
        async with app_database.get_db() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.lastrowid

    async def _rows(self, sql, params=()):
        async with app_database.get_db() as db:
            cursor = await db.execute(sql, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def _team(self, team_id, *, status="active", auth_state=None,
                    active_until=None, will_renew=1, seats=5):
        # load_active_teams 按空位多少排序：seats 越大越先被选去发邀请。
        await self._exec(
            """INSERT INTO teams
               (id, name, status, auth_state, access_token, device_id,
                seats_entitled, chatgpt_count, active_until, will_renew,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (team_id, f"Team {team_id}", status, auth_state, f"tok-{team_id}",
             f"dev-{team_id}", seats, active_until, will_renew, CREATED, CREATED),
        )

    async def _expiry(self, team_id, *, source="self_service", expires_at=FUTURE,
                      kicked=0, user_id="u-1"):
        await self._exec(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, source, created_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
            (team_id, user_id, EMAIL, expires_at, kicked, source, CREATED),
        )

    async def _cache(self, team_id, *, members=(), pending=()):
        await self._exec(
            """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            (team_id, json.dumps(list(members)), json.dumps(list(pending)), CREATED),
        )

    def _live_member(self, team_id, *, user_id="u-1", expires_at=FUTURE, is_owner=False):
        self.live[team_id] = {
            "members": [{"email": EMAIL, "id": user_id, "is_owner": is_owner,
                         "expires_at": expires_at, "source": "self_service"}],
            "pending_invites": [],
        }

    async def _token(self, raw):
        return int(await self._exec(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses,
                used_count, disabled, created_at)
               VALUES (?, ?, '30d', 1, 0, 0, ?)""",
            (access_tokens._hash_token(raw), raw[:12], CREATED),
        ))

    async def _redeem(self, raw, email=EMAIL, team_id=None):
        return await access_tokens.redeem_access_token(
            access_tokens.RedeemAccessTokenRequest(email=email, token=raw, team_id=team_id),
            Mock(),
        )

    async def _used_count(self, token_id):
        rows = await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,))
        return int(rows[0]["used_count"])

    async def _expiry_of(self, team_id):
        rows = await self._rows(
            "SELECT expires_at FROM member_expiry WHERE team_id = ? AND kicked = 0", (team_id,)
        )
        return [row["expires_at"] for row in rows]


class RedemptionLedgerCase(unittest.TestCase):
    """A fresh database holding Team 'team-1' (no credentials)."""

    def setUp(self):
        self.db_path = start_temp_db(self)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO teams (id, name, status, created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', '2026-09-11', '2026-09-11')"""
        )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _new_token_use(self, email="user@example.com", result="pending",
                       grant="30d", action="invite_pending"):
        """One spent single-use code and its redemption row on team-1. Returns the row id."""
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'p', ?, 1, 1, 0, '2026-09-11')""",
            (f"h-{uuid.uuid4().hex}", grant),
        )
        token_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                created_at)
               VALUES (?, ?, ?, 'team-1', NULL, NULL, ?, '2026-09-11')""",
            (token_id, email, action, result),
        )
        token_use_id = cur.lastrowid
        conn.commit()
        conn.close()
        return token_use_id


class ConnectedTeamCase(unittest.TestCase):
    """A fresh database holding Team 'team-1' with stub credentials."""

    def setUp(self):
        self.db_path = start_temp_db(self)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO teams (id, name, status, access_token, device_id,
                                  created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', 'stub-token', 'stub-device',
                       '2026-10-01', '2026-10-01')"""
        )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _new_token_use(self, *, result="pending", grant="30d"):
        """One spent code and its redemption by EMAIL on team-1, created now. Returns the row id."""
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'p', ?, 1, 1, 0, '2026-10-01')""",
            (f"h-{uuid.uuid4().hex}", grant),
        )
        token_id = cur.lastrowid
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                created_at)
               VALUES (?, ?, 'invite_pending', 'team-1', NULL, NULL, ?, ?)""",
            (token_id, EMAIL, result, now_iso),
        )
        token_use_id = cur.lastrowid
        conn.commit()
        conn.close()
        return token_use_id
