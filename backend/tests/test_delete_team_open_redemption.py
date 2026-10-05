"""Deleting a Team is refused while a redemption is still open in it.

delete_team keeps the redemption, its barrier and its email claim. Once the Team
is gone, the reconciler has no credentials to confirm it, and admin invite
guards ignore an uncertain redemption in a deleted Team (otherwise the email
could never be invited anywhere again). So the admin can invite the email into
another Team; re-adding the deleted Team (same id: it is the upstream account
id) lets the reconciler see the person there and confirm the redemption as
well, and one code holds two seats. The redemption has to be settled while the
Team still exists, and the admin's 确认成功 / 确认失败 must still work then.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import logging
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes import access_tokens
from app.routes import teams as teams_routes
from app.utils.durations import expiry_from_duration

EMAIL = "buyer@example.com"
TEAM = "team-t"
OTHER_TEAM = "team-u"
ABSENT = {"members": [], "pending_invites": []}


class _DeleteTeamCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        sessions_patch = patch.object(
            teams_routes, "get_sessions_dir", return_value=self.tmpdir.name
        )
        sessions_patch.start()
        self.addCleanup(sessions_patch.stop)
        telegram_patch = patch.object(teams_routes, "sync_email_chat_commands_sync")
        telegram_patch.start()
        self.addCleanup(telegram_patch.stop)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        conn = self._conn()
        for team_id, name in ((TEAM, "Team T"), (OTHER_TEAM, "Team U")):
            conn.execute(
                """INSERT INTO teams (id, name, status, access_token, device_id,
                                      created_at, updated_at)
                   VALUES (?, ?, 'active', 'access', 'device', '2026-10-01', '2026-10-01')""",
                (team_id, name),
            )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _pending_invite(self, team_id=TEAM):
        """A redemption that has sent its invite on team_id and is still pending."""
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
        token_use_id = asyncio.run(
            access_tokens._reserve_token_use(
                token_id, EMAIL, expiry_from_duration("30d").isoformat()
            )
        )
        asyncio.run(
            access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=team_id)
        )
        return token_use_id

    def _uncertain_invite(self, team_id=TEAM):
        """The invite result on team_id is unknown: uncertain + barrier."""
        token_use_id = self._pending_invite(team_id)
        self.assertTrue(
            asyncio.run(
                access_tokens._lock_uncertain_with_barrier(
                    token_use_id,
                    team_id=team_id,
                    email=EMAIL,
                    error_message="OpenAI invite result is uncertain",
                    reason="OpenAI invite result is uncertain",
                )
            )
        )
        return token_use_id

    def _delete(self, team_id=TEAM):
        return asyncio.run(teams_routes.delete_team(team_id))

    def _delete_refused(self, team_id=TEAM):
        with self.assertRaises(HTTPException) as caught:
            self._delete(team_id)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self._team_exists(team_id), "a refused delete must leave the Team in place")
        return caught.exception.detail

    def _team_exists(self, team_id):
        conn = self._conn()
        row = conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone()
        conn.close()
        return row is not None

    def _admin_confirm(self, token_use_id):
        return asyncio.run(
            access_tokens.resolve_pending_confirmation(
                token_use_id,
                access_tokens.ResolvePendingConfirmationRequest(outcome="success"),
            )
        )

    def _admin_release(self, token_use_id):
        # The Team list and the active check are the real ones; only the
        # upstream member list is faked (the person is not there).
        with (
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


class DeleteTeamRefusedTest(_DeleteTeamCase):
    def test_delete_refused_while_an_uncertain_redemption_points_at_the_team(self):
        token_use_id = self._uncertain_invite()

        detail = self._delete_refused()

        self.assertIn("Team Team T", detail)
        self.assertIn(f"#{token_use_id}", detail)
        self.assertIn("「兑换码 → 待确认的兑换」", detail)
        self.assertIn("确认成功或确认失败", detail)
        self.assertNotIn("{", detail)

    def test_delete_refused_while_a_redemption_is_pending_in_the_team(self):
        token_use_id = self._pending_invite()

        detail = self._delete_refused()

        self.assertIn(f"#{token_use_id}", detail)
        self.assertIn("自动收尾", detail)
        self.assertNotIn("确认成功或确认失败", detail)

    def test_redemption_in_another_team_does_not_block(self):
        self._uncertain_invite(OTHER_TEAM)

        self.assertEqual(self._delete(), {"status": "ok"})
        self.assertFalse(self._team_exists(TEAM))


class SettleThenDeleteTest(_DeleteTeamCase):
    """While the Team is active, the admin can settle the redemption either way
    and then delete the Team."""

    def test_admin_confirmation_then_delete(self):
        token_use_id = self._uncertain_invite()
        self._delete_refused()

        self.assertEqual(self._admin_confirm(token_use_id)["outcome"], "success")

        self.assertEqual(self._delete(), {"status": "ok"})
        self.assertFalse(self._team_exists(TEAM))

    def test_admin_release_then_delete(self):
        token_use_id = self._uncertain_invite()
        self._delete_refused()

        self.assertEqual(self._admin_release(token_use_id)["outcome"], "released")

        self.assertEqual(self._delete(), {"status": "ok"})
        self.assertFalse(self._team_exists(TEAM))


class LoggedOutTeamTest(_DeleteTeamCase):
    """Team T's login is dead (token_expired): 确认失败 cannot verify absence and
    is refused, and the reconciler waits. The refusal says how to get out, and
    确认成功 still works, so the admin is not stuck."""

    def _log_out(self):
        conn = self._conn()
        conn.execute("UPDATE teams SET status = 'token_expired' WHERE id = ?", (TEAM,))
        conn.commit()
        conn.close()

    def test_delete_refusal_on_a_logged_out_team_offers_reimport_or_confirmation(self):
        token_use_id = self._uncertain_invite()
        self._log_out()

        detail = self._delete_refused()

        self.assertIn(f"#{token_use_id}", detail)
        self.assertIn("Team T 登录已失效", detail)
        self.assertIn("重新导入恢复登录", detail)
        self.assertIn("直接确认成功", detail)

        # 确认失败 needs the live list of T and is refused while T is logged out.
        with self.assertRaises(HTTPException) as caught:
            self._admin_release(token_use_id)
        self.assertEqual(caught.exception.status_code, 409)

        self.assertEqual(self._admin_confirm(token_use_id)["outcome"], "success")
        self.assertEqual(self._delete(), {"status": "ok"})

    def test_active_team_refusal_has_no_login_clause(self):
        self._uncertain_invite()

        self.assertNotIn("登录已失效", self._delete_refused())


if __name__ == "__main__":
    unittest.main()
