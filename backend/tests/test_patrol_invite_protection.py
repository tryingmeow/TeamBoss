"""Pending patrol preserves Team-scoped authorization through absence and claim races.

The shared fixture isolates databases, upstream calls and notifications.
"""

import _isolation  # noqa: F401  must precede app imports
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from test_patrol_overage_kick import PROD_OWNER, _Fixture
from test_premium_patrol import OLD, RecordingClient, _pending

from app import scheduler
from app.services import patrol


class PendingAuthorizationProtectionTest(_Fixture):
    def _pending_team(self, team_id, invite):
        self._armed_team(team_id, [PROD_OWNER], pending=[invite], seats_entitled=2, codex=1)
        self._setting("patrol_strict_mode_enabled", "0")

    def _revoke_gate(self, team_id, invite):
        conn = self._conn()
        try:
            return patrol._patrol_revoke_invite(conn, RecordingClient(), team_id, invite)
        finally:
            conn.close()

    def _returning_customer(self, team_id, *, kick_source="detected", source="self_service"):
        invite = _pending("payer@example.com", first_seen_at=OLD)
        self._team(team_id, seats_entitled=2, codex=1)
        self._expiry(team_id, invite["email"], "u-pay", source=source,
                     expires_at="2099-01-01T00:00:00+00:00", first_seen_at=OLD)
        conn = self._conn()
        conn.execute(
            "UPDATE member_expiry SET kicked = 1, kick_source = ?, kicked_at = ? WHERE team_id = ?",
            (kick_source, OLD, team_id),
        )
        self.assertTrue(scheduler._reactivate_or_insert_detected_member(
            conn, team_id, "", invite["email"], OLD
        ))
        conn.commit()
        conn.close()
        self._cache(team_id, [PROD_OWNER], pending=[invite])
        self._arm()
        self._baseline(team_id)
        self._setting("patrol_strict_mode_enabled", "0")
        return invite

    def test_returning_authorized_pending_invites_are_excluded_from_preview_and_revoke(self):
        for source in ("system", "self_service"):
            for dry_run in (True, False):
                with self.subTest(source=source, dry_run=dry_run):
                    RecordingClient.calls = []
                    team_id = f"returning-pending-{source}-{dry_run}"
                    invite = self._returning_customer(team_id, source=source)
                    result = self._patrol(dry_run=dry_run, allow=[team_id])
                    self.assertEqual(result["invites_would_revoke"], 0)
                    self.assertEqual(result["invites_revoked"], 0)
                    self.assertEqual(RecordingClient.calls, [])
                    ok, _ = self._revoke_gate(team_id, invite)
                    self.assertFalse(ok)
                    self.assertEqual(RecordingClient.calls, [])

    def test_owner_email_is_protected_in_pending_preview_and_gate(self):
        team_id = "pending-owner"
        invite = _pending("Boss@Example.com", first_seen_at=OLD)
        self._pending_team(team_id, invite)
        conn = self._conn()
        conn.execute("UPDATE teams SET owner_email = ' boss@EXAMPLE.com ' WHERE id = ?", (team_id,))
        conn.commit()
        conn.close()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = self._patrol(dry_run=dry_run, allow=[team_id])
                self.assertEqual(result["invites_would_revoke"], 0)
                self.assertEqual(result["invites_revoked"], 0)
        ok, _ = self._revoke_gate(team_id, invite)
        self.assertFalse(ok)
        self.assertEqual(RecordingClient.calls, [])

    def test_existing_expiry_is_protected_consistently_in_pending_preview(self):
        team_id = "pending-expiry"
        invite = _pending("tracked@example.com", first_seen_at=OLD)
        invite["expires_at"] = "2099-01-01T00:00:00+00:00"
        self._pending_team(team_id, invite)
        conn = self._conn()
        conn.execute("UPDATE member_expiry SET expires_at = ? WHERE team_id = ?",
                     (invite["expires_at"], team_id))
        conn.commit()
        conn.close()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = self._patrol(dry_run=dry_run, allow=[team_id])
                self.assertEqual(result["invites_would_revoke"], 0)
                self.assertEqual(result["invites_revoked"], 0)
        self.assertEqual(RecordingClient.calls, [])

    def test_service_ended_by_expiry_admin_or_patrol_allows_external_pending_reentry(self):
        for kick_source in ("auto_expire", "admin", "patrol", "patrol_strict", "patrol_invite_revoke"):
            with self.subTest(kick_source=kick_source):
                RecordingClient.calls = []
                team_id = f"ended-pending-{kick_source}"
                invite = self._returning_customer(team_id, kick_source=kick_source)
                result = self._patrol(dry_run=False, allow=[team_id])
                self.assertEqual(result["invites_revoked"], 1)
                self.assertEqual(self._calls("revoke_invite"), [("revoke_invite", invite["email"])])

    def test_other_team_history_does_not_protect_new_codex_invites_with_strict_off(self):
        for seat_type in ("default", "usage_based"):
            with self.subTest(seat_type=seat_type):
                RecordingClient.calls = []
                invite = _pending("stranger@example.com", seat_type=seat_type, first_seen_at=OLD)
                team_id = f"pending-outsider-{seat_type}"
                self._pending_team(team_id, invite)
                self._closed_row("other-team", invite["email"], "u-stranger",
                                 source="self_service", kick_source="detected")
                result = self._patrol(dry_run=False, allow=[team_id])
                self.assertEqual(result["invites_revoked"], 1)
                self.assertEqual(self._calls("revoke_invite"), [("revoke_invite", invite["email"])])

    def test_invite_id_is_not_treated_as_a_member_user_id(self):
        invite = _pending("stranger@example.com", first_seen_at=OLD)
        team_id = "pending-identity"
        self._pending_team(team_id, invite)
        self._closed_row(team_id, "someone-else@example.com", invite["id"],
                         source="self_service", kick_source="detected")
        result = self._patrol(dry_run=False, allow=[team_id])
        self.assertEqual(result["invites_revoked"], 1)
        self.assertEqual(self._calls("revoke_invite"), [("revoke_invite", invite["email"])])

    def test_authorization_history_is_rechecked_after_the_revoke_claim(self):
        team_id = "pending-claim-history"
        invite = _pending("payer@example.com", first_seen_at=OLD)
        self._pending_team(team_id, invite)
        original_claim = patrol.member_operation_claim_sync

        @contextmanager
        def add_history_on_claim(conn, *args, **kwargs):
            with original_claim(conn, *args, **kwargs) as acquired:
                if acquired:
                    conn.execute(
                        """INSERT INTO member_expiry
                           (team_id, user_id, email, expires_at, auto_kick, kicked,
                            kick_source, first_seen_at, source, created_at)
                           VALUES (?, 'u-pay', ?, '2099-01-01', 1, 1, 'detected', ?, 'self_service', ?)""",
                        (team_id, invite["email"], OLD, OLD),
                    )
                    conn.commit()
                yield acquired

        with patch.object(patrol, "member_operation_claim_sync", add_history_on_claim):
            ok, _ = self._revoke_gate(team_id, invite)
        self.assertFalse(ok)
        self.assertEqual(RecordingClient.calls, [])

    def test_owner_email_is_rechecked_after_the_revoke_claim(self):
        team_id = "pending-claim-owner"
        invite = _pending("payer@example.com", first_seen_at=OLD)
        self._pending_team(team_id, invite)
        original_claim = patrol.member_operation_claim_sync

        @contextmanager
        def set_owner_on_claim(conn, *args, **kwargs):
            with original_claim(conn, *args, **kwargs) as acquired:
                if acquired:
                    conn.execute("UPDATE teams SET owner_email = ? WHERE id = ?", (invite["email"], team_id))
                    conn.commit()
                yield acquired

        with patch.object(patrol, "member_operation_claim_sync", set_owner_on_claim):
            ok, _ = self._revoke_gate(team_id, invite)
        self.assertFalse(ok)
        self.assertEqual(RecordingClient.calls, [])


if __name__ == "__main__":
    unittest.main()
