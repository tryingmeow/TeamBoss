import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import scheduler as app_scheduler
from app.scheduler import _reconcile_pending_invites_sync
from app.services.member_expiry import upsert_member_expiry


class MemberExpirySourceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_dir = self.tmpdir.name

        # Patch get_db_dir to return temp directory
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.db_dir)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)

        asyncio.run(app_database.init_database())

        self.db_path = app_database.get_db_path()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO teams (id, name, status, created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', '2026-07-21', '2026-07-21')"""
        )
        conn.commit()
        conn.close()


    def test_system_invite_and_fallback_reconciliation_restore_trusted_source(self):
        asyncio.run(
            upsert_member_expiry(
                "team-1", "u1", "member@example.com", None, source="detected"
            )
        )
        asyncio.run(
            upsert_member_expiry(
                "team-1", "u1", "member@example.com", None, source="system"
            )
        )

        conn = sqlite3.connect(self.db_path)
        source = conn.execute(
            "SELECT source FROM member_expiry WHERE team_id = 'team-1' AND kicked = 0"
        ).fetchone()[0]
        self.assertEqual(source, "system")

        # Simulate the rare race: the live invite succeeded, its primary
        # write failed, and an earlier sync already classified it detected.
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', '', 'fallback@example.com', NULL, 0, 0,
                       '2026-07-21', 'detected', '2026-07-21')"""
        )
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved, created_at)
               VALUES ('team-1', '', 'fallback@example.com',
                       '2026-08-21T00:00:00+00:00', 'self_service',
                       'database locked', 0, '2026-07-21')"""
        )
        conn.commit()
        conn.row_factory = sqlite3.Row

        reconciled = _reconcile_pending_invites_sync(
            conn,
            "team-1",
            [],
            [{"email": "fallback@example.com"}],
            "2026-07-22T00:00:00+00:00",
        )
        conn.commit()

        fallback = conn.execute(
            """SELECT source, expires_at, auto_kick
               FROM member_expiry
               WHERE team_id = 'team-1' AND email = 'fallback@example.com' AND kicked = 0"""
        ).fetchone()
        pending = conn.execute(
            """SELECT resolved, resolved_at
               FROM pending_invite_reconciliations
               WHERE team_id = 'team-1' AND email = 'fallback@example.com'"""
        ).fetchone()
        conn.close()

        self.assertEqual(reconciled, 1)
        self.assertEqual(
            dict(fallback),
            {
                "source": "self_service",
                "expires_at": "2026-08-21T00:00:00+00:00",
                "auto_kick": 1,
            },
        )
        self.assertEqual(pending["resolved"], 1)
        self.assertEqual(pending["resolved_at"], "2026-07-22T00:00:00+00:00")

        # The same unresolved marker also fails closed in the scheduled
        # expiry path, even if a stale row is already past its expiry.
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'u-blocked', 'blocked@example.com',
                       '2020-01-01T00:00:00+00:00', 1, 0,
                       '2020-01-01', 'system', '2020-01-01')"""
        )
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, source, reason, resolved, created_at)
               VALUES ('team-1', 'u-blocked', 'blocked@example.com',
                       'system', 'database locked', 0, '2026-07-21')"""
        )
        conn.commit()
        conn.close()

        with patch.object(
            app_scheduler,
            "ChatGPTClient",
            side_effect=AssertionError("auto kick must not contact OpenAI"),
        ):
            app_scheduler.auto_kick_job()

        conn = sqlite3.connect(self.db_path)
        kicked = conn.execute(
            """SELECT kicked FROM member_expiry
               WHERE team_id = 'team-1' AND user_id = 'u-blocked'"""
        ).fetchone()[0]
        conn.close()
        self.assertEqual(kicked, 0)


if __name__ == "__main__":
    unittest.main()
