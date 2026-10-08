"""管理员拉人与邀请结果分类的回归测试。

1. 管理员（网页 / Telegram /invite）对已在该 Team 的邮箱再发邀请，会把成员已有
   的时长覆盖成这次填的有效期（剩 300 天 → 30 天；永久 → 30 天并装上自动踢）。
   现在邀请前在 team_invite_lock 里实时拉一次成员 + 待接受邀请：已是成员则 409，
   待接受的邀请按重发处理（到期只合并不缩短），拉不到则失败关闭；批量 GPT 拉人同理，不能拿缓存快照当"不在"的证据。
   record_confirmed_invite 本身也不再缩短 / 替换已有到期（纵深防御）。
2. 邀请接口回 2xx、但本次邮箱列在 ``errored_emails`` 里：上游没有发出邀请，却被
   标成 confirmed，兑换码被消耗而人没进 Team。现在本次邮箱被明确列出 → rejected
   （带 error，兑换码退回、换下一个 Team）；任何对不上的形状 → uncertain（锁码
   等对账），绝不 confirmed。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import direct_call, start_temp_db, start_temp_db_async

from app import database as app_database
from app import tg_bot
from app.chatgpt_client import ChatGPTClient
from app.models import InviteMemberRequest
from app.routes import access_tokens, members
from app.scheduler import _reconcile_pending_invites_sync
from app.services import gpt_invites, member_expiry
from app.services.member_expiry import record_confirmed_invite


UTC = timezone.utc
EMAIL = "member@example.com"


class _TempDb:
    """真实 init_database() 建表，落在临时目录里。"""

    def _start_db(self):
        self.db_path = start_temp_db(self)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_team(self, team_id="team-1"):
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, created_at, updated_at)
               VALUES (?, ?, 'active', '2026-10-01', '2026-10-01')""",
            (team_id, team_id),
        )
        conn.commit()
        conn.close()

    def _insert_expiry(self, expires_at, *, source="self_service", team_id="team-1", user_id="u-1"):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, source,
                first_seen_at, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, '2026-10-01', '2026-10-01')""",
            (team_id, user_id, EMAIL, expires_at, 1 if expires_at else 0, source),
        )
        conn.commit()
        conn.close()

    def _expiry_rows(self, team_id="team-1"):
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


class _InviteClient:
    """上游客户端替身：真正的邀请调用一律记下来，测试据此断言"有没有发出去"。"""

    def __init__(self):
        self.invites = []

    def invite_member(self, email, seat_type="default"):
        self.invites.append((email, seat_type))
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


# ── 管理员单个拉人：POST /api/teams/{team_id}/members/invite ───────────────

class AdminReinviteGuardTest(_TempDb, unittest.TestCase):
    def setUp(self):
        self._start_db()
        self._insert_team()
        self.future = (datetime.now(UTC) + timedelta(days=300)).replace(microsecond=0).isoformat()

    def _invite(self, snapshot=None, *, fetch_error=None, expires_in="30d", email=EMAIL):
        client = _InviteClient()
        fetch = AsyncMock(side_effect=fetch_error) if fetch_error else AsyncMock(return_value=snapshot)
        patches = [
            patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(members, "fetch_and_cache_members", new=fetch),
            patch.object(members, "_ensure_default_seat_available", new=AsyncMock()),
            patch.object(members, "run_chatgpt_call", new=direct_call),
            patch.object(members, "add_member_watch", new=AsyncMock()),
            patch.object(members, "reserve_default_seat", new=AsyncMock()),
            patch.object(members, "notify_member_event", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            result = asyncio.run(
                members.invite_member(
                    "team-1", InviteMemberRequest(email=email, expires_in=expires_in)
                )
            )
            return result, client, None
        except HTTPException as exc:
            return None, client, exc
        finally:
            for p in patches:
                p.stop()

    def test_existing_member_is_refused_before_any_upstream_mutation(self):
        self._insert_expiry(self.future)
        before = self._expiry_rows()

        _result, client, exc = self._invite(
            {"members": [{"id": "u-1", "email": "Member@Example.com"}], "pending_invites": []},
            email="MEMBER@example.com",
        )

        self.assertIsNotNone(exc, "已在 Team 的成员不能被再邀请一次")
        self.assertEqual(exc.status_code, 409)
        self.assertIsInstance(exc.detail, str)
        self.assertIn("续期", exc.detail)
        self.assertIn("设置到期", exc.detail)
        self.assertEqual(client.invites, [])
        self.assertEqual(self._expiry_rows(), before)
        self.assertEqual(self._reconciliation_rows(), [])

    def test_permanent_member_keeps_permanence(self):
        self._insert_expiry(None, source="system")
        before = self._expiry_rows()

        _result, client, exc = self._invite(
            {"members": [{"id": "u-1", "email": EMAIL}], "pending_invites": []},
        )

        self.assertEqual(getattr(exc, "status_code", None), 409)
        self.assertEqual(client.invites, [])
        self.assertEqual(self._expiry_rows(), before)

    def test_pending_invite_is_resent_without_shortening(self):
        """待接受的邀请直接重发；详细用例见 test_admin_invite_resend_pending。"""
        self._insert_expiry(self.future)
        before = self._expiry_rows()

        _result, client, exc = self._invite(
            {"members": [], "pending_invites": [{"email": "member@EXAMPLE.com"}]},
        )

        self.assertIsNone(exc)
        self.assertEqual(client.invites, [(EMAIL, "default")])
        self.assertEqual(self._expiry_rows(), before)
        self.assertEqual(self._reconciliation_rows(), [])

    def test_member_lookup_failure_fails_closed(self):
        self._insert_expiry(self.future)
        before = self._expiry_rows()

        _result, client, exc = self._invite(
            fetch_error=HTTPException(status_code=502, detail="Upstream response has no usable list field"),
        )

        self.assertIsNotNone(exc, "拉不到成员名单时不能当作'不在 Team'去发邀请")
        self.assertEqual(exc.status_code, 502)
        self.assertIn("未发送邀请", exc.detail)
        self.assertEqual(client.invites, [])
        self.assertEqual(self._expiry_rows(), before)

    def test_absent_email_is_still_invited(self):
        result, client, exc = self._invite({"members": [], "pending_invites": []})

        self.assertIsNone(exc)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.invites, [(EMAIL, "default")])
        rows = self._expiry_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["expires_at"])
        self.assertEqual(rows[0]["auto_kick"], 1)

    def test_refusal_reaches_the_telegram_reply_verbatim(self):
        """TG /invite 走的是同一个 HTTP 接口，409 的 detail 必须原样出现在回复里。"""
        self._insert_expiry(self.future)
        _result, _client, exc = self._invite(
            {"members": [{"id": "u-1", "email": EMAIL}], "pending_invites": []},
        )
        self.assertIsNotNone(exc)

        response = Mock(status_code=409)
        response.json.return_value = {"detail": exc.detail}  # FastAPI 对 HTTPException 的响应体
        http_error = Exception("409 Client Error")
        http_error.response = response

        self.assertEqual(tg_bot._extract_api_error(http_error), exc.detail[:300])


# ── 批量 GPT 拉人：缓存快照不能当"不在"的证据 ─────────────────────────────

def _gpt_team(team_id):
    return {
        "id": team_id,
        "name": team_id,
        "owner_email": f"owner-{team_id}@example.com",
        "seats_in_use": 1,
        "seats_entitled": 5,
        "codex_count": 0,
        "chatgpt_count": 1,
        "created_at": "2026-10-01T00:00:00+00:00",
        "active_until": None,
        "will_renew": 1,
    }


class GptBatchInviteStaleSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def _invite_to_team(self, live_snapshot=None, *, fetch_error=None):
        client = _InviteClient()
        fetch = AsyncMock(side_effect=fetch_error) if fetch_error else AsyncMock(return_value=live_snapshot)
        record = AsyncMock(return_value=None)
        with (
            patch.object(gpt_invites, "find_open_redemption", new=AsyncMock(return_value=None)),
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(gpt_invites, "fetch_and_cache_members", new=fetch),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "record_confirmed_invite", new=record),
            patch.object(gpt_invites, "record_uncertain_invite", new=AsyncMock()),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "reserve_default_seat", new=AsyncMock()),
            patch.object(gpt_invites, "notify_member_event", new=AsyncMock()),
            patch.object(gpt_invites, "log_operation", new=AsyncMock()),
        ):
            added, error = await gpt_invites._invite_to_team(
                _gpt_team("team-a"),
                EMAIL,
                None,
                check_capacity=True,
                action="invite_gpt_member",
                # 调用方传进来的缓存快照是旧的：里面还没有这个人。
                cached_snapshot={"members": [], "pending_invites": []},
            )
        return added, error, client, record

    async def test_stale_cache_cannot_invite_a_live_member(self):
        added, error, client, record = await self._invite_to_team(
            {"members": [{"email": "Member@Example.com"}], "pending_invites": []},
        )

        self.assertIsNone(added)
        self.assertEqual(error, gpt_invites.EMAIL_ALREADY_IN_TEAM)
        self.assertEqual(client.invites, [])
        record.assert_not_awaited()

    async def test_stale_cache_cannot_invite_a_live_pending_invite(self):
        added, error, client, record = await self._invite_to_team(
            {"members": [], "pending_invites": [{"email": EMAIL}]},
        )

        self.assertIsNone(added)
        self.assertEqual(error, gpt_invites.EMAIL_ALREADY_IN_TEAM)
        self.assertEqual(client.invites, [])

    async def test_live_lookup_failure_fails_closed(self):
        added, error, client, record = await self._invite_to_team(
            fetch_error=HTTPException(status_code=502, detail="pagination did not terminate"),
        )

        self.assertIsNone(added)
        self.assertTrue(error)
        self.assertEqual(client.invites, [])
        record.assert_not_awaited()

    async def test_absent_email_is_still_invited(self):
        added, error, client, record = await self._invite_to_team(
            {"members": [], "pending_invites": []},
        )

        self.assertIsNone(error)
        self.assertEqual(added["team_id"], "team-a")
        self.assertEqual(client.invites, [(EMAIL, "default")])
        record.assert_awaited_once()


# ── 纵深防御：确认成功的邀请落库绝不缩短 / 替换已有到期 ─────────────────────

class ConfirmedInviteNeverShortensTest(_TempDb, unittest.TestCase):
    def setUp(self):
        self._start_db()
        self._insert_team()
        self.now = datetime.now(UTC).replace(microsecond=0)

    def _record(self, expires_at, *, user_id=""):
        return asyncio.run(record_confirmed_invite("team-1", user_id, EMAIL, expires_at))

    def test_longer_existing_expiry_is_kept(self):
        existing = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(existing)

        returned = self._record(self.now + timedelta(days=30))

        rows = self._expiry_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expires_at"], existing)
        self.assertEqual(rows[0]["auto_kick"], 1)
        self.assertEqual(rows[0]["user_id"], "u-1")
        self.assertEqual(returned, existing)

    def test_authorized_permanent_row_stays_permanent(self):
        self._insert_expiry(None, source="system")

        returned = self._record(self.now + timedelta(days=30))

        rows = self._expiry_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["expires_at"])
        self.assertEqual(rows[0]["auto_kick"], 0)
        self.assertEqual(rows[0]["source"], "system")
        self.assertIsNone(returned)

    def test_detected_null_row_takes_the_invite_expiry(self):
        """detected + NULL 只是"外部发现、尚未授权"，不是永久。"""
        self._insert_expiry(None, source="detected")
        new = self.now + timedelta(days=30)

        returned = self._record(new)

        rows = self._expiry_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expires_at"], new.isoformat())
        self.assertEqual(rows[0]["auto_kick"], 1)
        self.assertEqual(rows[0]["source"], "system")
        self.assertEqual(returned, new.isoformat())

    def test_later_invite_expiry_extends_a_shorter_one(self):
        self._insert_expiry((self.now + timedelta(days=5)).isoformat())
        new = self.now + timedelta(days=30)

        self._record(new)

        self.assertEqual(self._expiry_rows()[0]["expires_at"], new.isoformat())

    def test_permanent_invite_upgrades_a_dated_row(self):
        self._insert_expiry((self.now + timedelta(days=5)).isoformat())

        self._record(None)

        row = self._expiry_rows()[0]
        self.assertIsNone(row["expires_at"])
        self.assertEqual(row["auto_kick"], 0)

    def test_no_existing_row_creates_one(self):
        new = self.now + timedelta(days=30)

        self._record(new)

        rows = self._expiry_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expires_at"], new.isoformat())
        self.assertEqual(rows[0]["source"], "system")

    def test_fallback_backfill_row_resolves_without_shortening(self):
        """主写入失败 → 兜底行 kind='backfill'，调度器按 max 结清，结果与主写入一致。"""
        existing = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(existing)

        with (
            patch.object(member_expiry, "_CONFIRM_WRITE_BACKOFF_SECONDS", 0),
            patch.object(member_expiry, "_merge_confirmed_invite_expiry",
                         new=AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))),
        ):
            self._record(self.now + timedelta(days=30))

        pending = self._reconciliation_rows()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "backfill")
        self.assertEqual(self._expiry_rows()[0]["expires_at"], existing)

        conn = self._conn()
        _reconcile_pending_invites_sync(
            conn, "team-1", [{"id": "u-1", "email": EMAIL}], [], self.now.isoformat()
        )
        conn.commit()
        conn.close()
        self.assertEqual(self._expiry_rows()[0]["expires_at"], existing)



# ── 2xx 但本次邮箱在 errored_emails 里：不是成功 ─────────────────────────────

def _ok_response(body):
    """一个 2xx 响应替身。body 形状取自 ChatGPT 管理页前端读取的字段：
    ``account_invites`` / ``errored_emails[{email_address, error}]`` /
    ``already_member_emails``。"""
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def _client_returning(body):
    client = ChatGPTClient("access", "team-1", "device-1")
    client.session.post = Mock(return_value=_ok_response(body))
    return client


class InviteErroredEmailsClassificationTest(unittest.TestCase):
    def _invite(self, body, email="user@example.com"):
        return _client_returning(body).invite_member(email)

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


class AdminInviteErroredEmailTest(_TempDb, unittest.TestCase):
    """管理员拉人：rejected 报失败，不落成员记录；uncertain 走原有的不确定处理。"""

    def setUp(self):
        self._start_db()
        self._insert_team()

    def _invite(self, body):
        client = _client_returning(body)
        patches = [
            patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(members, "fetch_and_cache_members",
                         new=AsyncMock(return_value={"members": [], "pending_invites": []})),
            patch.object(members, "_ensure_default_seat_available", new=AsyncMock()),
            patch.object(members, "run_chatgpt_call", new=direct_call),
            patch.object(members, "add_member_watch", new=AsyncMock()),
            patch.object(members, "reserve_default_seat", new=AsyncMock()),
            patch.object(members, "notify_member_event", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(members.invite_member("team-1", InviteMemberRequest(email=EMAIL, expires_in="30d")))
        finally:
            for p in patches:
                p.stop()
        return raised.exception

    def test_rejected_errored_email_is_reported_as_failure(self):
        exc = self._invite({"account_invites": [],
                            "errored_emails": [{"email_address": EMAIL, "error": "Invalid email"}]})

        self.assertEqual(exc.status_code, 502)
        self.assertIn("Invalid email", exc.detail)
        self.assertEqual(self._expiry_rows(), [])
        self.assertEqual(self._reconciliation_rows(), [])

    def test_ambiguous_errored_emails_takes_the_uncertain_path(self):
        exc = self._invite({"account_invites": [], "errored_emails": "?"})

        self.assertEqual(exc.status_code, 409)
        self.assertIn("确认中", exc.detail)
        self.assertEqual(self._expiry_rows(), [])
        pending = self._reconciliation_rows()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["email"], EMAIL)


class GptBatchInviteErroredEmailTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_errored_email_is_not_recorded_as_added(self):
        client = _client_returning(
            {"account_invites": [], "errored_emails": [{"email_address": EMAIL, "error": "Invalid email"}]}
        )
        record = AsyncMock(return_value=None)
        with (
            patch.object(gpt_invites, "find_open_redemption", new=AsyncMock(return_value=None)),
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(gpt_invites, "fetch_and_cache_members",
                         new=AsyncMock(return_value={"members": [], "pending_invites": []})),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "record_confirmed_invite", new=record),
            patch.object(gpt_invites, "log_operation", new=AsyncMock()),
        ):
            added, error = await gpt_invites._invite_to_team(
                _gpt_team("team-a"), EMAIL, None, check_capacity=True, action="invite_gpt_member",
            )

        self.assertIsNone(added)
        self.assertIn("Invalid email", error)
        record.assert_not_awaited()


# ── 自助兑换完整流程：码必须退回 / 锁住 ────────────────────────────────────

def _redeem_team(team_id):
    return {"id": team_id, "name": team_id.upper(), "access_token": f"tok-{team_id}",
            "device_id": f"dev-{team_id}", "proxy_id": None}


class RedemptionErroredEmailTest(_TempDb, unittest.IsolatedAsyncioTestCase):
    """驱动真实的 redeem_access_token；上游只替换成带假 session 的真 ChatGPTClient。"""

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
        """bodies: team_id -> 邀请接口的 2xx 响应体。"""
        posts = []

        def _client_factory(access_token, team_id, device_id, proxy_url=None):
            client = ChatGPTClient(access_token, team_id, device_id, proxy_url=proxy_url)

            def _post(url, **kwargs):
                posts.append(team_id)
                return _ok_response(bodies[team_id])

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


if __name__ == "__main__":
    unittest.main()
