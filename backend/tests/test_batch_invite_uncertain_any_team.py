"""批量「添加 GPT 成员」不能把 uncertain 兑换的邮箱拉进另一个 Team。

一笔兑换在 Team U 的邀请结果不明：兑换锁成 uncertain、钉在 U，对账在 U 里看到人就
确认、在 U 记账。管理员这时批量把同一个邮箱拉进 Team T，一张码就占了两个席位。所以
批量拉人的未结兑换检查对 uncertain 不分 Team。

批量入口在循环前已经用 _team_with_unresolved_invite 挡住带未结对账行的邮箱，U 的屏障
行在场时到不了 _invite_to_team。这里复现那道检查挡不住的时序：检查读库时兑换还在 U
发邀请（pending，U 上还没有屏障），读完之后那次邀请超时、兑换在 U 锁成 uncertain 并
立屏障；批量接着在 T 的锁里查未结兑换，这时只剩一笔钉在 U 的 uncertain。

驱动真实的 invite_gpt_member_any_team、真实的循环前检查和临时目录里 init_database()
建的库；兑换侧状态用生产代码的同一组函数建；只替换上游客户端、现拉名单和现拉空位。
"""

import _isolation  # noqa: F401  must precede any app import
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes import access_tokens
from app.services import gpt_invites
from app.services.member_expiry import expires_in_to_datetime
from app.utils.durations import expiry_from_duration


EMAIL = "member@example.com"
TEAM_T = "batch-team-t"
TEAM_U = "batch-team-u"
ABSENT = {"members": [], "pending_invites": []}
VISIBLE = {"members": [], "pending_invites": [{"email": EMAIL}]}


async def _direct_call(func, *args, **kwargs):
    return func(*args, **kwargs)


class _Client:
    def __init__(self):
        self.invites = []

    def invite_member(self, email, seat_type="default"):
        self.invites.append(email)
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


class BatchInviteUncertainInAnotherTeamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        await app_database.init_database()
        self.db_path = app_database.get_db_path()
        # U 的最后一个席位给了那笔兑换（缓存里已满），批量拉人的候选只剩 T。
        others = [{"email": f"u{i}@example.com", "seat_type": "default"} for i in range(2)]
        for team_id, entitled, members in ((TEAM_T, 5, []), (TEAM_U, 2, others)):
            self._execute(
                """INSERT INTO teams (id, name, status, owner_email, access_token, device_id,
                                      seats_in_use, seats_entitled, codex_count, chatgpt_count,
                                      active_until, will_renew, created_at, updated_at)
                   VALUES (?, ?, 'active', ?, ?, ?, 0, ?, 0, 0, NULL, 1,
                           '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')""",
                team_id, team_id.upper(), f"owner-{team_id}@example.com", f"tok-{team_id}",
                f"dev-{team_id}", entitled,
            )
            self._execute(
                """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
                   VALUES (?, ?, '[]', '2026-10-01T00:00:00+00:00')""",
                team_id, json.dumps(members),
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

    async def _invite_in_flight_in_u(self):
        """兑换已在 U 发出邀请、还没拿到结果：pending + invite_pending，U 上还没有屏障。"""
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'atm_x', '30d', 1, 0, 0, '2026-10-01')""",
            (f"hash-{uuid.uuid4().hex}",),
        )
        token_id = cur.lastrowid
        conn.commit()
        conn.close()
        token_use_id = await access_tokens._reserve_token_use(
            token_id, EMAIL, expiry_from_duration("30d").isoformat()
        )
        await access_tokens._set_token_use_phase(token_use_id, "invite_pending", team_id=TEAM_U)
        return token_use_id

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

        clients = {TEAM_T: _Client(), TEAM_U: _Client()}
        fetch = AsyncMock(return_value=ABSENT)
        exc = None
        with (
            patch.object(gpt_invites, "_team_with_unresolved_invite", new=check_then_redemption_times_out),
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(side_effect=lambda t: clients[t])),
            patch.object(gpt_invites, "fetch_and_cache_members", new=fetch),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(gpt_invites, "run_chatgpt_call", new=_direct_call),
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
        with (
            patch.object(access_tokens, "_get_proxy_url", new=AsyncMock(return_value=None)),
            patch.object(access_tokens, "ChatGPTClient", lambda *a, **k: object()),
            patch.object(access_tokens, "fetch_and_cache_members", new=AsyncMock(return_value=VISIBLE)),
            patch.object(access_tokens, "log_operation", new=AsyncMock()),
        ):
            await access_tokens.reconcile_pending_redemptions()
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
