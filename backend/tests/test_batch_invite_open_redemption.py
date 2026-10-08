"""批量「添加 GPT 成员」不能叠在一笔未结兑换上。

进程重启杀掉一笔自助兑换：码已占用、兑换已在 Team T 进入 invite_pending，邀请结果
还没记下。access_token_uses 留着 result='pending'，对账任务要过 10 分钟才把它转成
uncertain 并立屏障，这之前没有任何对账行。管理员这时批量拉同一个邮箱 30 天、批量挑中
T：到期记成 now+30；对账随后在 T 里看到这个人，确认兑换，又累加 30 天，成员拿到
60 天。现在 _invite_to_team 在 team_invite_lock 之内、任何上游请求之前先查未结兑换，
有就跳过、什么都不写，这个邮箱也不再换下一个 Team。

驱动真实的 invite_gpt_member_any_team 和临时目录里 init_database() 建的库；兑换侧
状态用生产代码的同一组函数建；只替换上游客户端、现拉名单和现拉空位。
"""

import _isolation  # noqa: F401  must precede any app import
import sqlite3
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import direct_call, start_temp_db_async

from app.routes import access_tokens
from app.services import gpt_invites
from app.services.member_expiry import expires_in_to_datetime
from app.utils.durations import expiry_from_duration


UTC = timezone.utc
EMAIL = "member@example.com"
TEAM = "batch-team-1"
OTHER_TEAM = "batch-team-2"
ABSENT = {"members": [], "pending_invites": []}
VISIBLE = {"members": [], "pending_invites": [{"email": EMAIL}]}


class _Client:
    def __init__(self):
        self.invites = []

    def invite_member(self, email, seat_type="default"):
        self.invites.append(email)
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


class _BatchOpenRedemptionCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = await start_temp_db_async(self)
        # 两个空 Team，候选顺序先 TEAM 后 OTHER_TEAM（空位相同，按创建时间）。
        for team_id, created_at in ((TEAM, "2026-10-01T00:00:00+00:00"),
                                    (OTHER_TEAM, "2026-10-02T00:00:00+00:00")):
            self._execute(
                """INSERT INTO teams (id, name, status, owner_email, access_token, device_id,
                                      seats_in_use, seats_entitled, codex_count, chatgpt_count,
                                      active_until, will_renew, created_at, updated_at)
                   VALUES (?, ?, 'active', ?, ?, ?, 0, 5, 0, 0, NULL, 1, ?, ?)""",
                team_id, team_id.upper(), f"owner-{team_id}@example.com", f"tok-{team_id}",
                f"dev-{team_id}", created_at, created_at,
            )
            self._execute(
                "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, '[]', '[]', ?)",
                team_id, created_at,
            )

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

    # ── 兑换侧的状态，走生产代码的同一组函数建出来 ─────────────────────────

    async def _reserve(self, email=EMAIL, grant="30d"):
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
        return await access_tokens._reserve_token_use(
            token_id, email, nominal.isoformat() if nominal else None
        )

    async def _interrupted_invite(self, team_id=TEAM, email=EMAIL):
        """兑换在 team_id 上进入 invite_pending 后进程被杀：pending、没有任何对账行。"""
        token_use_id = await self._reserve(email)
        await access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=team_id)
        return token_use_id

    # ── 批量拉人 / 对账 ───────────────────────────────────────────────────

    async def _batch_invite(self, expires_in="30d"):
        """invite_gpt_member_any_team；现拉名单里看不到这个邮箱，现拉空位始终有。"""
        self.clients = {TEAM: _Client(), OTHER_TEAM: _Client()}
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
        with (
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(access_tokens, "fetch_and_cache_members", new=AsyncMock(return_value=snapshot)),
            patch.object(access_tokens, "log_operation", new=AsyncMock()),
        ):
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
        self.assertEqual(self.clients[TEAM].invites, [], "拒绝必须发生在任何上游邀请之前")
        self.assertEqual(self.clients[OTHER_TEAM].invites, [])
        self.get_client.assert_not_awaited()
        self.fetch.assert_not_awaited()
        self.reserve.assert_not_awaited()
        self.notify.assert_not_awaited()
        self.assertEqual(self._expiry_rows(), [])
        # 拒绝针对邮箱、与 Team 无关：只在第一个候选 Team 记一次跳过，不再换 Team。
        logs = self._skip_logs()
        self.assertEqual([row["team_id"] for row in logs], [TEAM])
        self.assertIn(f"token_use_id={token_use_id}", logs[0]["detail"])
        self.assertEqual(exc.team_id, TEAM)


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
        self.assertEqual([row["team_id"] for row in rows], [TEAM])
        days = (datetime.fromisoformat(rows[0]["expires_at"]) - datetime.now(UTC)).total_seconds() / 86400
        self.assertLess(days, 31, f"30 天码最终记成了 {days:.1f} 天：批量拉人叠在了兑换上")
        self.assertGreater(days, 29)

    async def test_batch_invite_is_refused_while_a_redemption_is_in_flight(self):
        """pending 的兑换还会换 Team（未落 Team 的查找、被拒后换下一个队重试），不论现在指向哪都拒。"""
        cases = {
            "lookup, no team yet": None,
            "invite interrupted on the first candidate": TEAM,
            "invite interrupted on another team": OTHER_TEAM,
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
        self.assertEqual(result["team_id"], TEAM)
        self.assertEqual(self.clients[TEAM].invites, [EMAIL])
        self.assertEqual([row["team_id"] for row in self._expiry_rows()], [TEAM])
        self.assertEqual(self._skip_logs(), [])


if __name__ == "__main__":
    unittest.main()
