"""管理员拉人与改动成员的回归测试。

拉人走 POST /api/teams/{team_id}/members/invite（网页与 Telegram /invite 共用）；
改动走 set_expiry / remove_member / revoke_invite。

1. 对已在该 Team 的邮箱再发邀请，会把成员已有的时长覆盖成这次填的有效期（剩 300 天
   → 30 天；永久 → 30 天并装上自动踢）。现在邀请前在 team_invite_lock 里实时拉一次
   成员 + 待接受邀请：已是成员则 409，拉不到则失败关闭。
2. 邮箱在该 Team 已有待接受的邀请时，接口曾回 409 并让管理员"先撤销再邀请"；撤销会把
   本地记录标成 kicked，已授予时长 / 永久授权随之丢失。现在直接重发：本地到期按
   record_confirmed_invite 的合并规则只延长、不缩短、不把永久变有限。
   record_confirmed_invite 本身也不缩短 / 替换已有到期（纵深防御）。
3. 合并后实际落库的到期（例如一条仍未踢出的永久记录被保留）与接口响应、Telegram
   卡片上写的有效期必须一致，不能照抄这次填写的 30d。
4. 邀请接口回 2xx、但本次邮箱列在 ``errored_emails`` 里：报失败、不落成员记录；
   形状对不上的走不确定处理。分类本身见 test_invite_classification。
5. 管理员改动与自助续期、到期移除共用成员操作占用，在同一 Team 的同一邮箱上互斥。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db
from _invite_fixtures import InviteClient, client_returning, run_admin_invite

from app import tg_bot
from app.models import InviteMemberRequest, SetExpiryRequest
from app.routes import members
from app.scheduler import _reconcile_pending_invites_sync
from app.services import member_expiry
from app.services.member_expiry import record_confirmed_invite
from app.services.team_locks import member_operation_claim


UTC = timezone.utc
LOCAL_TZ = timezone(timedelta(hours=8))
EMAIL = "member@example.com"
ABSENT = {"members": [], "pending_invites": []}


def _pending(email=EMAIL, seat_type="default"):
    return {"members": [], "pending_invites": [{"email": email, "seat_type": seat_type}]}


def _member(email=EMAIL):
    return {"members": [{"id": "u-1", "email": email}], "pending_invites": []}


def _display_of(stored_iso):
    """卡片上"有效期"应当以什么开头：永久，或北京时间的到期分钟。"""
    if stored_iso is None:
        return "永久"
    value = datetime.fromisoformat(stored_iso)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


class _AdminInviteDb(unittest.TestCase):
    """真实 init_database() 建表，落在临时目录里；Team team-1 已建好。"""

    def setUp(self):
        self.db_path = start_temp_db(self)
        self._insert_team()
        self.now = datetime.now(UTC).replace(microsecond=0)

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

    def _active_row(self):
        rows = [row for row in self._expiry_rows() if row["kicked"] == 0]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def _reconciliation_rows(self):
        conn = self._conn()
        rows = [dict(row) for row in conn.execute("SELECT * FROM pending_invite_reconciliations")]
        conn.close()
        return rows

    def _claims(self):
        conn = self._conn()
        rows = [dict(row) for row in conn.execute("SELECT * FROM member_operation_claims")]
        conn.close()
        return rows


class _AdminInviteRouteCase(_AdminInviteDb):
    def _invite(self, snapshot=None, *, fetch_error=None, expires_in="30d", email=EMAIL,
                seat_type="default"):
        """跑一次真实的 invite_member 路由；上游、现拉名单、席位检查和 Telegram 一律替身。

        返回 (响应, HTTPException)；上游替身在 self.client，席位检查在 self.seat_check，
        Telegram 通知在 self.notify。fetch_error：现拉名单时抛出的异常。
        """
        self.client = InviteClient()
        self.seat_check = AsyncMock()
        self.notify = AsyncMock()
        fetch = AsyncMock(side_effect=fetch_error) if fetch_error else AsyncMock(return_value=snapshot)
        try:
            return run_admin_invite(
                "team-1",
                InviteMemberRequest(email=email, expires_in=expires_in, seat_type=seat_type),
                client=self.client,
                fetch=fetch,
                seat_check=self.seat_check,
                notify=self.notify,
            ), None
        except HTTPException as exc:
            return None, exc

    def _success_card(self):
        self.notify.assert_awaited_once()
        args, kwargs = self.notify.await_args
        self.assertEqual(args[0], "后台拉人")
        self.assertEqual(kwargs.get("result", "success"), "success")
        return kwargs["detail"]

    def _telegram_reply(self, response, expires_in="30d"):
        """Telegram /invite 的结果卡片：_invite_worker 拿到同一个接口的响应后渲染。"""
        edit = Mock()
        with (
            patch.object(tg_bot, "_api_post", new=Mock(return_value=response)),
            patch.object(tg_bot, "_update_watch_tg_info", new=Mock()),
            patch.object(tg_bot, "_edit_message", new=edit),
        ):
            tg_bot._invite_worker("chat-1", 42, "team-1", EMAIL, expires_in, False, "Team One")
        edit.assert_called_once()
        return edit.call_args.args[2]


# ── 1. 已是成员：409，什么都不动；拉不到名单：失败关闭 ─────────────────────────

class AdminReinviteGuardTest(_AdminInviteRouteCase):
    def setUp(self):
        super().setUp()
        self.future = (datetime.now(UTC) + timedelta(days=300)).replace(microsecond=0).isoformat()

    def test_existing_member_is_refused_before_any_upstream_mutation(self):
        self._insert_expiry(self.future)
        before = self._expiry_rows()

        _result, exc = self._invite(
            {"members": [{"id": "u-1", "email": "Member@Example.com"}], "pending_invites": []},
            email="MEMBER@example.com",
        )

        self.assertIsNotNone(exc, "已在 Team 的成员不能被再邀请一次")
        self.assertEqual(exc.status_code, 409)
        self.assertIsInstance(exc.detail, str)
        self.assertIn("续期", exc.detail)
        self.assertIn("设置到期", exc.detail)
        self.assertEqual(self.client.invites, [])
        self.assertEqual(self._expiry_rows(), before)
        self.assertEqual(self._reconciliation_rows(), [])
        self.assertEqual(self._claims(), [], "成员操作占用必须在请求结束时释放")

    def test_permanent_member_keeps_permanence(self):
        self._insert_expiry(None, source="system")
        before = self._expiry_rows()

        _result, exc = self._invite(
            {"members": [{"id": "u-1", "email": EMAIL}], "pending_invites": []},
        )

        self.assertEqual(getattr(exc, "status_code", None), 409)
        self.assertEqual(self.client.invites, [])
        self.assertEqual(self._expiry_rows(), before)

    def test_member_lookup_failure_fails_closed(self):
        self._insert_expiry(self.future)
        before = self._expiry_rows()

        _result, exc = self._invite(
            fetch_error=HTTPException(status_code=502, detail="Upstream response has no usable list field"),
        )

        self.assertIsNotNone(exc, "拉不到成员名单时不能当作'不在 Team'去发邀请")
        self.assertEqual(exc.status_code, 502)
        self.assertIn("未发送邀请", exc.detail)
        self.assertEqual(self.client.invites, [])
        self.assertEqual(self._expiry_rows(), before)

    def test_absent_email_is_still_invited(self):
        result, exc = self._invite({"members": [], "pending_invites": []})

        self.assertIsNone(exc)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.client.invites, [(EMAIL, "default")])
        rows = self._expiry_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["expires_at"])
        self.assertEqual(rows[0]["auto_kick"], 1)

    def test_refusal_reaches_the_telegram_reply_verbatim(self):
        """TG /invite 走的是同一个 HTTP 接口，409 的 detail 必须原样出现在回复里。"""
        self._insert_expiry(self.future)
        _result, exc = self._invite(
            {"members": [{"id": "u-1", "email": EMAIL}], "pending_invites": []},
        )
        self.assertIsNotNone(exc)

        response = Mock(status_code=409)
        response.json.return_value = {"detail": exc.detail}  # FastAPI 对 HTTPException 的响应体
        http_error = Exception("409 Client Error")
        http_error.response = response

        self.assertEqual(tg_bot._extract_api_error(http_error), exc.detail[:300])


# ── 2. 待接受的邀请可以重发，到期只延长不缩短 ─────────────────────────────────

class PendingInviteResendTest(_AdminInviteRouteCase):
    def test_pending_invite_resend_keeps_longer_paid_expiry(self):
        paid = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(paid, source="self_service")
        before = self._expiry_rows()

        response, exc = self._invite(_pending(email="Member@EXAMPLE.com"), expires_in="30d")

        self.assertIsNone(exc, "待接受的邀请应当直接重发，不能让管理员去撤销")
        self.assertEqual(response["status"], "ok")
        self.assertTrue(response["resent"])
        self.assertEqual(self.client.invites, [(EMAIL, "default")])
        row = self._active_row()
        self.assertEqual(row["expires_at"], paid)
        self.assertEqual(row["auto_kick"], 1)
        self.assertEqual(row["source"], "self_service")
        self.assertEqual(self._expiry_rows(), before)
        self.assertEqual(self._reconciliation_rows(), [])
        self.assertEqual(response["expires_at"], paid)
        self.assertEqual(self._claims(), [], "成员操作占用必须在请求结束时释放")

    def test_pending_invite_resend_keeps_permanent_authorization(self):
        self._insert_expiry(None, source="system")

        response, exc = self._invite(_pending(), expires_in="30d")

        self.assertIsNone(exc)
        self.assertEqual(self.client.invites, [(EMAIL, "default")])
        row = self._active_row()
        self.assertIsNone(row["expires_at"], "重发邀请不能把永久授权变成 30 天")
        self.assertEqual(row["auto_kick"], 0)
        self.assertEqual(row["source"], "system")
        self.assertIsNone(response["expires_at"])

    def test_pending_invite_resend_never_shortens_any_request(self):
        """不管这次填多短，已存的到期都不会往前挪。"""
        paid = (self.now + timedelta(days=90)).isoformat()
        for expires_in in ("1h", "1d", "30d"):
            with self.subTest(expires_in=expires_in):
                self._insert_expiry(paid)
                try:
                    _response, exc = self._invite(_pending(), expires_in=expires_in)
                    self.assertIsNone(exc)
                    self.assertEqual(self._active_row()["expires_at"], paid)
                finally:
                    conn = self._conn()
                    conn.execute("DELETE FROM member_expiry")
                    conn.commit()
                    conn.close()

    def test_pending_invite_resend_does_not_ask_for_overage(self):
        """待接受的同类邀请已经占着那个席位，重发不新增席位，不该弹超额确认。"""
        self._insert_expiry((self.now + timedelta(days=10)).isoformat())

        _response, exc = self._invite(_pending(seat_type="default"))

        self.assertIsNone(exc)
        self.seat_check.assert_not_awaited()

    def test_absent_email_still_checks_capacity(self):
        response, exc = self._invite(ABSENT)

        self.assertIsNone(exc)
        self.assertFalse(response["resent"])
        self.seat_check.assert_awaited_once()

    def test_member_operation_in_progress_refuses_before_any_upstream_call(self):
        """巡逻 / 到期撤邀请正占着这个邮箱时，重发不能和它交错。"""
        paid = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(paid)
        before = self._expiry_rows()
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_operation_claims
               (operation_key, team_id, email, user_id, operation, owner_token,
                expires_at, created_at)
               VALUES (?, 'team-1', ?, '', 'patrol_revoke_invite', 'other-owner', ?, ?)""",
            (
                f"team-1|email:{EMAIL}",
                EMAIL,
                (self.now + timedelta(minutes=5)).isoformat(),
                self.now.isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        _response, exc = self._invite(_pending())

        self.assertIsNotNone(exc)
        self.assertEqual(exc.status_code, 409)
        self.assertIn("稍后重试", exc.detail)
        self.assertEqual(self.client.invites, [])
        self.assertEqual(self._expiry_rows(), before)
        self.assertEqual([c["owner_token"] for c in self._claims()], ["other-owner"])


# ── 3. 响应与 Telegram 卡片报告实际落库的到期 ────────────────────────────────

class InviteReportsStoredExpiryTest(_AdminInviteRouteCase):
    def _assert_reports_stored(self, response):
        row = self._active_row()
        stored = row["expires_at"]
        self.assertEqual(response["expires_at"], stored)
        expected = _display_of(stored)
        self.assertTrue(
            response["expiry_display"].startswith(expected),
            (response["expiry_display"], expected),
        )
        self.assertIn(f"有效期：{expected}", self._success_card())
        self.assertIn(f"⏳ 有效期：{expected}", self._telegram_reply(response))
        return stored

    def test_stale_permanent_row_is_reported_as_permanent(self):
        """本地仍有一条未踢出的已授权永久记录（人已不在 Team，同步还没跟上）。"""
        self._insert_expiry(None, source="system")

        response, exc = self._invite(ABSENT, expires_in="30d")

        self.assertIsNone(exc)
        stored = self._assert_reports_stored(response)
        self.assertIsNone(stored, "已授权的永久记录不能被一次有限期邀请降级")
        self.assertTrue(response["expiry_recorded"])

    def test_stale_detected_null_row_takes_the_invite_expiry(self):
        """detected + NULL 只是外部发现、未授权，不是永久：按这次邀请的到期落库。"""
        self._insert_expiry(None, source="detected")

        response, exc = self._invite(ABSENT, expires_in="30d")

        self.assertIsNone(exc)
        stored = self._assert_reports_stored(response)
        self.assertIsNotNone(stored)
        self.assertGreater(datetime.fromisoformat(stored), self.now + timedelta(days=29))
        self.assertNotIn("永久", response["expiry_display"])
        self.assertEqual(self._active_row()["source"], "system")

    def test_longer_existing_expiry_is_reported(self):
        paid = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(paid)

        response, exc = self._invite(ABSENT, expires_in="30d")

        self.assertIsNone(exc)
        self.assertEqual(self._assert_reports_stored(response), paid)

    def test_fresh_invite_reports_the_new_expiry(self):
        response, exc = self._invite(ABSENT, expires_in="30d")

        self.assertIsNone(exc)
        stored = self._assert_reports_stored(response)
        self.assertIsNotNone(stored)
        self.assertNotIn("未生效", response["expiry_display"])

    def test_permanent_invite_reports_permanent(self):
        self._insert_expiry((self.now + timedelta(days=5)).isoformat())

        response, exc = self._invite(ABSENT, expires_in="never")

        self.assertIsNone(exc)
        self.assertIsNone(self._assert_reports_stored(response))

    def test_failed_local_write_is_not_reported_as_stored(self):
        """本地写入全部失败、只留下兜底对账行时，不能把申请值当成已落库的到期。"""
        paid = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(paid)

        with (
            patch.object(member_expiry, "_CONFIRM_WRITE_BACKOFF_SECONDS", 0),
            patch.object(member_expiry, "_merge_confirmed_invite_expiry",
                         new=AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))),
        ):
            response, exc = self._invite(ABSENT, expires_in="30d")

        self.assertIsNone(exc, "上游邀请已成功，本地记账失败不能把请求变成失败")
        self.assertFalse(response["expiry_recorded"])
        self.assertIn("对账", response["expiry_display"])
        self.assertIn("对账", self._success_card())
        self.assertIn("对账", self._telegram_reply(response))
        self.assertEqual(self._active_row()["expires_at"], paid)


# ── 纵深防御：确认成功的邀请落库绝不缩短 / 替换已有到期 ─────────────────────

class ConfirmedInviteNeverShortensTest(_AdminInviteDb):
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


# ── 4. 2xx 但本次邮箱在 errored_emails 里：不是成功 ─────────────────────────

class AdminInviteErroredEmailTest(_AdminInviteDb):
    """管理员拉人：rejected 报失败，不落成员记录；uncertain 走原有的不确定处理。"""

    def _invite(self, body):
        with self.assertRaises(HTTPException) as raised:
            run_admin_invite(
                "team-1",
                InviteMemberRequest(email=EMAIL, expires_in="30d"),
                client=client_returning(body),
                fetch=AsyncMock(return_value={"members": [], "pending_invites": []}),
            )
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


# ── 5. 管理员改动与自助续期 / 到期移除在同一 Team 上互斥 ───────────────────────

class AdminMemberMutationClaimsTest(unittest.TestCase):
    """Admin changes serialize with paid renewal and expiry removal on the same Team."""

    def setUp(self):
        start_temp_db(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.resolve = self.stack.enter_context(patch.object(members, '_resolve_member_identity', new=AsyncMock(return_value=('user-1', 'member@example.com'))))
        self.open = self.stack.enter_context(patch.object(members, 'find_open_redemption', new=AsyncMock(return_value=None)))
        self.client = self.stack.enter_context(patch.object(members, 'get_team_client', new=AsyncMock(return_value=object())))
        self.snapshot = self.stack.enter_context(patch.object(members, 'fetch_and_cache_members', new=AsyncMock(return_value={'members': [], 'pending_invites': [{'email': 'member@example.com'}]})))
        self.remote = self.stack.enter_context(patch.object(members, 'run_chatgpt_call', new=AsyncMock(return_value={})))
        self.write = self.stack.enter_context(patch.object(members, 'upsert_member_expiry', new=AsyncMock(return_value='future')))
        self.kicked = self.stack.enter_context(patch.object(members, 'mark_member_kicked', new=AsyncMock()))
        for name in ('log_operation', 'notify_member_event', '_refresh_members_after_mutation', 'update_cached_member_expiry'):
            self.stack.enter_context(patch.object(members, name, new=AsyncMock()))

    def call(self, operation, team='team-1'):
        if operation == 'set':
            return members.set_expiry(team, 'user-1', SetExpiryRequest(expires_in='30d', email='member@example.com'))
        if operation == 'remove':
            return members.remove_member(team, 'user-1')
        return members.revoke_invite(team, 'Member%40example.com')

    def test_existing_renewal_or_auto_kick_claim_blocks_admin_changes(self):
        async def scenario(operation):
            # A renewal has verified upstream presence but not committed its receipt,
            # or expiry removal has read the old expiry but not issued its DELETE.
            async with member_operation_claim('team-1', email='member@example.com', user_id='user-1', operation='self_service_renew') as held:
                self.assertTrue(held)
                with self.assertRaises(HTTPException) as raised:
                    await self.call(operation)
                self.assertEqual(raised.exception.status_code, 409)
            self.remote.assert_not_awaited()
            self.write.assert_not_awaited()
        for operation in ('set', 'remove', 'revoke'):
            with self.subTest(operation=operation):
                asyncio.run(scenario(operation))

    def test_admin_claim_prevents_renewal_during_local_write_or_remote_delete(self):
        async def protected(*args, **kwargs):
            async with member_operation_claim('team-1', email='member@example.com', operation='self_service_renew') as acquired:
                self.assertFalse(acquired)
            return {}
        self.write.side_effect = protected
        self.remote.side_effect = protected
        # supply client methods without contacting any upstream
        self.client.return_value = SimpleNamespace(remove_member=object(), revoke_invite=object())
        for operation in ('set', 'remove', 'revoke'):
            with self.subTest(operation=operation):
                asyncio.run(self.call(operation))

    def test_same_email_in_another_team_is_independent(self):
        async def scenario():
            async with member_operation_claim('team-other', email='member@example.com', operation='self_service_renew') as held:
                self.assertTrue(held)
                await self.call('set')
        asyncio.run(scenario())
        self.write.assert_awaited_once()

    def test_unresolved_redemption_is_rechecked_under_admin_claim(self):
        async def barrier(*args, **kwargs):
            async with member_operation_claim('team-1', email='member@example.com', operation='self_service_renew') as acquired:
                self.assertFalse(acquired)
            return {'token_use_id': 7, 'result': 'uncertain'}
        self.open.side_effect = barrier
        self.stack.enter_context(patch.object(members, 'open_redemption_detail', return_value='unresolved'))
        for operation in ('set', 'remove', 'revoke'):
            with self.subTest(operation=operation):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(self.call(operation))
                self.assertEqual(raised.exception.status_code, 409)
        self.remote.assert_not_awaited()
        self.write.assert_not_awaited()

    def test_member_disappearing_before_claim_recheck_cannot_be_modified(self):
        for operation in ('set', 'remove'):
            self.resolve.side_effect = [('user-1', 'member@example.com'), HTTPException(status_code=404)]
            with self.subTest(operation=operation), self.assertRaises(HTTPException):
                asyncio.run(self.call(operation))
        self.remote.assert_not_awaited()
        self.write.assert_not_awaited()

    def test_accepted_invite_cannot_be_revoked_as_a_stale_pending_invite(self):
        self.snapshot.return_value = {'members': [{'id': 'user-1', 'email': 'member@example.com'}], 'pending_invites': []}
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(self.call('revoke'))
        self.assertEqual(raised.exception.status_code, 409)
        self.remote.assert_not_awaited()

    def test_ambiguous_remote_failure_does_not_mark_kicked_and_releases_claim(self):
        self.client.return_value = SimpleNamespace(remove_member=object(), revoke_invite=object())
        self.remote.return_value = {'error': 'timeout'}
        async def scenario(operation):
            with self.assertRaises(HTTPException) as raised:
                await self.call(operation)
            self.assertEqual(raised.exception.status_code, 502)
            async with member_operation_claim('team-1', email='member@example.com', operation='self_service_renew') as acquired:
                self.assertTrue(acquired)
        for operation in ('remove', 'revoke'):
            with self.subTest(operation=operation):
                asyncio.run(scenario(operation))
        self.kicked.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
