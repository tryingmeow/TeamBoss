"""管理员拉人与邀请结果分类的回归测试。

1. 管理员（网页 / Telegram /invite）对已在该 Team 的邮箱再发邀请，会把成员已买
   的时长覆盖成这次填的有效期（剩 300 天 → 30 天；永久 → 30 天并装上自动踢）。
   现在邀请前在 team_invite_lock 里实时拉一次成员 + 待接受邀请：已在则 409，
   拉不到则失败关闭；批量 GPT 拉人同理，不能拿缓存快照当"不在"的证据。
   record_confirmed_invite 本身也不再缩短 / 替换已有到期（纵深防御）。
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
from app.scheduler import _reconcile_pending_invites_sync
from app.services import gpt_invites, member_expiry
from app.services.member_expiry import record_confirmed_invite


UTC = timezone.utc
EMAIL = "member@example.com"


class _TempDb:
    """真实 init_database() 建表，落在临时目录里。"""

    def _start_db(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()

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


async def _direct_call(func, *args, **kwargs):
    return func(*args, **kwargs)


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
            patch.object(members, "run_chatgpt_call", new=_direct_call),
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

    def test_pending_invite_is_refused(self):
        self._insert_expiry(self.future)
        before = self._expiry_rows()

        _result, client, exc = self._invite(
            {"members": [], "pending_invites": [{"email": "member@EXAMPLE.com"}]},
        )

        self.assertEqual(getattr(exc, "status_code", None), 409)
        self.assertIn("待接受的邀请", exc.detail)
        self.assertEqual(client.invites, [])
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
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(gpt_invites, "fetch_and_cache_members", new=fetch),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(gpt_invites, "run_chatgpt_call", new=_direct_call),
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


if __name__ == "__main__":
    unittest.main()
