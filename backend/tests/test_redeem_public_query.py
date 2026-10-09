"""What the public status and code queries reveal.

A redemption history is shown only to the holder of that code, a failed attempt
never hands out the upstream user id, and an empty expiry is labelled with what
it really means (permanent, external, unrecorded), never silently as permanent.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from _redemption_fixtures import CREATED, EMAIL, RedeemFlowCase, RedemptionLedgerCase

from app.routes import access_tokens

TEAM = {"id": "team-a", "name": "Team A", "access_token": "t", "device_id": "d", "proxy_id": None}


# ── 匿名查询的兑换历史需要出示这个邮箱自己的兑换码 ──────────────────────────

class RedemptionHistoryNeedsProofTest(RedemptionLedgerCase):
    def setUp(self):
        super().setUp()
        self.raw_token = "atm_secret_code"
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'atm_sec', '30d', 1, 1, 0, '2026-09-11')""",
            (access_tokens._hash_token(self.raw_token),),
        )
        token_id = cur.lastrowid
        conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result, created_at)
               VALUES (?, 'owner@example.com', 'invited', 'team-1', 'u1',
                       '2026-10-11T00:00:00+00:00', 'success', '2026-09-11')""",
            (token_id,),
        )
        conn.commit()
        conn.close()

    def _status(self, email, token):
        with patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[])
        ):
            return asyncio.run(access_tokens._query_membership_status(email, token))

    def test_bare_email_gets_membership_but_no_history(self):
        result = self._status("owner@example.com", None)
        self.assertEqual(result["status"], "absent")
        self.assertEqual(result["redemption_history"], [])

    def test_own_code_unlocks_the_history(self):
        result = self._status("owner@example.com", self.raw_token)
        self.assertEqual(len(result["redemption_history"]), 1)

    def test_someone_elses_email_with_that_code_stays_locked(self):
        result = self._status("victim@example.com", self.raw_token)
        self.assertEqual(result["redemption_history"], [])

    def test_unknown_code_stays_locked(self):
        result = self._status("owner@example.com", "atm_not_a_real_code")
        self.assertEqual(result["redemption_history"], [])


# ── 手里有一张没用过的码，不能读任意邮箱的兑换历史 ─────────────────────────

VICTIM = "victim@example.com"


class HistoryIsScopedToThePresentedCodeTest(RedeemFlowCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._team("team-a")
        # 受害者自己用过一张码，后来到期被移出。
        self.victim_token_id = await self._token("atm_victim_own")
        await self._exec(
            "UPDATE access_tokens SET used_count = 1 WHERE id = ?", (self.victim_token_id,)
        )
        await self._exec(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result, created_at)
               VALUES (?, ?, 'invited', 'team-a', 'u-v', '2026-09-01T00:00:00+00:00',
                       'success', '2026-08-01T00:00:00+00:00')""",
            (self.victim_token_id, VICTIM),
        )
        await self._exec(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at,
                source, created_at)
               VALUES ('team-a', 'u-v', ?, '2026-09-01T00:00:00+00:00', 1, 1,
                       '2026-09-02T00:00:00+00:00', 'self_service', ?)""",
            (VICTIM, CREATED),
        )
        # 攻击者：一张没用过的码，对受害者邮箱发起一次必然失败的兑换。
        # 选一个不存在的 Team：在任何上游请求之前就 409 退码，但留下一条使用记录。
        self.attacker_token_id = await self._token("atm_attacker")
        with self.assertRaises(HTTPException) as cm:
            await self._redeem("atm_attacker", email=VICTIM, team_id="no-such-team")
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._used_count(self.attacker_token_id), 0)

    def _prefixes(self, history):
        return {item["token_prefix"] for item in history}

    async def test_status_with_an_unrelated_code_shows_only_that_codes_attempts(self):
        result = await access_tokens.query_membership_status(
            access_tokens.QueryMembershipRequest(email=VICTIM, token="atm_attacker"), Mock()
        )
        history = result["redemption_history"]
        self.assertEqual(self._prefixes(history), {"atm_attacker"[:12]})
        self.assertEqual([item["result"] for item in history], ["failed"])

    async def test_query_with_an_unrelated_code_shows_only_that_codes_attempts(self):
        result = await access_tokens.query_self_service(
            access_tokens.QuerySelfServiceRequest(query=VICTIM, token="atm_attacker"), Mock()
        )
        history = result["membership"]["redemption_history"]
        self.assertEqual(self._prefixes(history), {"atm_attacker"[:12]})

    async def test_code_query_does_not_reveal_the_emails_removal_record(self):
        result = await access_tokens.query_self_service(
            access_tokens.QuerySelfServiceRequest(query="atm_attacker"), Mock()
        )
        self.assertEqual(result["token"]["token_status"], "unused")
        usage = result["usage"]
        # 这次尝试本身照旧可见（持码人自己发起的），但不附带该邮箱的移出记录。
        self.assertEqual(usage["email"], VICTIM)
        self.assertEqual(usage["email_status"], "unknown")
        self.assertIsNone(usage["kicked_at"])

    async def test_the_owner_of_a_code_still_sees_that_codes_history(self):
        result = await access_tokens.query_membership_status(
            access_tokens.QueryMembershipRequest(email=VICTIM, token="atm_victim_own"), Mock()
        )
        history = result["redemption_history"]
        self.assertEqual(
            [(item["action"], item["result"], item["token_prefix"]) for item in history],
            [("invited", "success", "atm_victim_own"[:12])],
        )
        # 响应结构不变：前端 JoinPage 读的字段都在。
        self.assertEqual(
            set(history[0]),
            {"action", "result", "team_id", "team_name", "token_prefix",
             "grant_expires_in", "expires_at", "error_message", "created_at"},
        )

    async def test_used_code_still_reports_the_status_of_the_email_it_was_used_for(self):
        result = await access_tokens.query_self_service(
            access_tokens.QuerySelfServiceRequest(query="atm_victim_own"), Mock()
        )
        self.assertEqual(result["usage"]["email_status"], "expired_removed")
        self.assertEqual(result["usage"]["kicked_at"], "2026-09-02T00:00:00+00:00")


# ── The code query hides the upstream user id of a failed attempt ─────────

class CodeQueryUserIdTest(RedeemFlowCase):
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


# ── 到期时间为空的真实含义，不能让成员以为是永久 ─────────────────────────────

class PublicExpiryStateTest(unittest.IsolatedAsyncioTestCase):
    async def _query(self, member):
        async def cached(team_id):
            return {"members": [member], "pending_invites": [], "updated_at": None}

        with patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM])
        ), patch.object(access_tokens, "get_cached_members", new=cached), patch.object(
            access_tokens, "_history_proof_accepted", new=AsyncMock(return_value=False)
        ):
            return await access_tokens._query_membership_status(EMAIL)

    async def _state(self, **extra):
        member = {"email": EMAIL, "id": "u1", "is_owner": False, **extra}
        result = await self._query(member)
        return result["memberships"][0]

    async def test_dated(self):
        entry = await self._state(expires_at="2026-12-01T00:00:00+00:00", source="system")
        self.assertEqual(entry["expiry_state"], "dated")

    async def test_permanent(self):
        entry = await self._state(expires_at=None, source="system")
        self.assertEqual(entry["expiry_state"], "permanent")

    async def test_external(self):
        entry = await self._state(expires_at=None, source="detected")
        self.assertEqual(entry["expiry_state"], "external")

    async def test_unrecorded(self):
        entry = await self._state(expires_at=None)
        self.assertEqual(entry["expiry_state"], "unrecorded")

    async def test_public_entry_exposes_no_source(self):
        entry = await self._state(expires_at=None, source="detected")
        self.assertNotIn("source", entry)
        self.assertFalse(entry["is_owner"])
        self.assertEqual(
            access_tokens.MembershipTeamEntry(**entry).expiry_state, "external"
        )


if __name__ == "__main__":
    unittest.main()
