"""公开兑换在终审中发现的资金路径缺口的回归测试。

每个类对应一个缺口，断言的是修好之后的行为。所有上游调用都被替换掉，不会发出
任何网络请求；数据库是每个测试一份的临时库（真实 init_database() 建表）。
"""

import _isolation  # noqa: F401  must precede any app import
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app import database as app_database
from app.routes import access_tokens

EMAIL = "buyer@example.com"
CREATED = "2026-09-01T00:00:00+00:00"
FUTURE = "2027-01-01T00:00:00+00:00"


class _RedeemFlowTest(unittest.IsolatedAsyncioTestCase):
    """跑真实的 ``redeem_access_token`` 流程，只把上游换成可控的假实现。

    ``self.live[team_id]`` 是该 Team 的实时成员快照；``self.broken`` 里的 Team 实时拉取
    会失败（模拟上游 401/断网）；``self.invites`` 记录真正发出的邀请落在哪个 Team。
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()

        self.live: dict[str, dict] = {}
        self.broken: set[str] = set()
        self.invites: list[str] = []
        self.budget = access_tokens._RedeemLookupBudget(
            per_code=10, per_code_window=3600, global_limit=60, global_window=600
        )

        async def fake_fetch(team_id, client):
            if team_id in self.broken:
                raise HTTPException(status_code=502, detail="Failed to fetch members: 401")
            return self.live.get(team_id, {"members": [], "pending_invites": []})

        async def fake_run(func, *args, **kwargs):
            self.invites.append(func.__self__.team_id)
            return {}

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

    async def _team(self, team_id, *, status="active", auth_state=None,
                    active_until=None, will_renew=1, seats=5):
        # load_active_teams 按空位多少排序：seats 越大越先被选去发邀请。
        await self._exec(
            """INSERT INTO teams
               (id, name, status, auth_state, access_token, device_id,
                seats_entitled, chatgpt_count, active_until, will_renew,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (team_id, f"Team {team_id}", status, auth_state, f"tok-{team_id}",
             f"dev-{team_id}", seats, active_until, will_renew, CREATED, CREATED),
        )

    async def _expiry(self, team_id, *, source="self_service", expires_at=FUTURE,
                      kicked=0, user_id="u-1"):
        await self._exec(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, source, created_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
            (team_id, user_id, EMAIL, expires_at, kicked, source, CREATED),
        )

    async def _cache(self, team_id, *, members=(), pending=()):
        import json

        await self._exec(
            """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            (team_id, json.dumps(list(members)), json.dumps(list(pending)), CREATED),
        )

    async def _token(self, raw):
        return int(await self._exec(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses,
                used_count, disabled, created_at)
               VALUES (?, ?, '30d', 1, 0, 0, ?)""",
            (access_tokens._hash_token(raw), raw[:12], CREATED),
        ))

    async def _redeem(self, raw, email=EMAIL, team_id=None):
        return await access_tokens.redeem_access_token(
            access_tokens.RedeemAccessTokenRequest(email=email, token=raw, team_id=team_id),
            Mock(),
        )

    async def _used_count(self, token_id):
        rows = await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,))
        return int(rows[0]["used_count"])


# ── 缺口 3：非 active Team 里的成员不能被当成"不在任何 Team" ────────────────

class UnavailableTeamMembershipBlocksRedemptionTest(_RedeemFlowTest):
    async def _assert_blocked_without_consuming(self, raw, token_id):
        with self.assertRaises(HTTPException) as cm:
            await self._redeem(raw)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("联系管理员", cm.exception.detail)
        self.assertIn("兑换码未使用", cm.exception.detail)
        # 没发邀请、码没消耗、邮箱占用已释放。
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._used_count(token_id), 0)
        self.assertEqual(await self._rows("SELECT * FROM redemption_email_claims"), [])
        uses = await self._rows("SELECT result FROM access_token_uses WHERE token_id = ?", (token_id,))
        self.assertEqual([u["result"] for u in uses], ["failed"])
        # 整个过程没碰上游，单码和全站的尝试次数都不扣。
        self.assertEqual(self.budget._per_code_hits.get(token_id, []), [])
        self.assertEqual(self.budget._global_hits, [])

    async def test_paid_member_of_a_token_expired_team_is_not_invited_elsewhere(self):
        await self._team("team-a")
        await self._team("team-x", status="token_expired")
        await self._expiry("team-x")
        token_id = await self._token("atm_inactive_newseat")

        await self._assert_blocked_without_consuming("atm_inactive_newseat", token_id)

    async def test_renewal_in_the_active_team_is_not_chosen_for_the_customer(self):
        await self._team("team-a")
        await self._team("team-x", status="token_expired")
        await self._expiry("team-a", expires_at="2026-12-01T00:00:00+00:00")
        await self._expiry("team-x")
        self.live["team-a"] = {
            "members": [{"email": EMAIL, "id": "u-1", "is_owner": False,
                         "expires_at": "2026-12-01T00:00:00+00:00", "source": "self_service"}],
            "pending_invites": [],
        }
        token_id = await self._token("atm_inactive_renew")

        await self._assert_blocked_without_consuming("atm_inactive_renew", token_id)
        rows = await self._rows(
            "SELECT expires_at FROM member_expiry WHERE team_id = 'team-a'"
        )
        self.assertEqual(rows[0]["expires_at"], "2026-12-01T00:00:00+00:00")

    async def test_cached_pending_invite_in_a_non_active_team_blocks(self):
        await self._team("team-a")
        await self._team("team-x", status="token_expired")
        await self._cache("team-x", pending=[{"email": EMAIL.upper(), "id": "inv-1"}])
        token_id = await self._token("atm_inactive_cache")

        await self._assert_blocked_without_consuming("atm_inactive_cache", token_id)

    async def test_detected_row_in_a_non_active_team_blocks(self):
        await self._team("team-a")
        await self._team("team-x", status="token_expired")
        await self._expiry("team-x", source="detected", expires_at=None)
        token_id = await self._token("atm_inactive_detect")

        await self._assert_blocked_without_consuming("atm_inactive_detect", token_id)

    async def test_kicked_row_in_a_non_active_team_does_not_block(self):
        await self._team("team-a")
        await self._team("team-x", status="token_expired")
        await self._expiry("team-x", kicked=1)
        await self._token("atm_inactive_kicked")

        result = await self._redeem("atm_inactive_kicked")
        self.assertEqual((result["status"], result["team_id"]), ("ok", "team-a"))
        self.assertEqual(self.invites, ["team-a"])

    async def test_rows_of_a_deleted_team_do_not_block(self):
        # 删除 Team 时 member_expiry 刻意保留作审计；Team 已不受管理，不能永久挡住兑换。
        await self._team("team-a")
        await self._expiry("team-gone")
        await self._token("atm_inactive_gone")

        result = await self._redeem("atm_inactive_gone")
        self.assertEqual((result["status"], result["team_id"]), ("ok", "team-a"))


# ── 缺口 10：新邀请不能发到订阅已到期 / 登录被拒的 Team ─────────────────────

LAPSED = "2026-01-01T00:00:00+00:00"


class RedemptionInviteSkipsUnusableTeamsTest(_RedeemFlowTest):
    async def test_new_seat_is_not_sold_in_a_team_whose_subscription_lapsed(self):
        await self._team("team-lapsed", active_until=LAPSED, will_renew=0, seats=50)
        await self._team("team-b")
        await self._token("atm_lapsed_skip")

        result = await self._redeem("atm_lapsed_skip")

        self.assertEqual((result["status"], result["team_id"]), ("ok", "team-b"))
        self.assertEqual(self.invites, ["team-b"])

    async def test_only_lapsed_teams_left_means_no_seat_and_the_code_is_kept(self):
        await self._team("team-lapsed", active_until=LAPSED, will_renew=0)
        token_id = await self._token("atm_lapsed_only")

        with self.assertRaises(HTTPException) as cm:
            await self._redeem("atm_lapsed_only")

        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._used_count(token_id), 0)

    async def test_members_of_a_lapsed_team_are_still_found_and_renewed_there(self):
        # 只过滤"去哪发新邀请"；找人和续期照旧覆盖这个 Team，不会给他另开席位。
        await self._team("team-lapsed", active_until=LAPSED, will_renew=0)
        await self._team("team-b")
        await self._expiry("team-lapsed", expires_at="2026-12-01T00:00:00+00:00")
        self.live["team-lapsed"] = {
            "members": [{"email": EMAIL, "id": "u-1", "is_owner": False,
                         "expires_at": "2026-12-01T00:00:00+00:00", "source": "self_service"}],
            "pending_invites": [],
        }
        await self._token("atm_lapsed_renew")

        result = await self._redeem("atm_lapsed_renew")

        self.assertEqual(
            (result["status"], result["action"], result["team_id"]),
            ("ok", "renewed_member", "team-lapsed"),
        )
        self.assertEqual(self.invites, [])

    async def test_a_team_with_rejected_login_does_not_fail_every_redemption(self):
        await self._team("team-rejected", auth_state="rejected", seats=50)
        await self._team("team-b")
        self.broken.add("team-rejected")
        await self._token("atm_rejected_skip")

        result = await self._redeem("atm_rejected_skip")

        self.assertEqual((result["status"], result["team_id"]), ("ok", "team-b"))
        self.assertEqual(self.invites, ["team-b"])

    async def test_members_of_a_team_with_rejected_login_are_sent_to_the_admin(self):
        await self._team("team-rejected", auth_state="rejected")
        await self._team("team-b")
        self.broken.add("team-rejected")
        await self._expiry("team-rejected")
        token_id = await self._token("atm_rejected_member")

        with self.assertRaises(HTTPException) as cm:
            await self._redeem("atm_rejected_member")

        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("联系管理员", cm.exception.detail)
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._used_count(token_id), 0)


if __name__ == "__main__":
    unittest.main()
