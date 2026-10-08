"""Patrol never acts on the Team owner_email or on members TeamBoss authorized before.

Covers the strict-kick path and the pending-invite-revoke path, through absence
and claim races. The shared fixture isolates databases, upstream calls and notifications.
"""

import _isolation  # noqa: F401  must precede app imports
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from _patrol_fixtures import (
    LIVE_PROD_OWNER,
    OLD,
    PROD_OWNER,
    PatrolHistoryCase,
    RecordingClient,
    _live,
    _member,
    _pending,
)

from app import scheduler
from app.services import patrol

PAYER_ID = "u-pay"


def _insert_authorization_history(conn, team_id, user_id, email, source="self_service"):
    conn.execute(
        """INSERT INTO member_expiry
           (team_id, user_id, email, expires_at, auto_kick, kicked,
            kick_source, first_seen_at, source, created_at)
           VALUES (?, ?, ?, '2099-01-01', 1, 1, 'detected', ?, ?, ?)""",
        (team_id, user_id, email, OLD, source, OLD),
    )


@contextmanager
def _during_claim(action):
    """Run action(conn) right after patrol acquires a member-operation claim."""
    original_claim = patrol.member_operation_claim_sync

    @contextmanager
    def claim_then_act(conn, *args, **kwargs):
        with original_claim(conn, *args, **kwargs) as acquired:
            if acquired:
                action(conn)
                conn.commit()
            yield acquired

    with patch.object(patrol, "member_operation_claim_sync", claim_then_act):
        yield


class _AuthorizationCases:
    """Cases shared by both paths; each subclass supplies the path-specific hooks."""

    PATH = ""
    WOULD_KEY = ""   # dry-run counter in the patrol result
    DONE_KEY = ""    # executed counter in the patrol result
    STRICT_FLAG = ""
    REACTIVATE_USER_ID = ""

    # hooks
    def _subject(self, email, user_id=PAYER_ID, **kw):
        raise NotImplementedError

    def _protected_team(self, team_id, subject):
        raise NotImplementedError

    def _gate(self, team_id, subject):
        raise NotImplementedError

    def _assert_no_action(self):
        raise NotImplementedError

    def _arm_returning(self, team_id, subject):
        raise NotImplementedError

    def _returning_customer(self, team_id, *, kick_source="detected", source="self_service"):
        subject = self._subject("payer@example.com")
        self._team(team_id, seats_entitled=2, codex=1)
        self._expiry(team_id, subject["email"], PAYER_ID, source=source,
                     expires_at="2099-01-01T00:00:00+00:00", first_seen_at=OLD)
        conn = self._conn()
        conn.execute(
            "UPDATE member_expiry SET kicked = 1, kick_source = ?, kicked_at = ? WHERE team_id = ?",
            (kick_source, OLD, team_id),
        )
        # Reappearance uses the same helper as a successful scheduler sync.
        self.assertTrue(scheduler._reactivate_or_insert_detected_member(
            conn, team_id, self.REACTIVATE_USER_ID, subject["email"], OLD
        ))
        conn.commit()
        conn.close()
        self._arm_returning(team_id, subject)
        self._arm()
        self._baseline(team_id)
        self._setting("patrol_strict_mode_enabled", self.STRICT_FLAG)
        return subject

    def test_owner_email_is_protected_in_preview_and_gate(self):
        team_id = f"{self.PATH}-owner"
        subject = self._subject("Boss@Example.com", "u-boss")
        self._protected_team(team_id, subject)
        conn = self._conn()
        conn.execute("UPDATE teams SET owner_email = ' boss@EXAMPLE.com ' WHERE id = ?", (team_id,))
        conn.commit()
        conn.close()
        for dry_run in (True, False):
            with self.subTest(path=self.PATH, dry_run=dry_run):
                result = self._patrol(dry_run=dry_run, allow=[team_id])
                self.assertEqual(result[self.WOULD_KEY], 0)
                self.assertEqual(result[self.DONE_KEY], 0)
                self._assert_no_action()
        ok, _ = self._gate(team_id, subject)
        self.assertFalse(ok)
        self._assert_no_action()

    def test_authorization_history_is_rechecked_after_the_claim(self):
        team_id = f"{self.PATH}-claim-history"
        subject = self._subject("payer@example.com")
        self._protected_team(team_id, subject)
        with _during_claim(lambda conn: _insert_authorization_history(
                conn, team_id, PAYER_ID, subject["email"])):
            ok, _ = self._gate(team_id, subject)
        self.assertFalse(ok, self.PATH)
        self._assert_no_action()

    def test_owner_email_is_rechecked_after_the_claim(self):
        team_id = f"{self.PATH}-claim-owner"
        subject = self._subject("payer@example.com")
        self._protected_team(team_id, subject)
        with _during_claim(lambda conn: conn.execute(
                "UPDATE teams SET owner_email = ? WHERE id = ?", (subject["email"], team_id))):
            ok, _ = self._gate(team_id, subject)
        self.assertFalse(ok, self.PATH)
        self._assert_no_action()


class StrictAuthorizationProtectionTest(_AuthorizationCases, PatrolHistoryCase):
    PATH = "strict"
    WOULD_KEY = "strict_would_kick"
    DONE_KEY = "strict_kicked"
    STRICT_FLAG = "1"
    REACTIVATE_USER_ID = PAYER_ID

    def _subject(self, email, user_id=PAYER_ID, **kw):
        return _member(email, user_id, first_seen_at=OLD, **kw)

    def _protected_team(self, team_id, member):
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2, codex=1)
        self._setting("patrol_strict_mode_enabled", "1")
        RecordingClient.live_members = [
            LIVE_PROD_OWNER,
            _live(member["email"], member["id"], member["seat_type"]),
        ]

    def _gate(self, team_id, member):
        conn = self._conn()
        try:
            return patrol._patrol_strict_kick(conn, RecordingClient(), team_id, member)
        finally:
            conn.close()

    def _assert_no_action(self):
        self.assertEqual(self._calls("remove_member"), [])

    def _arm_returning(self, team_id, member):
        self._cache(team_id, [PROD_OWNER, member])
        # Live upstream roster mirrors the cached one.
        RecordingClient.live_members = [LIVE_PROD_OWNER, _live(member["email"], member["id"], "default")]

    def test_sync_absence_does_not_turn_a_paying_customer_into_a_strict_outsider(self):
        for source in ("system", "self_service"):
            for dry_run in (True, False):
                with self.subTest(source=source, dry_run=dry_run):
                    RecordingClient.calls = []
                    team_id = f"returning-{source}-{dry_run}"
                    member = self._returning_customer(team_id, source=source)
                    result = self._patrol(dry_run=dry_run, allow=[team_id])
                    self.assertEqual(result["strict_would_kick"], 0)
                    self.assertEqual(result["strict_kicked"], 0)
                    self.assertEqual(self._calls("remove_member"), [])
                    self.assertEqual(self._calls("get_members"), [])
                    ok, _ = self._gate(team_id, member)
                    self.assertFalse(ok)
                    self.assertEqual(self._calls("remove_member"), [])

    def test_service_ended_by_expiry_admin_or_patrol_allows_external_reentry(self):
        for kick_source in ("auto_expire", "admin", "patrol", "patrol_strict"):
            with self.subTest(kick_source=kick_source):
                RecordingClient.calls = []
                team_id = f"ended-{kick_source}"
                member = self._returning_customer(team_id, kick_source=kick_source)
                result = self._patrol(dry_run=False, allow=[team_id])
                self.assertEqual(result["strict_kicked"], 1)
                self.assertEqual(self._calls("remove_member"), [("remove_member", member["id"])])

    def test_history_on_another_team_does_not_protect_an_external_member(self):
        member = self._subject("payer@example.com")
        self._protected_team("outsider", member)
        self._closed_row("other-team", member["email"], member["id"],
                         source="self_service", kick_source="detected")
        result = self._patrol(dry_run=False, allow=["outsider"])
        self.assertEqual(result["strict_kicked"], 1)
        self.assertEqual(self._calls("remove_member"), [("remove_member", member["id"])])

    def test_protected_history_does_not_reduce_the_abnormal_batch_count(self):
        team_id = "mixed-anomaly"
        protected = [
            _member(f"payer{n}@example.com", f"u-pay{n}", first_seen_at=OLD)
            for n in range(3)
        ]
        outsider = _member("stranger@example.com", "u-stray", first_seen_at=OLD)
        members = [PROD_OWNER, *protected, outsider]
        self._armed_team(team_id, members, seats_entitled=5, codex=1)
        self._setting("patrol_strict_mode_enabled", "1")
        for member in protected:
            self._closed_row(team_id, member["email"], member["id"],
                             source="self_service", kick_source="detected")
        RecordingClient.live_members = [
            LIVE_PROD_OWNER,
            *[_live(member["email"], member["id"], "default") for member in members[1:]],
        ]
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = self._patrol(dry_run=dry_run, allow=[team_id])
                self.assertEqual(result["strict_would_kick"], 0)
                self.assertEqual(result["strict_kicked"], 0)
                guard = [e for e in result["events"] if e.get("action") == "strict_batch_guard"]
                self.assertEqual(len(guard), 1)
                self.assertEqual((guard[0]["count"], guard[0]["team_size"]), (4, 5))
                self.assertEqual(RecordingClient.calls, [])

    def test_authorization_observed_during_refresh_is_excluded_from_post_refresh_selection(self):
        member = self._subject("payer@example.com")
        team_id = "refresh-history"
        self._protected_team(team_id, member)
        original_refresh = patrol._refresh_team_snapshot_sync

        def add_history_during_refresh(conn, team):
            outcome = original_refresh(conn, team)
            _insert_authorization_history(conn, team_id, member["id"], member["email"], source="system")
            conn.commit()
            return outcome

        with patch.object(patrol, "_refresh_team_snapshot_sync", add_history_during_refresh):
            result = self._patrol(dry_run=False, allow=[team_id])
        self.assertEqual(self._calls("get_members"), [("get_members", 0)])
        self.assertEqual(result["strict_kicked"], 0)
        self.assertEqual([e for e in result["events"] if e.get("action") == "strict_kick"], [])
        self.assertEqual(self._calls("remove_member"), [])


class PendingAuthorizationProtectionTest(_AuthorizationCases, PatrolHistoryCase):
    PATH = "pending"
    WOULD_KEY = "invites_would_revoke"
    DONE_KEY = "invites_revoked"
    STRICT_FLAG = "0"
    REACTIVATE_USER_ID = ""  # an invite has no user id yet

    def _subject(self, email, user_id=PAYER_ID, **kw):
        return _pending(email, first_seen_at=OLD, **kw)

    def _protected_team(self, team_id, invite):
        self._armed_team(team_id, [PROD_OWNER], pending=[invite], seats_entitled=2, codex=1)
        self._setting("patrol_strict_mode_enabled", "0")

    def _gate(self, team_id, invite):
        conn = self._conn()
        try:
            return patrol._patrol_revoke_invite(conn, RecordingClient(), team_id, invite)
        finally:
            conn.close()

    def _assert_no_action(self):
        self.assertEqual(RecordingClient.calls, [])

    def _arm_returning(self, team_id, invite):
        self._cache(team_id, [PROD_OWNER], pending=[invite])

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
                    ok, _ = self._gate(team_id, invite)
                    self.assertFalse(ok)
                    self.assertEqual(RecordingClient.calls, [])

    def test_existing_expiry_is_protected_consistently_in_pending_preview(self):
        team_id = "pending-expiry"
        invite = self._subject("tracked@example.com")
        invite["expires_at"] = "2099-01-01T00:00:00+00:00"
        self._protected_team(team_id, invite)
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
                invite = self._subject("stranger@example.com", seat_type=seat_type)
                team_id = f"pending-outsider-{seat_type}"
                self._protected_team(team_id, invite)
                self._closed_row("other-team", invite["email"], "u-stranger",
                                 source="self_service", kick_source="detected")
                result = self._patrol(dry_run=False, allow=[team_id])
                self.assertEqual(result["invites_revoked"], 1)
                self.assertEqual(self._calls("revoke_invite"), [("revoke_invite", invite["email"])])

    def test_invite_id_is_not_treated_as_a_member_user_id(self):
        invite = self._subject("stranger@example.com")
        team_id = "pending-identity"
        self._protected_team(team_id, invite)
        self._closed_row(team_id, "someone-else@example.com", invite["id"],
                         source="self_service", kick_source="detected")
        result = self._patrol(dry_run=False, allow=[team_id])
        self.assertEqual(result["invites_revoked"], 1)
        self.assertEqual(self._calls("revoke_invite"), [("revoke_invite", invite["email"])])


if __name__ == "__main__":
    unittest.main()
