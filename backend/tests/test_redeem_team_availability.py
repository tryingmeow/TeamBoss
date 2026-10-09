"""Which Team a public redemption may renew in or invite into.

Evidence of membership in a Team that cannot be used right now (token expired,
login rejected) blocks only a step that would become a new invite elsewhere; a
live Team the member picks is renewed there, and the multi-Team prompt lists the
dead Team as not renewable. New seats are never sold in a Team whose
subscription lapsed or whose login is rejected. Every upstream call is faked;
each test runs on its own database built by the real ``init_database()``.
"""

import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from _redemption_fixtures import EMAIL, FUTURE, RedeemFlowCase

from app.routes import access_tokens

LATER = "2027-02-01T00:00:00+00:00"


# ── 非 active Team 里的成员不能被当成"不在任何 Team" ──────────────────────────

class UnavailableTeamMembershipBlocksRedemptionTest(RedeemFlowCase):
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
        # 拒绝前要先实时扫描可用 Team（人若在其中就该走选择提示而不是拒绝），所以这次
        # 尝试像其他实时查询之后的拒绝一样计入预算。没有可用 Team 时的纯本地拒绝仍全额
        # 退回，见 DeadTeamOnlyBlocksNewInvitesTest。
        self.assertEqual(len(self.budget._per_code_hits.get(token_id, [])), 1)
        self.assertEqual(len(self.budget._global_hits), 1)

    async def test_paid_member_of_a_token_expired_team_is_not_invited_elsewhere(self):
        await self._team("team-a")
        await self._team("team-x", status="token_expired")
        await self._expiry("team-x")
        token_id = await self._token("atm_inactive_newseat")

        await self._assert_blocked_without_consuming("atm_inactive_newseat", token_id)

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


# ── 新邀请不能发到订阅已到期 / 登录被拒的 Team ───────────────────────────────

LAPSED = "2026-01-01T00:00:00+00:00"


class RedemptionInviteSkipsUnusableTeamsTest(RedeemFlowCase):
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


# ── A dead Team only blocks what would become a new invite ──────────────────

class DeadTeamOnlyBlocksNewInvitesTest(RedeemFlowCase):
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


if __name__ == "__main__":
    unittest.main()
