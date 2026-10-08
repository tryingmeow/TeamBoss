"""A settled redemption leaves none of its pending_invite_reconciliations rows open.

A redemption can own several rows: kind='barrier' (uncertain invite, patrol
shield) and kind='extend' (the remote invite succeeded but the local expiry
write failed). Once the redemption is terminal, the scheduler never credits
those rows again, but batch invites (gpt_invites._team_with_unresolved_invite)
treat any open row as "an invite may already be in that Team" and refuse that
email until a sync happens to see the person there, which never happens once
he is gone. Every path that makes a redemption terminal must therefore close
all of its rows in the same transaction, and only its own rows.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import logging
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes import access_tokens
from app.services import gpt_invites, member_expiry
from app.utils.durations import expiry_from_duration

UTC = timezone.utc
TEAM = "team-1"
EMAIL = "redeemer@example.com"
OTHER_EMAIL = "other@example.com"
PRESENT = {"members": [{"id": "u1", "email": EMAIL}], "pending_invites": []}
ABSENT = {"members": [], "pending_invites": []}


class _RowsCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        no_backoff = patch.object(member_expiry, "_CONFIRM_WRITE_BACKOFF_SECONDS", 0)
        no_backoff.start()
        self.addCleanup(no_backoff.stop)
        # The failed-write fallback logs on purpose; keep test output clean.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, access_token, device_id,
                                  created_at, updated_at)
               VALUES (?, 'Team 1', 'active', 'access', 'device', '2026-10-01', '2026-10-01')""",
            (TEAM,),
        )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── redemption state, built with the production helpers ─────────────────

    def _invite_confirmed_but_local_write_failed(self, email=EMAIL):
        """Reserve a code, send the invite on TEAM, and fail every local expiry write.

        Leaves what a live redeem leaves in that case: the use is still pending
        (invite_pending on TEAM) and owns one kind='extend' row.
        """
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'atm_x', '30d', 1, 0, 0, '2026-10-01')""",
            (f"hash-{uuid.uuid4().hex}",),
        )
        token_id = cur.lastrowid
        conn.commit()
        conn.close()
        token_use_id = asyncio.run(
            access_tokens._reserve_token_use(
                token_id, email, expiry_from_duration("30d").isoformat()
            )
        )
        asyncio.run(
            access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=TEAM)
        )
        failing = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))
        with patch.object(member_expiry, "extend_member_expiry", failing):
            asyncio.run(
                member_expiry.record_confirmed_invite_extension(
                    TEAM, "", email, "30d",
                    source="self_service",
                    token_use_id=token_use_id,
                    token_action="invited",
                )
            )
        self.assertEqual(self._rows(token_use_id), [("extend", 0)])
        return token_use_id

    def _lock_interrupted(self, token_use_id, email=EMAIL):
        """The reconciler could not see the person in time: uncertain + barrier."""
        locked = asyncio.run(
            access_tokens._lock_uncertain_with_barrier(
                token_use_id,
                team_id=TEAM,
                email=email,
                error_message="OpenAI invite result is uncertain",
                reason="invite interrupted before its result was recorded",
            )
        )
        self.assertTrue(locked)
        self.assertEqual(self._rows(token_use_id), [("extend", 0), ("barrier", 0)])

    def _admin_invite_left_uncertain(self, email=EMAIL):
        """An admin invite with an uncertain result: an open row with no token_use_id."""
        asyncio.run(
            member_expiry.record_uncertain_invite(
                TEAM, "", email,
                datetime.now(UTC) + timedelta(days=30),
                source="system", reason="OpenAI invite result is uncertain",
            )
        )

    # ── settlement paths ─────────────────────────────────────────────────

    def _run_reconciler(self, snapshot=PRESENT):
        with (
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(
                access_tokens, "fetch_and_cache_members", new=AsyncMock(return_value=snapshot)
            ),
        ):
            return asyncio.run(access_tokens.reconcile_pending_redemptions())

    def _admin_confirm(self, token_use_id):
        return asyncio.run(
            access_tokens.resolve_pending_confirmation(
                token_use_id,
                access_tokens.ResolvePendingConfirmationRequest(outcome="success"),
            )
        )

    def _admin_release(self, token_use_id):
        with (
            patch.object(
                access_tokens,
                "load_active_teams",
                new=AsyncMock(
                    return_value=[
                        {"id": TEAM, "name": "Team 1", "access_token": "access",
                         "device_id": "device", "proxy_id": None}
                    ]
                ),
            ),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(
                access_tokens, "fetch_and_cache_members", new=AsyncMock(return_value=ABSENT)
            ),
        ):
            return asyncio.run(
                access_tokens.resolve_pending_confirmation(
                    token_use_id,
                    access_tokens.ResolvePendingConfirmationRequest(
                        outcome="released", note="verified absent"
                    ),
                )
            )

    # ── observations ──────────────────────────────────────────────────────

    def _rows(self, token_use_id):
        conn = self._conn()
        rows = conn.execute(
            """SELECT COALESCE(kind, 'backfill') AS kind, resolved
               FROM pending_invite_reconciliations WHERE token_use_id = ? ORDER BY id""",
            (token_use_id,),
        ).fetchall()
        conn.close()
        return [(r["kind"], r["resolved"]) for r in rows]

    def _open_rows_without_token_use(self):
        conn = self._conn()
        count = conn.execute(
            """SELECT COUNT(*) FROM pending_invite_reconciliations
               WHERE token_use_id IS NULL AND resolved = 0"""
        ).fetchone()[0]
        conn.close()
        return count

    def _use(self, token_use_id):
        conn = self._conn()
        row = conn.execute(
            """SELECT atu.result, atu.expires_at, at.used_count
               FROM access_token_uses atu JOIN access_tokens at ON at.id = atu.token_id
               WHERE atu.id = ?""",
            (token_use_id,),
        ).fetchone()
        conn.close()
        return dict(row)

    def _expiry_rows(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT expires_at FROM member_expiry WHERE team_id = ? AND kicked = 0", (TEAM,)
        ).fetchall()
        conn.close()
        return [r["expires_at"] for r in rows]

    def _assert_credited_once(self):
        expiry_rows = self._expiry_rows()
        self.assertEqual(len(expiry_rows), 1)
        remaining = datetime.fromisoformat(expiry_rows[0]) - datetime.now(UTC)
        self.assertGreater(remaining, timedelta(days=29, hours=23))
        self.assertLessEqual(remaining, timedelta(days=30))

    def _unresolved_invite_team(self, email=EMAIL):
        return asyncio.run(gpt_invites._team_with_unresolved_invite(email))


class SuccessSettlesAllRowsTest(_RowsCase):
    def test_reconciler_confirmation_closes_the_extend_row(self):
        token_use_id = self._invite_confirmed_but_local_write_failed()
        self.assertIsNotNone(self._unresolved_invite_team())

        counts = self._run_reconciler()

        self.assertEqual(counts["confirmed"], 1)
        self.assertEqual(self._use(token_use_id)["result"], "success")
        self.assertEqual(self._rows(token_use_id), [("extend", 1)])
        self._assert_credited_once()
        self.assertIsNone(
            self._unresolved_invite_team(),
            "a settled redemption must not keep blocking batch invites for this email",
        )

    def test_admin_confirmation_closes_barrier_and_extend_rows(self):
        token_use_id = self._invite_confirmed_but_local_write_failed()
        self._lock_interrupted(token_use_id)

        self._admin_confirm(token_use_id)

        self.assertEqual(self._use(token_use_id)["result"], "success")
        self.assertEqual(self._rows(token_use_id), [("extend", 1), ("barrier", 1)])
        self._assert_credited_once()
        self.assertIsNone(self._unresolved_invite_team())


class AdminReleaseSettlesAllRowsTest(_RowsCase):
    def test_release_closes_barrier_and_extend_rows(self):
        token_use_id = self._invite_confirmed_but_local_write_failed()
        self._lock_interrupted(token_use_id)

        result = self._admin_release(token_use_id)

        self.assertEqual(result["outcome"], "released")
        use = self._use(token_use_id)
        self.assertEqual((use["result"], use["used_count"]), ("failed", 0))
        self.assertEqual(self._rows(token_use_id), [("extend", 1), ("barrier", 1)])
        self.assertEqual(self._expiry_rows(), [])
        self.assertIsNone(
            self._unresolved_invite_team(),
            "a released redemption must not keep blocking batch invites for this email",
        )


class RefundRacingReconcilerTest(_RowsCase):
    """The admin's 确认失败 commits after the reconciler saw the person but before
    its local write: that write fails because the redemption is no longer open,
    and its fallback must not leave an 'extend' row behind for a refunded code.
    Nothing would ever close that row, so batch invites would refuse the email
    for good."""

    def _uncertain_redemption(self):
        conn = self._conn()
        token_id = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'atm_x', '30d', 1, 0, 0, '2026-10-01')""",
            (f"hash-{uuid.uuid4().hex}",),
        ).lastrowid
        conn.commit()
        conn.close()
        token_use_id = asyncio.run(access_tokens._reserve_token_use(token_id, EMAIL, None))
        asyncio.run(
            access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=TEAM)
        )
        self.assertTrue(
            asyncio.run(
                access_tokens._lock_uncertain_with_barrier(
                    token_use_id,
                    team_id=TEAM,
                    email=EMAIL,
                    error_message="OpenAI invite result is uncertain",
                    reason="OpenAI invite result is uncertain",
                )
            )
        )
        return token_use_id

    def test_refund_during_reconciler_write_leaves_no_extend_row(self):
        token_use_id = self._uncertain_redemption()

        async def snapshot_then_admin_refund(*_args, **_kwargs):
            # The reconciler has its snapshot (the person is there); the admin's
            # refund commits before the reconciler writes anything.
            released = await access_tokens._release_uncertain_token_use(
                token_use_id, error_message="admin_released: verified absent"
            )
            self.assertTrue(released)
            return PRESENT

        with (
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(
                access_tokens,
                "fetch_and_cache_members",
                new=AsyncMock(side_effect=snapshot_then_admin_refund),
            ),
        ):
            counts = asyncio.run(access_tokens.reconcile_pending_redemptions())

        self.assertEqual(counts["confirmed"], 0)
        use = self._use(token_use_id)
        self.assertEqual((use["result"], use["used_count"]), ("failed", 0))
        self.assertEqual(self._rows(token_use_id), [("barrier", 1)])
        self.assertEqual(self._expiry_rows(), [])
        self.assertIsNone(
            self._unresolved_invite_team(),
            "a refunded redemption must not leave a row that blocks batch invites",
        )


class OtherRowsStayOpenTest(_RowsCase):
    """Settling one redemption closes its own rows only."""

    def _bystanders(self):
        # Same email and Team, no token_use_id: an admin invite still unresolved.
        self._admin_invite_left_uncertain(EMAIL)
        # Another redemption, still pending, with its own extend row.
        other_use_id = self._invite_confirmed_but_local_write_failed(OTHER_EMAIL)
        return other_use_id

    def _assert_bystanders_open(self, other_use_id):
        self.assertEqual(self._open_rows_without_token_use(), 1)
        self.assertEqual(self._rows(other_use_id), [("extend", 0)])
        self.assertEqual(self._use(other_use_id)["result"], "pending")
        self.assertIsNotNone(self._unresolved_invite_team(EMAIL))
        self.assertIsNotNone(self._unresolved_invite_team(OTHER_EMAIL))

    def test_confirmation_leaves_other_rows_open(self):
        token_use_id = self._invite_confirmed_but_local_write_failed()
        self._lock_interrupted(token_use_id)
        other_use_id = self._bystanders()

        self._admin_confirm(token_use_id)

        self.assertEqual(self._rows(token_use_id), [("extend", 1), ("barrier", 1)])
        self._assert_bystanders_open(other_use_id)

    def test_release_leaves_other_rows_open(self):
        token_use_id = self._invite_confirmed_but_local_write_failed()
        self._lock_interrupted(token_use_id)
        other_use_id = self._bystanders()

        self._admin_release(token_use_id)

        self.assertEqual(self._rows(token_use_id), [("extend", 1), ("barrier", 1)])
        self._assert_bystanders_open(other_use_id)


class ConfirmRacesRefundTest(_RowsCase):
    """A refund commits after 确认成功 read the redemption but before its local write."""

    def _confirm_after_refund(self, token_use_id):
        real = member_expiry.record_confirmed_invite_extension

        async def refund_then_write(*args, **kwargs):
            conn = self._conn()
            conn.execute(
                "UPDATE access_token_uses SET result = 'failed', action = 'redeem_admin_released' "
                "WHERE id = ?",
                (token_use_id,),
            )
            conn.execute("UPDATE access_tokens SET used_count = 0")
            conn.commit()
            conn.close()
            return await real(*args, **kwargs)

        with (
            patch.object(access_tokens, "record_confirmed_invite_extension", refund_then_write),
            patch.object(member_expiry, "_CONFIRM_WRITE_BACKOFF_SECONDS", 0),
        ):
            return self._admin_confirm(token_use_id)

    def _logged_actions(self, after_id=0):
        conn = self._conn()
        rows = conn.execute(
            "SELECT action, result FROM operation_logs WHERE id > ? ORDER BY id", (after_id,)
        ).fetchall()
        conn.close()
        return [(r["action"], r["result"]) for r in rows]

    def test_confirm_success_after_a_refund_answers_409_and_logs_no_success(self):
        token_use_id = self._invite_confirmed_but_local_write_failed()
        self._lock_interrupted(token_use_id)

        with self.assertRaises(HTTPException) as caught:
            self._confirm_after_refund(token_use_id)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("已被退码", caught.exception.detail)
        self.assertNotIn(
            "self_service_invite_admin_confirmed", [a for a, _ in self._logged_actions()]
        )
        self.assertEqual(self._use(token_use_id)["result"], "failed")
        self.assertEqual(self._expiry_rows(), [])

    def test_skipped_fallback_is_logged_as_skipped_not_as_a_write_failure(self):
        token_use_id = self._invite_confirmed_but_local_write_failed()
        self._lock_interrupted(token_use_id)
        # The setup's own (genuine) write failure is logged too; look past it.
        conn = self._conn()
        last_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM operation_logs").fetchone()[0]
        conn.close()

        with self.assertRaises(HTTPException):
            self._confirm_after_refund(token_use_id)

        actions = self._logged_actions(after_id=last_id)
        self.assertIn(("member_expiry_write_skipped", "skipped"), actions)
        self.assertNotIn("member_expiry_write_failed", [a for a, _ in actions])


if __name__ == "__main__":
    unittest.main()
