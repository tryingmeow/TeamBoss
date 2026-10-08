"""Regression tests for three money-path gaps in public self-service redemption.

1. Local evidence of membership in an unavailable Team (token_expired, login
   rejected) must only stop a redemption whose next step would be a new invite.
   A member who picks a live Team they belong to is renewed there; a member
   who picks nothing gets the multi-Team prompt with the dead Team listed as not
   renewable.
2. Turning a pending invite into "uncertain" and arming its patrol barrier is
   one transaction: a use settled in between must never leave an unresolved
   barrier behind (that barrier would exempt the member from patrol forever).
3. The code query does not hand out the upstream user id of a failed attempt.

Every upstream call is replaced; each test runs on its own temporary database
built by the real ``init_database()``.
"""

import _isolation  # noqa: F401  must precede any app import
import json
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app import database as app_database
from app.routes import access_tokens
from app.utils.durations import utc_now

EMAIL = "redeemer@example.com"
CREATED = "2026-09-01T00:00:00+00:00"
FUTURE = "2027-01-01T00:00:00+00:00"
LATER = "2027-02-01T00:00:00+00:00"


class _RedeemFlowTest(unittest.IsolatedAsyncioTestCase):
    """Runs the real redemption flow against fake upstream calls.

    ``self.live[team_id]`` is that Team's live member snapshot, ``self.fetched``
    records which Teams were fetched live, ``self.invites`` which Teams received
    an invite, and ``self.invite_result`` is what every invite call returns.
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()

        self.live: dict[str, dict] = {}
        self.fetched: list[str] = []
        self.invites: list[str] = []
        self.invite_result: dict = {}
        self.budget = access_tokens._RedeemLookupBudget(
            per_code=10, per_code_window=3600, global_limit=60, global_window=600
        )

        async def fake_fetch(team_id, client):
            self.fetched.append(team_id)
            return self.live.get(team_id, {"members": [], "pending_invites": []})

        async def fake_run(func, *args, **kwargs):
            self.invites.append(func.__self__.team_id)
            return dict(self.invite_result)

        for target, value in (
            ("_check_rate_limit", AsyncMock()),
            ("log_operation", AsyncMock()),
            ("notify_member_event", AsyncMock()),
            ("add_member_watch", AsyncMock()),
            ("reserve_default_seat", AsyncMock()),
            ("_get_proxy_url", AsyncMock(return_value=None)),
            ("_chatgpt_available", AsyncMock(return_value=(True, "available=1"))),
            ("fetch_and_cache_members", fake_fetch),
            ("run_chatgpt_call", fake_run),
            ("_redeem_lookup_budget", self.budget),
        ):
            p = patch.object(access_tokens, target, new=value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _exec(self, sql, params=()):
        async with app_database.get_db() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.lastrowid

    async def _rows(self, sql, params=()):
        async with app_database.get_db() as db:
            cursor = await db.execute(sql, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def _team(self, team_id, *, status="active", auth_state=None, seats=5):
        await self._exec(
            """INSERT INTO teams
               (id, name, status, auth_state, access_token, device_id,
                seats_entitled, chatgpt_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (team_id, f"Team {team_id}", status, auth_state, f"tok-{team_id}",
             f"dev-{team_id}", seats, CREATED, CREATED),
        )

    async def _expiry(self, team_id, *, expires_at=FUTURE, source="self_service", user_id="u-1"):
        await self._exec(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, source, created_at)
               VALUES (?, ?, ?, ?, 1, 0, ?, ?)""",
            (team_id, user_id, EMAIL, expires_at, source, CREATED),
        )

    async def _cache(self, team_id, *, members=(), pending=()):
        await self._exec(
            """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            (team_id, json.dumps(list(members)), json.dumps(list(pending)), CREATED),
        )

    def _live_member(self, team_id, *, user_id="u-1", expires_at=FUTURE, is_owner=False):
        self.live[team_id] = {
            "members": [{"email": EMAIL, "id": user_id, "is_owner": is_owner,
                         "expires_at": expires_at, "source": "self_service"}],
            "pending_invites": [],
        }

    async def _token(self, raw):
        return int(await self._exec(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses,
                used_count, disabled, created_at)
               VALUES (?, ?, '30d', 1, 0, 0, ?)""",
            (access_tokens._hash_token(raw), raw[:12], CREATED),
        ))

    async def _redeem(self, raw, team_id=None):
        return await access_tokens.redeem_access_token(
            access_tokens.RedeemAccessTokenRequest(email=EMAIL, token=raw, team_id=team_id),
            Mock(),
        )

    async def _used_count(self, token_id):
        rows = await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,))
        return int(rows[0]["used_count"])

    async def _expiry_of(self, team_id):
        rows = await self._rows(
            "SELECT expires_at FROM member_expiry WHERE team_id = ? AND kicked = 0", (team_id,)
        )
        return [row["expires_at"] for row in rows]


# ── 1. A dead Team only blocks what would become a new invite ────────────────

class DeadTeamOnlyBlocksNewInvitesTest(_RedeemFlowTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The member's old membership sits in a Team whose login expired; the
        # admin has since moved them to the live team-a.
        await self._team("team-a")
        await self._team("team-x", status="token_expired")
        await self._expiry("team-x", expires_at=LATER, user_id="u-old")

    async def test_selected_live_team_is_renewed_despite_a_dead_team_row(self):
        await self._expiry("team-a")
        self._live_member("team-a")
        token_id = await self._token("atm_g1_selected")

        result = await self._redeem("atm_g1_selected", team_id="team-a")

        self.assertEqual(
            (result["status"], result["action"], result["team_id"]),
            ("ok", "renewed_member", "team-a"),
        )
        self.assertEqual(self.invites, [])
        self.assertNotIn("team-x", self.fetched)
        self.assertEqual(await self._used_count(token_id), 1)
        [renewed] = await self._expiry_of("team-a")
        self.assertGreater(renewed, FUTURE)
        # The dead Team's paid time is left exactly as it was.
        self.assertEqual(await self._expiry_of("team-x"), [LATER])

    async def test_selected_live_team_is_renewed_when_the_dead_team_rejected_login(self):
        await self._team("team-r", auth_state="rejected")
        await self._expiry("team-r", user_id="u-r")
        await self._expiry("team-a")
        self._live_member("team-a")
        await self._token("atm_g1_rejected")

        result = await self._redeem("atm_g1_rejected", team_id="team-a")

        self.assertEqual((result["status"], result["team_id"]), ("ok", "team-a"))
        self.assertNotIn("team-r", self.fetched)

    async def test_no_selection_returns_the_prompt_with_the_dead_team_not_renewable(self):
        await self._expiry("team-a")
        self._live_member("team-a")
        token_id = await self._token("atm_g1_prompt")

        result = await self._redeem("atm_g1_prompt")

        self.assertEqual(result["status"], "team_selection_required")
        choices = {choice["team_id"]: choice for choice in result["choices"]}
        self.assertEqual(set(choices), {"team-a", "team-x"})
        self.assertTrue(choices["team-a"]["renewable"])
        dead = choices["team-x"]
        self.assertEqual(
            (dead["renewable"], dead["blocked_reason"], dead["status"],
             dead["expiry_state"], dead["expires_at"], dead["is_owner"]),
            (False, "team_unavailable", "joined", "dated", LATER, False),
        )
        self.assertEqual(dead["team_name"], "Team team-x")
        # Response model accepts every choice as-is.
        access_tokens.RedeemAccessTokenResponse(**result)
        # Nothing renewed, nothing invited, code handed back as a notice.
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._expiry_of("team-a"), [FUTURE])
        self.assertEqual(await self._used_count(token_id), 0)
        uses = await self._rows("SELECT result FROM access_token_uses WHERE token_id = ?", (token_id,))
        self.assertEqual([use["result"] for use in uses], ["notice"])
        self.assertEqual(await self._rows("SELECT * FROM redemption_email_claims"), [])

    async def test_cached_pending_invite_in_the_dead_team_is_listed_as_pending(self):
        await self._exec("DELETE FROM member_expiry WHERE team_id = 'team-x'")
        await self._cache("team-x", pending=[{"email": EMAIL.upper(), "id": "inv-1"}])
        self._live_member("team-a")
        await self._token("atm_g1_pending")

        result = await self._redeem("atm_g1_pending")

        self.assertEqual(result["status"], "team_selection_required")
        dead = {choice["team_id"]: choice for choice in result["choices"]}["team-x"]
        self.assertEqual(
            (dead["status"], dead["renewable"], dead["expiry_state"], dead["expires_at"]),
            ("pending", False, "unmanaged", None),
        )

    async def test_not_live_in_any_available_team_is_still_refused(self):
        # The ce66fe4 case: the next step would be a new invite in team-a.
        token_id = await self._token("atm_g1_refused")

        with self.assertRaises(HTTPException) as cm:
            await self._redeem("atm_g1_refused")

        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.detail, access_tokens._UNAVAILABLE_TEAM_DETAIL)
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._used_count(token_id), 0)
        self.assertEqual(await self._rows("SELECT * FROM redemption_email_claims"), [])
        uses = await self._rows(
            "SELECT result, error_message FROM access_token_uses WHERE token_id = ?", (token_id,)
        )
        self.assertEqual(
            [(u["result"], u["error_message"]) for u in uses],
            [("failed", "unavailable_team_membership")],
        )

    async def test_selected_live_team_without_membership_never_becomes_an_invite(self):
        token_id = await self._token("atm_g1_notfound")

        with self.assertRaises(HTTPException) as cm:
            await self._redeem("atm_g1_notfound", team_id="team-a")

        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._used_count(token_id), 0)

    async def test_selecting_the_dead_team_never_becomes_an_invite(self):
        self._live_member("team-a")
        token_id = await self._token("atm_g1_pickdead")

        with self.assertRaises(HTTPException) as cm:
            await self._redeem("atm_g1_pickdead", team_id="team-x")

        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._used_count(token_id), 0)
        self.assertEqual(await self._expiry_of("team-x"), [LATER])

    async def test_no_available_team_at_all_is_refused_before_any_upstream_call(self):
        await self._exec("UPDATE teams SET status = 'paused' WHERE id = 'team-a'")
        token_id = await self._token("atm_g1_local")

        with self.assertRaises(HTTPException) as cm:
            await self._redeem("atm_g1_local")

        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.detail, access_tokens._UNAVAILABLE_TEAM_DETAIL)
        self.assertEqual(self.fetched, [])
        self.assertEqual(await self._used_count(token_id), 0)
        # No upstream call was made, so neither budget layer keeps the attempt.
        self.assertEqual(self.budget._per_code_hits.get(token_id, []), [])
        self.assertEqual(self.budget._global_hits, [])


# ── 2. "Uncertain" and its patrol barrier land together or not at all ────────

class _CommitHookConnection:
    """Delegates to a real connection and runs ``after_commit`` after each commit."""

    def __init__(self, db, after_commit):
        self._db = db
        self._after_commit = after_commit

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def commit(self):
        await self._db.commit()
        await self._after_commit()


class UncertainLockIsAtomicTest(_RedeemFlowTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._team("team-a")
        self.invite_result = {"error": "read timeout", "_mutation_status": "uncertain"}

    async def _stuck_use(self, *, age_minutes=20):
        token_id = await self._exec(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at, last_used_at)
               VALUES (?, 'atm_stuck', '30d', 1, 1, 0, ?, ?)""",
            (access_tokens._hash_token("atm_g1_stuck"), CREATED, CREATED),
        )
        created = (utc_now() - timedelta(minutes=age_minutes)).isoformat()
        use_id = int(await self._exec(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                error_message, created_at)
               VALUES (?, ?, 'invite_pending', 'team-a', NULL, NULL, 'pending', NULL, ?)""",
            (token_id, EMAIL, created),
        ))
        await self._exec(
            "INSERT INTO redemption_email_claims (email, token_use_id, created_at) VALUES (?, ?, ?)",
            (EMAIL, use_id, created),
        )
        return token_id, use_id

    async def _use(self, use_id):
        return (await self._rows("SELECT * FROM access_token_uses WHERE id = ?", (use_id,)))[0]

    async def _barriers(self, use_id, *, unresolved_only=False):
        sql = "SELECT * FROM pending_invite_reconciliations WHERE token_use_id = ?"
        if unresolved_only:
            sql += " AND resolved = 0"
        return await self._rows(sql, (use_id,))

    async def _fail_barrier_writes(self):
        await self._exec(
            """CREATE TRIGGER g1_fail_barrier BEFORE INSERT ON pending_invite_reconciliations
               BEGIN SELECT RAISE(ABORT, 'barrier write failed'); END"""
        )

    async def _allow_barrier_writes(self):
        await self._exec("DROP TRIGGER g1_fail_barrier")

    def _admin_settles_as_soon_as_uncertain_is_visible(self):
        """Interleave an admin 'confirm success' right after the first commit that
        makes an uncertain use visible to other connections."""
        fired: list[int] = []
        real_get_db = app_database.get_db

        async def after_commit():
            if fired:
                return
            async with real_get_db() as db:
                cursor = await db.execute(
                    "SELECT id FROM access_token_uses WHERE result = 'uncertain'"
                )
                row = await cursor.fetchone()
            if row is None:
                return
            fired.append(int(row["id"]))
            await access_tokens.resolve_pending_confirmation(
                int(row["id"]),
                access_tokens.ResolvePendingConfirmationRequest(outcome="success"),
            )

        @asynccontextmanager
        async def hooked_get_db():
            async with real_get_db() as db:
                yield _CommitHookConnection(db, after_commit)

        p = patch.object(access_tokens, "get_db", hooked_get_db)
        p.start()
        self.addCleanup(p.stop)
        return fired

    async def test_interrupted_invite_failed_barrier_write_leaves_the_use_pending(self):
        token_id, use_id = await self._stuck_use()
        await self._fail_barrier_writes()

        with self.assertLogs(access_tokens.logger, "ERROR"):
            counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual((counts["uncertain"], counts["waiting"]), (0, 1))
        self.assertEqual((await self._use(use_id))["result"], "pending")
        self.assertEqual(await self._barriers(use_id), [])
        self.assertEqual(await self._used_count(token_id), 1)

        # The next pass converts it, with both writes present.
        await self._allow_barrier_writes()
        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual((await self._use(use_id))["result"], "uncertain")
        [barrier] = await self._barriers(use_id)
        self.assertEqual(
            (barrier["kind"], barrier["resolved"], barrier["expires_at"], barrier["team_id"]),
            ("barrier", 0, None, "team-a"),
        )

    async def test_interrupted_invite_settled_in_between_leaves_no_open_barrier(self):
        _, use_id = await self._stuck_use()
        fired = self._admin_settles_as_soon_as_uncertain_is_visible()

        await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(fired, [use_id])
        self.assertEqual((await self._use(use_id))["result"], "success")
        self.assertEqual(await self._barriers(use_id, unresolved_only=True), [])

    async def test_live_uncertain_invite_failed_barrier_write_leaves_the_use_pending(self):
        token_id = await self._token("atm_g1_live_fail")
        await self._fail_barrier_writes()

        with self.assertLogs(access_tokens.logger, "ERROR"):
            result = await self._redeem("atm_g1_live_fail")

        self.assertEqual(result["status"], "pending_confirmation")
        [use] = await self._rows("SELECT * FROM access_token_uses WHERE token_id = ?", (token_id,))
        self.assertEqual((use["action"], use["result"]), ("invite_pending", "pending"))
        self.assertEqual(await self._barriers(use["id"]), [])
        # Still consumed and locked to its Team: never released or refunded.
        self.assertEqual(await self._used_count(token_id), 1)
        claims = await self._rows("SELECT token_use_id FROM redemption_email_claims")
        self.assertEqual([c["token_use_id"] for c in claims], [use["id"]])

        # Once it counts as interrupted, the reconciler converts it with its barrier.
        await self._allow_barrier_writes()
        await self._exec(
            "UPDATE access_token_uses SET created_at = ? WHERE id = ?",
            ((utc_now() - timedelta(minutes=20)).isoformat(), use["id"]),
        )
        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual((await self._use(use["id"]))["result"], "uncertain")
        self.assertEqual(len(await self._barriers(use["id"], unresolved_only=True)), 1)

    async def test_live_uncertain_invite_settled_in_between_leaves_no_open_barrier(self):
        token_id = await self._token("atm_g1_live_gap")
        fired = self._admin_settles_as_soon_as_uncertain_is_visible()

        await self._redeem("atm_g1_live_gap")

        [use] = await self._rows("SELECT * FROM access_token_uses WHERE token_id = ?", (token_id,))
        self.assertEqual(fired, [use["id"]])
        self.assertEqual(use["result"], "success")
        self.assertEqual(await self._barriers(use["id"], unresolved_only=True), [])


# ── 3. The code query hides the upstream user id of a failed attempt ─────────

class CodeQueryUserIdTest(_RedeemFlowTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._team("team-a")

    async def _query(self, raw):
        return await access_tokens.query_self_service(
            access_tokens.QuerySelfServiceRequest(query=raw), Mock()
        )

    async def test_failed_attempt_does_not_return_the_user_id(self):
        # A rejected renewal stores the live member id on its (failed) use row.
        self._live_member("team-a", user_id="u-owner", expires_at=None, is_owner=True)
        await self._token("atm_g1_failed")
        with self.assertRaises(HTTPException) as cm:
            await self._redeem("atm_g1_failed")
        self.assertEqual(cm.exception.status_code, 409)
        stored = await self._rows("SELECT result, user_id FROM access_token_uses")
        self.assertEqual([(u["result"], u["user_id"]) for u in stored], [("failed", "u-owner")])

        result = await self._query("atm_g1_failed")

        self.assertEqual(result["usage"]["result"], "failed")
        self.assertIn("user_id", result["usage"])
        self.assertIsNone(result["usage"]["user_id"])

    async def test_successful_attempt_still_returns_the_user_id(self):
        await self._expiry("team-a")
        self._live_member("team-a", user_id="u-1")
        await self._token("atm_g1_success")
        result = await self._redeem("atm_g1_success")
        self.assertEqual(result["status"], "ok")

        query = await self._query("atm_g1_success")

        self.assertEqual(query["usage"]["result"], "success")
        self.assertEqual(query["usage"]["user_id"], "u-1")


if __name__ == "__main__":
    unittest.main()
