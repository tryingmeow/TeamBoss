"""管理员拉人（POST /api/teams/{team_id}/members/invite，网页与 Telegram /invite 共用）。

1. 邮箱在该 Team 已有待接受的邀请时，接口曾回 409 并让管理员"先撤销再邀请"；
   撤销会把本地记录标成 kicked，已付时长 / 永久授权随之丢失。现在对待接受的邀请
   直接重发：上游重发邮件，本地到期按 record_confirmed_invite 的合并规则只延长、
   不缩短、不把永久变有限。已是正式成员的邮箱仍然 409。
2. 合并后实际落库的到期（例如一条仍未踢出的永久记录被保留）与接口响应、Telegram
   卡片上写的有效期必须一致，不能照抄这次填写的 30d。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import tg_bot
from app.models import InviteMemberRequest
from app.routes import members
from app.services import member_expiry


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


class _InviteClient:
    def __init__(self):
        self.invites = []

    def invite_member(self, email, seat_type="default"):
        self.invites.append((email, seat_type))
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


async def _direct_call(func, *args, **kwargs):
    return func(*args, **kwargs)


class _AdminInviteBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, created_at, updated_at)
               VALUES ('team-1', 'team-1', 'active', '2026-10-01', '2026-10-01')"""
        )
        conn.commit()
        conn.close()
        self.now = datetime.now(UTC).replace(microsecond=0)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_expiry(self, expires_at, *, source="self_service", user_id="u-1"):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, source,
                first_seen_at, created_at)
               VALUES ('team-1', ?, ?, ?, ?, 0, ?, '2026-10-01', '2026-10-01')""",
            (user_id, EMAIL, expires_at, 1 if expires_at else 0, source),
        )
        conn.commit()
        conn.close()

    def _rows(self):
        conn = self._conn()
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT user_id, email, expires_at, auto_kick, kicked, source
                   FROM member_expiry WHERE team_id = 'team-1' ORDER BY id"""
            )
        ]
        conn.close()
        return rows

    def _active_row(self):
        rows = [row for row in self._rows() if row["kicked"] == 0]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def _claims(self):
        conn = self._conn()
        rows = [dict(row) for row in conn.execute("SELECT * FROM member_operation_claims")]
        conn.close()
        return rows

    def _invite(self, snapshot, *, expires_in="30d", email=EMAIL, seat_type="default"):
        """跑一次真实的 invite_member 路由；上游、席位检查和 Telegram 一律替身。"""
        self.client = _InviteClient()
        self.seat_check = AsyncMock()
        self.notify = AsyncMock()
        patches = [
            patch.object(members, "get_team_client", new=AsyncMock(return_value=self.client)),
            patch.object(members, "fetch_and_cache_members", new=AsyncMock(return_value=snapshot)),
            patch.object(members, "_ensure_default_seat_available", new=self.seat_check),
            patch.object(members, "run_chatgpt_call", new=_direct_call),
            patch.object(members, "add_member_watch", new=AsyncMock()),
            patch.object(members, "reserve_default_seat", new=AsyncMock()),
            patch.object(members, "notify_member_event", new=self.notify),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(
                members.invite_member(
                    "team-1",
                    InviteMemberRequest(email=email, expires_in=expires_in, seat_type=seat_type),
                )
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

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


# ── 1. 待接受的邀请可以重发，到期只延长不缩短 ─────────────────────────────────

class PendingInviteResendTest(_AdminInviteBase):
    def test_pending_invite_resend_keeps_longer_paid_expiry(self):
        paid = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(paid, source="self_service")

        response, exc = self._invite(_pending(email="Member@EXAMPLE.com"), expires_in="30d")

        self.assertIsNone(exc, "待接受的邀请应当直接重发，不能让管理员去撤销")
        self.assertEqual(response["status"], "ok")
        self.assertTrue(response["resent"])
        self.assertEqual(self.client.invites, [(EMAIL, "default")])
        row = self._active_row()
        self.assertEqual(row["expires_at"], paid)
        self.assertEqual(row["auto_kick"], 1)
        self.assertEqual(row["source"], "self_service")
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

    def test_active_member_is_still_refused(self):
        paid = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(paid)
        before = self._rows()

        _response, exc = self._invite(_member(email="MEMBER@example.com"))

        self.assertIsNotNone(exc)
        self.assertEqual(exc.status_code, 409)
        self.assertIn("续期", exc.detail)
        self.assertEqual(self.client.invites, [])
        self.assertEqual(self._rows(), before)
        self.assertEqual(self._claims(), [])

    def test_member_operation_in_progress_refuses_before_any_upstream_call(self):
        """巡逻 / 到期撤邀请正占着这个邮箱时，重发不能和它交错。"""
        paid = (self.now + timedelta(days=300)).isoformat()
        self._insert_expiry(paid)
        before = self._rows()
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
        self.assertEqual(self._rows(), before)
        self.assertEqual([c["owner_token"] for c in self._claims()], ["other-owner"])


# ── 2. 响应与 Telegram 卡片报告实际落库的到期 ────────────────────────────────

class InviteReportsStoredExpiryTest(_AdminInviteBase):
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


if __name__ == "__main__":
    unittest.main()
