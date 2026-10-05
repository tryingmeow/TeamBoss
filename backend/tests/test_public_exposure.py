"""匿名可访问的接口不应泄露精确版本号或 Owner 身份。"""

import _isolation  # noqa: F401  must precede any app import
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import main
from app.routes import access_tokens

OWNER = "owner@example.com"
MEMBER = "member@example.com"
TEAM_A = {"id": "team-a", "name": "Team A", "access_token": "tok-a", "device_id": "dev-a", "proxy_id": None}
TEAM_B = {"id": "team-b", "name": "Team B", "access_token": "tok-b", "device_id": "dev-b", "proxy_id": None}


class HealthVersionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _body(self, is_admin: bool) -> dict:
        response = await main.health_check(is_admin=is_admin)
        return json.loads(response.body)

    async def test_anonymous_health_has_status_but_no_version(self):
        body = await self._body(is_admin=False)
        self.assertIn("status", body)
        self.assertIn("timestamp", body)
        self.assertNotIn("version", body)
        self.assertNotIn("components", body)

    async def test_admin_health_reports_version(self):
        body = await self._body(is_admin=True)
        self.assertEqual(body["version"], main.APP_VERSION)
        self.assertIn("components", body)


class PublicLookupOwnerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()

        snapshots = {
            "team-a": {
                "members": [
                    {"email": OWNER, "id": "u-owner", "is_owner": True, "expires_at": None},
                    {"email": MEMBER, "id": "u-member-a", "is_owner": False, "expires_at": "2026-12-01T00:00:00+00:00"},
                ],
                "pending_invites": [],
                "updated_at": "2026-10-01T00:00:00+00:00",
            },
            "team-b": {
                "members": [
                    {"email": MEMBER, "id": "u-member-b", "is_owner": False, "expires_at": "2026-11-01T00:00:00+00:00"},
                ],
                "pending_invites": [],
                "updated_at": "2026-10-01T00:00:00+00:00",
            },
        }

        async def cached(team_id):
            return snapshots.get(team_id)

        for target, value in (
            ("_check_rate_limit", AsyncMock()),
            ("load_active_teams", AsyncMock(return_value=[TEAM_A, TEAM_B])),
            ("get_cached_members", cached),
        ):
            p = patch.object(access_tokens, target, new=value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _query(self, email: str) -> dict:
        result = await access_tokens.query_self_service(
            access_tokens.QuerySelfServiceRequest(query=email), Mock()
        )
        self.assertEqual(result["query_type"], "email")
        return result["membership"]

    async def test_owner_email_looks_exactly_like_an_unknown_email(self):
        owner = await self._query(OWNER)
        unknown = await self._query("nobody@example.com")
        self.assertEqual(owner["status"], "absent")
        self.assertEqual(owner["memberships"], [])
        self.assertEqual({k: v for k, v in owner.items() if k != "email"},
                         {k: v for k, v in unknown.items() if k != "email"})

    async def test_member_lookup_keeps_its_shape_and_never_flags_owner(self):
        member = await self._query(MEMBER)
        self.assertEqual(member["status"], "joined")
        self.assertFalse(member["is_owner"])
        self.assertEqual([m["team_id"] for m in member["memberships"]], ["team-a", "team-b"])
        for entry in member["memberships"]:
            self.assertFalse(entry["is_owner"])
            self.assertEqual(
                set(entry), {"status", "team_id", "team_name", "expires_at", "is_owner", "cache_updated_at"}
            )

    async def test_status_endpoint_hides_the_owner_too(self):
        result = await access_tokens.query_membership_status(
            access_tokens.QueryMembershipRequest(email=OWNER), Mock()
        )
        self.assertEqual(result["status"], "absent")


if __name__ == "__main__":
    unittest.main()
