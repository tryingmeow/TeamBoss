"""邀请结果分类的回归测试。

1. 邀请接口回 2xx、``errored_emails`` 为空，但 ``account_invites`` 列表里没有本次
   邮箱：上游没说给这个邮箱建了邀请，却被当成 confirmed（兑换码被消耗、记下到期，
   人可能根本没被邀请）。现在只要响应带了逐个邮箱的结果字段，就必须在
   ``account_invites`` 里读到本次邮箱才算 confirmed，否则 uncertain（邀请可能已经
   到了 OpenAI，不能当成拒绝）。完全不带这些字段的响应（空体 / 坏 JSON）保持原先
   的 confirmed。
"""

import _isolation  # noqa: F401  must precede any app import
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.chatgpt_client import ChatGPTClient
from app.routes import access_tokens


EMAIL = "member@example.com"


def _ok_response(body):
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


async def _direct_call(func, *args, **kwargs):
    return func(*args, **kwargs)


# ── 1. 2xx 但 account_invites 里没有本次邮箱：不是 confirmed ─────────────────

class InviteAccountInvitesClassificationTest(unittest.TestCase):
    def _invite(self, body, email="user@example.com"):
        client = ChatGPTClient("access", "team-1", "device-1")
        client.session.post = Mock(return_value=_ok_response(body))
        return client.invite_member(email)

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

        response = _ok_response(None)
        response.json.side_effect = ValueError("broken json")
        client = ChatGPTClient("access", "team-1", "device-1")
        client.session.post = Mock(return_value=response)
        self.assertEqual(client.invite_member("user@example.com")["_mutation_status"], "confirmed")

    def test_errored_email_classification_is_unchanged(self):
        ours = {"email_address": "user@example.com", "error": "Invalid email"}
        rejected = self._invite({"account_invites": [], "errored_emails": [ours]})
        self.assertEqual(rejected["_mutation_status"], "rejected")
        self.assertIn("Invalid email", rejected["error"])

        conflicting = self._invite({"account_invites": [{"email_address": "user@example.com"}],
                                    "errored_emails": [ours]})
        self.assertEqual(conflicting["_mutation_status"], "uncertain")

        foreign = self._invite({"account_invites": [], "errored_emails": ["other@example.com"]})
        self.assertEqual(foreign["_mutation_status"], "uncertain")

    def test_http_error_classification_is_unchanged(self):
        client = ChatGPTClient("access", "team-1", "device-1")
        client.session.post = Mock(side_effect=requests.HTTPError("bad", response=Mock(status_code=400)))
        self.assertEqual(client.invite_member("user@example.com")["_mutation_status"], "rejected")
        client.session.post = Mock(side_effect=requests.Timeout("slow"))
        self.assertEqual(client.invite_member("user@example.com")["_mutation_status"], "uncertain")


# ── 共用：真实 init_database() 建表，落在临时目录里 ───────────────────────────

class _TempDbAsync:
    async def _start_db(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        await app_database.init_database()
        self.db_path = app_database.get_db_path()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_team(self, team_id, *, created_at, members=(), pending=()):
        import json

        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, owner_email, access_token, device_id,
                                  seats_in_use, seats_entitled, codex_count, chatgpt_count,
                                  active_until, will_renew, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?, ?, 1, 5, 0, 1, NULL, 1, ?, ?)""",
            (team_id, team_id.upper(), f"owner-{team_id}@example.com", f"tok-{team_id}",
             f"dev-{team_id}", created_at, created_at),
        )
        conn.execute(
            "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, ?, ?, ?)",
            (team_id, json.dumps(list(members)), json.dumps(list(pending)), created_at),
        )
        conn.commit()
        conn.close()

    def _rows(self, sql, *params):
        conn = self._conn()
        rows = [dict(row) for row in conn.execute(sql, params)]
        conn.close()
        return rows

    def _expiry_rows(self, team_id):
        return self._rows("SELECT team_id, email, expires_at FROM member_expiry WHERE team_id = ?", team_id)

    def _reconciliation_rows(self):
        return self._rows("SELECT team_id, email, kind, resolved FROM pending_invite_reconciliations")


# ── 1b. 兑换主路径：这种 2xx 必须把码锁在原 Team，不能换 Team 再发 ──────────

class RedemptionAccountInvitesMissingEmailTest(_TempDbAsync, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self._start_db()
        self._insert_team("team-1", created_at="2026-10-01T00:00:00+00:00")
        self._insert_team("team-2", created_at="2026-10-02T00:00:00+00:00")
        budget = patch.object(
            access_tokens,
            "_redeem_lookup_budget",
            new=access_tokens._RedeemLookupBudget(
                per_code=100, per_code_window=3600, global_limit=100, global_window=600
            ),
        )
        budget.start()
        self.addCleanup(budget.stop)

    async def test_missing_email_in_account_invites_keeps_the_code_locked_to_its_team(self):
        raw = "atm_g4_missing_account_invite"
        async with app_database.get_db() as db:
            cursor = await db.execute(
                """INSERT INTO access_tokens
                   (token_hash, token_prefix, grant_expires_in, max_uses,
                    used_count, disabled, created_at)
                   VALUES (?, 'atm_g4', '30d', 1, 0, 0, '2026-10-01T00:00:00+00:00')""",
                (access_tokens._hash_token(raw),),
            )
            await db.commit()
            token_id = int(cursor.lastrowid)

        bodies = {
            "team-1": {"account_invites": [], "errored_emails": [], "already_member_emails": []},
            "team-2": {"account_invites": [{"email_address": EMAIL}], "errored_emails": []},
        }
        posts = []

        def _client_factory(access_token, team_id, device_id, proxy_url=None):
            client = ChatGPTClient(access_token, team_id, device_id, proxy_url=proxy_url)

            def _post(url, **kwargs):
                posts.append(team_id)
                return _ok_response(bodies[team_id])

            client.session.post = _post
            return client

        teams = [
            {"id": t, "name": t.upper(), "access_token": f"tok-{t}", "device_id": f"dev-{t}", "proxy_id": None}
            for t in ("team-1", "team-2")
        ]
        with (
            patch.object(access_tokens, "_check_rate_limit", new=AsyncMock()),
            patch.object(access_tokens, "load_active_teams", new=AsyncMock(return_value=teams)),
            patch.object(access_tokens, "_find_all_memberships", new=AsyncMock(return_value=[])),
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "_chatgpt_available", new=AsyncMock(return_value=(True, "ok"))),
            patch.object(access_tokens, "ChatGPTClient", _client_factory),
            patch.object(access_tokens, "run_chatgpt_call", new=_direct_call),
            # 邀请后立即现拉一次原 Team：还看不到这个人，结果仍不明确。
            patch.object(access_tokens, "fetch_and_cache_members",
                         new=AsyncMock(return_value={"members": [], "pending_invites": []})),
            patch.object(access_tokens, "add_member_watch", new=AsyncMock()),
            patch.object(access_tokens, "reserve_default_seat", new=AsyncMock()),
            patch.object(access_tokens, "notify_member_event", new=AsyncMock()),
        ):
            result = await access_tokens.redeem_access_token(
                access_tokens.RedeemAccessTokenRequest(email=EMAIL, token=raw), Mock()
            )

        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(result["team_id"], "team-1")
        self.assertEqual(posts, ["team-1"], "结果不明确时不能换 Team 再发一次")
        token = self._rows("SELECT used_count FROM access_tokens WHERE id = ?", token_id)[0]
        use = self._rows("SELECT result, team_id FROM access_token_uses WHERE token_id = ?", token_id)[0]
        self.assertEqual(token["used_count"], 1, "邀请可能已到 OpenAI，码必须保持已消耗")
        self.assertEqual(use, {"result": "uncertain", "team_id": "team-1"})
        self.assertEqual(self._expiry_rows("team-1"), [])
        self.assertEqual(self._expiry_rows("team-2"), [])
        barrier = self._reconciliation_rows()
        self.assertEqual(len(barrier), 1)
        self.assertEqual((barrier[0]["team_id"], barrier[0]["kind"]), ("team-1", "barrier"))


if __name__ == "__main__":
    unittest.main()
