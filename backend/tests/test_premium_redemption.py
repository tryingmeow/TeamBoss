"""兑换码的席位类型（ChatGPT / Premium）：路由、绝不超员、续期类型核对、生成与列表。

全部走真实的兑换流程，上游换成只记录调用的假客户端：任何拒绝都必须**没有**发出邀请、
码保持未使用（used_count=0、兑换行不是 pending/uncertain、邮箱占用已释放）。
"""

import _isolation  # noqa: F401  must precede any app import
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI, HTTPException

from app import database as app_database
from app.routes import access_tokens
from app.security import require_admin
from app.services import seat_capacity as seat_capacity_module
from app.services import team_locks
from app.services.team_locks import reserve_seat, reserved_seats

EMAIL = "premium.buyer@example.com"
OTHER = "someone.else@example.com"
CREATED = "2026-09-01T00:00:00+00:00"
FUTURE = (datetime.now(timezone.utc) + timedelta(days=10)).replace(microsecond=0).isoformat()


async def _direct_call(func, *args, **kwargs):
    return func(*args, **kwargs)


def _subscription(*, entitled=5, in_use=0, capacity=None):
    sub = {"seats_entitled": entitled, "seats_in_use": in_use}
    if capacity is not None:
        sub["seat_capacity"] = capacity
    return sub


class _FlowBase(unittest.IsolatedAsyncioTestCase):
    """Real redemption flow; upstream reads come from ``self.upstream[team_id]``,
    member snapshots from ``self.live[team_id]``; every upstream invite is recorded in
    ``self.invites`` as (team_id, email, seat_type)."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()
        team_locks._reservations.clear()
        self.addCleanup(team_locks._reservations.clear)

        self.upstream: dict[str, dict] = {}
        self.live: dict[str, dict] = {}
        # 某个 Team 收到邀请之后再拉名单时返回的快照（没设就沿用 self.live）。
        self.after_invite: dict[str, dict] = {}
        self.invites: list[tuple[str, str, str]] = []
        self.capacity_reads: list[str] = []
        self.invite_result: dict = {"_mutation_status": "confirmed"}
        test = self

        class FakeClient:
            def __init__(self, access_token, team_id, device_id, proxy_url=None):
                self.team_id = team_id

            def get_subscription(self):
                test.capacity_reads.append(self.team_id)
                return json.loads(json.dumps(test.upstream[self.team_id]["subscription"]))

            def get_seat_type_counts(self):
                return {"seat_type_counts": dict(test.upstream[self.team_id]["counts"])}

            def get_pending_invites(self, offset=0, limit=100):
                return {"items": list(test.upstream[self.team_id].get("pending", []))}

            def invite_member(self, email, seat_type="default", role="standard-user"):
                test.invites.append((self.team_id, email, seat_type))
                return dict(test.invite_result)

        async def fake_fetch(team_id, client):
            if team_id in self.after_invite and any(inv[0] == team_id for inv in self.invites):
                return self.after_invite[team_id]
            return self.live.get(team_id, {"members": [], "pending_invites": []})

        self.notify_admins = AsyncMock(return_value=1)
        self.notify_member_event = AsyncMock(return_value=1)
        for module, target, value in (
            (access_tokens, "_check_rate_limit", AsyncMock()),
            (access_tokens, "_get_proxy_url", AsyncMock(return_value=None)),
            (access_tokens, "ChatGPTClient", FakeClient),
            (access_tokens, "fetch_and_cache_members", fake_fetch),
            (access_tokens, "add_member_watch", AsyncMock()),
            (access_tokens, "run_chatgpt_call", _direct_call),
            (seat_capacity_module, "run_chatgpt_call", _direct_call),
            (access_tokens, "notify_admins", self.notify_admins),
            (access_tokens, "_premium_notice_sent", {}),
            (access_tokens, "notify_member_event", self.notify_member_event),
            (
                access_tokens,
                "_redeem_lookup_budget",
                access_tokens._RedeemLookupBudget(
                    per_code=100, per_code_window=3600, global_limit=1000, global_window=600
                ),
            ),
        ):
            p = patch.object(module, target, new=value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    # ---- data helpers -------------------------------------------------------------

    async def _exec(self, sql, params=()):
        async with app_database.get_db() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.lastrowid

    async def _rows(self, sql, params=()):
        async with app_database.get_db() as db:
            cursor = await db.execute(sql, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def _team(self, team_id, *, policy="confirm", cached_capacity=None, created_at=CREATED):
        await self._exec(
            """INSERT INTO teams
               (id, name, status, access_token, device_id, seats_entitled, chatgpt_count,
                seat_capacity_json, overage_policy, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?, 5, 0, ?, ?, ?, ?)""",
            (
                team_id,
                f"Team {team_id}",
                f"tok-{team_id}",
                f"dev-{team_id}",
                json.dumps(cached_capacity) if cached_capacity is not None else None,
                policy,
                created_at,
                created_at,
            ),
        )

    def _upstream(self, team_id, *, subscription, counts=None, pending=()):
        self.upstream[team_id] = {
            "subscription": subscription,
            "counts": counts or {"default": 0, "usage_based": 0},
            "pending": list(pending),
        }

    async def _token(self, raw, seat_type="default"):
        return int(
            await self._exec(
                """INSERT INTO access_tokens
                   (token_hash, token_prefix, grant_expires_in, max_uses,
                    used_count, disabled, created_at, seat_type)
                   VALUES (?, ?, '30d', 1, 0, 0, ?, ?)""",
                (hashlib.sha256(raw.encode()).hexdigest(), raw[:12], CREATED, seat_type),
            )
        )

    async def _redeem(self, raw, *, team_id=None, email=EMAIL):
        return await access_tokens.redeem_access_token(
            access_tokens.RedeemAccessTokenRequest(email=email, token=raw, team_id=team_id),
            object(),
        )

    async def _assert_refused(self, raw, detail):
        with self.assertRaises(HTTPException) as raised:
            await self._redeem(raw)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, detail)
        return raised.exception

    async def _assert_not_consumed(self, token_id):
        token = (await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,)))[0]
        self.assertEqual(token["used_count"], 0)
        uses = await self._rows(
            "SELECT result FROM access_token_uses WHERE token_id = ?", (token_id,)
        )
        self.assertTrue(uses)
        self.assertTrue(all(use["result"] not in ("pending", "uncertain", "success") for use in uses))
        claims = await self._rows("SELECT * FROM redemption_email_claims")
        self.assertEqual(claims, [])

    async def _logs(self, action):
        return await self._rows(
            "SELECT team_id, target_email, detail, result, error_message FROM operation_logs WHERE action = ?",
            (action,),
        )


class ChatGPTCodeNeverOverfillsTest(_FlowBase):
    async def _full_teams(self, policy):
        for team_id, created in (("team-a", CREATED), ("team-b", "2026-09-02T00:00:00+00:00")):
            await self._team(team_id, policy=policy, created_at=created)
            self._upstream(
                team_id,
                subscription=_subscription(
                    entitled=2,
                    in_use=2,
                    capacity=[{"type": "default", "paid": 2, "available": 0}],
                ),
                counts={"default": 2, "usage_based": 0},
            )

    async def _assert_full_refused(self, policy):
        await self._full_teams(policy)
        token_id = await self._token(f"atm_full_{policy}")
        await self._assert_refused(f"atm_full_{policy}", "没有可用 ChatGPT 席位，请联系管理员")
        self.assertEqual(self.invites, [])
        self.assertEqual(sorted(set(self.capacity_reads)), ["team-a", "team-b"])
        await self._assert_not_consumed(token_id)
        self.notify_admins.assert_not_awaited()

    async def test_full_teams_with_auto_policy_refuse_without_invite(self):
        await self._assert_full_refused("auto")

    async def test_full_teams_with_confirm_policy_refuse_without_invite(self):
        await self._assert_full_refused("confirm")

    async def test_min_rule_refuses_when_per_type_available_is_zero(self):
        # 旧公式：5 已付 − 1 在用 = 4 个空位；seat_capacity.default.available = 0。取小 → 0。
        await self._team("team-a", policy="auto")
        self._upstream(
            "team-a",
            subscription=_subscription(
                entitled=5, in_use=1, capacity=[{"type": "default", "paid": 5, "available": 0}]
            ),
            counts={"default": 1, "usage_based": 0},
        )
        token_id = await self._token("atm_min_rule")
        await self._assert_refused("atm_min_rule", "没有可用 ChatGPT 席位，请联系管理员")
        self.assertEqual(self.invites, [])
        await self._assert_not_consumed(token_id)

    async def test_free_chatgpt_seat_still_invites_as_default(self):
        await self._team("team-a")
        self._upstream(
            "team-a",
            subscription=_subscription(
                entitled=5, in_use=1, capacity=[{"type": "default", "paid": 5, "available": 4}]
            ),
            counts={"default": 1, "usage_based": 0},
        )
        await self._token("atm_chatgpt_ok")
        result = await self._redeem("atm_chatgpt_ok")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.invites, [("team-a", EMAIL, "default")])
        self.assertEqual(await reserved_seats("team-a", "default"), 1)
        self.assertEqual(await reserved_seats("team-a", "prolite"), 0)

    async def test_chatgpt_invite_visible_in_snapshot_is_not_reserved(self):
        # ChatGPT 照旧：刷新后的名单里已经看得见这个邀请，就不再额外占位。
        await self._team("team-a")
        self._upstream(
            "team-a",
            subscription=_subscription(
                entitled=5, in_use=1, capacity=[{"type": "default", "paid": 5, "available": 4}]
            ),
            counts={"default": 1, "usage_based": 0},
        )
        self.after_invite["team-a"] = {
            "members": [],
            "pending_invites": [{"email": EMAIL, "seat_type": "default"}],
        }
        await self._token("atm_chatgpt_visible")
        result = await self._redeem("atm_chatgpt_visible")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.invites, [("team-a", EMAIL, "default")])
        self.assertEqual(await reserved_seats("team-a", "default"), 0)


class PremiumCodeRoutingTest(_FlowBase):
    def _premium_upstream(self, team_id, *, paid=1, available=1, pending=(), entry=True):
        capacity = [{"type": "default", "paid": 5, "available": 5}]
        if entry:
            capacity.append({"type": "prolite", "paid": paid, "available": available})
        self._upstream(
            team_id,
            subscription=_subscription(entitled=5 + paid, in_use=0, capacity=capacity),
            counts={"default": 0, "usage_based": 0, "prolite": max(0, paid - available)},
            pending=pending,
        )

    async def test_free_premium_seat_invites_as_prolite_and_reserves_it(self):
        # team-a：缓存里没有 Premium 空位 → 预筛跳过，不做实时读取。
        await self._team("team-a", cached_capacity={"prolite": {"paid": 0, "available": 0}})
        # team-b 设成禁止超员也无妨：有已付空位，不需要超员。
        await self._team(
            "team-b",
            policy="forbid",
            cached_capacity={"prolite": {"paid": 1, "available": 1}},
            created_at="2026-09-02T00:00:00+00:00",
        )
        self._premium_upstream("team-a", paid=0, available=0)
        self._premium_upstream("team-b", paid=1, available=1)
        token_id = await self._token("atm_premium_ok", "prolite")

        result = await self._redeem("atm_premium_ok")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "invited")
        self.assertEqual(result["team_id"], "team-b")
        self.assertEqual(self.invites, [("team-b", EMAIL, "prolite")])
        self.assertEqual(self.capacity_reads, ["team-b"])
        # 名单里还看不见新邀请：占住一个 Premium 空位，不占 ChatGPT 的。
        self.assertEqual(await reserved_seats("team-b", "prolite"), 1)
        self.assertEqual(await reserved_seats("team-b", "default"), 0)
        token = (await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,)))[0]
        self.assertEqual(token["used_count"], 1)
        use = (await self._rows("SELECT action, result FROM access_token_uses WHERE token_id = ?", (token_id,)))[0]
        self.assertEqual(use, {"action": "invited", "result": "success"})
        invite_logs = await self._logs("self_service_invite")
        self.assertIn("seat_type=prolite", invite_logs[0]["detail"])
        self.notify_admins.assert_not_awaited()

    async def _assert_premium_refused(self, raw, *, expect_live_read=True):
        token_id = await self._token(raw, "prolite")
        await self._assert_refused(raw, access_tokens._NO_PREMIUM_SEAT_DETAIL)
        self.assertEqual(self.invites, [])
        if not expect_live_read:
            self.assertEqual(self.capacity_reads, [])
        await self._assert_not_consumed(token_id)
        use = (await self._rows("SELECT action, error_message FROM access_token_uses WHERE token_id = ?", (token_id,)))[0]
        self.assertEqual(use["action"], "redeem_failed")
        self.assertEqual(use["error_message"], access_tokens._NO_PREMIUM_SEAT_DETAIL)

        self.notify_admins.assert_awaited_once()
        text = self.notify_admins.await_args.args[0]
        self.assertIn("Premium", text)
        self.assertIn("兑换码未消耗", text)
        self.assertNotIn(EMAIL, text)
        self.assertIn("premi…@example", text)
        self.assertNotIn(raw, text)
        logs = await self._logs("redeem_no_premium_seat")
        self.assertEqual(len(logs), 1)
        self.assertIn("seat_type=prolite", logs[0]["detail"])
        self.assertIn("released=True", logs[0]["detail"])
        return token_id

    async def test_missing_live_premium_entry_refuses(self):
        await self._team("team-a", policy="auto", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", entry=False)
        await self._assert_premium_refused("atm_prem_noentry")
        self.assertEqual(self.capacity_reads, ["team-a"])

    async def test_zero_available_premium_refuses_even_with_auto_policy(self):
        await self._team("team-a", policy="auto", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", paid=1, available=0)
        await self._assert_premium_refused("atm_prem_zero")

    async def test_pending_premium_invite_eats_the_free_seat(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream(
            "team-a",
            paid=1,
            available=1,
            pending=[{"email_address": OTHER, "seat_type": "prolite"}],
        )
        await self._assert_premium_refused("atm_prem_pending")

    async def test_pending_chatgpt_invite_does_not_eat_a_premium_seat(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream(
            "team-a",
            paid=1,
            available=1,
            pending=[{"email_address": OTHER, "seat_type": "default"}],
        )
        await self._token("atm_prem_pending_default", "prolite")
        result = await self._redeem("atm_prem_pending_default")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.invites, [("team-a", EMAIL, "prolite")])

    async def test_in_memory_premium_reservation_eats_the_free_seat(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", paid=1, available=1)
        await reserve_seat("team-a", OTHER, "prolite")
        await self._assert_premium_refused("atm_prem_reserved")

    async def test_live_read_failure_refuses(self):
        await self._team("team-a", policy="auto", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._upstream("team-a", subscription={"error": "upstream unavailable"})
        await self._assert_premium_refused("atm_prem_readfail")

    async def test_no_cached_premium_capacity_refuses_without_live_read(self):
        await self._team("team-a", policy="auto", cached_capacity=None)
        self._premium_upstream("team-a", paid=1, available=1)
        await self._assert_premium_refused("atm_prem_nocache", expect_live_read=False)

    async def test_rejected_premium_invite_refuses_and_releases_the_code(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", paid=1, available=1)
        self.invite_result = {"error": "seat type not allowed", "_mutation_status": "rejected"}
        token_id = await self._token("atm_prem_rejected", "prolite")
        await self._assert_refused("atm_prem_rejected", access_tokens._NO_PREMIUM_SEAT_DETAIL)
        self.assertEqual(self.invites, [("team-a", EMAIL, "prolite")])
        await self._assert_not_consumed(token_id)
        self.notify_admins.assert_awaited_once()

    async def test_repeated_no_seat_refusals_notify_once_per_code_but_log_each(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", paid=1, available=0)
        await self._assert_premium_refused("atm_prem_repeat")
        # 缓存已被实时复查写成 0，第二次在预筛就被挡住；照样拒绝、照样记日志，但不再发通知。
        with self.assertRaises(HTTPException):
            await self._redeem("atm_prem_repeat")
        self.notify_admins.assert_awaited_once()
        self.assertEqual(len(await self._logs("redeem_no_premium_seat")), 2)

        # 另一个码照常通知。
        await self._token("atm_prem_other", "prolite")
        with self.assertRaises(HTTPException):
            await self._redeem("atm_prem_other")
        self.assertEqual(self.notify_admins.await_count, 2)

    async def test_refused_premium_code_can_be_used_once_a_seat_frees_up(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", paid=1, available=0)
        await self._assert_premium_refused("atm_prem_retry")
        # 实时复查把「0 个空位」写回了缓存，之后的兑换先被预筛挡住、不再实时读取。
        self.capacity_reads.clear()
        with self.assertRaises(HTTPException):
            await self._redeem("atm_prem_retry")
        self.assertEqual(self.capacity_reads, [])
        # 管理员买了席位、同步了 Team（缓存刷新），同一个码就能用。
        self._premium_upstream("team-a", paid=2, available=1)
        await self._exec(
            "UPDATE teams SET seat_capacity_json = ? WHERE id = 'team-a'",
            (json.dumps({"prolite": {"paid": 2, "available": 1}}),),
        )
        result = await self._redeem("atm_prem_retry")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.invites, [("team-a", EMAIL, "prolite")])

    async def test_premium_invite_visible_in_snapshot_still_reserves_the_seat(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 2, "available": 2}})
        self._premium_upstream("team-a", paid=2, available=2)
        self.after_invite["team-a"] = {
            "members": [],
            "pending_invites": [{"email": EMAIL, "seat_type": "prolite"}],
        }
        await self._token("atm_prem_visible", "prolite")
        result = await self._redeem("atm_prem_visible")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.invites, [("team-a", EMAIL, "prolite")])
        self.assertEqual(await reserved_seats("team-a", "prolite"), 1)
        self.assertEqual(await reserved_seats("team-a", "default"), 0)

    async def test_uncertain_premium_invite_seen_on_recheck_still_reserves_the_seat(self):
        # 超时后重新拉名单看见了人 → 升级成确认成功，照样占住 Premium 空位。
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", paid=1, available=1)
        self.invite_result = {"error": "timeout", "_mutation_status": "uncertain"}
        self.after_invite["team-a"] = {
            "members": [],
            "pending_invites": [{"email": EMAIL, "seat_type": "prolite"}],
        }
        await self._token("atm_prem_uncertain_seen", "prolite")
        result = await self._redeem("atm_prem_uncertain_seen")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(await reserved_seats("team-a", "prolite"), 1)

    async def test_uncertain_premium_invite_keeps_code_locked_and_holds_the_seat(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", paid=1, available=1)
        self.invite_result = {"error": "timeout", "_mutation_status": "uncertain"}
        token_id = await self._token("atm_prem_uncertain", "prolite")
        result = await self._redeem("atm_prem_uncertain")
        self.assertEqual(result["status"], "pending_confirmation")
        token = (await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,)))[0]
        self.assertEqual(token["used_count"], 1)
        self.assertEqual(await reserved_seats("team-a", "prolite"), 1)
        self.notify_admins.assert_not_awaited()

    async def test_code_with_unsupported_seat_type_is_refused_before_anything(self):
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._premium_upstream("team-a", paid=1, available=1)
        token_id = await self._token("atm_bad_type", "usage_based")
        await self._assert_refused("atm_bad_type", access_tokens._INVALID_CODE_SEAT_TYPE_DETAIL)
        self.assertEqual(self.invites, [])
        self.assertEqual(self.capacity_reads, [])
        uses = await self._rows("SELECT * FROM access_token_uses WHERE token_id = ?", (token_id,))
        self.assertEqual(uses, [])
        token = (await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,)))[0]
        self.assertEqual(token["used_count"], 0)


class RenewalSeatTypeTest(_FlowBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._team("team-a", cached_capacity={"prolite": {"paid": 1, "available": 1}})
        self._upstream(
            "team-a",
            subscription=_subscription(
                entitled=6,
                in_use=1,
                capacity=[
                    {"type": "default", "paid": 5, "available": 5},
                    {"type": "prolite", "paid": 1, "available": 1},
                ],
            ),
        )

    async def _member(self, team_id, seat_type, *, kind="member", user_id="u-1"):
        await self._exec(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, source, created_at)
               VALUES (?, ?, ?, ?, 1, 0, 'self_service', ?)""",
            (team_id, user_id if kind == "member" else "", EMAIL, FUTURE, CREATED),
        )
        entry = {
            "email": EMAIL,
            "id": user_id if kind == "member" else "invite-1",
            "is_owner": False,
            "expires_at": FUTURE,
            "source": "self_service",
        }
        if seat_type is not None:
            entry["seat_type"] = seat_type
        self.live[team_id] = (
            {"members": [entry], "pending_invites": []}
            if kind == "member"
            else {"members": [], "pending_invites": [entry]}
        )

    async def _expiry(self, team_id="team-a"):
        rows = await self._rows(
            "SELECT expires_at FROM member_expiry WHERE team_id = ? AND lower(email) = ?",
            (team_id, EMAIL),
        )
        return rows[0]["expires_at"]

    async def _assert_renews(self, raw, code_seat_type, member_seat_type, *, kind="member"):
        await self._member("team-a", member_seat_type, kind=kind)
        token_id = await self._token(raw, code_seat_type)
        result = await self._redeem(raw)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["action"], "renewed_member" if kind == "member" else "renewed_invite"
        )
        self.assertNotEqual(await self._expiry(), FUTURE)
        token = (await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,)))[0]
        self.assertEqual(token["used_count"], 1)
        self.assertEqual(self.invites, [])

    async def _assert_mismatch(
        self, raw, code_seat_type, member_seat_type, detail, log_message, *, kind="member"
    ):
        await self._member("team-a", member_seat_type, kind=kind)
        token_id = await self._token(raw, code_seat_type)
        # 客户看到的（HTTP 答复）是第二人称。
        await self._assert_refused(raw, detail)
        self.assertEqual(self.invites, [])
        self.assertEqual(await self._expiry(), FUTURE)
        await self._assert_not_consumed(token_id)
        logs = await self._logs("redeem_seat_type_mismatch")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["team_id"], "team-a")
        self.assertIn(f"seat_type={code_seat_type}", logs[0]["detail"])
        # 管理员看的操作日志说明是第三人称。
        self.assertEqual(logs[0]["error_message"], log_message)
        self.assertNotIn("你", logs[0]["error_message"])
        # 公开兑换记录（给客户自己看）保持第二人称，与 HTTP 答复一致。
        use = (await self._rows(
            "SELECT error_message FROM access_token_uses WHERE token_id = ?", (token_id,)
        ))[0]
        self.assertEqual(use["error_message"], detail)
        self.notify_admins.assert_not_awaited()
        return logs[0]

    async def test_premium_code_renews_premium_member(self):
        await self._assert_renews("atm_r_pp", "prolite", "prolite")

    async def test_premium_code_renews_pending_premium_invite(self):
        await self._assert_renews("atm_r_ppi", "prolite", "prolite", kind="invite")

    async def test_chatgpt_code_renews_chatgpt_member(self):
        await self._assert_renews("atm_r_dd", "default", "default")

    async def test_chatgpt_code_renews_member_with_missing_seat_type(self):
        await self._assert_renews("atm_r_dnone", "default", None)

    async def test_chatgpt_code_still_renews_codex_member(self):
        await self._assert_renews("atm_r_dcodex", "default", "usage_based")

    async def test_chatgpt_code_on_premium_member_is_refused(self):
        log = await self._assert_mismatch(
            "atm_r_dp",
            "default",
            "prolite",
            "兑换码是 ChatGPT 码，你当前是 Premium 席位，不能用它续期。兑换码未使用。",
            "兑换码是 ChatGPT 码，该成员当前是 Premium 席位，未续期，兑换码未使用。",
        )
        self.assertIn("member_seat_type=prolite", log["detail"])
        self.assertIn("reason=seat_type_mismatch", log["detail"])

    async def test_premium_code_on_chatgpt_member_is_refused(self):
        await self._assert_mismatch(
            "atm_r_pd",
            "prolite",
            "default",
            "兑换码是 Premium 码，你当前是 ChatGPT 席位，不能用它续期。兑换码未使用。",
            "兑换码是 Premium 码，该成员当前是 ChatGPT 席位，未续期，兑换码未使用。",
        )

    async def test_premium_code_on_member_with_missing_seat_type_is_refused(self):
        await self._assert_mismatch(
            "atm_r_pnone",
            "prolite",
            None,
            "兑换码是 Premium 码，你当前是 ChatGPT 席位，不能用它续期。兑换码未使用。",
            "兑换码是 Premium 码，该成员当前是 ChatGPT 席位，未续期，兑换码未使用。",
        )

    async def test_premium_code_on_pending_chatgpt_invite_is_refused(self):
        await self._assert_mismatch(
            "atm_r_pdi",
            "prolite",
            "default",
            "兑换码是 Premium 码，你当前是 ChatGPT 席位，不能用它续期。兑换码未使用。",
            "兑换码是 Premium 码，该成员当前是 ChatGPT 席位，未续期，兑换码未使用。",
            kind="invite",
        )

    async def test_premium_code_on_codex_member_is_refused(self):
        await self._assert_mismatch(
            "atm_r_pcodex",
            "prolite",
            "usage_based",
            "兑换码是 Premium 码，你当前是 Codex 席位，不能用它续期。兑换码未使用。",
            "兑换码是 Premium 码，该成员当前是 Codex 席位，未续期，兑换码未使用。",
        )

    async def test_unknown_member_seat_type_is_refused_for_both_code_types(self):
        log = await self._assert_mismatch(
            "atm_r_dauto",
            "default",
            "automation",
            access_tokens._UNKNOWN_MEMBER_SEAT_TYPE_DETAIL,
            "该成员当前是 其他（automation） 席位，TeamBoss 不认识这种席位，未续期，兑换码未使用。",
        )
        self.assertIn("reason=unknown_member_seat_type", log["detail"])
        await self._exec("DELETE FROM member_expiry")
        await self._exec("DELETE FROM operation_logs")
        await self._assert_mismatch(
            "atm_r_pauto",
            "prolite",
            "automation",
            access_tokens._UNKNOWN_MEMBER_SEAT_TYPE_DETAIL,
            "该成员当前是 其他（automation） 席位，TeamBoss 不认识这种席位，未续期，兑换码未使用。",
        )

    async def test_team_choices_mark_seat_type_mismatch_as_not_renewable(self):
        await self._team("team-b", cached_capacity=None, created_at="2026-09-02T00:00:00+00:00")
        await self._member("team-a", "prolite")
        await self._member("team-b", "default", user_id="u-2")
        token_id = await self._token("atm_r_choice", "prolite")
        result = await self._redeem("atm_r_choice")
        self.assertEqual(result["status"], "team_selection_required")
        by_team = {choice["team_id"]: choice for choice in result["choices"]}
        self.assertTrue(by_team["team-a"]["renewable"])
        self.assertIsNone(by_team["team-a"]["blocked_reason"])
        self.assertFalse(by_team["team-b"]["renewable"])
        self.assertEqual(by_team["team-b"]["blocked_reason"], "seat_type_mismatch")
        access_tokens.RedeemAccessTokenResponse.model_validate(result)
        await self._assert_not_consumed(token_id)
        self.assertEqual(self.invites, [])


class AccessTokenSeatTypeRouteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()
        app = FastAPI()
        app.include_router(access_tokens.admin_router)
        app.dependency_overrides[require_admin] = lambda: None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self._env.stop()
        self._tmp.cleanup()

    async def test_generate_and_list_round_trip_seat_type(self):
        premium = await self.client.post(
            "/api/access-tokens", json={"grant_expires_in": "30d", "seat_type": "prolite"}
        )
        self.assertEqual(premium.status_code, 200)
        self.assertEqual(premium.json()["seat_type"], "prolite")
        chatgpt = await self.client.post("/api/access-tokens", json={"grant_expires_in": "30d"})
        self.assertEqual(chatgpt.status_code, 200)
        self.assertEqual(chatgpt.json()["seat_type"], "default")

        listed = await self.client.get("/api/access-tokens")
        self.assertEqual(listed.status_code, 200)
        by_id = {item["id"]: item for item in listed.json()}
        self.assertEqual(by_id[premium.json()["id"]]["seat_type"], "prolite")
        self.assertEqual(by_id[chatgpt.json()["id"]]["seat_type"], "default")

        async with app_database.get_db() as db:
            cursor = await db.execute(
                "SELECT detail FROM operation_logs WHERE action = 'create_access_token' ORDER BY id"
            )
            details = [row["detail"] for row in await cursor.fetchall()]
        self.assertEqual(len(details), 2)
        self.assertIn("seat_type=prolite", details[0])
        self.assertIn("seat_type=default", details[1])

        public = await access_tokens._query_token(premium.json()["token"])
        self.assertEqual(public["token"]["seat_type"], "prolite")
        self.assertEqual(public["token"]["seat_type_label"], "Premium")

    async def test_invalid_seat_types_are_rejected_with_422(self):
        for value in ("usage_based", "automation", ""):
            with self.subTest(seat_type=value):
                response = await self.client.post(
                    "/api/access-tokens", json={"grant_expires_in": "30d", "seat_type": value}
                )
                self.assertEqual(response.status_code, 422)
        async with app_database.get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM access_tokens")
            self.assertEqual((await cursor.fetchone())["n"], 0)


if __name__ == "__main__":
    unittest.main()
