"""A redemption's terminal state and its patrol barrier commit together.

An uncertain self-service invite carries a ``kind='barrier'`` row in
``pending_invite_reconciliations``; while it is open, auto-kick and patrol skip
that member. Once the redemption is settled (``success``, or ``failed`` after an
admin release) nothing scans it again, so a barrier that misses that moment stays
open forever. Each test makes the barrier resolution itself fail and checks that
either both writes landed or neither did, then lets it succeed and checks that
no open barrier is left.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import logging
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes import access_tokens
from app.services import member_expiry
from app.services.member_expiry import (
    record_confirmed_invite_extension,
    record_uncertain_invite,
)

EMAIL = "redeemer@example.com"


class _BarrierTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(
            app_database, "get_db_dir", return_value=self.tmpdir.name
        )
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        no_backoff = patch.object(member_expiry, "_CONFIRM_WRITE_BACKOFF_SECONDS", 0)
        no_backoff.start()
        self.addCleanup(no_backoff.stop)
        # The failure paths log retries and fallbacks on purpose; keep test output clean.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, access_token, device_id,
                                  created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', 't', 'd', '2026-10-01', '2026-10-01')"""
        )
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES ('h', 'p', '30d', 1, 1, 0, '2026-10-01')"""
        )
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                created_at)
               VALUES (?, ?, 'invite_pending', 'team-1', NULL, NULL, 'uncertain',
                       '2026-10-01')""",
            (cur.lastrowid, EMAIL),
        )
        self.use_id = cur.lastrowid
        conn.commit()
        conn.close()
        asyncio.run(
            record_uncertain_invite(
                "team-1", "", EMAIL, None,
                source="self_service", reason="timeout",
                token_use_id=self.use_id, kind="barrier",
            )
        )

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _fail_barrier_resolution(self):
        conn = self._conn()
        conn.execute(
            """CREATE TRIGGER fail_barrier_resolution
               BEFORE UPDATE OF resolved ON pending_invite_reconciliations
               WHEN OLD.resolved = 0 AND NEW.resolved = 1
                    AND COALESCE(OLD.kind, 'backfill') = 'barrier'
               BEGIN SELECT RAISE(ABORT, 'database is locked'); END"""
        )
        conn.commit()
        conn.close()

    def _allow_barrier_resolution(self):
        conn = self._conn()
        conn.execute("DROP TRIGGER fail_barrier_resolution")
        conn.commit()
        conn.close()

    def _result(self):
        conn = self._conn()
        row = conn.execute(
            "SELECT result FROM access_token_uses WHERE id = ?", (self.use_id,)
        ).fetchone()
        conn.close()
        return row["result"]

    def _open_barriers(self):
        conn = self._conn()
        count = conn.execute(
            """SELECT COUNT(*) FROM pending_invite_reconciliations
               WHERE token_use_id = ? AND resolved = 0
                 AND COALESCE(kind, 'backfill') = 'barrier'""",
            (self.use_id,),
        ).fetchone()[0]
        conn.close()
        return count

    def _used_count(self):
        conn = self._conn()
        value = conn.execute("SELECT used_count FROM access_tokens").fetchone()[0]
        conn.close()
        return value

    def _assert_both_or_neither(self, settled_result):
        result = self._result()
        open_barriers = self._open_barriers()
        self.assertEqual(
            result == settled_result,
            open_barriers == 0,
            f"redemption result={result!r} but open barriers={open_barriers}: "
            "settlement and barrier resolution committed separately",
        )


class AdminConfirmSettlesWithBarrierTest(_BarrierTest):
    def _confirm(self):
        return asyncio.run(
            access_tokens.resolve_pending_confirmation(
                self.use_id,
                access_tokens.ResolvePendingConfirmationRequest(outcome="success"),
            )
        )

    def test_failed_barrier_resolution_does_not_leave_a_settled_use_behind(self):
        self._fail_barrier_resolution()
        try:
            self._confirm()
        except Exception:
            pass
        self._assert_both_or_neither("success")
        # Not settled means still waiting for a terminal state, never dropped.
        self.assertEqual(self._result(), "uncertain")
        self.assertEqual(self._open_barriers(), 1)

        self._allow_barrier_resolution()
        self._confirm()
        self.assertEqual(self._result(), "success")
        self.assertEqual(self._open_barriers(), 0)


class ReconcilerSettlesWithBarrierTest(_BarrierTest):
    def _reconcile(self):
        with (
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(
                access_tokens,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [{"email": EMAIL}]}),
            ),
        ):
            return asyncio.run(access_tokens.reconcile_pending_redemptions())

    def test_failed_barrier_resolution_does_not_leave_a_settled_use_behind(self):
        self._fail_barrier_resolution()
        try:
            self._reconcile()
        except Exception:
            pass
        self._assert_both_or_neither("success")
        self.assertEqual(self._result(), "uncertain")

        self._allow_barrier_resolution()
        counts = self._reconcile()
        self.assertEqual(counts["confirmed"], 1)
        self.assertEqual(self._result(), "success")
        self.assertEqual(self._open_barriers(), 0)


class AdminReleaseSettlesWithBarrierTest(_BarrierTest):
    def _release(self):
        with (
            patch.object(
                access_tokens,
                "load_active_teams",
                new=AsyncMock(
                    return_value=[
                        {"id": "team-1", "name": "Team 1", "access_token": "t",
                         "device_id": "d", "proxy_id": None}
                    ]
                ),
            ),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(
                access_tokens,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [], "pending_invites": []}),
            ),
        ):
            return asyncio.run(
                access_tokens.resolve_pending_confirmation(
                    self.use_id,
                    access_tokens.ResolvePendingConfirmationRequest(
                        outcome="released", note="verified absent"
                    ),
                )
            )

    def test_failed_barrier_resolution_does_not_leave_a_released_code_behind(self):
        self._fail_barrier_resolution()
        try:
            self._release()
        except Exception:
            pass
        self._assert_both_or_neither("failed")
        self.assertEqual(self._result(), "uncertain")
        self.assertEqual(self._used_count(), 1)

        self._allow_barrier_resolution()
        self._release()
        self.assertEqual(self._result(), "failed")
        self.assertEqual(self._used_count(), 0)
        self.assertEqual(self._open_barriers(), 0)


class AnySettlementResolvesTheBarrierTest(_BarrierTest):
    def test_confirmed_invite_settling_an_uncertain_use_closes_its_barrier(self):
        """The live redeem request can finish after the reconciler already locked
        its use as uncertain with a barrier; settling it must close that barrier."""
        expires = asyncio.run(
            record_confirmed_invite_extension(
                "team-1", "", EMAIL, "30d",
                source="self_service",
                token_use_id=self.use_id,
                token_action="invited",
            )
        )
        self.assertIsNotNone(expires)
        self.assertEqual(self._result(), "success")
        self.assertEqual(self._open_barriers(), 0)


if __name__ == "__main__":
    unittest.main()
