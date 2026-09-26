"""Endpoints must tell the operator to re-import a Team whose login is dead.

A Team with ``auth_state='rejected'`` can only produce upstream 401s. Manual
sync, member-expiry edits and manual token refresh used to answer with a bare
502, which reads like a transient upstream fault. Re-import must reset the
state to ``ok``.
"""

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import jwt
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import chatgpt_limiter
from app import database as app_database
from app import team_service, team_sync_service
from app.chatgpt_client import ChatGPTClient
from app.models import ExtendExpiryRequest, TeamSession
from app.routes import members, teams
from app.services.team_clients import TEAM_AUTH_REJECTED_DETAIL


TEAM_ID = "00000000-0000-4000-8000-0000000a0a0a"
MEMBERS_401 = HTTPException(
    status_code=502,
    detail=(
        "Failed to fetch members: 401 Client Error: Unauthorized for url: "
        f"https://chatgpt.com/backend-api/accounts/{TEAM_ID}/users?offset=0&limit=100"
    ),
)
MEMBERS_TIMEOUT = HTTPException(
    status_code=502,
    detail="Failed to fetch members: HTTPSConnectionPool(host='chatgpt.com', port=443): Read timed out.",
)


def _token(delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iat": int(now.timestamp()),
            "exp": int((now + delta).timestamp()),
            "https://api.openai.com/auth": {"chatgpt_account_id": TEAM_ID},
        },
        key="",
        algorithm="none",
    )


class TeamAuthRejectedRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        # Every test here runs against the temp DB only.
        self.assertTrue(self.db_path.startswith(self.tmpdir.name))

        self.expired_token = _token(timedelta(hours=-12))
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO teams (id, name, access_token, session_token, device_id,
                                  status, created_at, updated_at)
               VALUES (?, 'trump', ?, 'session-1', 'device-1', 'active',
                       '2026-09-01', '2026-09-01')""",
            (TEAM_ID, self.expired_token),
        )
        conn.commit()
        conn.close()

    def _set_auth_state(self, auth_state: str | None):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE teams SET auth_state = ?, auth_state_since = ?, "
            "sync_failing_since = ?, sync_suspended_at = ? WHERE id = ?",
            (
                auth_state,
                "2026-09-25T12:51:50+00:00" if auth_state == "rejected" else None,
                "2026-09-24T15:34:57+00:00",
                "2026-09-25T15:35:09+00:00",
                TEAM_ID,
            ),
        )
        conn.commit()
        conn.close()

    def _row(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT status, COALESCE(auth_state, 'ok'), auth_state_since, sync_suspended_at "
                "FROM teams WHERE id = ?",
                (TEAM_ID,),
            ).fetchone()
        finally:
            conn.close()

    # ── manual sync ──────────────────────────────────────────────────────

    def _sync(self, members_error: Exception):
        with (
            patch.object(team_sync_service, "get_team_client", new=AsyncMock(return_value=Mock())),
            patch.object(team_sync_service, "_fetch_overview", new=AsyncMock(side_effect=members_error)),
            patch.object(team_sync_service, "fetch_and_cache_members", new=AsyncMock(side_effect=members_error)),
            patch.object(team_sync_service, "_fetch_workspace_settings", new=AsyncMock(side_effect=members_error)),
            patch.object(team_sync_service, "report_team_failure", new=AsyncMock()),
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(teams.sync_team(TEAM_ID, force=True))
        return ctx.exception

    def test_sync_of_rejected_team_says_reimport(self):
        self._set_auth_state("rejected")
        exc = self._sync(MEMBERS_401)
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail, TEAM_AUTH_REJECTED_DETAIL)

    def test_sync_401_of_healthy_team_keeps_original_error(self):
        self._set_auth_state("ok")
        exc = self._sync(MEMBERS_401)
        self.assertEqual(exc.status_code, 502)
        self.assertIn("401", exc.detail)

    def test_sync_network_failure_of_rejected_team_is_not_relabelled(self):
        self._set_auth_state("rejected")
        exc = self._sync(MEMBERS_TIMEOUT)
        self.assertEqual(exc.status_code, 502)
        self.assertIn("timed out", exc.detail)

    # ── member expiry extension ──────────────────────────────────────────

    def _extend(self):
        with (
            patch.object(members, "get_team_client", new=AsyncMock(return_value=Mock())),
            patch.object(members, "fetch_and_cache_members", new=AsyncMock(side_effect=MEMBERS_401)),
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    members.extend_expiry(
                        TEAM_ID,
                        "user-1",
                        ExtendExpiryRequest(
                            expires_in="30d",
                            email="member@example.com",
                            request_id="request-1",
                        ),
                    )
                )
        return ctx.exception

    def test_extend_expiry_of_rejected_team_says_reimport(self):
        self._set_auth_state("rejected")
        exc = self._extend()
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail, TEAM_AUTH_REJECTED_DETAIL)
        conn = sqlite3.connect(self.db_path)
        expiry_rows = conn.execute("SELECT COUNT(*) FROM member_expiry").fetchone()[0]
        conn.close()
        self.assertEqual(expiry_rows, 0)

    def test_extend_expiry_of_healthy_team_keeps_retry_message(self):
        self._set_auth_state("ok")
        exc = self._extend()
        self.assertEqual(exc.status_code, 502)
        self.assertEqual(exc.detail, "无法确认成员身份，请稍后重试")

    # ── manual token refresh (end to end through the limiter) ────────────

    def test_manual_refresh_with_refresh_access_token_error_marks_and_says_reimport(self):
        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                return_value={"error": "RefreshAccessTokenError", "status_code": 200},
            ),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
            patch.object(teams, "report_team_failure", new=AsyncMock()) as report_failure,
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(teams.refresh_team_token(TEAM_ID))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, TEAM_AUTH_REJECTED_DETAIL)
        self.assertEqual(self._row()[:2], ("active", "rejected"))
        report_failure.assert_awaited_once()

    def test_manual_refresh_timeout_stays_ok_and_keeps_502(self):
        with (
            patch.object(ChatGPTClient, "refresh_token", return_value={"error": "Read timed out"}),
            patch.object(teams, "report_team_failure", new=AsyncMock()),
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(teams.refresh_team_token(TEAM_ID))

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(self._row()[:2], ("active", "ok"))

    # ── re-import resets the state ───────────────────────────────────────

    def test_reimport_resets_rejected_to_ok(self):
        self._set_auth_state("rejected")
        fresh_token = _token(timedelta(days=10))

        async def fake_run(func, *args, **kwargs):
            if getattr(func, "__name__", "") == "get_account_info":
                return {"accounts": {TEAM_ID: {"account": {"name": "trump"}}}}
            return {"error": "not needed for this test"}

        session = TeamSession(
            user={"email": "owner@example.com"},
            expires="2026-12-01T00:00:00Z",
            account={"id": TEAM_ID},
            accessToken=fresh_token,
            sessionToken="fresh-session",
        )
        with (
            patch.object(team_service, "run_chatgpt_call", new=fake_run),
            patch.object(team_service, "fetch_seat_pricing", new=AsyncMock(return_value={})),
            patch.object(team_service, "write_session_file"),
        ):
            asyncio.run(
                team_service.upsert_team_from_session(
                    session,
                    log_action="reimport_team",
                    expected_team_id=TEAM_ID,
                )
            )

        self.assertEqual(self._row(), ("active", "ok", None, None))


if __name__ == "__main__":
    unittest.main()
