"""公开自助查询必须告诉成员到期时间为空的真实含义，不能让他们以为是永久。"""

import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import access_tokens

EMAIL = "redeemer@example.com"
TEAM = {"id": "team-a", "name": "Team A", "access_token": "t", "device_id": "d", "proxy_id": None}


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
