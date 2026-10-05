"""管理员邀请 / 重发不能叠在一笔未结兑换上。

一张 30 天码的邀请结果不明：兑换锁成 uncertain、钉在原 Team，码锁着。管理员这时
对同一个邮箱在同一个 Team 邀请或重发 30 天，到期记成 now+30；之后对账确认那笔
兑换，又在上面累加 30 天，客户拿到 60 天。现在管理员接口在 team_invite_lock 和
成员操作占用之内、发任何上游请求之前，先查这个邮箱上对账日后还会记账的兑换，
有就 409，什么都不写。

结果不明的兑换钉在别的 Team 时，邀请同样要拒：对账日后在原 Team 看见人就确认，
管理员若已把人邀进这个 Team，客户就凭一张码占了两个席位。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.models import ExtendExpiryRequest, InviteMemberRequest, SetExpiryRequest
from app.routes import access_tokens, members
from app.utils.durations import expiry_from_duration


UTC = timezone.utc
EMAIL = "member@example.com"
TEAM = "team-1"
OTHER_TEAM = "team-2"
ABSENT = {"members": [], "pending_invites": []}


class _InviteClient:
    def __init__(self):
        self.invites = []

    def invite_member(self, email, seat_type="default"):
        self.invites.append((email, seat_type))
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


async def _direct_call(func, *args, **kwargs):
    return func(*args, **kwargs)


class _OpenRedemptionCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        conn = self._conn()
        for team_id in (TEAM, OTHER_TEAM):
            conn.execute(
                """INSERT INTO teams (id, name, status, access_token, device_id,
                                      created_at, updated_at)
                   VALUES (?, ?, 'active', 'access', 'device', '2026-10-01', '2026-10-01')""",
                (team_id, team_id),
            )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 兑换侧的状态，走生产代码的同一组函数建出来 ─────────────────────────

    def _reserve(self, email=EMAIL, grant="30d"):
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'atm_x', ?, 1, 0, 0, '2026-10-01')""",
            (f"hash-{uuid.uuid4().hex}", grant),
        )
        token_id = cur.lastrowid
        conn.commit()
        conn.close()
        nominal = expiry_from_duration(grant)
        return asyncio.run(
            access_tokens._reserve_token_use(
                token_id, email, nominal.isoformat() if nominal else None
            )
        )

    def _open_uncertain(self, team_id=TEAM, email=EMAIL):
        """兑换在 team_id 上发了邀请、结果不明：invite_pending → uncertain + 巡逻屏障。"""
        token_use_id = self._reserve(email)
        asyncio.run(access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=team_id))
        locked = asyncio.run(
            access_tokens._lock_uncertain_with_barrier(
                token_use_id,
                team_id=team_id,
                email=email,
                error_message="OpenAI invite result is uncertain",
                reason="OpenAI invite result is uncertain",
            )
        )
        self.assertTrue(locked)
        return token_use_id

    # ── 管理员邀请 / 对账 ─────────────────────────────────────────────────

    def _admin_invite(self, snapshot=ABSENT, *, email=EMAIL, team_id=TEAM, expires_in="30d"):
        client = _InviteClient()
        fetch = AsyncMock(return_value=snapshot)
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
            try:
                result = asyncio.run(
                    members.invite_member(
                        team_id, InviteMemberRequest(email=email, expires_in=expires_in)
                    )
                )
                return result, client, fetch, None
            except HTTPException as exc:
                return None, client, fetch, exc
        finally:
            for p in patches:
                p.stop()

    def _reconcile(self, snapshot):
        with (
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(access_tokens, "fetch_and_cache_members", new=AsyncMock(return_value=snapshot)),
            patch.object(access_tokens, "log_operation", new=AsyncMock()),
        ):
            return asyncio.run(access_tokens.reconcile_pending_redemptions())

    def _expiry_rows(self, team_id=TEAM):
        conn = self._conn()
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT email, expires_at, kicked FROM member_expiry WHERE team_id = ? ORDER BY id",
                (team_id,),
            )
        ]
        conn.close()
        return rows

    def _token_use(self, token_use_id):
        conn = self._conn()
        row = dict(conn.execute("SELECT * FROM access_token_uses WHERE id = ?", (token_use_id,)).fetchone())
        conn.close()
        return row

    def _assert_refused_before_upstream(self, client, fetch, exc, *, token_use_id, phrase):
        self.assertIsNotNone(exc, "有未结兑换时管理员邀请必须被拒")
        self.assertEqual(exc.status_code, 409)
        self.assertIsInstance(exc.detail, str)  # TG /invite 原样转发 detail
        self.assertIn(f"#{token_use_id}", exc.detail)
        self.assertIn(phrase, exc.detail)
        self.assertEqual(client.invites, [], "拒绝必须发生在任何上游邀请之前")
        fetch.assert_not_awaited()
        self.assertEqual(self._expiry_rows(), [])


class AdminInviteOpenRedemptionTest(_OpenRedemptionCase):

    def test_paid_days_are_credited_once_when_admin_reinvites_an_uncertain_redemption(self):
        token_use_id = self._open_uncertain()

        # 管理员按 30 天补发；不论接口是否接受，之后对账确认那笔兑换。
        self._admin_invite()
        visible = {"members": [], "pending_invites": [{"email": EMAIL}]}
        self._reconcile(visible)

        self.assertEqual(self._token_use(token_use_id)["result"], "success")
        rows = [row for row in self._expiry_rows() if not row["kicked"]]
        self.assertEqual(len(rows), 1)
        expires_at = datetime.fromisoformat(rows[0]["expires_at"])
        days = (expires_at - datetime.now(UTC)).total_seconds() / 86400
        self.assertLess(days, 31, f"30 天码最终记成了 {days:.1f} 天：管理员补发叠在了兑换上")
        self.assertGreater(days, 29)

    def test_invite_is_refused_while_a_redemption_is_uncertain_in_this_team(self):
        token_use_id = self._open_uncertain()

        _result, client, fetch, exc = self._admin_invite()

        self._assert_refused_before_upstream(
            client, fetch, exc, token_use_id=token_use_id, phrase="待确认的兑换"
        )
        conn = self._conn()
        log = conn.execute(
            """SELECT detail, result FROM operation_logs
               WHERE action = 'invite_member' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        self.assertEqual(log["result"], "skipped")
        self.assertIn(f"token_use_id={token_use_id}", log["detail"])

    def test_resend_of_the_uncertain_invite_is_refused(self):
        """不明的那次邀请其实到了，名单里是待接受邀请：重发分支同样被拒。"""
        token_use_id = self._open_uncertain()

        _result, client, fetch, exc = self._admin_invite(
            {"members": [], "pending_invites": [{"email": EMAIL}]}
        )

        self._assert_refused_before_upstream(
            client, fetch, exc, token_use_id=token_use_id, phrase="待确认的兑换"
        )

    def test_admin_email_is_compared_normalized(self):
        token_use_id = self._open_uncertain()

        _result, client, fetch, exc = self._admin_invite(email="  Member@Example.COM ")

        self._assert_refused_before_upstream(
            client, fetch, exc, token_use_id=token_use_id, phrase="待确认的兑换"
        )

    def test_in_flight_redemption_blocks_wherever_it_currently_points(self):
        """pending 的兑换还会换 Team（未落 Team 的查找、被拒后换下一个队重试），不论现在指向哪都拒。"""
        cases = {
            "lookup, no team yet": None,
            "invite in flight on another team": OTHER_TEAM,
            "invite in flight on this team": TEAM,
        }
        for name, phase_team in cases.items():
            with self.subTest(name):
                token_use_id = self._reserve()
                if phase_team is not None:
                    asyncio.run(
                        access_tokens._set_token_use_phase(
                            token_use_id, "invite_pending", team_id=phase_team
                        )
                    )

                try:
                    _result, client, fetch, exc = self._admin_invite()

                    self._assert_refused_before_upstream(
                        client, fetch, exc, token_use_id=token_use_id, phrase="正在处理"
                    )
                finally:
                    # 子用例互不影响：这笔按失败退掉（放开邮箱占用），清掉可能写下的到期。
                    asyncio.run(
                        access_tokens._fail_and_release_token_use(
                            token_use_id, action="redeem_failed", error_message="test"
                        )
                    )
                    conn = self._conn()
                    conn.execute("DELETE FROM member_expiry")
                    conn.commit()
                    conn.close()

    def test_fallback_row_with_an_open_token_use_blocks(self):
        """兜底行挂着仍未结的兑换凭据，调度器回填时会认领并记账：即使凭据本身没指向这个 Team 也要拒。"""
        token_use_id = self._reserve()
        conn = self._conn()
        conn.execute(
            "UPDATE access_token_uses SET action = 'invite_pending', result = 'uncertain' WHERE id = ?",
            (token_use_id,),
        )
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               VALUES (?, '', ?, ?, 'self_service', 'write failed', 0, ?, ?, 'extend')""",
            (
                TEAM,
                EMAIL,
                (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                datetime.now(UTC).isoformat(),
                token_use_id,
            ),
        )
        conn.commit()
        conn.close()

        _result, client, fetch, exc = self._admin_invite()

        self._assert_refused_before_upstream(
            client, fetch, exc, token_use_id=token_use_id, phrase="待确认的兑换"
        )

    def test_uncertain_redemption_in_another_team_blocks_the_invite(self):
        """钉在别的 Team 的 uncertain 日后在那个 Team 确认：再邀进这里就是一张码两个席位。"""
        token_use_id = self._open_uncertain(team_id=OTHER_TEAM)

        _result, client, fetch, exc = self._admin_invite()

        self._assert_refused_before_upstream(
            client, fetch, exc, token_use_id=token_use_id, phrase="待确认的兑换"
        )
        self.assertIn(OTHER_TEAM, exc.detail, "说明里要点名兑换所在的 Team")
        self.assertEqual(self._expiry_rows(OTHER_TEAM), [])
        conn = self._conn()
        log = conn.execute(
            """SELECT team_id, result FROM operation_logs
               WHERE action = 'invite_member' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        self.assertEqual((log["team_id"], log["result"]), (TEAM, "skipped"))

    def test_uncertain_redemption_in_a_deleted_team_does_not_block(self):
        """Team 删掉后对账拿不到它的凭据、再也确认不了那笔兑换，叠不出第二个席位；
        管理员也没法在那里核实退码，算上就让这个邮箱永远拉不进别的 Team。"""
        self._open_uncertain(team_id=OTHER_TEAM)
        conn = self._conn()
        conn.execute("DELETE FROM teams WHERE id = ?", (OTHER_TEAM,))
        conn.commit()
        conn.close()

        result, client, _fetch, exc = self._admin_invite()

        self.assertIsNone(exc)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.invites, [(EMAIL, "default")])

    def test_settled_redemptions_do_not_block(self):
        succeeded = self._open_uncertain()
        conn = self._conn()
        conn.execute("UPDATE access_token_uses SET result = 'success' WHERE id = ?", (succeeded,))
        conn.execute("DELETE FROM redemption_email_claims WHERE token_use_id = ?", (succeeded,))
        conn.execute(
            "UPDATE pending_invite_reconciliations SET resolved = 1 WHERE token_use_id = ?",
            (succeeded,),
        )
        conn.commit()
        conn.close()
        released = self._reserve()
        asyncio.run(
            access_tokens._fail_and_release_token_use(
                released, action="redeem_failed", error_message="test"
            )
        )

        result, client, _fetch, exc = self._admin_invite()

        self.assertIsNone(exc)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.invites, [(EMAIL, "default")])


class AdminExpiryEditOpenRedemptionTest(_OpenRedemptionCase):
    """「设置到期」「续期」写的是同一条到期记录，同样不能叠在未结兑换上。"""

    USER_ID = "user-1"
    MEMBER_SNAPSHOT = {
        "members": [{"id": "user-1", "email": EMAIL}],
        "pending_invites": [],
    }

    def _edit_expiry(self, operation):
        patches = [
            patch.object(members, "get_cached_members", new=AsyncMock(return_value=self.MEMBER_SNAPSHOT)),
            patch.object(members, "get_team_client", new=AsyncMock(return_value=object())),
            patch.object(members, "fetch_and_cache_members", new=AsyncMock(return_value=self.MEMBER_SNAPSHOT)),
            patch.object(members, "update_cached_member_expiry", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            if operation == "set_expiry":
                call = members.set_expiry(TEAM, self.USER_ID, SetExpiryRequest(expires_in="30d", email=EMAIL))
            else:
                call = members.extend_expiry(
                    TEAM,
                    self.USER_ID,
                    ExtendExpiryRequest(expires_in="30d", email=EMAIL, request_id=f"req-{uuid.uuid4().hex}"),
                )
            try:
                return asyncio.run(call), None
            except HTTPException as exc:
                return None, exc
        finally:
            for p in patches:
                p.stop()

    def _assert_credited_once(self, operation):
        token_use_id = self._open_uncertain()

        # 那次邀请其实到了、人已经进 Team；管理员按 30 天设置 / 续期，之后对账确认兑换。
        self._edit_expiry(operation)
        self._reconcile(self.MEMBER_SNAPSHOT)

        self.assertEqual(self._token_use(token_use_id)["result"], "success")
        rows = [row for row in self._expiry_rows() if not row["kicked"]]
        self.assertEqual(len(rows), 1)
        expires_at = datetime.fromisoformat(rows[0]["expires_at"])
        days = (expires_at - datetime.now(UTC)).total_seconds() / 86400
        self.assertLess(days, 31, f"{operation}: 30 天码最终记成了 {days:.1f} 天")
        self.assertGreater(days, 29)

    def _assert_refused(self, operation):
        token_use_id = self._open_uncertain()

        result, exc = self._edit_expiry(operation)

        self.assertIsNone(result)
        self.assertIsNotNone(exc, "有未结兑换时管理员改到期必须被拒")
        self.assertEqual(exc.status_code, 409)
        self.assertIn(f"#{token_use_id}", exc.detail)
        self.assertIn("待确认的兑换", exc.detail)
        self.assertEqual(self._expiry_rows(), [])

    def _assert_other_team_does_not_block(self, operation):
        self._open_uncertain(team_id=OTHER_TEAM)

        result, exc = self._edit_expiry(operation)

        self.assertIsNone(exc)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self._expiry_rows()), 1)

    def test_set_expiry_does_not_stack_on_an_uncertain_redemption(self):
        self._assert_credited_once("set_expiry")

    def test_extend_expiry_does_not_stack_on_an_uncertain_redemption(self):
        self._assert_credited_once("extend_expiry")

    def test_set_expiry_is_refused_while_a_redemption_is_uncertain_in_this_team(self):
        self._assert_refused("set_expiry")

    def test_extend_expiry_is_refused_while_a_redemption_is_uncertain_in_this_team(self):
        self._assert_refused("extend_expiry")

    def test_set_expiry_ignores_an_uncertain_redemption_in_another_team(self):
        self._assert_other_team_does_not_block("set_expiry")

    def test_extend_expiry_ignores_an_uncertain_redemption_in_another_team(self):
        self._assert_other_team_does_not_block("extend_expiry")

if __name__ == "__main__":
    unittest.main()
