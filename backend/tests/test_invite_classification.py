"""邀请结果分类（ChatGPTClient.invite_member 的 ``_mutation_status``）与兑换主路径的回归测试。

1. 邀请接口回 2xx、但本次邮箱列在 ``errored_emails`` 里：上游没有发出邀请，却被
   标成 confirmed，兑换码被消耗而人没进 Team。现在本次邮箱被明确列出 → rejected
   （带 error，兑换码退回、换下一个 Team）；任何对不上的形状 → uncertain（锁码
   等对账），绝不 confirmed。
2. 邀请接口回 2xx、``errored_emails`` 为空，但 ``account_invites`` 列表里没有本次
   邮箱：上游没说给这个邮箱建了邀请，却被当成 confirmed（兑换码被消耗、记下到期，
   人可能根本没被邀请）。现在只要响应带了逐个邮箱的结果字段，就必须在
   ``account_invites`` 里读到本次邮箱才算 confirmed，否则 uncertain（邀请可能已经
   到了 OpenAI，不能当成拒绝）。完全不带这些字段的响应（空体 / 坏 JSON）保持原先
   的 confirmed。
3. 请求本身失败：超时 → uncertain；明确的 4xx 拒绝 → rejected。各种 curl_cffi 传输
   异常和状态码的全表见 test_chatgpt_transport。
4. 兑换主路径（redeem_access_token）按分类退码 / 换 Team / 锁码。管理员拉人与批量
   GPT 拉人对同样结果的处理见 test_admin_invite、test_gpt_batch_invite。
"""

import _isolation  # noqa: F401  must precede any app import
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from curl_cffi.const import CurlECode
from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import Timeout
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import direct_call, start_temp_db_async
from _invite_fixtures import client_returning, ok_response

from app import database as app_database
from app.chatgpt_client import ChatGPTClient
from app.routes import access_tokens


EMAIL = "member@example.com"


# ── 1. 2xx 但本次邮箱在 errored_emails 里：不是成功 ─────────────────────────────

class InviteErroredEmailsClassificationTest(unittest.TestCase):
    def _invite(self, body, email="user@example.com"):
        return client_returning(body).invite_member(email)

    def test_errored_object_entry_for_our_email_is_rejected(self):
        result = self._invite({
            "account_invites": [],
            "errored_emails": [{"email_address": "User@Example.com", "error": "Invalid email domain"}],
        })

        self.assertEqual(result["_mutation_status"], "rejected")
        self.assertIn("error", result)
        self.assertIn("Invalid email domain", result["error"])

    def test_errored_string_entry_for_our_email_is_rejected(self):
        result = self._invite({"account_invites": [], "errored_emails": ["user@example.com"]},
                              email=" USER@example.com ")

        self.assertEqual(result["_mutation_status"], "rejected")
        self.assertTrue(result["error"])

    def test_errored_entry_with_email_key_is_rejected(self):
        result = self._invite({"errored_emails": [{"email": "user@example.com", "error": "blocked"}]})

        self.assertEqual(result["_mutation_status"], "rejected")
        self.assertIn("blocked", result["error"])

    def test_ambiguous_errored_shapes_are_uncertain_never_confirmed(self):
        ours = {"email_address": "user@example.com", "error": "x"}
        cases = {
            "not a list (string)": {"errored_emails": "user@example.com"},
            "not a list (object)": {"errored_emails": ours},
            "null": {"errored_emails": None},
            "entry without email": {"errored_emails": [{"error": "x"}]},
            "entry email not a string": {"errored_emails": [{"email_address": 123, "error": "x"}]},
            "entry not a string/object": {"errored_emails": [42]},
            "only another email": {"account_invites": [], "errored_emails": ["other@example.com"]},
            "ours plus another email": {"errored_emails": ["user@example.com", "other@example.com"]},
            "ours also in account_invites": {
                "account_invites": [{"email_address": "user@example.com"}],
                "errored_emails": [ours],
            },
            "ours also in already_member_emails": {
                "account_invites": [],
                "errored_emails": [ours],
                "already_member_emails": ["user@example.com"],
            },
            "account_invites unreadable": {"account_invites": "?", "errored_emails": [ours]},
        }
        for name, body in cases.items():
            with self.subTest(name):
                result = self._invite(body)
                self.assertEqual(result["_mutation_status"], "uncertain")
                self.assertTrue(result.get("error"))

    def test_absent_or_empty_errored_emails_is_unchanged(self):
        for body in (
            {"account_invites": [{"email_address": "user@example.com"}], "errored_emails": []},
            {"account_invites": [{"email_address": "user@example.com"}]},
            {},
        ):
            with self.subTest(body=body):
                result = self._invite(body)
                self.assertEqual(result["_mutation_status"], "confirmed")
                self.assertNotIn("error", result)


# ── 2. 2xx 但 account_invites 里没有本次邮箱：不是 confirmed ─────────────────

class InviteAccountInvitesClassificationTest(unittest.TestCase):
    def _invite(self, body, email="user@example.com"):
        return client_returning(body).invite_member(email)

    def test_our_email_in_account_invites_is_confirmed(self):
        cases = {
            "object, case and whitespace differ": (
                {"account_invites": [{"email_address": " User@Example.COM "}],
                 "errored_emails": [], "already_member_emails": []},
                " USER@example.com",
            ),
            "string entry": ({"account_invites": ["user@example.com"], "errored_emails": []},
                             "user@example.com"),
            "email key": ({"account_invites": [{"email": "user@example.com"}]}, "user@example.com"),
            "ours among unreadable / other entries": (
                {"account_invites": [{"id": "x"}, "other@example.com", {"email_address": "user@example.com"}],
                 "errored_emails": []},
                "user@example.com",
            ),
        }
        for name, (body, email) in cases.items():
            with self.subTest(name):
                result = self._invite(body, email=email)
                self.assertEqual(result["_mutation_status"], "confirmed")
                self.assertNotIn("error", result)

    def test_account_invites_without_our_email_is_uncertain(self):
        cases = {
            "empty list, errored empty": {"account_invites": [], "errored_emails": [],
                                          "already_member_emails": []},
            "empty list, errored absent": {"account_invites": []},
            "only another email": {"account_invites": [{"email_address": "other@example.com"}],
                                   "errored_emails": []},
            "entries without a readable email": {"account_invites": [{"id": "inv-1"}, 42, {"email_address": None}],
                                                 "errored_emails": []},
            "ours only in already_member_emails": {"account_invites": [], "errored_emails": [],
                                                   "already_member_emails": ["user@example.com"]},
        }
        for name, body in cases.items():
            with self.subTest(name):
                result = self._invite(body)
                self.assertEqual(result["_mutation_status"], "uncertain")
                self.assertTrue(result.get("error"))

    def test_unreadable_or_missing_account_invites_in_a_per_email_body_is_uncertain(self):
        cases = {
            "account_invites null": {"account_invites": None, "errored_emails": []},
            "account_invites a string": {"account_invites": "user@example.com", "errored_emails": []},
            "account_invites an object": {"account_invites": {"email_address": "user@example.com"},
                                          "errored_emails": []},
            "only errored_emails: []": {"errored_emails": []},
            "only already_member_emails": {"already_member_emails": ["user@example.com"]},
        }
        for name, body in cases.items():
            with self.subTest(name):
                result = self._invite(body)
                self.assertEqual(result["_mutation_status"], "uncertain")
                self.assertTrue(result.get("error"))

    def test_body_without_per_email_fields_is_still_confirmed(self):
        for body in ({}, {"ok": True}, ["unexpected", "list"]):
            with self.subTest(body=body):
                result = self._invite(body)
                self.assertEqual(result["_mutation_status"], "confirmed")
                self.assertNotIn("error", result)

        response = ok_response(None)
        response.json.side_effect = ValueError("broken json")
        client = ChatGPTClient("access", "team-1", "device-1")
        client.session.post = Mock(return_value=response)
        self.assertEqual(client.invite_member("user@example.com")["_mutation_status"], "confirmed")


# ── 3. 请求本身失败：超时不明确，明确的 4xx 才算拒绝 ─────────────────────────

class InviteMutationClassificationTest(unittest.TestCase):
    def setUp(self):
        self.client = ChatGPTClient("access", "team-1", "device-1")

    def test_timeout_is_uncertain(self):
        self.client.session.post = Mock(
            side_effect=Timeout("timed out", CurlECode.OPERATION_TIMEDOUT)
        )

        result = self.client.invite_member("user@example.com")

        self.assertEqual(result["_mutation_status"], "uncertain")

    def test_clear_400_rejection_can_be_retried(self):
        response = Response()
        response.status_code = 400
        response.ok = False
        self.client.session.post = Mock(return_value=response)

        result = self.client.invite_member("user@example.com")

        self.assertEqual(result["_mutation_status"], "rejected")


# ── 4. 兑换主路径：码必须退回 / 锁住 ──────────────────────────────────────────

def _redeem_team(team_id):
    return {"id": team_id, "name": team_id.upper(), "access_token": f"tok-{team_id}",
            "device_id": f"dev-{team_id}", "proxy_id": None}


class _RedemptionCase(unittest.IsolatedAsyncioTestCase):
    """驱动真实的 redeem_access_token；上游只替换成带假 session 的真 ChatGPTClient。

    临时库里有 team-1、team-2；邀请后的现拉名单始终看不到这个人。
    """

    async def asyncSetUp(self):
        self.db_path = await start_temp_db_async(self)
        for team_id in ("team-1", "team-2"):
            self._insert_team(team_id)
        # 每个测试的库都是新的、码 id 会重复；尝试预算是进程级单例，必须每个测试一份。
        budget = patch.object(
            access_tokens,
            "_redeem_lookup_budget",
            new=access_tokens._RedeemLookupBudget(
                per_code=100, per_code_window=3600, global_limit=100, global_window=600
            ),
        )
        budget.start()
        self.addCleanup(budget.stop)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_team(self, team_id):
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, created_at, updated_at)
               VALUES (?, ?, 'active', '2026-10-01', '2026-10-01')""",
            (team_id, team_id),
        )
        conn.commit()
        conn.close()

    def _expiry_rows(self, team_id):
        conn = self._conn()
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT user_id, email, expires_at, auto_kick, kicked, source
                   FROM member_expiry WHERE team_id = ? ORDER BY id""",
                (team_id,),
            )
        ]
        conn.close()
        return rows

    def _reconciliation_rows(self):
        conn = self._conn()
        rows = [dict(row) for row in conn.execute("SELECT * FROM pending_invite_reconciliations")]
        conn.close()
        return rows

    async def _make_token(self, raw_token):
        async with app_database.get_db() as db:
            cursor = await db.execute(
                """INSERT INTO access_tokens
                   (token_hash, token_prefix, grant_expires_in, max_uses,
                    used_count, disabled, created_at)
                   VALUES (?, 'atm_err', '30d', 1, 0, 0, '2026-10-01T00:00:00+00:00')""",
                (access_tokens._hash_token(raw_token),),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def _redeem(self, raw_token, bodies, teams):
        """bodies: team_id -> 邀请接口的 2xx 响应体。返回 (结果, HTTPException, 发过邀请的 Team)。"""
        posts = []

        def _client_factory(access_token, team_id, device_id, proxy_url=None):
            client = ChatGPTClient(access_token, team_id, device_id, proxy_url=proxy_url)

            def _post(url, **kwargs):
                posts.append(team_id)
                return ok_response(bodies[team_id])

            client.session.post = _post
            return client

        with (
            patch.object(access_tokens, "_check_rate_limit", new=AsyncMock()),
            patch.object(access_tokens, "load_active_teams", new=AsyncMock(return_value=teams)),
            patch.object(access_tokens, "_find_all_memberships", new=AsyncMock(return_value=[])),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "_chatgpt_available", new=AsyncMock(return_value=(True, "ok"))),
            patch.object(access_tokens, "ChatGPTClient", _client_factory),
            patch.object(access_tokens, "run_chatgpt_call", new=direct_call),
            patch.object(access_tokens, "fetch_and_cache_members",
                         new=AsyncMock(return_value={"members": [], "pending_invites": []})),
            patch.object(access_tokens, "add_member_watch", new=AsyncMock()),
            patch.object(access_tokens, "reserve_default_seat", new=AsyncMock()),
            patch.object(access_tokens, "notify_member_event", new=AsyncMock()),
        ):
            try:
                result = await access_tokens.redeem_access_token(
                    access_tokens.RedeemAccessTokenRequest(email=EMAIL, token=raw_token), Mock()
                )
                return result, None, posts
            except HTTPException as exc:
                return None, exc, posts

    async def _token_state(self, token_id):
        async with app_database.get_db() as db:
            token = await (await db.execute(
                "SELECT used_count FROM access_tokens WHERE id = ?", (token_id,)
            )).fetchone()
            use = await (await db.execute(
                "SELECT result, team_id FROM access_token_uses WHERE token_id = ? ORDER BY id DESC",
                (token_id,),
            )).fetchone()
        return token["used_count"], dict(use)


class RedemptionErroredEmailTest(_RedemptionCase):
    async def test_errored_email_releases_the_code(self):
        raw = "atm_errored_release"
        token_id = await self._make_token(raw)
        body = {"account_invites": [], "errored_emails": [{"email_address": EMAIL, "error": "Invalid email"}]}

        result, exc, posts = await self._redeem(raw, {"team-1": body}, [_redeem_team("team-1")])

        self.assertIsNone(result)
        self.assertEqual(exc.status_code, 409)
        used_count, use = await self._token_state(token_id)
        self.assertEqual(used_count, 0, "上游没发出邀请，兑换码必须退回")
        self.assertEqual(use["result"], "failed")
        self.assertEqual(posts, ["team-1"])
        self.assertEqual(self._expiry_rows("team-1"), [])
        self.assertEqual(self._reconciliation_rows(), [])

    async def test_errored_email_moves_on_to_the_next_team(self):
        raw = "atm_errored_next"
        token_id = await self._make_token(raw)
        bodies = {
            "team-1": {"account_invites": [],
                       "errored_emails": [{"email_address": EMAIL, "error": "Invalid email"}]},
            "team-2": {"account_invites": [{"email_address": EMAIL}], "errored_emails": []},
        }

        result, exc, posts = await self._redeem(
            raw, bodies, [_redeem_team("team-1"), _redeem_team("team-2")]
        )

        self.assertIsNone(exc)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["team_id"], "team-2")
        self.assertEqual(posts, ["team-1", "team-2"])
        used_count, use = await self._token_state(token_id)
        self.assertEqual(used_count, 1)
        self.assertEqual(use["result"], "success")
        self.assertEqual(self._expiry_rows("team-1"), [])
        self.assertEqual(len(self._expiry_rows("team-2")), 1)

    async def test_ambiguous_errored_emails_keeps_the_code_locked_to_its_team(self):
        raw = "atm_errored_ambiguous"
        token_id = await self._make_token(raw)
        bodies = {
            "team-1": {"account_invites": [], "errored_emails": [{"error": "unknown"}]},
            "team-2": {"account_invites": [{"email_address": EMAIL}], "errored_emails": []},
        }

        result, exc, posts = await self._redeem(
            raw, bodies, [_redeem_team("team-1"), _redeem_team("team-2")]
        )

        self.assertIsNone(exc)
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(result["team_id"], "team-1")
        self.assertEqual(posts, ["team-1"], "结果不明确时不能换 Team 再发一次")
        used_count, use = await self._token_state(token_id)
        self.assertEqual(used_count, 1)
        self.assertEqual(use["result"], "uncertain")
        barrier = self._reconciliation_rows()
        self.assertEqual(len(barrier), 1)
        self.assertEqual(barrier[0]["kind"], "barrier")
        self.assertEqual(barrier[0]["team_id"], "team-1")


class RedemptionAccountInvitesMissingEmailTest(_RedemptionCase):
    """这种 2xx 必须把码锁在原 Team，不能换 Team 再发。"""

    async def test_missing_email_in_account_invites_keeps_the_code_locked_to_its_team(self):
        raw = "atm_g4_missing_account_invite"
        token_id = await self._make_token(raw)
        bodies = {
            "team-1": {"account_invites": [], "errored_emails": [], "already_member_emails": []},
            "team-2": {"account_invites": [{"email_address": EMAIL}], "errored_emails": []},
        }

        # 邀请后立即现拉一次原 Team：还看不到这个人，结果仍不明确。
        result, exc, posts = await self._redeem(
            raw, bodies, [_redeem_team("team-1"), _redeem_team("team-2")]
        )

        self.assertIsNone(exc)
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(result["team_id"], "team-1")
        self.assertEqual(posts, ["team-1"], "结果不明确时不能换 Team 再发一次")
        used_count, use = await self._token_state(token_id)
        self.assertEqual(used_count, 1, "邀请可能已到 OpenAI，码必须保持已消耗")
        self.assertEqual(use, {"result": "uncertain", "team_id": "team-1"})
        self.assertEqual(self._expiry_rows("team-1"), [])
        self.assertEqual(self._expiry_rows("team-2"), [])
        barrier = self._reconciliation_rows()
        self.assertEqual(len(barrier), 1)
        self.assertEqual((barrier[0]["team_id"], barrier[0]["kind"]), ("team-1", "barrier"))


if __name__ == "__main__":
    unittest.main()
