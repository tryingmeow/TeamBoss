"""Patrol never acts on the Team owner_email or on members TeamBoss authorized before.

Every action path is covered: over-quota kick, Premium kick, strict kick and pending-invite revoke,
in preview, in the live round, in the _patrol_kick / _patrol_strict_kick / _patrol_revoke_invite
gates, and again after the member-operation claim (records that appear during the claim).

- owner_email (case-insensitive, trimmed) is the Owner even when the upstream role is not
  account-owner.
- Protecting history: an open system / self_service row, a row closed by sync absence
  (kick_source='detected'), or a successful / pending / uncertain redemption in this Team.
  A row closed by expiry, an admin or patrol ends that protection, and a redemption protects
  only while no later expiry / admin / patrol closure exists for this Team + email (times
  compared after parsing; an unreadable time keeps the protection).
- History on another Team, or an invite id that equals some member's user id, protects nobody.
- A protected member's over-quota slot is not handed on; GET /api/patrol/status (Telegram
  /watch, /team) lists protected people as "not removed automatically", never as pending.

Seat-change / invite operation logs that block Premium kicks are in test_patrol_premium.
Shared fixtures isolate databases, upstream calls and notifications.
"""

import _isolation  # noqa: F401  must precede app imports
import asyncio
import contextlib
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from _patrol_fixtures import (
    LIVE_PROD_OWNER,
    OLD,
    OWNER,
    PROD_OWNER,
    PatrolHistoryCase,
    RecordingClient,
    _live,
    _member,
    _outsider,
    _pending,
)

from app import scheduler, tg_bot
from app.routes import patrol as patrol_routes
from app.services import patrol

PAYER_ID = "u-pay"
KEEPER = _member("keeper@example.com", "u-k", source="system")

REDEEMED = "2026-07-09T00:00:00+00:00"
AFTER = "2026-08-02T00:00:00+00:00"
BEFORE = "2026-07-01T00:00:00+00:00"


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


# ═══ strict kick and pending-invite revoke: owner_email and authorization history ═══

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


class _HistoryCase(PatrolHistoryCase):
    """PatrolHistoryCase plus writers for the owner_email and the history that protects a member."""

    _token_seq = 0

    def _owner_email(self, team_id, owner_email):
        conn = self._conn()
        conn.execute("UPDATE teams SET owner_email = ? WHERE id = ?", (owner_email, team_id))
        conn.commit()
        conn.close()

    def _redeem(self, team_id, email, created_at, *, result="success", seat_type="default",
                user_id=None):
        """这个 Team + 邮箱的一次兑换（access_token_uses 一行）。"""
        _HistoryCase._token_seq += 1
        conn = self._conn()
        token_id = conn.execute(
            """INSERT INTO access_tokens (token_hash, token_prefix, grant_expires_in, max_uses,
                                          used_count, disabled, created_at, seat_type)
               VALUES (?, 'p', '30d', 1, 1, 0, '2026-07-01', ?)""",
            (f"hash-history-{_HistoryCase._token_seq}", seat_type),
        ).lastrowid
        conn.execute(
            """INSERT INTO access_token_uses (token_id, email, action, team_id, user_id, result, created_at)
               VALUES (?, ?, 'invite', ?, ?, ?, ?)""",
            (token_id, email, team_id, user_id, result, created_at),
        )
        conn.commit()
        conn.close()

    def _redemption(self, team_id, email, result="success", *, seat_type="default", user_id=None):
        """_redeem() on 2026-07-09."""
        self._redeem(team_id, email, "2026-07-09", result=result, seat_type=seat_type,
                     user_id=user_id)

    def _closed(self, team_id, email, user_id, *, kick_source, kicked_at, source="self_service"):
        """这个 Team + 邮箱一条已关闭的 member_expiry 行（kicked=1、auto_kick=1，关闭时间由调用方给）。"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at, kick_source,
                first_seen_at, source, created_at)
               VALUES (?, ?, ?, '2026-08-01T00:00:00+00:00', 1, 1, ?, ?,
                       '2026-07-01T00:00:00+00:00', ?, '2026-07-01T00:00:00+00:00')""",
            (team_id, user_id, email, kicked_at, kick_source, source),
        )
        conn.commit()
        conn.close()

    def _history_row(self, team_id, email, user_id, *, source, kicked, created_at, kicked_at,
                     expires_at=None, kick_source="detected"):
        """任意形状的 member_expiry 行（auto_kick=0；first_seen_at = created_at）。"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at, kick_source,
                first_seen_at, source, created_at)
               VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (team_id, user_id, email, expires_at, kicked, kicked_at, kick_source,
             created_at, source, created_at),
        )
        conn.commit()
        conn.close()


# ═══ teams.owner_email 也是 Owner（Premium 和超员两条路） ═════════════════════════

class OwnerEmailIsOwnerTest(_HistoryCase):
    def _owner_row(self, seat_type):
        # Owner 的成员条目角色不是 account-owner（is_owner=False），来源记录是 detected。
        return _member("Boss@Example.com", "u-boss", seat_type=seat_type,
                       first_seen_at="2026-08-01T00:00:00+00:00")

    def test_premium_owner_by_email_is_not_kicked(self):
        team_id = "team-f4-premium"
        boss = self._owner_row("prolite")
        self._armed_team(team_id, [PROD_OWNER, boss], seats_entitled=2)
        self._owner_email(team_id, "boss@EXAMPLE.com ")

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        self.assertEqual(self._logs("patrol_kick"), [])
        self.assertEqual(self._logs("patrol_premium_alert"), [])
        self.assertEqual(
            patrol.select_premium_kick_candidates([PROD_OWNER, boss], "boss@example.com"), []
        )

    def test_premium_kick_gate_rejects_the_owner_email(self):
        team_id = "team-f4-gate"
        boss = self._owner_row("prolite")
        self._armed_team(team_id, [PROD_OWNER, boss], seats_entitled=2)
        self._owner_email(team_id, "boss@example.com")

        ok, reason = self._kick(team_id, boss, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)

        self.assertFalse(ok)
        self.assertIn("owner_email", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_over_quota_skips_the_owner_email_and_takes_the_next_outsider(self):
        # 1 个席位：Owner 条目（按邮箱认）+ 一个更老的外部成员，超 1 个。Owner 不算候选，
        # 和 is_owner=True 的 Owner 一样；超员的那一个是外部成员。
        team_id = "team-f4-oq"
        boss = self._owner_row("default")
        out = _outsider(1, day=10)
        self._armed_team(team_id, [boss, out], seats_entitled=1)
        self._owner_email(team_id, "boss@example.com")

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-out1")])
        RecordingClient.calls = []
        ok, reason = self._kick(team_id, boss)
        self.assertFalse(ok)
        self.assertIn("owner_email", reason)
        self.assertEqual(self._calls("remove_member"), [])


# ═══ 只有在管 / 因缺席才没在管 / 兑换过的人受保护（两条踢人路径） ══════════════════════

class ManagedHistoryNarrowTest(_HistoryCase):
    def _history_cases(self):
        """名字 → (怎么造历史, 是否保护)。"""
        return {
            "closed_by_expiry": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="auto_expire"), False),
            "self_service_closed_by_expiry": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="auto_expire", source="self_service"), False),
            "closed_by_admin": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="admin"), False),
            "closed_by_patrol": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="patrol"), False),
            "closed_by_sync_absence": (lambda t: self._closed_row(
                t, "ex@example.com", "u-ex", kick_source="detected"), True),
            "closed_by_sync_absence_matched_by_user_id": (lambda t: self._closed_row(
                t, "old-address@example.com", "u-ex", kick_source="detected", source="self_service"), True),
            "open_system_row": (lambda t: self._open_row(t, "ex@example.com", "u-ex"), True),
            "successful_redemption": (lambda t: self._redemption(t, "ex@example.com", "success"), True),
            "pending_redemption": (lambda t: self._redemption(t, "ex@example.com", "pending"), True),
            "uncertain_redemption": (lambda t: self._redemption(t, "ex@example.com", "uncertain"), True),
            "failed_redemption": (lambda t: self._redemption(t, "ex@example.com", "failed"), False),
        }

    def test_over_quota_kick(self):
        # 1 个席位：Owner + 这个外部成员，超 1 个。
        for name, (add_history, protects) in self._history_cases().items():
            with self.subTest(case=name):
                RecordingClient.calls = []
                team_id = f"team-p3-oq-{name}"
                member = _member("ex@example.com", "u-ex")
                add_history(team_id)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=1)

                self._patrol(dry_run=False, allow=[team_id])

                expected = [] if protects else [("remove_member", "u-ex")]
                self.assertEqual(self._calls("remove_member"), expected)

    def test_premium_kick(self):
        for name, (add_history, protects) in self._history_cases().items():
            with self.subTest(case=name):
                RecordingClient.calls = []
                team_id = f"team-p3-pr-{name}"
                member = _member("ex@example.com", "u-ex", seat_type="prolite")
                add_history(team_id)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

                self._patrol(dry_run=False, allow=[team_id])

                expected = [] if protects else [("remove_member", "u-ex")]
                self.assertEqual(self._calls("remove_member"), expected)

    def test_expired_customer_back_as_premium_is_a_normal_outsider(self):
        team_id = "team-p3-gate"
        member = _member("ex@example.com", "u-ex", seat_type="prolite")
        self._closed_row(team_id, "ex@example.com", "u-ex", kick_source="auto_expire")
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

        conn = self._conn()
        self.assertFalse(patrol._teamboss_managed_history_sync(conn, team_id, "ex@example.com", "u-ex"))
        self.assertIsNone(patrol._premium_kick_veto_sync(conn, team_id, "ex@example.com", "u-ex"))
        conn.close()
        ok, reason = self._kick(team_id, member, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        self.assertTrue(ok, reason)
        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-ex")])


# ═══ 兑换的保护在之后的到期 / 管理员 / 巡逻关闭时结束 ═══════════════════════════════

class RedemptionProtectionEndsTest(_HistoryCase):
    EMAIL = "ex@example.com"
    USER_ID = "u-ex"

    def _close(self, team_id, kick_source, kicked_at, *, source="self_service", email=None):
        self._closed(team_id, email or self.EMAIL, self.USER_ID,
                     kick_source=kick_source, kicked_at=kicked_at, source=source)

    def _cases(self):
        """名字 → (怎么造历史, 是否保护)。兑换都在这个 Team、这个邮箱上。"""
        e = self.EMAIL
        return {
            # 到期踢掉 / 管理员移出 / 巡逻移出发生在兑换之后：服务已经结束，回来就是外部成员。
            "expired_after": (lambda t: (self._redeem(t, e, REDEEMED),
                                         self._close(t, "auto_expire", AFTER)), False),
            "pending_redemption_expired_after": (lambda t: (
                self._redeem(t, e, REDEEMED, result="pending"),
                self._close(t, "auto_expire", AFTER)), False),
            "uncertain_redemption_expired_after": (lambda t: (
                self._redeem(t, e, REDEEMED, result="uncertain"),
                self._close(t, "auto_expire", AFTER)), False),
            "admin_after": (lambda t: (self._redeem(t, e, REDEEMED),
                                       self._close(t, "admin", AFTER)), False),
            "patrol_after": (lambda t: (self._redeem(t, e, REDEEMED),
                                        self._close(t, "patrol", AFTER, source="detected")), False),
            "patrol_premium_after": (lambda t: (
                self._redeem(t, e, REDEEMED),
                self._close(t, "patrol_premium", AFTER, source="detected")), False),
            "expired_after_z_suffix": (lambda t: (self._redeem(t, e, REDEEMED),
                                                  self._close(t, "auto_expire", "2026-08-02T00:00:00Z")),
                                       False),
            # 字符串顺序和时间顺序相反：+08:00 的兑换时间（UTC 00:00）字符串更"大"，关闭其实晚 1 小时。
            "closure_after_in_other_timezone": (lambda t: (
                self._redeem(t, e, "2026-08-02T08:00:00+08:00"),
                self._close(t, "auto_expire", "2026-08-02T01:00:00+00:00")), False),
            # 反过来：关闭时间字符串更"大"，其实比兑换早 30 分钟。
            "closure_before_in_other_timezone": (lambda t: (
                self._redeem(t, e, "2026-08-02T01:00:00+00:00"),
                self._close(t, "auto_expire", "2026-08-02T08:30:00+08:00")), True),
            # 仍然保护的：关闭在兑换之前、同步因缺席关掉、同一时刻、别的 Team / 别的邮箱、时间读不出、
            # 到期之后又兑换了。
            "expired_before": (lambda t: (self._redeem(t, e, REDEEMED),
                                          self._close(t, "auto_expire", BEFORE)), True),
            "sync_absence_after": (lambda t: (
                self._redeem(t, e, REDEEMED),
                self._close(t, "detected", AFTER, source="detected")), True),
            "closed_at_the_same_instant": (lambda t: (self._redeem(t, e, REDEEMED),
                                                      self._close(t, "auto_expire", REDEEMED)), True),
            "closure_on_another_team": (lambda t: (self._redeem(t, e, REDEEMED),
                                                   self._close("team-elsewhere", "auto_expire", AFTER)),
                                        True),
            "closure_for_another_email": (lambda t: (
                self._redeem(t, e, REDEEMED),
                self._close(t, "auto_expire", AFTER, email="someone@example.com")), True),
            "unparsable_closure_time": (lambda t: (self._redeem(t, e, REDEEMED),
                                                   self._close(t, "auto_expire", "not a time")), True),
            "unparsable_redemption_time": (lambda t: (self._redeem(t, e, "not a time"),
                                                      self._close(t, "auto_expire", AFTER)), True),
            "redeemed_again_after_expiry": (lambda t: (
                self._redeem(t, e, REDEEMED),
                self._close(t, "auto_expire", AFTER),
                self._redeem(t, e, "2026-08-10T00:00:00+00:00")), True),
        }

    def _run_cases(self, *, seat_type, seats_entitled, prefix):
        for name, (add_history, protects) in self._cases().items():
            with self.subTest(case=name):
                RecordingClient.calls = []
                team_id = f"team-f6-{prefix}-{name}"
                member = _member(self.EMAIL, self.USER_ID, seat_type=seat_type)
                add_history(team_id)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=seats_entitled)

                self._patrol(dry_run=False, allow=[team_id])

                expected = [] if protects else [("remove_member", self.USER_ID)]
                self.assertEqual(self._calls("remove_member"), expected)

    def test_over_quota_kick(self):
        # 1 个席位：Owner + 这个外部成员，超 1 个。
        self._run_cases(seat_type="default", seats_entitled=1, prefix="oq")

    def test_premium_kick(self):
        self._run_cases(seat_type="prolite", seats_entitled=2, prefix="pr")

    def test_expired_self_service_customer_back_is_kickable_through_the_gate(self):
        for seat_type, seats, rule in (
            ("default", 1, patrol.KICK_RULE_OVER_QUOTA),
            ("prolite", 2, patrol.KICK_RULE_PREMIUM_OUTSIDER),
        ):
            with self.subTest(rule=rule):
                RecordingClient.calls = []
                team_id = f"team-f6-gate-{rule}"
                member = _member(self.EMAIL, self.USER_ID, seat_type=seat_type)
                self._redeem(team_id, self.EMAIL, REDEEMED)
                self._close(team_id, "auto_expire", AFTER)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=seats)

                conn = self._conn()
                try:
                    self.assertFalse(patrol._teamboss_managed_history_sync(
                        conn, team_id, self.EMAIL, self.USER_ID))
                finally:
                    conn.close()
                ok, reason = self._kick(team_id, member, rule=rule)
                self.assertTrue(ok, reason)
                self.assertEqual(self._calls("remove_member"), [("remove_member", self.USER_ID)])

    def test_premium_code_use_ends_with_the_service(self):
        # Premium 兑换码分配出的席位：到期之后从外面回来占 Premium，是普通外部成员。
        team_id = "team-f6-code-expired"
        member = _member(self.EMAIL, self.USER_ID, seat_type="prolite")
        self._redeem(team_id, self.EMAIL, REDEEMED, seat_type="prolite")
        self._close(team_id, "auto_expire", AFTER)
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

        conn = self._conn()
        try:
            self.assertFalse(patrol._teamboss_seat_record_sync(conn, team_id, self.EMAIL, self.USER_ID))
            self.assertIsNone(patrol._premium_kick_veto_sync(conn, team_id, self.EMAIL, self.USER_ID))
        finally:
            conn.close()
        self._patrol(dry_run=False)
        self.assertEqual(self._calls("remove_member"), [("remove_member", self.USER_ID)])

    def test_premium_code_use_still_counts_while_the_service_runs(self):
        team_id = "team-f6-code-live"
        member = _member(self.EMAIL, self.USER_ID, seat_type="prolite")
        self._redeem(team_id, self.EMAIL, REDEEMED, seat_type="prolite")
        self._close(team_id, "detected", AFTER, source="detected")
        self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        ok, reason = self._kick(team_id, member, rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        self.assertFalse(ok)
        self.assertIn("seat or invite record", reason)

    def test_seat_and_invite_records_still_block_premium_after_a_closure(self):
        # 改席位 / 邀请记录永久有效（test_patrol_premium），之后被到期 / 管理员移出也照样挡 Premium 踢人。
        for action, detail in (
            ("change_seat", "user_id=u-ex, seat_type=default, from_seat_type=prolite"),
            ("invite_member", "seat_type=prolite, expires_in=30d"),
            ("invite_gpt_member", "seat_type=default"),
        ):
            with self.subTest(action=action):
                RecordingClient.calls = []
                team_id = f"team-f6-p4-{action}"
                member = _member(self.EMAIL, self.USER_ID, seat_type="prolite")
                self._seat_log(team_id, action, self.EMAIL, detail, "success", REDEEMED)
                self._close(team_id, "admin", AFTER)
                self._armed_team(team_id, [PROD_OWNER, member], seats_entitled=2)

                self._patrol(dry_run=False, allow=[team_id])

                self.assertEqual(self._calls("remove_member"), [])


# ═══ TeamBoss 以前拉过 / 分配过席位的人重新以 Premium 出现：不踢、只提醒 ══════════════════

class ManagedHistoryVetoTest(_HistoryCase):
    def _rejoined_team(self, team_id):
        # 付费成员（self_service，带到期）掉出过一次完整名单，记录被同步关掉；他回来时同步
        # 给他建了一条新的 detected 行，没有到期时间，席位是 Premium。
        member = _member("payer@example.com", "u-pay", seat_type="prolite",
                         first_seen_at="2026-08-02T00:00:00+00:00")
        self._history_row(team_id, "payer@example.com", "u-pay", source="self_service", kicked=1,
                          created_at="2026-07-01T00:00:00+00:00",
                          kicked_at="2026-08-01T00:00:00+00:00",
                          expires_at="2026-12-01T00:00:00+00:00")
        self._armed_team(team_id, [OWNER, KEEPER, member])
        return member

    def test_redetected_paying_member_is_not_kicked_and_alerts(self):
        member = self._rejoined_team("team-k2")

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        # 候选筛选就挡住了：没有走到踢人入口，不推"处理失败"。
        self.assertEqual(self._logs("patrol_kick"), [])
        self.assertFalse(any("处理失败" in t for t in self.notify_calls))
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("TeamBoss 以前拉过他或他兑换过", alerts[0])
        self.assertIn("payer", alerts[0])
        logged = self._logs("patrol_premium_alert")
        self.assertIn("kind=premium_detected_was_managed", logged[0]["detail"])

        conn = self._conn()
        ok, reason = patrol._patrol_kick(conn, RecordingClient(), "team-k2", member,
                                         rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
        conn.close()
        self.assertFalse(ok)
        self.assertIn("placed or assigned a seat", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_every_teamboss_record_shape_blocks_the_kick(self):
        cases = {
            "closed_system_row": lambda t: self._history_row(
                t, "x@example.com", "u-x", source="system", kicked=1,
                created_at="2026-07-01T00:00:00+00:00", kicked_at="2026-08-01T00:00:00+00:00"),
            "closed_row_matched_by_user_id_only": lambda t: self._history_row(
                t, "old-address@example.com", "u-x", source="self_service", kicked=1,
                created_at="2026-07-01T00:00:00+00:00", kicked_at="2026-08-01T00:00:00+00:00"),
            "chatgpt_code_redeemed_here": lambda t: self._redemption(t, "x@example.com"),
            "redemption_still_uncertain": lambda t: self._redemption(
                t, "x@example.com", result="uncertain"),
        }
        for name, add_history in cases.items():
            with self.subTest(case=name):
                RecordingClient.calls = []
                team_id = f"team-k2-{name}"
                member = _member("x@example.com", "u-x", seat_type="prolite")
                add_history(team_id)
                self._armed_team(team_id, [OWNER, KEEPER, member])

                self._patrol(dry_run=False, allow=[team_id])

                self.assertEqual(self._calls("remove_member"), [])

    def test_kick_audit_row_and_refunded_redemption_do_not_block(self):
        # 踢人时没有记录补写的审计行（system、kicked=1、kicked_at = created_at）不是 TeamBoss
        # 拉的人；退回的兑换（failed）没分配出席位。这两样都不挡踢人。
        team_id = "team-k2-none"
        member = _member("x@example.com", "u-x", seat_type="prolite")
        self._history_row(team_id, "x@example.com", "u-x", source="system", kicked=1,
                          created_at="2026-07-01T00:00:00+00:00",
                          kicked_at="2026-07-01T00:00:00+00:00", kick_source="admin")
        self._redemption(team_id, "x@example.com", result="failed")
        self._armed_team(team_id, [OWNER, KEEPER, member])

        self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [("remove_member", "u-x")])

    def test_record_appearing_during_claim_defers_the_kick(self):
        # claim 前查记录时还没有，claim 期间一笔兑换落进了这个 Team：claim 后复查必须拦下。
        team_id = "team-k2-race"
        member = _member("x@example.com", "u-x", seat_type="prolite")
        self._armed_team(team_id, [OWNER, KEEPER, member])
        fixture = self

        @contextlib.contextmanager
        def claim_with_redemption(*args, **kwargs):
            fixture._redemption(team_id, "x@example.com", result="uncertain")
            yield True

        with patch.object(patrol, "member_operation_claim_sync", claim_with_redemption):
            conn = self._conn()
            ok, reason = patrol._patrol_kick(conn, RecordingClient(), team_id, member,
                                             rule=patrol.KICK_RULE_PREMIUM_OUTSIDER)
            conn.close()

        self.assertFalse(ok)
        self.assertEqual(reason, patrol.PREMIUM_KICK_DEFERRED)
        self.assertEqual(self._calls("remove_member"), [])
        deferred = [l for l in self._logs("patrol_kick") if l["result"] == "skipped"]
        self.assertIn("deferred=teamboss_record", deferred[0]["detail"])


# ═══ 超员踢人同样套用记录否决（先取最新 over_by 个再剔除、claim 后复查、限频提醒） ═══════════

class OverQuotaHistoryVetoTest(_HistoryCase):
    def _over_quota_alerts(self):
        return [t for t in self.notify_calls if "超员未移除提醒" in t]

    def _rejoined_over_quota_team(self, team_id):
        # 2 个 ChatGPT 席位、3 个人在用 ChatGPT：超 1 个。最新混进来的 payer 其实是付费成员：
        # 掉出过一次完整名单、记录被同步关掉，回来时成了一条新的 detected 记录（没有到期时间）。
        payer = _member("payer@example.com", "u-pay", first_seen_at="2026-08-02T00:00:00+00:00")
        outsider = _member("outsider@example.com", "u-out", first_seen_at="2026-07-10T00:00:00+00:00")
        self._history_row(team_id, "payer@example.com", "u-pay", source="self_service", kicked=1,
                          created_at="2026-07-01T00:00:00+00:00",
                          kicked_at="2026-08-01T00:00:00+00:00",
                          expires_at="2026-12-01T00:00:00+00:00")
        self._armed_team(team_id, [OWNER, KEEPER, payer, outsider], seats_entitled=2)
        return payer, outsider

    def test_redetected_paying_member_is_not_an_over_quota_candidate_and_alerts(self):
        payer, _outsider = self._rejoined_over_quota_team("team-oq-k2")

        result = self._patrol(dry_run=False)

        # 最新的 1 个是付费成员，受保护不踢；他的名额不往后补给更老的 outsider。
        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        self.assertIsNone(self._kicked("team-oq-k2", "u-pay")[1])
        self.assertIsNone(self._kicked("team-oq-k2", "u-out")[1])
        alerts = self._over_quota_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("payer", alerts[0])
        logged = [l for l in self._logs("patrol_premium_alert") if "u-pay" in (l["detail"] or "")
                  or (l["target_email"] or "") == "payer@example.com"]
        self.assertIn("kind=chatgpt_detected_was_managed", logged[0]["detail"])
        over_quota_card = [t for t in self.notify_calls if "巡逻发现超员" in t]
        self.assertIn("TeamBoss 以前拉过或他兑换过，没有移除", over_quota_card[0])

        # 踢人入口自己也挡：调用方把他传进来也不踢。
        conn = self._conn()
        ok, reason = patrol._patrol_kick(conn, RecordingClient(), "team-oq-k2", payer)
        conn.close()
        self.assertFalse(ok)
        self.assertIn("placed or assigned a seat", reason)
        self.assertEqual(self._calls("remove_member"), [])

    def test_redemption_alone_keeps_a_member_out_of_over_quota_candidates(self):
        team_id = "team-oq-k2-redeem"
        redeemer = _member("redeemer@example.com", "u-buy", first_seen_at="2026-08-02T00:00:00+00:00")
        self._redemption(team_id, "redeemer@example.com", result="pending")
        self._armed_team(team_id, [OWNER, KEEPER, redeemer], seats_entitled=1)

        result = self._patrol(dry_run=True)

        self.assertEqual(result["would_kick"], 0)
        self.assertEqual(self._logs("patrol_would_kick"), [])

    def test_record_appearing_during_claim_defers_the_over_quota_kick(self):
        team_id = "team-oq-k2-race"
        member = _member("x@example.com", "u-x")
        self._armed_team(team_id, [OWNER, KEEPER, member], seats_entitled=1)
        fixture = self

        @contextlib.contextmanager
        def claim_with_redemption(*args, **kwargs):
            fixture._redemption(team_id, "x@example.com", result="uncertain")
            yield True

        with patch.object(patrol, "member_operation_claim_sync", claim_with_redemption):
            conn = self._conn()
            ok, reason = patrol._patrol_kick(conn, RecordingClient(), team_id, member)
            conn.close()

        self.assertFalse(ok)
        self.assertEqual(reason, patrol.KICK_DEFERRED_TEAMBOSS_RECORD)
        self.assertEqual(self._calls("remove_member"), [])
        deferred = [l for l in self._logs("patrol_kick") if l["result"] == "skipped"]
        self.assertIn("reason=over_quota, deferred=teamboss_record", deferred[0]["detail"])


# ═══ Telegram 风险视图的"待处理"和真踢同一份选人 ═══════════════════════════════════

class RiskViewSelectionTest(_HistoryCase):
    def _over_team(self, team_id):
        # 2 个席位：Owner + 老外部成员 + 最新进来的付费用户（兑换还在保护期），超 1 个。
        payer = _member("payer@example.com", "u-pay", first_seen_at="2026-08-02T00:00:00+00:00")
        older = _outsider(1, day=10)
        self._armed_team(team_id, [PROD_OWNER, older, payer], seats_entitled=2)
        return payer

    def _status_for(self, team_id):
        status = asyncio.run(patrol_routes.get_patrol_status(refresh=False))
        return status, {t["team_id"]: t for t in status["teams"]}[team_id]

    @staticmethod
    def _tg_views(status, team_id):
        def fake_get(path, params=None, timeout=None):
            if path == "/api/patrol/status":
                return status
            raise RuntimeError("finance overview is not part of this test")

        with patch.object(tg_bot, "_api_get", side_effect=fake_get):
            return tg_bot.cmd_watch({}, ""), tg_bot.cmd_team({}, team_id)

    def test_protected_member_is_not_listed_as_pending(self):
        team_id = "team-n1-kept"
        self._redeem(team_id, "payer@example.com", REDEEMED)
        self._over_team(team_id)

        status, team = self._status_for(team_id)

        self.assertEqual(team["risk"], "over")
        self.assertEqual(team["over_by"], 1)
        # 最新的那个受保护，名额不往后补：真踢一个都不踢，视图也没有待处理。
        self.assertEqual(team["detected_over"], [])
        self.assertEqual([c["email"] for c in team["detected_over_kept"]], ["payer@example.com"])
        self._patrol(dry_run=True, allow=[team_id])
        self.assertEqual(self._logs("patrol_would_kick"), [])

        for text in self._tg_views(status, team_id):
            self.assertNotIn("待处理", text)
            self.assertIn("🛡️ 不自动移除：payer@example.com", text)

    def test_kickable_member_is_listed_as_pending(self):
        # 同一个人，兑换之后已经到期踢掉过：真踢会踢他，视图把他列为待处理。
        team_id = "team-n1-pending"
        self._redeem(team_id, "payer@example.com", REDEEMED)
        self._closed(team_id, "payer@example.com", "u-pay", kick_source="auto_expire", kicked_at=AFTER)
        self._over_team(team_id)

        status, team = self._status_for(team_id)

        self.assertEqual([c["email"] for c in team["detected_over"]], ["payer@example.com"])
        self.assertEqual(team["detected_over_kept"], [])
        self._patrol(dry_run=True, allow=[team_id])
        self.assertEqual([l["target_email"] for l in self._logs("patrol_would_kick")],
                         ["payer@example.com"])

        watch, card = self._tg_views(status, team_id)
        self.assertIn("👤 待处理：payer@example.com · ChatGPT", watch)
        self.assertIn("👤 待处理：payer@example.com", card)
        self.assertNotIn("不自动移除", watch + card)

    def test_owner_email_is_never_pending(self):
        team_id = "team-n1-owner"
        boss = _member("boss@example.com", "u-boss", first_seen_at="2026-08-02T00:00:00+00:00")
        self._armed_team(team_id, [boss, _outsider(1, day=10)], seats_entitled=1)
        self._owner_email(team_id, "BOSS@example.com")

        _status, team = self._status_for(team_id)

        self.assertEqual([c["email"] for c in team["detected_over"]], ["out1@example.com"])


if __name__ == "__main__":
    unittest.main()
