import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import access_tokens
from app.database import get_db, init_database
from app.services.member_expiry import extend_member_expiry


class AccessTokenQueryLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_public_email_query_consumes_one_rate_limit_check(self):
        membership = {
            "status": "absent",
            "email": "user@example.com",
            "message": "未找到记录",
            "redemption_history": [],
            "cache_updated_at": None,
        }
        request = object()

        with (
            patch.object(
                access_tokens, "_check_rate_limit", new=AsyncMock()
            ) as check_rate_limit,
            patch.object(
                access_tokens, "_get_token_by_raw", new=AsyncMock(return_value=None)
            ),
            patch.object(
                access_tokens,
                "_query_membership_status",
                new=AsyncMock(return_value=membership),
            ) as query_membership,
        ):
            result = await access_tokens.query_self_service(
                access_tokens.QuerySelfServiceRequest(query=" User@Example.com "),
                request,
            )

            self.assertEqual(result["membership"], membership)
            check_rate_limit.assert_awaited_once_with(
                request, access_tokens._limiter_query
            )
            query_membership.assert_awaited_once_with("user@example.com")

            check_rate_limit.reset_mock()
            query_membership.reset_mock()
            status_result = await access_tokens.query_membership_status(
                access_tokens.QueryMembershipRequest(email=" User@Example.com "),
                request,
            )

            self.assertEqual(status_result, membership)
            check_rate_limit.assert_awaited_once_with(
                request, access_tokens._limiter_query
            )
            query_membership.assert_awaited_once_with("user@example.com")


class AccessTokenRedemptionSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {"AUTO_TEAM_DATA_DIR": self._tmp.name},
            clear=False,
        )
        self._env.start()
        await init_database()

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _insert_token(self, suffix: str) -> int:
        async with get_db() as db:
            cursor = await db.execute(
                """INSERT INTO access_tokens
                   (token_hash, token_prefix, grant_expires_in, max_uses,
                    used_count, disabled, created_at)
                   VALUES (?, ?, '30d', 1, 0, 0, ?)""",
                (f"hash-{suffix}", f"atm_{suffix}", "2026-07-31T00:00:00+00:00"),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def test_same_email_claim_blocks_second_token_without_consuming_it(self):
        first_token = await self._insert_token("first")
        second_token = await self._insert_token("second")
        use_id = await access_tokens._reserve_token_use(
            first_token,
            "user@example.com",
            "2026-08-30T00:00:00+00:00",
        )
        self.assertGreater(use_id, 0)

        with self.assertRaises(HTTPException) as raised:
            await access_tokens._reserve_token_use(
                second_token,
                "user@example.com",
                "2026-08-30T00:00:00+00:00",
            )
        self.assertEqual(raised.exception.status_code, 409)

        async with get_db() as db:
            row = await (
                await db.execute(
                    "SELECT used_count FROM access_tokens WHERE id = ?",
                    (second_token,),
                )
            ).fetchone()
        self.assertEqual(row["used_count"], 0)

    async def test_renewal_and_receipt_commit_atomically(self):
        token_id = await self._insert_token("renew")
        use_id = await access_tokens._reserve_token_use(
            token_id,
            "user@example.com",
            "2026-08-30T00:00:00+00:00",
        )
        await access_tokens._set_token_use_phase(
            use_id,
            "renew_pending",
            team_id="team-1",
            user_id="user-1",
        )

        expires_at = await extend_member_expiry(
            "team-1",
            "user-1",
            "user@example.com",
            "30d",
            source="self_service",
            token_use_id=use_id,
            token_action="renewed_member",
        )

        async with get_db() as db:
            usage = await (
                await db.execute(
                    "SELECT action, result, expires_at FROM access_token_uses WHERE id = ?",
                    (use_id,),
                )
            ).fetchone()
            claim = await (
                await db.execute(
                    "SELECT 1 FROM redemption_email_claims WHERE token_use_id = ?",
                    (use_id,),
                )
            ).fetchone()
        self.assertEqual(usage["result"], "success")
        self.assertEqual(usage["action"], "renewed_member")
        self.assertEqual(usage["expires_at"], expires_at)
        self.assertIsNone(claim)

    async def test_uncertain_invite_keeps_token_and_email_locked(self):
        token_id = await self._insert_token("uncertain")
        use_id = await access_tokens._reserve_token_use(
            token_id,
            "user@example.com",
            "2026-08-30T00:00:00+00:00",
        )
        retained = Mock()
        team = {
            "id": "team-1",
            "name": "Team 1",
            "access_token": "access",
            "device_id": "device",
            "proxy_id": None,
        }

        with (
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(
                access_tokens,
                "_chatgpt_available",
                new=AsyncMock(return_value=(True, "available=1")),
            ),
            patch.object(
                access_tokens,
                "run_chatgpt_call",
                new=AsyncMock(
                    return_value={
                        "error": "timeout",
                        "_mutation_status": "uncertain",
                    }
                ),
            ),
            patch.object(
                access_tokens,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [], "pending_invites": []}),
            ),
            patch.object(access_tokens, "log_operation", new=AsyncMock()),
        ):
            result = await access_tokens._invite_to_available_team(
                "user@example.com",
                "30d",
                [team],
                token_use_id=use_id,
                on_invite_confirmed=retained,
            )

        self.assertEqual(result["status"], "pending_confirmation")
        retained.assert_called_once_with()
        async with get_db() as db:
            usage = await (
                await db.execute(
                    "SELECT action, result FROM access_token_uses WHERE id = ?",
                    (use_id,),
                )
            ).fetchone()
            claim = await (
                await db.execute(
                    "SELECT email FROM redemption_email_claims WHERE token_use_id = ?",
                    (use_id,),
                )
            ).fetchone()
        self.assertEqual(dict(usage), {"action": "invite_pending", "result": "uncertain"})
        self.assertEqual(claim["email"], "user@example.com")


if __name__ == "__main__":
    unittest.main()
