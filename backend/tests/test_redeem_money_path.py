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

EMAIL = "redeemer@example.com"
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
        # 拒绝前要先实时扫描可用 Team（人若在其中就该走选择提示而不是拒绝），所以这次
        # 尝试像其他实时查询之后的拒绝一样计入预算。没有可用 Team 时的纯本地拒绝仍全额
        # 退回，见 test_redeem_unavailable_team。
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


# ── 缺口 6：停服打断的邀请不能永远卡在 pending ───────────────────────────────

class InterruptedInviteBecomesUncertainTest(_RedeemFlowTest):
    """进程在邀请请求途中被杀，留下 action='invite_pending', result='pending'。

    对账任务看不见这个人时原先永远 waiting：管理员列表只列 uncertain，码和邮箱占用
    永远锁死，巡逻也没有屏障挡着。超过安全时限后应转成 uncertain 并立屏障。
    """

    async def _stuck_use(self, *, age_minutes, team_id="team-a"):
        from datetime import timedelta

        from app.utils.durations import utc_now

        token_id = await self._exec(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at, last_used_at)
               VALUES (?, 'atm_stuck', '30d', 1, 1, 0, ?, ?)""",
            (access_tokens._hash_token(f"atm_stuck_{age_minutes}_{team_id}"), CREATED, CREATED),
        )
        created = (utc_now() - timedelta(minutes=age_minutes)).isoformat()
        use_id = int(await self._exec(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                error_message, created_at)
               VALUES (?, ?, 'invite_pending', ?, NULL, NULL, 'pending', NULL, ?)""",
            (token_id, EMAIL, team_id, created),
        ))
        await self._exec(
            "INSERT INTO redemption_email_claims (email, token_use_id, created_at) VALUES (?, ?, ?)",
            (EMAIL, use_id, created),
        )
        return token_id, use_id

    async def _use(self, use_id):
        return (await self._rows("SELECT * FROM access_token_uses WHERE id = ?", (use_id,)))[0]

    async def _barriers(self, use_id):
        return await self._rows(
            "SELECT * FROM pending_invite_reconciliations WHERE token_use_id = ?", (use_id,)
        )

    async def _assert_locked_uncertain(self, token_id, use_id, team_id="team-a"):
        use = await self._use(use_id)
        self.assertEqual(
            (use["action"], use["result"], use["team_id"]),
            ("invite_pending", "uncertain", team_id),
        )
        barriers = await self._barriers(use_id)
        self.assertEqual(len(barriers), 1)
        barrier = barriers[0]
        self.assertEqual(
            (barrier["team_id"], barrier["email"], barrier["kind"], barrier["source"],
             barrier["expires_at"], barrier["resolved"]),
            (team_id, EMAIL, "barrier", "self_service", None, 0),
        )
        # 不退码、不放邮箱、不给时长。
        self.assertEqual(await self._used_count(token_id), 1)
        claims = await self._rows("SELECT token_use_id FROM redemption_email_claims")
        self.assertEqual([c["token_use_id"] for c in claims], [use_id])
        self.assertEqual(await self._rows("SELECT * FROM member_expiry"), [])
        # 管理员收尾列表里看得到它。
        listed = await access_tokens.list_pending_confirmations()
        self.assertEqual([row["id"] for row in listed], [use_id])

    async def test_stale_interrupted_invite_is_locked_as_uncertain_for_the_admin(self):
        await self._team("team-a")
        token_id, use_id = await self._stuck_use(age_minutes=20)

        counts = await access_tokens.reconcile_pending_redemptions()

        await self._assert_locked_uncertain(token_id, use_id)
        self.assertEqual(counts["uncertain"], 1)
        logged = [c.args[1] for c in access_tokens.log_operation.await_args_list]
        self.assertIn("self_service_invite_interrupted", logged)

    async def test_conversion_is_idempotent(self):
        await self._team("team-a")
        token_id, use_id = await self._stuck_use(age_minutes=20)

        await access_tokens.reconcile_pending_redemptions()
        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(counts["uncertain"], 0)
        await self._assert_locked_uncertain(token_id, use_id)

    async def test_original_team_unreachable_still_reaches_the_admin_list(self):
        await self._team("team-a", status="token_expired")
        self.broken.add("team-a")
        token_id, use_id = await self._stuck_use(age_minutes=20)

        await access_tokens.reconcile_pending_redemptions()

        await self._assert_locked_uncertain(token_id, use_id)

    async def test_recent_invite_is_left_alone(self):
        await self._team("team-a")
        _, use_id = await self._stuck_use(age_minutes=3)

        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual((counts["waiting"], counts["uncertain"]), (1, 0))
        self.assertEqual((await self._use(use_id))["result"], "pending")
        self.assertEqual(await self._barriers(use_id), [])

    async def test_visible_member_is_confirmed_first_not_locked(self):
        await self._team("team-a")
        _, use_id = await self._stuck_use(age_minutes=20)
        self.live["team-a"] = {"members": [], "pending_invites": [{"email": EMAIL}]}

        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual((counts["confirmed"], counts["uncertain"]), (1, 0))
        self.assertEqual((await self._use(use_id))["result"], "success")
        self.assertEqual(await self._barriers(use_id), [])

    async def test_redemption_still_running_in_this_process_is_not_touched(self):
        await self._team("team-a")
        _, use_id = await self._stuck_use(age_minutes=20)
        access_tokens._inflight_token_uses.add(use_id)
        self.addCleanup(access_tokens._inflight_token_uses.discard, use_id)

        counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual((counts["waiting"], counts["uncertain"]), (1, 0))
        self.assertEqual((await self._use(use_id))["result"], "pending")
        self.assertEqual(await self._barriers(use_id), [])

    async def test_a_redemption_finishing_concurrently_wins(self):
        # 对账读完 pending 之后、转换之前，原兑换刚好落了终态：不能被改写，也不能
        # 留下一道永远不会撤的屏障（那会让巡逻和自动踢人永远跳过这个人）。
        await self._team("team-a")
        _, use_id = await self._stuck_use(age_minutes=20)

        async def finish_then_absent(team_id, client):
            await self._exec(
                "UPDATE access_token_uses SET result = 'success' WHERE id = ?", (use_id,)
            )
            return {"members": [], "pending_invites": []}

        with patch.object(access_tokens, "fetch_and_cache_members", new=finish_then_absent):
            counts = await access_tokens.reconcile_pending_redemptions()

        self.assertEqual(counts["uncertain"], 0)
        self.assertEqual((await self._use(use_id))["result"], "success")
        self.assertEqual(await self._barriers(use_id), [])

    async def test_redeem_flow_registers_and_clears_its_attempt(self):
        await self._team("team-a")
        await self._token("atm_inflight_seen")
        seen = []

        async def fake_run(func, *args, **kwargs):
            seen.append(set(access_tokens._inflight_token_uses))
            return {}

        with patch.object(access_tokens, "run_chatgpt_call", new=fake_run):
            result = await self._redeem("atm_inflight_seen")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(access_tokens._inflight_token_uses, set())


# ── 缺口 13：手里有一张没用过的码，不能读任意邮箱的兑换历史 ─────────────────

VICTIM = "victim@example.com"


class HistoryIsScopedToThePresentedCodeTest(_RedeemFlowTest):
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


if __name__ == "__main__":
    unittest.main()
