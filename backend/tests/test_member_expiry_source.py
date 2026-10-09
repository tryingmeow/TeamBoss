"""What a member_expiry row's source means, and when it may change.

'detected' with no expiry is an outsider the sync found, not a permanent grant:
it renews like an unmanaged member, and a trusted invite or a fallback credit
upgrades its source. An authorized row with no expiry stays permanent. When the
upstream user id drifts, the row reused by email keeps its paid source.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _redemption_fixtures import RedemptionLedgerCase

from app import scheduler as app_scheduler
from app.scheduler import (
    _reactivate_or_insert_detected_member,
    _reconcile_pending_invites_sync,
)
from app.services.member_expiry import (
    PermanentMembershipError,
    extend_member_expiry,
    get_active_expiry_state,
    upsert_member_expiry,
)


class MemberExpirySourceTest(RedemptionLedgerCase):
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


# ── detected + NULL 不是永久授权 ───────────────────────────────────────────────

class DetectedNullIsNotPermanentTest(RedemptionLedgerCase):
    def test_state_and_renewal(self):
        asyncio.run(
            upsert_member_expiry(
                "team-1", "u1", "stranger@example.com", None, source="detected"
            )
        )
        self.assertEqual(
            asyncio.run(
                get_active_expiry_state("team-1", "u1", "stranger@example.com")
            ),
            "unmanaged",
        )

        token_use_id = self._new_token_use("stranger@example.com", result="pending")
        expires = asyncio.run(
            extend_member_expiry(
                "team-1",
                "u1",
                "stranger@example.com",
                "30d",
                source="self_service",
                token_use_id=token_use_id,
                token_action="renewed_member",
            )
        )
        self.assertIsNotNone(expires)

        conn = self._conn()
        row = conn.execute(
            "SELECT expires_at, auto_kick, source FROM member_expiry"
        ).fetchone()
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row["expires_at"])
        self.assertEqual(row["auto_kick"], 1)
        self.assertEqual(row["source"], "self_service")
        self.assertEqual(use["result"], "success")

    def test_authorized_null_row_is_still_permanent(self):
        asyncio.run(
            upsert_member_expiry(
                "team-1", "u2", "vip@example.com", None, source="self_service"
            )
        )
        self.assertEqual(
            asyncio.run(get_active_expiry_state("team-1", "u2", "vip@example.com")),
            "permanent",
        )
        with self.assertRaises(PermanentMembershipError):
            asyncio.run(
                extend_member_expiry(
                    "team-1", "u2", "vip@example.com", "30d", source="self_service"
                )
            )


# ── 复用行不被降级成 detected ──────────────────────────────────────────────────

class DetectedSourceIsNotDowngradedTest(RedemptionLedgerCase):
    def test_reusing_a_paid_row_keeps_its_source(self):
        """上游 user_id 漂移时会按邮箱复用已有行。无条件写 'detected' 会把付费
        成员降级成外人，出现在管理端"检测到的成员"列表里等着被手工清掉。
        """
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'old-uid', 'paid@example.com',
                       '2027-01-01T00:00:00+00:00', 1, 0,
                       '2026-09-01T00:00:00+00:00', 'self_service',
                       '2026-09-01T00:00:00+00:00')"""
        )
        conn.commit()
        created = _reactivate_or_insert_detected_member(
            conn, "team-1", "new-uid", "paid@example.com", "2026-09-11T00:00:00+00:00"
        )
        conn.commit()
        row = conn.execute(
            "SELECT user_id, source, expires_at FROM member_expiry WHERE team_id='team-1'"
        ).fetchone()
        conn.close()
        self.assertFalse(created)
        self.assertEqual(row["user_id"], "new-uid")
        self.assertEqual(row["source"], "self_service")
        self.assertEqual(row["expires_at"], "2027-01-01T00:00:00+00:00")

    def test_a_genuinely_detected_row_is_still_labelled_detected(self):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'old-uid', 'stranger@example.com', NULL, 0, 0,
                       '2026-09-01T00:00:00+00:00', 'detected',
                       '2026-09-01T00:00:00+00:00')"""
        )
        conn.commit()
        _reactivate_or_insert_detected_member(
            conn, "team-1", "new-uid", "stranger@example.com", "2026-09-11T00:00:00+00:00"
        )
        conn.commit()
        source = conn.execute(
            "SELECT source FROM member_expiry WHERE team_id='team-1'"
        ).fetchone()["source"]
        conn.close()
        self.assertEqual(source, "detected")


if __name__ == "__main__":
    unittest.main()
