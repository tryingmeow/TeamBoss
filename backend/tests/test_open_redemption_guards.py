"""管理员邀请 / 改到期 / 批量拉人不能叠在一笔未结兑换上。

一张 30 天码的邀请结果不明：兑换锁成 uncertain、钉在原 Team，码锁着。管理员这时
对同一个邮箱在同一个 Team 邀请或重发 30 天，到期记成 now+30；之后对账确认那笔
兑换，又在上面累加 30 天，成员拿到 60 天。现在管理员接口在 team_invite_lock 和
成员操作占用之内、发任何上游请求之前，先查这个邮箱上对账日后还会记账的兑换，
有就 409，什么都不写。「设置到期」「续期」写同一条到期记录，同样要拒。

结果不明的兑换钉在别的 Team 时，邀请同样要拒：对账日后在原 Team 看见人就确认，
管理员若已把人邀进这个 Team，成员就凭一张码占了两个席位。

批量「添加 GPT 成员」同理：进程重启杀掉一笔自助兑换（码已占用、在 Team T 进入
invite_pending，结果没记下），对账任务要过 10 分钟才把它转成 uncertain 并立屏障，
这之前没有任何对账行；批量这时挑中 T 拉同一个邮箱 30 天，对账随后又累加 30 天。
现在 _invite_to_team 在 team_invite_lock 之内、任何上游请求之前先查未结兑换，有就
跳过、什么都不写，这个邮箱也不再换下一个 Team；uncertain 不分 Team。

被拒说明（open_redemption_detail）不能把管理员引向手动补授予。

驱动真实的路由 / invite_gpt_member_any_team 和临时目录里 init_database() 建的库；
兑换侧状态用生产代码的同一组函数建；只替换上游客户端、现拉名单和现拉空位。
删 Team 被未结兑换挡住见 test_delete_team_open_redemption。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import unittest
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import direct_call, start_temp_db, start_temp_db_async
from _invite_fixtures import InviteClient, run_admin_invite

from app.models import ExtendExpiryRequest, InviteMemberRequest, SetExpiryRequest
from app.routes import access_tokens, members
from app.services import gpt_invites, open_redemptions
from app.services.member_expiry import expires_in_to_datetime
from app.utils.durations import expiry_from_duration


UTC = timezone.utc
EMAIL = "member@example.com"
ABSENT = {"members": [], "pending_invites": []}
VISIBLE = {"members": [], "pending_invites": [{"email": EMAIL}]}
# 管理员接口用例的 Team
TEAM = "team-1"
OTHER_TEAM = "team-2"
# 批量拉人用例的 Team：成功邀请后的席位预留在进程内存里、按 Team id 记，id 与别的
# 用例错开，免得互相影响候选顺序。
BATCH_TEAM = "batch-team-1"
BATCH_OTHER_TEAM = "batch-team-2"
TEAM_T = "batch-team-t"
TEAM_U = "batch-team-u"


def _new_token(db_path, grant="30d"):
    """建一张单次可用的兑换码，返回 access_tokens.id。"""
    conn = sqlite3.connect(db_path)
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
    return token_id


def _nominal_expiry(grant):
    nominal = expiry_from_duration(grant)
    return nominal.isoformat() if nominal else None


def _reconcile_patches(snapshot):
    """对账任务（reconcile_pending_redemptions）的替身：每个 Team 的现拉名单都是 snapshot。"""
    stack = ExitStack()
    for p in (
        patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
        patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
        patch.object(access_tokens, "fetch_and_cache_members", new=AsyncMock(return_value=snapshot)),
        patch.object(access_tokens, "log_operation", new=AsyncMock()),
    ):
        stack.enter_context(p)
    return stack


# ── 管理员接口：邀请 / 重发 / 设置到期 / 续期 ───────────────────────────────────

class _OpenRedemptionCase(unittest.TestCase):
    def setUp(self):
        self.db_path = start_temp_db(self)
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
        token_id = _new_token(self.db_path, grant)
        return asyncio.run(
            access_tokens._reserve_token_use(token_id, email, _nominal_expiry(grant))
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
        client = InviteClient()
        fetch = AsyncMock(return_value=snapshot)
        try:
            result = run_admin_invite(
                team_id,
                InviteMemberRequest(email=email, expires_in=expires_in),
                client=client,
                fetch=fetch,
            )
            return result, client, fetch, None
        except HTTPException as exc:
            return None, client, fetch, exc

    def _reconcile(self, snapshot):
        with _reconcile_patches(snapshot):
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

    def test_uncertain_redemption_in_a_logged_out_team_says_how_to_settle_it(self):
        token_use_id = self._open_uncertain(team_id=OTHER_TEAM)
        conn = self._conn()
        conn.execute("UPDATE teams SET status = 'token_expired' WHERE id = ?", (OTHER_TEAM,))
        conn.commit()
        conn.close()

        _result, client, fetch, exc = self._admin_invite()

        self._assert_refused_before_upstream(
            client, fetch, exc, token_use_id=token_use_id, phrase="重新导入恢复登录"
        )
        self.assertIn(f"Team {OTHER_TEAM} 登录已失效", exc.detail)
        self.assertIn("直接确认成功", exc.detail)

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


class OpenRedemptionDetailTest(unittest.TestCase):
    """被拒说明不能把管理员引向手动补授予。

    退码后码的 used_count 归零，成员还能用同一张码再兑换一次；管理员若按旧说明
    "另行邀请"、设置到期或续期去补，成员再兑换又拿一份，一张码两次授予。
    """

    OPERATIONS = ("invite", "batch_invite", "set_expiry", "extend_expiry")
    REDEEM_AGAIN = "请让成员用同一兑换码重新兑换"
    NO_MANUAL_GRANT = "不要用手动邀请、设置到期或续期来补"
    UNCERTAIN = {
        "token_use_id": 41, "result": "uncertain", "action": "invite_pending",
        "team_id": OTHER_TEAM, "team_name": "Beta", "created_at": "2026-10-01T00:00:00+00:00",
    }
    PENDING = {
        "token_use_id": 42, "result": "pending", "action": "lookup_pending",
        "team_id": None, "team_name": None, "created_at": "2026-10-01T00:00:00+00:00",
    }

    def test_refunded_redemption_is_redeemed_again_not_granted_by_hand(self):
        for operation in self.OPERATIONS:
            for hit in (self.UNCERTAIN, self.PENDING):
                with self.subTest(operation=operation, result=hit["result"]):
                    detail = open_redemptions.open_redemption_detail(hit, operation=operation)
                    self.assertIn(f"兑换记录 #{hit['token_use_id']}", detail)
                    self.assertIn(self.REDEEM_AGAIN, detail)
                    self.assertIn(self.NO_MANUAL_GRANT, detail)
                    self.assertIn("兑换码时长已记过一次", detail)
                    self.assertNotIn("另行邀请", detail)
                    self.assertNotIn("{", detail)

    def test_uncertain_detail_names_the_redemptions_team_and_auto_confirmation(self):
        detail = open_redemptions.open_redemption_detail(self.UNCERTAIN, operation="invite")
        self.assertIn("Team Beta", detail)  # 点名兑换所在的 Team
        self.assertIn("自动确认", detail)
        self.assertIn("「兑换码 → 待确认的兑换」", detail)

        unnamed = dict(self.UNCERTAIN, team_name=None)
        detail = open_redemptions.open_redemption_detail(unnamed, operation="batch_invite")
        self.assertIn(f"Team {OTHER_TEAM}", detail)

    def test_uncertain_detail_names_the_team_the_email_must_show_up_in(self):
        """管理员可能正在另一个 Team 的页面上：「出现在这个 Team」会被读成他眼前那个。"""
        for operation in self.OPERATIONS:
            with self.subTest(operation=operation):
                detail = open_redemptions.open_redemption_detail(
                    self.UNCERTAIN, operation=operation
                )
                self.assertIn("该邮箱出现在 Team Beta（含待接受邀请）后会自动确认", detail)
                self.assertNotIn("这个 Team", detail)

    def test_logged_out_team_points_to_reimport_or_confirm_success(self):
        """原 Team 登录失效时自动确认等不来、确认失败被拒：说明要给出能走的路。"""
        for label, state in (
            ("session dead", {"team_status": "token_expired", "team_auth_state": "ok"}),
            ("refresh rejected", {"team_status": "active", "team_auth_state": "rejected"}),
        ):
            with self.subTest(label):
                detail = open_redemptions.open_redemption_detail(
                    dict(self.UNCERTAIN, **state), operation="invite"
                )
                self.assertIn("Team Beta 登录已失效", detail)
                self.assertIn("重新导入恢复登录", detail)
                self.assertIn("直接确认成功", detail)
                self.assertNotIn("{", detail)

        for state in ({}, {"team_status": "active", "team_auth_state": None}):
            detail = open_redemptions.open_redemption_detail(
                dict(self.UNCERTAIN, **state), operation="invite"
            )
            self.assertNotIn("登录已失效", detail)
            self.assertNotIn("{", detail)

    def test_dead_team_detail_offers_confirm_success_then_new_code(self):
        """名单读不到的 Team（登录失效 / 同步暂停）：恢复不了就确认成功收尾、换发新码。"""
        for label, state in (
            ("session dead", {"team_status": "token_expired"}),
            ("refresh rejected", {"team_auth_state": "rejected"}),
            ("sync suspended", {"team_sync_suspended_at": "2026-10-01T00:00:00+00:00"}),
        ):
            with self.subTest(label):
                detail = open_redemptions.open_redemption_detail(
                    dict(self.UNCERTAIN, **state), operation="invite"
                )
                self.assertIn("确认成功把这条记录收尾，再给成员换发一张同规格的新码", detail)
                self.assertNotIn("{", detail)
        suspended = open_redemptions.open_redemption_detail(
            dict(self.UNCERTAIN, team_sync_suspended_at="2026-10-01T00:00:00+00:00"),
            operation="invite",
        )
        self.assertIn("Team Beta 的成员名单已读不到（同步已暂停）", suspended)
        healthy = open_redemptions.open_redemption_detail(
            dict(self.UNCERTAIN, team_sync_suspended_at=None), operation="invite"
        )
        self.assertNotIn("换发一张同规格的新码", healthy.split("确认成功则")[0])

    def test_pending_detail_quotes_the_reconciler_timings(self):
        # 说明里的分钟数和对账任务用的是同一组常量。
        self.assertEqual(
            access_tokens._INTERRUPTED_INVITE_AFTER_SECONDS,
            open_redemptions.INTERRUPTED_INVITE_AFTER_SECONDS,
        )
        self.assertEqual(
            access_tokens._STALE_LOCAL_REDEMPTION_AFTER_SECONDS,
            open_redemptions.STALE_LOCAL_REDEMPTION_AFTER_SECONDS,
        )
        detail = open_redemptions.open_redemption_detail(self.PENDING, operation="invite")
        self.assertIn(f"满 {open_redemptions.INTERRUPTED_INVITE_AFTER_SECONDS // 60} 分钟", detail)
        self.assertIn(f"满 {open_redemptions.STALE_LOCAL_REDEMPTION_AFTER_SECONDS // 60} 分钟", detail)

        # 改常量说明跟着变：没有写死的数字。不整分钟的向上取整，宁可说长。
        with (
            patch.object(open_redemptions, "INTERRUPTED_INVITE_AFTER_SECONDS", 7 * 60),
            patch.object(open_redemptions, "STALE_LOCAL_REDEMPTION_AFTER_SECONDS", 22 * 60 + 1),
        ):
            detail = open_redemptions.open_redemption_detail(self.PENDING, operation="set_expiry")
        self.assertIn("满 7 分钟", detail)
        self.assertIn("满 23 分钟", detail)
        self.assertIn("「兑换码 → 待确认的兑换」", detail)


# ── 批量「添加 GPT 成员」 ──────────────────────────────────────────────────────

class _BatchClient:
    """上游客户端替身：记下每次邀请的邮箱，一律 confirmed。"""

    def __init__(self):
        self.invites = []

    def invite_member(self, email, seat_type="default"):
        self.invites.append(email)
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


class _BatchOpenRedemptionCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = await start_temp_db_async(self)
        # 两个空 Team，候选顺序先 BATCH_TEAM 后 BATCH_OTHER_TEAM（空位相同，按创建时间）。
        self._insert_team(BATCH_TEAM, created_at="2026-10-01T00:00:00+00:00")
        self._insert_team(BATCH_OTHER_TEAM, created_at="2026-10-02T00:00:00+00:00")

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

    def _insert_team(self, team_id, *, created_at, entitled=5, members=()):
        self._execute(
            """INSERT INTO teams (id, name, status, owner_email, access_token, device_id,
                                  seats_in_use, seats_entitled, codex_count, chatgpt_count,
                                  active_until, will_renew, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?, ?, 0, ?, 0, 0, NULL, 1, ?, ?)""",
            team_id, team_id.upper(), f"owner-{team_id}@example.com", f"tok-{team_id}",
            f"dev-{team_id}", entitled, created_at, created_at,
        )
        self._execute(
            "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, ?, '[]', ?)",
            team_id, json.dumps(list(members)), created_at,
        )

    # ── 兑换侧的状态，走生产代码的同一组函数建出来 ─────────────────────────

    async def _reserve(self, email=EMAIL, grant="30d"):
        token_id = _new_token(self.db_path, grant)
        return await access_tokens._reserve_token_use(token_id, email, _nominal_expiry(grant))

    async def _interrupted_invite(self, team_id=BATCH_TEAM, email=EMAIL):
        """兑换在 team_id 上进入 invite_pending 后进程被杀：pending、没有任何对账行。"""
        token_use_id = await self._reserve(email)
        await access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=team_id)
        return token_use_id

    # ── 批量拉人 / 对账 ───────────────────────────────────────────────────

    async def _batch_invite(self, expires_in="30d"):
        """invite_gpt_member_any_team；现拉名单里看不到这个邮箱，现拉空位始终有。"""
        self.clients = {BATCH_TEAM: _BatchClient(), BATCH_OTHER_TEAM: _BatchClient()}
        self.get_client = AsyncMock(side_effect=lambda team_id: self.clients[team_id])
        self.fetch = AsyncMock(return_value=ABSENT)
        self.reserve = AsyncMock()
        self.notify = AsyncMock()
        with (
            patch.object(gpt_invites, "get_team_client", new=self.get_client),
            patch.object(gpt_invites, "fetch_and_cache_members", new=self.fetch),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "reserve_default_seat", new=self.reserve),
            patch.object(gpt_invites, "notify_member_event", new=self.notify),
        ):
            try:
                result = await gpt_invites.invite_gpt_member_any_team(
                    EMAIL, expires_in_to_datetime(expires_in), action="invite_gpt_member"
                )
            except gpt_invites.GptInviteFailed as exc:
                return None, exc
        return result, None

    async def _reconcile(self, snapshot):
        with _reconcile_patches(snapshot):
            return await access_tokens.reconcile_pending_redemptions()

    def _expiry_rows(self):
        return self._rows(
            "SELECT team_id, expires_at, kicked FROM member_expiry WHERE lower(email) = ? ORDER BY id",
            EMAIL,
        )

    def _skip_logs(self):
        return self._rows(
            """SELECT team_id, detail FROM operation_logs
               WHERE action = 'invite_gpt_member' AND result = 'skipped' AND target_email = ?
               ORDER BY id""",
            EMAIL,
        )

    def _assert_refused_before_upstream(self, result, exc, *, token_use_id):
        self.assertIsNone(result, "有未结兑换时批量拉人不能把人加进任何 Team")
        self.assertIsNotNone(exc)
        self.assertIn(f"#{token_use_id}", exc.reason)
        self.assertEqual(self.clients[BATCH_TEAM].invites, [], "拒绝必须发生在任何上游邀请之前")
        self.assertEqual(self.clients[BATCH_OTHER_TEAM].invites, [])
        self.get_client.assert_not_awaited()
        self.fetch.assert_not_awaited()
        self.reserve.assert_not_awaited()
        self.notify.assert_not_awaited()
        self.assertEqual(self._expiry_rows(), [])
        # 拒绝针对邮箱、与 Team 无关：只在第一个候选 Team 记一次跳过，不再换 Team。
        logs = self._skip_logs()
        self.assertEqual([row["team_id"] for row in logs], [BATCH_TEAM])
        self.assertIn(f"token_use_id={token_use_id}", logs[0]["detail"])
        self.assertEqual(exc.team_id, BATCH_TEAM)


class BatchInviteOpenRedemptionTest(_BatchOpenRedemptionCase):

    async def test_paid_days_are_credited_once_when_admin_batch_invites_an_interrupted_redemption(self):
        token_use_id = await self._interrupted_invite()

        # 管理员在 10 分钟内按 30 天批量拉同一个邮箱；不论是否被接受，之后对账在
        # 原 Team 里看到这个人、确认那笔兑换。
        await self._batch_invite()
        await self._reconcile(VISIBLE)

        self.assertEqual(
            self._rows("SELECT result FROM access_token_uses WHERE id = ?", token_use_id)[0]["result"],
            "success",
        )
        rows = [row for row in self._expiry_rows() if not row["kicked"]]
        self.assertEqual([row["team_id"] for row in rows], [BATCH_TEAM])
        days = (datetime.fromisoformat(rows[0]["expires_at"]) - datetime.now(UTC)).total_seconds() / 86400
        self.assertLess(days, 31, f"30 天码最终记成了 {days:.1f} 天：批量拉人叠在了兑换上")
        self.assertGreater(days, 29)

    async def test_batch_invite_is_refused_while_a_redemption_is_in_flight(self):
        """pending 的兑换还会换 Team（未落 Team 的查找、被拒后换下一个队重试），不论现在指向哪都拒。"""
        cases = {
            "lookup, no team yet": None,
            "invite interrupted on the first candidate": BATCH_TEAM,
            "invite interrupted on another team": BATCH_OTHER_TEAM,
        }
        for name, phase_team in cases.items():
            with self.subTest(name):
                if phase_team is None:
                    token_use_id = await self._reserve()
                else:
                    token_use_id = await self._interrupted_invite(team_id=phase_team)
                try:
                    result, exc = await self._batch_invite()

                    self._assert_refused_before_upstream(result, exc, token_use_id=token_use_id)
                finally:
                    # 子用例互不影响：这笔按失败退掉（放开邮箱占用），清掉本例写下的东西。
                    await access_tokens._fail_and_release_token_use(
                        token_use_id, action="redeem_failed", error_message="test"
                    )
                    self._execute("DELETE FROM member_expiry")
                    self._execute("DELETE FROM operation_logs")

    async def test_settled_or_unrelated_redemptions_do_not_block(self):
        settled = await self._interrupted_invite()
        self._execute("UPDATE access_token_uses SET result = 'success' WHERE id = ?", settled)
        self._execute("DELETE FROM redemption_email_claims WHERE token_use_id = ?", settled)
        released = await self._reserve()
        await access_tokens._fail_and_release_token_use(
            released, action="redeem_failed", error_message="test"
        )
        await self._interrupted_invite(email="someone-else@example.com")

        result, exc = await self._batch_invite()

        self.assertIsNone(exc)
        self.assertEqual(result["team_id"], BATCH_TEAM)
        self.assertEqual(self.clients[BATCH_TEAM].invites, [EMAIL])
        self.assertEqual([row["team_id"] for row in self._expiry_rows()], [BATCH_TEAM])
        self.assertEqual(self._skip_logs(), [])


class BatchInviteUncertainInAnotherTeamTest(_BatchOpenRedemptionCase):
    """批量拉人的未结兑换检查对 uncertain 不分 Team。

    一笔兑换在 Team U 的邀请结果不明：兑换锁成 uncertain、钉在 U，对账在 U 里看到人就
    确认、在 U 记账。管理员这时批量把同一个邮箱拉进 Team T，一张码就占了两个席位。

    批量入口在循环前已经用 _team_with_unresolved_invite 挡住带未结对账行的邮箱，U 的屏障
    行在场时到不了 _invite_to_team。这里复现那道检查挡不住的时序：检查读库时兑换还在 U
    发邀请（pending，U 上还没有屏障），读完之后那次邀请超时、兑换在 U 锁成 uncertain 并
    立屏障；批量接着在 T 的锁里查未结兑换，这时只剩一笔钉在 U 的 uncertain。
    """

    async def asyncSetUp(self):
        self.db_path = await start_temp_db_async(self)
        # U 的最后一个席位给了那笔兑换（缓存里已满），批量拉人的候选只剩 T。
        others = [{"email": f"u{i}@example.com", "seat_type": "default"} for i in range(2)]
        for team_id, entitled, members in ((TEAM_T, 5, []), (TEAM_U, 2, others)):
            self._insert_team(team_id, created_at="2026-10-01T00:00:00+00:00",
                              entitled=entitled, members=members)

    async def _invite_in_flight_in_u(self):
        """兑换已在 U 发出邀请、还没拿到结果：pending + invite_pending，U 上还没有屏障。"""
        return await self._interrupted_invite(team_id=TEAM_U)

    async def test_uncertain_redemption_in_another_team_blocks_the_batch(self):
        token_use_id = await self._invite_in_flight_in_u()

        real_unresolved_check = gpt_invites._team_with_unresolved_invite
        seen_by_pre_loop_check = []

        async def check_then_redemption_times_out(email):
            # 循环前检查照常读库；读完的这一刻，U 上那次邀请超时，兑换走生产代码的
            # 同一个函数锁成 uncertain 并立屏障。
            found = await real_unresolved_check(email)
            seen_by_pre_loop_check.append(found)
            locked = await access_tokens._lock_uncertain_with_barrier(
                token_use_id,
                team_id=TEAM_U,
                email=EMAIL,
                error_message="OpenAI invite result is uncertain",
                reason="OpenAI invite result is uncertain",
            )
            self.assertTrue(locked)
            return found

        clients = {TEAM_T: _BatchClient(), TEAM_U: _BatchClient()}
        fetch = AsyncMock(return_value=ABSENT)
        exc = None
        with (
            patch.object(gpt_invites, "_team_with_unresolved_invite", new=check_then_redemption_times_out),
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(side_effect=lambda t: clients[t])),
            patch.object(gpt_invites, "fetch_and_cache_members", new=fetch),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "reserve_default_seat", new=AsyncMock()),
            patch.object(gpt_invites, "notify_member_event", new=AsyncMock()),
        ):
            try:
                await gpt_invites.invite_gpt_member_any_team(
                    EMAIL, expires_in_to_datetime("30d"), action="invite_gpt_member"
                )
            except gpt_invites.GptInviteFailed as caught:
                exc = caught

        # 循环前检查确实什么都没看到：挡下来的只能是 _invite_to_team 里的检查。
        self.assertEqual(seen_by_pre_loop_check, [None])

        # 那次邀请其实到了 U：对账在 U 里看到人、确认兑换。一张码只能有一个席位。
        await self._reconcile(VISIBLE)
        seats = self._rows(
            "SELECT team_id FROM member_expiry WHERE lower(email) = ? AND kicked = 0 ORDER BY id", EMAIL
        )
        self.assertEqual([row["team_id"] for row in seats], [TEAM_U], "一张码在两个 Team 各占了一个席位")

        self.assertIsNotNone(exc, "uncertain 兑换钉在别的 Team 时批量拉人也必须被拒")
        self.assertIn(f"#{token_use_id}", exc.reason)
        self.assertEqual(clients[TEAM_T].invites, [], "拒绝必须发生在任何上游邀请之前")
        fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
