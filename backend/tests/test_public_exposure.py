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

    async def test_owner_email_looks_like_a_member_without_an_expiry(self):
        owner = await self._query(OWNER)
        self.assertEqual(owner["status"], "joined")
        self.assertFalse(owner["is_owner"])
        self.assertIsNone(owner["expires_at"])
        self.assertEqual(len(owner["memberships"]), 1)
        entry = owner["memberships"][0]
        self.assertEqual(entry["expiry_state"], "permanent")
        self.assertFalse(entry["is_owner"])
        self.assertIsNone(entry["expires_at"])
        self.assertNotIn("owner", json.dumps(owner).lower().replace("is_owner", "").replace(OWNER, ""))

    async def test_member_lookup_keeps_its_shape_and_never_flags_owner(self):
        member = await self._query(MEMBER)
        self.assertEqual(member["status"], "joined")
        self.assertFalse(member["is_owner"])
        self.assertEqual([m["team_id"] for m in member["memberships"]], ["team-a", "team-b"])
        for entry in member["memberships"]:
            self.assertFalse(entry["is_owner"])
            self.assertEqual(
                set(entry), {"status", "team_id", "team_name", "expires_at", "is_owner", "expiry_state", "cache_updated_at"}
            )

    async def test_status_endpoint_answers_for_the_owner_like_query(self):
        result = await access_tokens.query_membership_status(
            access_tokens.QueryMembershipRequest(email=OWNER), Mock()
        )
        self.assertEqual(result["status"], "joined")
        self.assertEqual(result["memberships"][0]["expiry_state"], "permanent")
        self.assertFalse(result["is_owner"])


class PublicRedeemOwnerTest(unittest.IsolatedAsyncioTestCase):
    """持码人用 /redeem 试一个 Owner 邮箱，得到的回答必须和「没有到期时间、不能续」的
    成员一模一样；Owner 照旧被拒、码照旧不消耗，内部审计照旧记成 owner。"""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()
        for target, value in (
            ("_check_rate_limit", AsyncMock()),
            ("load_active_teams", AsyncMock(return_value=[TEAM_A, TEAM_B])),
            ("log_operation", AsyncMock()),
            (
                "_redeem_lookup_budget",
                access_tokens._RedeemLookupBudget(per_code=100, per_code_window=3600, global_limit=100, global_window=600),
            ),
        ):
            p = patch.object(access_tokens, target, new=value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _make_token(self, raw_token: str) -> int:
        async with app_database.get_db() as db:
            cursor = await db.execute(
                """INSERT INTO access_tokens
                   (token_hash, token_prefix, grant_expires_in, max_uses,
                    used_count, disabled, created_at)
                   VALUES (?, 'atm_own', '30d', 1, 0, 0, '2026-10-01T00:00:00+00:00')""",
                (access_tokens._hash_token(raw_token),),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def _used_count(self, token_id: int) -> int:
        async with app_database.get_db() as db:
            row = await (await db.execute("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,))).fetchone()
        return int(row["used_count"])

    @staticmethod
    def _hit(team, *, is_owner, user_id="u-1", expires_at=None):
        return {"kind": "member", "team": team, "user_id": user_id, "is_owner": is_owner,
                "expires_at": expires_at, "cache_updated_at": None}

    async def _refusal(self, raw, email, **patches):
        with patch.multiple(access_tokens, **patches):
            with self.assertRaises(access_tokens.HTTPException) as cm:
                await access_tokens.redeem_access_token(
                    access_tokens.RedeemAccessTokenRequest(email=email, token=raw), Mock()
                )
        return cm.exception

    async def test_owner_refusal_reads_like_the_no_expiry_refusal(self):
        owner_token = await self._make_token("atm_owner_probe")
        member_token = await self._make_token("atm_noexpiry_probe")
        owner = await self._refusal(
            "atm_owner_probe", OWNER,
            _find_all_memberships=AsyncMock(return_value=[self._hit(TEAM_A, is_owner=True)]),
        )
        member = await self._refusal(
            "atm_noexpiry_probe", MEMBER,
            _find_all_memberships=AsyncMock(return_value=[self._hit(TEAM_A, is_owner=False)]),
            _renew_existing_membership=AsyncMock(
                side_effect=access_tokens.PermanentMembershipError("no expiry")
            ),
        )
        self.assertEqual((owner.status_code, owner.detail), (member.status_code, member.detail))
        self.assertEqual(owner.status_code, 409)
        self.assertNotIn("Owner", owner.detail)
        self.assertIn("兑换码未使用", owner.detail)
        self.assertEqual(await self._used_count(owner_token), 0)
        self.assertEqual(await self._used_count(member_token), 0)

        # Internal audit still says it was an Owner ...
        async with app_database.get_db() as db:
            row = await (await db.execute(
                "SELECT action, error_message FROM access_token_uses WHERE token_id = ?", (owner_token,)
            )).fetchone()
        self.assertEqual((row["action"], row["error_message"]), ("renew_owner_rejected", "owner_email"))

        # ... but the code holder's public views of that attempt do not.
        by_code = await access_tokens.query_self_service(
            access_tokens.QuerySelfServiceRequest(query="atm_owner_probe"), Mock()
        )
        self.assertEqual(by_code["usage"]["action"], "renew_permanent_rejected")
        self.assertEqual(by_code["usage"]["error_message"], "permanent_membership")
        with patch.object(access_tokens, "get_cached_members", new=AsyncMock(return_value=None)):
            by_email = await access_tokens.query_membership_status(
                access_tokens.QueryMembershipRequest(email=OWNER, token="atm_owner_probe"), Mock()
            )
        self.assertEqual(
            [(h["action"], h["error_message"]) for h in by_email["redemption_history"]],
            [("renew_permanent_rejected", "permanent_membership")],
        )
        self.assertNotIn("owner", json.dumps(by_code, ensure_ascii=False).lower().replace(OWNER, ""))

    async def test_owner_found_on_the_recheck_gets_the_same_refusal(self):
        token_id = await self._make_token("atm_owner_recheck")
        lookup = AsyncMock(side_effect=[
            [self._hit(TEAM_A, is_owner=False)],
            [self._hit(TEAM_A, is_owner=True)],
        ])
        exc = await self._refusal("atm_owner_recheck", OWNER, _find_all_memberships=lookup)
        self.assertEqual((exc.status_code, exc.detail), (409, access_tokens._NOT_RENEWABLE_DETAIL))
        self.assertEqual(lookup.await_count, 2)
        self.assertEqual(await self._used_count(token_id), 0)

    async def test_owner_team_choice_looks_like_a_no_expiry_member(self):
        await self._make_token("atm_owner_choices")
        async with app_database.get_db() as db:
            await db.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked, source, created_at)
                   VALUES ('team-b', 'u-1', ?, NULL, 0, 0, 'manual', '2026-08-17T00:00:00+00:00')""",
                (OWNER,),
            )
            await db.commit()
        hits = [
            self._hit(TEAM_A, is_owner=True, expires_at="2027-01-01T00:00:00+00:00"),
            self._hit(TEAM_B, is_owner=False),
        ]
        with patch.object(access_tokens, "_find_all_memberships", new=AsyncMock(return_value=hits)):
            result = await access_tokens.redeem_access_token(
                access_tokens.RedeemAccessTokenRequest(email=OWNER, token="atm_owner_choices"), Mock()
            )
        self.assertEqual(result["status"], "team_selection_required")
        choices = {c["team_id"]: c for c in result["choices"]}
        strip = lambda c: {k: v for k, v in c.items() if k not in ("team_id", "team_name")}
        self.assertEqual(strip(choices["team-a"]), strip(choices["team-b"]))
        self.assertFalse(choices["team-a"]["is_owner"])
        self.assertFalse(choices["team-a"]["renewable"])
        self.assertIsNone(choices["team-a"]["expires_at"])
        # The response model keeps the shape the frontend reads.
        access_tokens.RedeemAccessTokenResponse.model_validate(result)


if __name__ == "__main__":
    unittest.main()
