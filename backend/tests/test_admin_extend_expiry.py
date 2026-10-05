"""Regression coverage for the admin-only expiry extension endpoint."""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import member_cache_service
from app.models import ExtendExpiryRequest
from app.routes import members
from app.services.team_locks import member_operation_claim


UTC = timezone.utc


class AdminExtendExpiryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO teams (id, name, status, created_at, updated_at)
                   VALUES ('team-1', 'Team 1', 'active', '2026-09-17', '2026-09-17')"""
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _insert_expiry(self, expires_at: str | None, *, source: str = "system"):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked, source, created_at)
                   VALUES ('team-1', 'user-1', 'member@example.com', ?, ?, 0, ?, '2026-09-17')""",
                (expires_at, 1 if expires_at is not None else 0, source),
            )

    def _extend(self, duration: str, now: datetime, *, request_id: str = "request-1", cache_update=None):
        cache_update = cache_update or AsyncMock()
        with (
            patch("app.services.member_expiry.utc_now", return_value=now),
            patch.object(members, "update_cached_member_expiry", new=cache_update),
            patch.object(
                members,
                "_resolve_member_identity",
                new=AsyncMock(return_value=("user-1", "member@example.com")),
            ),
        ):
            result = asyncio.run(
                members.extend_expiry(
                    "team-1",
                    "user-1",
                    ExtendExpiryRequest(
                        expires_in=duration,
                        email="member@example.com",
                        request_id=request_id,
                    ),
                )
            )
        return result, cache_update

    def test_future_expiry_accumulates_duration_instead_of_overwriting(self):
        self._insert_expiry("2026-10-17T15:44:00+00:00")

        result, cache_update = self._extend("4d", datetime(2026, 9, 17, 12, tzinfo=UTC))

        self.assertEqual(result["expires_at"], "2026-10-21T15:44:00+00:00")
        cache_update.assert_awaited_once_with(
            "team-1",
            user_id="user-1",
            email="member@example.com",
            expires_at="2026-10-21T15:44:00+00:00",
        )
        with self._conn() as conn:
            expiry = conn.execute("SELECT expires_at FROM member_expiry").fetchone()["expires_at"]
            log = conn.execute(
                "SELECT action, result FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(expiry, "2026-10-21T15:44:00+00:00")
        self.assertEqual(dict(log), {"action": "extend_expiry", "result": "success"})

    def test_expired_record_starts_the_new_duration_from_now(self):
        self._insert_expiry("2026-09-01T00:00:00+00:00")
        now = datetime(2026, 9, 17, 12, tzinfo=UTC)

        result, _cache_update = self._extend("12h", now)

        self.assertEqual(result["expires_at"], (now + timedelta(hours=12)).isoformat())

    def test_permanent_membership_is_not_downgraded(self):
        self._insert_expiry(None)
        with self.assertRaises(HTTPException) as raised:
            self._extend("30d", datetime(2026, 9, 17, 12, tzinfo=UTC))

        self.assertEqual(raised.exception.status_code, 409)
        with self._conn() as conn:
            expiry = conn.execute("SELECT expires_at FROM member_expiry").fetchone()["expires_at"]
            log = conn.execute(
                "SELECT action, result FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNone(expiry)
        self.assertEqual(dict(log), {"action": "extend_expiry", "result": "failed"})

    def test_email_mismatch_is_rejected_before_any_expiry_write(self):
        cached = {
            "members": [{"id": "user-1", "email": "member@example.com"}],
            "pending_invites": [],
        }
        with patch.object(members, "get_cached_members", new=AsyncMock(return_value=cached)):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    members._resolve_member_identity(
                        "team-1", "user-1", "other@example.com"
                    )
                )
        self.assertEqual(raised.exception.status_code, 409)
        with self._conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM member_expiry").fetchone()[0], 0)

    def test_extend_request_requires_an_email(self):
        with self.assertRaises(ValidationError):
            ExtendExpiryRequest(expires_in="4d", request_id="missing-email")

    def test_member_cache_update_never_or_matches_another_members_email(self):
        cache = {
            "members": [
                {"id": "user-1", "email": "member@example.com", "expires_at": "old-1"},
                {"id": "user-2", "email": "other@example.com", "expires_at": "old-2"},
            ],
            "pending_invites": [],
        }
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, ?, ?, ?)",
                ("team-1", json.dumps(cache["members"]), "[]", "2026-09-17"),
            )

        asyncio.run(
            member_cache_service.update_cached_member_expiry(
                "team-1",
                user_id="user-1",
                email="member@example.com",
                expires_at="new-expiry",
            )
        )
        with self._conn() as conn:
            rows = json.loads(conn.execute("SELECT members_json FROM member_cache").fetchone()[0])
        self.assertEqual(rows[0]["expires_at"], "new-expiry")
        self.assertEqual(rows[1]["expires_at"], "old-2")

    def test_claim_contention_returns_conflict_without_extending(self):
        self._insert_expiry("2026-10-17T15:44:00+00:00")

        async def run():
            async with member_operation_claim(
                "team-1",
                email="member@example.com",
                user_id="user-1",
                operation="patrol_kick",
            ):
                with patch.object(
                    members,
                    "_resolve_member_identity",
                    new=AsyncMock(return_value=("user-1", "member@example.com")),
                ):
                    return await members.extend_expiry(
                        "team-1",
                        "user-1",
                        ExtendExpiryRequest(
                            expires_in="4d",
                            email="member@example.com",
                            request_id="contention-request",
                        ),
                    )

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(run())
        self.assertEqual(raised.exception.status_code, 409)
        with self._conn() as conn:
            expiry = conn.execute("SELECT expires_at FROM member_expiry").fetchone()["expires_at"]
        self.assertEqual(expiry, "2026-10-17T15:44:00+00:00")

    def test_cache_failure_returns_committed_result_and_retry_does_not_double_extend(self):
        self._insert_expiry("2026-10-17T15:44:00+00:00")
        now = datetime(2026, 9, 17, 12, tzinfo=UTC)
        cache_failure = AsyncMock(side_effect=RuntimeError("cache unavailable"))

        first, _ = self._extend("4d", now, request_id="retry-request", cache_update=cache_failure)
        second, _ = self._extend("4d", now, request_id="retry-request")

        self.assertEqual(first["expires_at"], "2026-10-21T15:44:00+00:00")
        self.assertEqual(second["expires_at"], first["expires_at"])
        with self._conn() as conn:
            expiry = conn.execute("SELECT expires_at FROM member_expiry").fetchone()["expires_at"]
            receipt_count = conn.execute(
                "SELECT COUNT(*) FROM admin_expiry_extension_receipts WHERE request_id = 'retry-request'"
            ).fetchone()[0]
        self.assertEqual(expiry, first["expires_at"])
        self.assertEqual(receipt_count, 1)

    def test_audit_failure_rolls_back_extension_and_retry_applies_it_once(self):
        self._insert_expiry("2026-10-17T15:44:00+00:00")
        now = datetime(2026, 9, 17, 12, tzinfo=UTC)
        with self._conn() as conn:
            conn.execute(
                """CREATE TRIGGER fail_extend_audit BEFORE INSERT ON operation_logs
                   WHEN NEW.action = 'extend_expiry'
                   BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"""
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self._extend("4d", now, request_id="audit-request")
        with self._conn() as conn:
            expiry = conn.execute("SELECT expires_at FROM member_expiry").fetchone()["expires_at"]
            receipt_count = conn.execute(
                "SELECT COUNT(*) FROM admin_expiry_extension_receipts WHERE request_id = 'audit-request'"
            ).fetchone()[0]
            conn.execute("DROP TRIGGER fail_extend_audit")
        self.assertEqual(expiry, "2026-10-17T15:44:00+00:00")
        self.assertEqual(receipt_count, 0)

        retried, _ = self._extend("4d", now, request_id="audit-request")
        self.assertEqual(retried["expires_at"], "2026-10-21T15:44:00+00:00")


if __name__ == "__main__":
    unittest.main()
