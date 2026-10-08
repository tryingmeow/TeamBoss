"""批量 GPT 拉人：邀请结果的判定与"仅重试失败邮箱"的回归测试。

1. 邀请成败只看 ChatGPTClient 的定性 ``_mutation_status``，不看响应里有没有
   ``error`` 键。confirmed 的 2xx 响应体带着 ``"error": null``（或任何 error 字段）
   时，原来按"有 error 键就是失败"处理，接着去下一个 Team 再拉一次，同一个人占两个
   席位。rejected 却没带 error 文本时反过来被当成成功落库。没有定性的结果按不明确
   处理，留在原 Team。
2. 结果不明确的邀请会在 ``pending_invite_reconciliations`` 写一条 resolved=0 的对账行。
   前端"仅重试失败邮箱"把这个邮箱原样再提交一遍时，原来完全不看这条行，按空位排序
   就可能把人拉进另一个 Team。现在这种邮箱直接跳过，作为失败项报回（带"等待对账"
   的原因），结清（resolved=1）之后才恢复正常邀请。

驱动真实的 POST /api/gpt-members/invite 路由和临时目录里真实 init_database() 建的库；
只替换上游客户端、现拉名单和现拉空位。
"""

import _isolation  # noqa: F401  must precede any app import
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import direct_call, start_temp_db_async

from app.routes import access_tokens, gpt_members
from app.services import gpt_invites, member_expiry
from app.services.team_locks import release_default_seat_reservation


EMAIL = "member@example.com"
TEAM_A = "outcome-team-a"
TEAM_B = "outcome-team-b"
ABSENT = {"members": [], "pending_invites": []}
UNCERTAIN = {"error": "timed out", "_mutation_status": "uncertain"}


def _others(prefix, count):
    return [{"email": f"{prefix}{i}@example.com", "seat_type": "default"} for i in range(count)]


class _Client:
    """上游客户端替身：依次返回给定的邀请结果，用完后一律 confirmed；记下每次邀请。"""

    def __init__(self, results=()):
        self.results = list(results)
        self.invites = []

    def invite_member(self, email, seat_type="default"):
        self.invites.append(email)
        if self.results:
            return dict(self.results.pop(0))
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


class _BatchInviteHarness(unittest.IsolatedAsyncioTestCase):
    async def _start(self):
        """新建一份临时库，两个 Team：A 空、B 已有两个别人，候选顺序是先 A 后 B。"""
        self.db_path = await start_temp_db_async(self)
        for team_id in (TEAM_A, TEAM_B):
            # 成功邀请后的席位预留在进程内存里，跨用例会互相影响。
            await release_default_seat_reservation(team_id, EMAIL)
            self.addAsyncCleanup(release_default_seat_reservation, team_id, EMAIL)
        self._insert_team(TEAM_A, created_at="2026-10-01T00:00:00+00:00", members=[])
        self._insert_team(TEAM_B, created_at="2026-10-02T00:00:00+00:00", members=_others("b", 2))

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute(self, sql, *params):
        conn = self._conn()
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def _rows(self, sql, *params):
        conn = self._conn()
        rows = [dict(row) for row in conn.execute(sql, params)]
        conn.close()
        return rows

    def _insert_team(self, team_id, *, created_at, members):
        self._execute(
            """INSERT INTO teams (id, name, status, owner_email, access_token, device_id,
                                  seats_in_use, seats_entitled, codex_count, chatgpt_count,
                                  active_until, will_renew, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?, ?, 0, 5, 0, 0, NULL, 1, ?, ?)""",
            team_id, team_id.upper(), f"owner-{team_id}@example.com", f"tok-{team_id}",
            f"dev-{team_id}", created_at, created_at,
        )
        self._execute(
            "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, ?, '[]', ?)",
            team_id, json.dumps(members), created_at,
        )

    def _set_cached_members(self, team_id, members):
        self._execute("UPDATE member_cache SET members_json = ? WHERE team_id = ?", json.dumps(members), team_id)

    def _expiry_teams(self):
        return [r["team_id"] for r in self._rows(
            "SELECT team_id FROM member_expiry WHERE lower(email) = ? ORDER BY id", EMAIL)]

    def _reconciliation_rows(self):
        return [(r["team_id"], r["email"], r["resolved"]) for r in self._rows(
            "SELECT team_id, email, resolved FROM pending_invite_reconciliations ORDER BY id")]

    async def _submit(self, emails, clients):
        """POST /api/gpt-members/invite；现拉名单里始终看不到这个邮箱，现拉空位始终有。"""
        with (
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(side_effect=lambda t: clients[t])),
            patch.object(gpt_invites, "fetch_and_cache_members", new=AsyncMock(return_value=ABSENT)),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "notify_member_event", new=AsyncMock()),
        ):
            return await gpt_members.invite_gpt_members(gpt_members.InviteGptMembersRequest(emails=emails))


# ── 1. 按定性判成败，不按 error 键 ───────────────────────────────────────────

class InviteOutcomeFollowsMutationStatusTest(_BatchInviteHarness):
    async def test_confirmed_reply_carrying_an_error_key_is_added_to_its_own_team(self):
        for label, reply in (
            ("error: null", {"error": None, "_mutation_status": "confirmed"}),
            ("error: empty", {"error": "", "_mutation_status": "confirmed"}),
            ("error: text", {"error": "upstream note", "_mutation_status": "confirmed"}),
        ):
            with self.subTest(label):
                await self._start()
                clients = {TEAM_A: _Client([reply]), TEAM_B: _Client()}

                result = await self._submit([EMAIL], clients)

                self.assertEqual([item["team_id"] for item in result["added"]], [TEAM_A])
                self.assertEqual(result["failed"], [])
                self.assertEqual(clients[TEAM_B].invites, [], "confirmed 的邀请不能再去下一个 Team 拉一次")
                self.assertEqual(self._expiry_teams(), [TEAM_A])

    async def test_rejected_reply_without_error_text_is_a_failure_and_moves_on(self):
        await self._start()
        clients = {TEAM_A: _Client([{"_mutation_status": "rejected"}]), TEAM_B: _Client()}

        result = await self._submit([EMAIL], clients)

        # 上游明确拒绝、没有远端副作用：照常换下一个 Team；Team A 不能落到期记录。
        self.assertEqual([item["team_id"] for item in result["added"]], [TEAM_B])
        self.assertEqual(clients[TEAM_A].invites, [EMAIL])
        self.assertEqual(self._expiry_teams(), [TEAM_B])

    async def test_unclassified_reply_stays_with_its_team(self):
        for label, reply in (
            ("error without status", {"error": "boom"}),
            ("empty dict", {}),
        ):
            with self.subTest(label):
                await self._start()
                clients = {TEAM_A: _Client([reply]), TEAM_B: _Client()}

                result = await self._submit([EMAIL], clients)

                self.assertEqual(result["added"], [])
                self.assertEqual([item["email"] for item in result["failed"]], [EMAIL])
                self.assertEqual(clients[TEAM_B].invites, [])
                self.assertEqual(self._expiry_teams(), [])
                self.assertEqual(self._reconciliation_rows(), [(TEAM_A, EMAIL, 0)])


# ── 2. 仅重试失败邮箱：未结清的不明确邀请不换 Team ─────────────────────────

class RetryFailedSkipsUnresolvedInviteTest(_BatchInviteHarness):
    async def _uncertain_first_attempt(self, clients):
        """第一次提交：Team A 的邀请结果不明确，留下一条未结清的对账行。

        之后 Team A 被别的邀请占满到只剩两个空位，B 有三个：重试时候选顺序变成先 B。
        """
        first = await self._submit([EMAIL], clients)
        self.assertEqual(first["added"], [])
        self.assertEqual([item["email"] for item in first["failed"]], [EMAIL])
        self.assertEqual(clients[TEAM_A].invites, [EMAIL])
        self.assertEqual(self._reconciliation_rows(), [(TEAM_A, EMAIL, 0)])
        self._set_cached_members(TEAM_A, _others("a", 3))
        return [item["email"] for item in first["failed"]]

    async def test_retry_skips_an_email_whose_uncertain_invite_is_unresolved(self):
        await self._start()
        clients = {TEAM_A: _Client([UNCERTAIN]), TEAM_B: _Client()}
        failed_emails = await self._uncertain_first_attempt(clients)

        retry = await self._submit(failed_emails, clients)

        self.assertEqual(retry["added"], [])
        self.assertEqual(len(retry["failed"]), 1)
        self.assertEqual(retry["failed"][0]["email"], EMAIL)
        self.assertIn("等待对账", retry["failed"][0]["error"])
        self.assertIn(TEAM_A.upper(), retry["failed"][0]["error"])
        self.assertEqual(clients[TEAM_B].invites, [], "邀请可能已在 Team A 生效，不能换 Team B 再拉")
        self.assertEqual(clients[TEAM_A].invites, [EMAIL], "重试也不能在没对账前重发")
        self.assertEqual(self._expiry_teams(), [])
        self.assertEqual(self._reconciliation_rows(), [(TEAM_A, EMAIL, 0)])

    async def test_unresolved_redemption_barrier_also_blocks_the_batch(self):
        await self._start()
        # 自助兑换结果不明确时立的屏障行（kind='barrier'）同样说明邀请可能已在 Team A。
        await member_expiry.record_uncertain_invite(
            TEAM_A, "", EMAIL, None, source="self_service", kind="barrier", token_use_id=1
        )
        self._set_cached_members(TEAM_A, _others("a", 3))
        clients = {TEAM_A: _Client(), TEAM_B: _Client()}

        result = await self._submit([EMAIL], clients)

        self.assertEqual(result["added"], [])
        self.assertIn("等待对账", result["failed"][0]["error"])
        self.assertEqual(clients[TEAM_A].invites, [])
        self.assertEqual(clients[TEAM_B].invites, [])

    async def test_open_redemption_barrier_points_to_the_redemption_not_a_manual_invite(self):
        """屏障属于一笔未结兑换时，说明不能再让管理员"确认没送达就单独邀请"：
        退码后手动补发，成员还能拿退回的码再兑换一次。"""
        await self._start()
        conn = self._conn()
        token_id = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES ('hash-outcome', 'atm_x', '30d', 1, 0, 0, '2026-10-01')"""
        ).lastrowid
        conn.commit()
        conn.close()
        token_use_id = await access_tokens._reserve_token_use(token_id, EMAIL, None)
        await access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=TEAM_A)
        self.assertTrue(await access_tokens._lock_uncertain_with_barrier(
            token_use_id, team_id=TEAM_A, email=EMAIL,
            error_message="OpenAI invite result is uncertain",
            reason="OpenAI invite result is uncertain",
        ))
        clients = {TEAM_A: _Client(), TEAM_B: _Client()}

        result = await self._submit([EMAIL], clients)

        self.assertEqual(result["added"], [])
        error = result["failed"][0]["error"]
        self.assertIn(f"#{token_use_id}", error)
        self.assertIn("请让成员用同一兑换码重新兑换", error)
        self.assertNotIn("单独邀请", error)
        self.assertEqual(clients[TEAM_A].invites, [])
        self.assertEqual(clients[TEAM_B].invites, [])

    async def test_settled_redemption_row_points_to_redeeming_again_not_a_manual_invite(self):
        """挡住批量的行挂着一笔已退码的兑换（修复前的确认兜底会在退码之后留下这种
        'extend' 行）：说明同样不能让管理员到原 Team 单独邀请，成员还拿着能用的码。"""
        await self._start()
        conn = self._conn()
        token_id = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES ('hash-settled', 'atm_x', '30d', 1, 0, 0, '2026-10-01')"""
        ).lastrowid
        conn.commit()
        conn.close()
        token_use_id = await access_tokens._reserve_token_use(token_id, EMAIL, None)
        await access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=TEAM_A)
        self.assertTrue(await access_tokens._lock_uncertain_with_barrier(
            token_use_id, team_id=TEAM_A, email=EMAIL,
            error_message="OpenAI invite result is uncertain",
            reason="OpenAI invite result is uncertain",
        ))
        self.assertTrue(await access_tokens._release_uncertain_token_use(
            token_use_id, error_message="admin_released: verified absent"
        ))
        self._execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved, created_at,
                token_use_id, kind)
               VALUES (?, '', ?, NULL, 'self_service', 'database is locked', 0,
                       '2026-10-05T00:00:00+00:00', ?, 'extend')""",
            TEAM_A, EMAIL, token_use_id,
        )
        clients = {TEAM_A: _Client(), TEAM_B: _Client()}

        result = await self._submit([EMAIL], clients)

        self.assertEqual(result["added"], [])
        error = result["failed"][0]["error"]
        self.assertIn(f"#{token_use_id}", error)
        self.assertIn("已退码", error)
        self.assertIn("请让成员用同一兑换码重新兑换", error)
        self.assertNotIn("单独邀请", error)
        self.assertEqual(clients[TEAM_A].invites, [])
        self.assertEqual(clients[TEAM_B].invites, [])

    async def test_resolved_row_no_longer_blocks_a_retry(self):
        await self._start()
        clients = {TEAM_A: _Client([UNCERTAIN]), TEAM_B: _Client()}
        failed_emails = await self._uncertain_first_attempt(clients)
        # 调度器同步 / 兑换结清写的都是 resolved = 1。
        self._execute(
            "UPDATE pending_invite_reconciliations SET resolved = 1, resolved_at = '2026-10-05T00:00:00+00:00'"
        )

        retry = await self._submit(failed_emails, clients)

        self.assertEqual(retry["failed"], [])
        self.assertEqual([item["team_id"] for item in retry["added"]], [TEAM_B])
        self.assertEqual(clients[TEAM_B].invites, [EMAIL])

    async def test_unresolved_row_of_a_deleted_team_does_not_block(self):
        await self._start()
        # 删 Team 不撤对账行、之后也不再同步它：这种行永远不会结清。
        await member_expiry.record_uncertain_invite("deleted-team", "", EMAIL, None)
        clients = {TEAM_A: _Client(), TEAM_B: _Client()}

        result = await self._submit([EMAIL], clients)

        self.assertEqual(result["failed"], [])
        self.assertEqual([item["team_id"] for item in result["added"]], [TEAM_A])


if __name__ == "__main__":
    unittest.main()
