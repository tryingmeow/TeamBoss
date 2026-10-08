"""Strict patrol preserves Team-scoped authorization after absence and during claims.

All databases, upstream calls and notifications are isolated by the shared fixture.
"""

import _isolation  # noqa: F401  must precede app imports
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from test_patrol_overage_kick import LIVE_PROD_OWNER, PROD_OWNER, _Fixture
from test_premium_patrol import OLD, RecordingClient, _live, _member

from app import scheduler
from app.services import patrol


class StrictAuthorizationProtectionTest(_Fixture):
    def _strict_team(self, team_id, member):
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2, codex=1)
        self._setting("patrol_strict_mode_enabled", "1")
        RecordingClient.live_members = [
            LIVE_PROD_OWNER,
            _live(member["email"], member["id"], member["seat_type"]),
        ]

    def _strict_gate(self, team_id, member):
        conn = self._conn()
        try:
            return patrol._patrol_strict_kick(conn, RecordingClient(), team_id, member)
        finally:
            conn.close()

    def _returning_customer(self, team_id, *, kick_source="detected", source="self_service"):
        member = _member("payer@example.com", "u-pay", first_seen_at=OLD)
        self._team(team_id, seats_entitled=2, codex=1)
        self._expiry(team_id, member["email"], member["id"], source=source,
                     expires_at="2099-01-01T00:00:00+00:00", first_seen_at=OLD)
        conn = self._conn()
        conn.execute(
            "UPDATE member_expiry SET kicked = 1, kick_source = ?, kicked_at = ? WHERE team_id = ?",
            (kick_source, OLD, team_id),
        )
        # Reappearance uses the same helper as a successful scheduler sync.
        self.assertTrue(scheduler._reactivate_or_insert_detected_member(
            conn, team_id, member["id"], member["email"], OLD
        ))
        conn.commit()
        conn.close()
        self._cache(team_id, [PROD_OWNER, member])
        self._arm()
        self._baseline(team_id)
        self._setting("patrol_strict_mode_enabled", "1")
        RecordingClient.live_members = [LIVE_PROD_OWNER, _live(member["email"], member["id"], "default")]
        return member

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
                    ok, _ = self._strict_gate(team_id, member)
                    self.assertFalse(ok)
                    self.assertEqual(self._calls("remove_member"), [])

    def test_owner_email_is_protected_when_upstream_role_is_not_account_owner(self):
        member = _member("Boss@Example.com", "u-boss", first_seen_at=OLD)
        team_id = "owner-email"
        self._strict_team(team_id, member)
        conn = self._conn()
        conn.execute("UPDATE teams SET owner_email = ' boss@EXAMPLE.com ' WHERE id = ?", (team_id,))
        conn.commit()
        conn.close()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = self._patrol(dry_run=dry_run, allow=[team_id])
                self.assertEqual(result["strict_would_kick"], 0)
                self.assertEqual(result["strict_kicked"], 0)
                self.assertEqual(self._calls("remove_member"), [])
        ok, _ = self._strict_gate(team_id, member)
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
        member = _member("payer@example.com", "u-pay", first_seen_at=OLD)
        self._strict_team("outsider", member)
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

    def test_authorization_history_is_rechecked_after_the_member_claim(self):
        member = _member("payer@example.com", "u-pay", first_seen_at=OLD)
        team_id = "claim-history"
        self._strict_team(team_id, member)
        original_claim = patrol.member_operation_claim_sync

        @contextmanager
        def add_history_on_claim(conn, *args, **kwargs):
            with original_claim(conn, *args, **kwargs) as acquired:
                if acquired:
                    conn.execute(
                        """INSERT INTO member_expiry
                           (team_id, user_id, email, expires_at, auto_kick, kicked,
                            kick_source, first_seen_at, source, created_at)
                           VALUES (?, ?, ?, '2099-01-01', 1, 1, 'detected', ?, 'self_service', ?)""",
                        (team_id, member["id"], member["email"], OLD, OLD),
                    )
                    conn.commit()
                yield acquired

        with patch.object(patrol, "member_operation_claim_sync", add_history_on_claim):
            ok, _ = self._strict_gate(team_id, member)
        self.assertFalse(ok)
        self.assertEqual(self._calls("remove_member"), [])

    def test_owner_email_is_rechecked_after_the_member_claim(self):
        member = _member("payer@example.com", "u-pay", first_seen_at=OLD)
        team_id = "claim-owner"
        self._strict_team(team_id, member)
        original_claim = patrol.member_operation_claim_sync

        @contextmanager
        def set_owner_on_claim(conn, *args, **kwargs):
            with original_claim(conn, *args, **kwargs) as acquired:
                if acquired:
                    conn.execute("UPDATE teams SET owner_email = ? WHERE id = ?", (member["email"], team_id))
                    conn.commit()
                yield acquired

        with patch.object(patrol, "member_operation_claim_sync", set_owner_on_claim):
            ok, _ = self._strict_gate(team_id, member)
        self.assertFalse(ok)
        self.assertEqual(self._calls("remove_member"), [])

    def test_authorization_observed_during_refresh_is_excluded_from_post_refresh_selection(self):
        member = _member("payer@example.com", "u-pay", first_seen_at=OLD)
        team_id = "refresh-history"
        self._strict_team(team_id, member)
        original_refresh = patrol._refresh_team_snapshot_sync

        def add_history_during_refresh(conn, team):
            outcome = original_refresh(conn, team)
            conn.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked,
                    kick_source, first_seen_at, source, created_at)
                   VALUES (?, ?, ?, '2099-01-01', 1, 1, 'detected', ?, 'system', ?)""",
                (team_id, member["id"], member["email"], OLD, OLD),
            )
            conn.commit()
            return outcome

        with patch.object(patrol, "_refresh_team_snapshot_sync", add_history_during_refresh):
            result = self._patrol(dry_run=False, allow=[team_id])
        self.assertEqual(self._calls("get_members"), [("get_members", 0)])
        self.assertEqual(result["strict_kicked"], 0)
        self.assertEqual([e for e in result["events"] if e.get("action") == "strict_kick"], [])
        self.assertEqual(self._calls("remove_member"), [])


if __name__ == "__main__":
    unittest.main()
