"""同一个人同时在多个 Team 里时，各 Team 的到期必须彼此独立。

一个邮箱可以被邀请进多个 Team，每个 Team 有自己的到期时间。踢人必须按
(team_id, email) 这一条记录走：A 队到期只从 A 队移除，不能顺手把 B 队的记录
也标成已踢，更不能拿 A 队的凭据去 B 队删人。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app import database as app_database
from app import scheduler as app_scheduler
from app.routes import access_tokens
from app.services.member_expiry import extend_member_expiry
from app.services.team_locks import _member_operation_key
from app.services.tg_member_bindings import deactivate_member_binding_if_inactive_sync

EMAIL = "shared@example.com"


class FakeClient:
    """记录 remove_member 被哪个 Team 的凭据调用过。"""

    calls: list[tuple[str, str]] = []

    def __init__(self, access_token, team_id, device_id, proxy_url=None):
        self.access_token = access_token
        self.team_id = team_id

    def remove_member(self, user_id):
        FakeClient.calls.append((self.team_id, user_id))
        return {"ok": True}


class MultiTeamMembershipTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        db_dir_patch = patch.object(
            app_database, "get_db_dir", return_value=self.tmpdir.name
        )
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)

        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()

        FakeClient.calls = []

        now = datetime.now(timezone.utc)
        past = (now - timedelta(days=1)).isoformat()
        future = (now + timedelta(days=30)).isoformat()

        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            """INSERT INTO teams (id, name, status, access_token, device_id, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?, ?, ?)""",
            [
                ("team-a", "Team A", "token-a", "dev-a", past, past),
                ("team-b", "Team B", "token-b", "dev-b", past, past),
            ],
        )
        # 同一个人：A 队昨天就到期了，B 队还有 30 天。
        conn.executemany(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, source, created_at)
               VALUES (?, 'u-shared', ?, ?, 1, 0, 'system', ?)""",
            [
                ("team-a", EMAIL, past, past),
                ("team-b", EMAIL, future, past),
            ],
        )
        conn.commit()
        conn.close()

    def _rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = {
            row["team_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM member_expiry WHERE lower(email) = ?", (EMAIL,)
            )
        }
        conn.close()
        return rows

    def test_expiry_in_one_team_does_not_kick_the_same_person_from_another(self):
        with patch.object(app_scheduler, "ChatGPTClient", FakeClient), patch.object(
            app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)
        ), patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None):
            app_scheduler.auto_kick_job()

        # 只有 A 队的凭据发起了删除，B 队一次都没被碰。
        self.assertEqual(FakeClient.calls, [("team-a", "u-shared")])

        rows = self._rows()
        self.assertEqual(rows["team-a"]["kicked"], 1)
        self.assertEqual(rows["team-a"]["kick_source"], "auto_expire")
        self.assertEqual(rows["team-b"]["kicked"], 0)
        self.assertIsNone(rows["team-b"]["kicked_at"])

    def test_renewing_one_team_leaves_the_other_teams_expiry_untouched(self):
        before = self._rows()["team-b"]["expires_at"]

        asyncio.run(
            extend_member_expiry(
                "team-a", "u-shared", EMAIL, "30d", source="self_service"
            )
        )

        after = self._rows()
        self.assertEqual(after["team-b"]["expires_at"], before)
        self.assertNotEqual(after["team-a"]["expires_at"], before)

    def test_member_operation_claims_do_not_collide_across_teams(self):
        """两个 Team 同时处理同一个人时不能互相阻塞。"""
        self.assertNotEqual(
            _member_operation_key("team-a", EMAIL),
            _member_operation_key("team-b", EMAIL),
        )

    def test_telegram_binding_survives_until_every_team_membership_is_gone(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO tg_member_bindings (chat_id, email, disabled, created_at, updated_at)
               VALUES ('42', ?, 0, '2026-08-01', '2026-08-01')""",
            (EMAIL,),
        )
        conn.commit()

        # A 队踢掉后 B 队还在：绑定必须保留，否则这个人在 B 队还是有效成员
        # 却再也收不到到期提醒。
        conn.execute("UPDATE member_expiry SET kicked = 1 WHERE team_id = 'team-a'")
        conn.commit()
        deactivate_member_binding_if_inactive_sync(conn, EMAIL)
        self.assertEqual(
            conn.execute(
                "SELECT disabled FROM tg_member_bindings WHERE email = ?", (EMAIL,)
            ).fetchone()[0],
            0,
        )

        # 最后一个 Team 也没了才停用。
        conn.execute("UPDATE member_expiry SET kicked = 1 WHERE team_id = 'team-b'")
        conn.commit()
        deactivate_member_binding_if_inactive_sync(conn, EMAIL)
        self.assertEqual(
            conn.execute(
                "SELECT disabled FROM tg_member_bindings WHERE email = ?", (EMAIL,)
            ).fetchone()[0],
            1,
        )
        conn.close()


TEAM_A = {"id": "team-a", "name": "Team A", "access_token": "tok-a", "device_id": "dev-a", "proxy_id": None}
TEAM_B = {"id": "team-b", "name": "Team B", "access_token": "tok-b", "device_id": "dev-b", "proxy_id": None}


class MultiTeamSelfServiceTest(unittest.IsolatedAsyncioTestCase):
    """自助端的多团队裁决（2026-08-17 用户拍板）：

    - 查询：列出邮箱所在的全部 Team，各自的状态和到期时间；
    - 兑换续期：多 Team 时返回 Team 选项让用户自己点，兑换码不消耗；用户带
      team_id 重新提交后，服务端按实时成员列表重新核对该 Team 再续期。
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        await app_database.init_database()
        # 每个测试的库都是新的，码的 id 会重复；尝试预算是进程级单例，必须每个测试一份。
        budget = patch.object(
            access_tokens,
            "_redeem_lookup_budget",
            new=access_tokens._RedeemLookupBudget(
                per_code=6, per_code_window=3600, global_limit=30, global_window=600
            ),
        )
        budget.start()
        self.addCleanup(budget.stop)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def test_query_lists_every_team_the_email_belongs_to(self):
        async def fake_cached_members(team_id):
            if team_id == "team-a":
                return {
                    "members": [{"email": EMAIL, "id": "u-shared", "is_owner": False,
                                 "expires_at": "2026-09-01T00:00:00+00:00"}],
                    "pending_invites": [],
                    "updated_at": "2026-08-17T00:00:00+00:00",
                }
            return {
                "members": [],
                "pending_invites": [{"email": EMAIL, "expires_at": "2026-10-01T00:00:00+00:00"}],
                "updated_at": "2026-08-17T00:00:00+00:00",
            }

        with patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(access_tokens, "get_cached_members", new=fake_cached_members):
            result = await access_tokens._query_membership_status(EMAIL)

        self.assertEqual(
            [(m["team_id"], m["status"]) for m in result["memberships"]],
            [("team-a", "joined"), ("team-b", "pending")],
        )
        # 顶层字段保持第一个命中（向后兼容），提示语说明总数。
        self.assertEqual(result["team_id"], "team-a")
        self.assertEqual(result["status"], "joined")
        self.assertIn("2 个 Team", result["message"])

    async def test_email_status_is_scoped_to_the_team_the_code_landed_on(self):
        """一张码的状态只能按它实际落到的那个队算，不能跨队取第一命中。"""
        async def fake_cached_members(team_id):
            if team_id == "team-b":
                return {
                    "members": [{"email": EMAIL, "id": "u-shared", "is_owner": False,
                                 "expires_at": "2026-10-01T00:00:00+00:00"}],
                    "pending_invites": [],
                    "updated_at": "2026-08-17T00:00:00+00:00",
                }
            return {"members": [], "pending_invites": [], "updated_at": None}

        with patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(access_tokens, "get_cached_members", new=fake_cached_members):
            scoped = await access_tokens._resolve_email_status(
                EMAIL, None, team_id="team-b"
            )
            other = await access_tokens._resolve_email_status(
                EMAIL, None, team_id="team-a"
            )

        self.assertEqual(scoped["status"], "joined")
        self.assertEqual(scoped["team_id"], "team-b")
        # A 队那张码不能借 B 队的成员身份显示成"已加入"。
        self.assertEqual(other["status"], "absent")

    async def test_a_paused_team_makes_the_status_unknown_not_absent(self):
        """限定的 Team 已经不在活跃列表里时，只能说"未知"。

        说"未找到"等于告诉一个正常缴过费的成员：你的会员不存在。我们只是没有
        数据源可查——既不能跨队去别的队取答案，也不该把没数据说成没会员。
        """
        with patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A])
        ), patch.object(
            access_tokens,
            "get_cached_members",
            new=AsyncMock(return_value={"members": [], "pending_invites": [], "updated_at": None}),
        ):
            resolved = await access_tokens._resolve_email_status(
                EMAIL, "2026-12-01T00:00:00+00:00", team_id="team-b"
            )

        self.assertEqual(resolved["status"], "unknown")
        self.assertEqual(resolved["status_label"], "未知")
        self.assertEqual(resolved["expires_at"], "2026-12-01T00:00:00+00:00")

    async def test_a_legacy_kick_row_without_a_team_still_answers_a_scoped_query(self):
        """per-team 到期之前留下的 team_id 为空的踢出记录，仍然是唯一的答案。"""
        async with app_database.get_db() as db:
            await db.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at,
                    source, created_at)
                   VALUES (NULL, 'u-shared', ?, '2026-07-01T00:00:00+00:00', 1, 1,
                           '2026-07-02T00:00:00+00:00', 'system', '2026-06-01T00:00:00+00:00')""",
                (EMAIL,),
            )
            await db.commit()

        with patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens,
            "get_cached_members",
            new=AsyncMock(return_value={"members": [], "pending_invites": [], "updated_at": None}),
        ):
            resolved = await access_tokens._resolve_email_status(
                EMAIL, None, team_id="team-b"
            )

        self.assertEqual(resolved["status"], "expired_removed")

    async def _make_token(self, raw_token: str) -> int:
        async with app_database.get_db() as db:
            cursor = await db.execute(
                """INSERT INTO access_tokens
                   (token_hash, token_prefix, grant_expires_in, max_uses,
                    used_count, disabled, created_at)
                   VALUES (?, 'atm_mul', '30d', 1, 0, 0, '2026-08-17T00:00:00+00:00')""",
                (access_tokens._hash_token(raw_token),),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def _token_state(self, token_id: int):
        async with app_database.get_db() as db:
            token_row = await (
                await db.execute(
                    "SELECT used_count FROM access_tokens WHERE id = ?", (token_id,)
                )
            ).fetchone()
            use_row = await (
                await db.execute(
                    """SELECT action, result, error_message, team_id, expires_at
                       FROM access_token_uses WHERE token_id = ?""",
                    (token_id,),
                )
            ).fetchone()
            claim_count = (
                await (
                    await db.execute("SELECT COUNT(*) AS n FROM redemption_email_claims")
                ).fetchone()
            )["n"]
        return token_row, use_row, claim_count

    @staticmethod
    def _hits():
        return [
            {"kind": "member", "team": TEAM_A, "user_id": "u-shared", "is_owner": False,
             "expires_at": "2026-09-01T00:00:00+00:00", "cache_updated_at": None},
            {"kind": "member", "team": TEAM_B, "user_id": "u-shared", "is_owner": False,
             "expires_at": "2026-10-01T00:00:00+00:00", "cache_updated_at": None},
        ]

    @classmethod
    def _lookup(cls, hits=None):
        """_find_all_memberships 的替身：只返回调用方真正传进来的那些 Team 的命中。

        真实实现是按传入的 teams 逐个拉的。用一个"无论传什么都返回全部命中"的
        AsyncMock 会把"只查用户选中的那个队"这件事整个盖掉——测试会绿，代码却
        可能在拿全量列表做决定。
        """
        source = hits if hits is not None else cls._hits()

        async def _fake(email, teams, use_cache_only=False):
            wanted = {team["id"] for team in teams}
            return [hit for hit in source if hit["team"]["id"] in wanted]

        return _fake

    async def test_redeem_without_a_choice_asks_which_team_and_returns_the_token(self):
        raw_token = "atm_multiteamtest"
        token_id = await self._make_token(raw_token)

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=self._lookup()
        ):
            result = await access_tokens.redeem_access_token(
                access_tokens.RedeemAccessTokenRequest(email=EMAIL, token=raw_token),
                Mock(),
            )

        self.assertEqual(result["status"], "team_selection_required")
        self.assertEqual(
            [(c["team_id"], c["status"], c["expires_at"]) for c in result["choices"]],
            [
                ("team-a", "joined", "2026-09-01T00:00:00+00:00"),
                ("team-b", "joined", "2026-10-01T00:00:00+00:00"),
            ],
        )

        token_row, use_row, claim_count = await self._token_state(token_id)
        # 兑换码没有被消耗，占用记录如实留痕，邮箱占位释放，用户可以带 team_id 再提交。
        self.assertEqual(token_row["used_count"], 0)
        self.assertEqual(use_row["action"], "renew_multi_team_prompt")
        # 这一步不是失败：码退回了、什么都没变，用户只是还要补一个选择。写成
        # failed 会在公开兑换历史里给一次成功的续期挂上一条红色失败记录。
        self.assertEqual(use_row["result"], "notice")
        self.assertIsNone(use_row["error_message"])
        # 名义到期是这张码的面额，这次并没有授出去；留着它历史里就会显示一个
        # 从未发生过的到期时间。
        self.assertIsNone(use_row["expires_at"])
        self.assertEqual(claim_count, 0)

    async def test_team_choices_mark_teams_that_cannot_be_renewed(self):
        """Owner 和永久成员点下去必然 409，选项里必须先标出来。

        还要把"永久"和"本地没有到期记录"分开：两者的 expires_at 都是 NULL，但
        前者续期会被拒，后者一续就会给这个人新建一条到期即自动踢出的记录。
        """
        raw_token = "atm_multiteamflags"
        await self._make_token(raw_token)
        hits = self._hits()
        hits[0]["is_owner"] = True
        async with app_database.get_db() as db:
            # team-b：有记录但 expires_at 为 NULL —— 真·永久成员。
            await db.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked, source, created_at)
                   VALUES ('team-b', 'u-shared', ?, NULL, 0, 0, 'manual',
                           '2026-08-17T00:00:00+00:00')""",
                (EMAIL,),
            )
            await db.commit()

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=self._lookup(hits)
        ):
            result = await access_tokens.redeem_access_token(
                access_tokens.RedeemAccessTokenRequest(email=EMAIL, token=raw_token),
                Mock(),
            )

        choices = {c["team_id"]: c for c in result["choices"]}
        self.assertTrue(choices["team-a"]["is_owner"])
        self.assertFalse(choices["team-a"]["renewable"])
        self.assertEqual(choices["team-a"]["blocked_reason"], "owner_email")
        # team-a 本地没有到期记录：续期会新建一条，不能显示成"永不过期"。
        self.assertEqual(choices["team-a"]["expiry_state"], "unmanaged")

        self.assertFalse(choices["team-b"]["renewable"])
        self.assertEqual(choices["team-b"]["blocked_reason"], "permanent_membership")
        self.assertEqual(choices["team-b"]["expiry_state"], "permanent")

    async def test_redeem_with_a_chosen_team_renews_only_that_team(self):
        """选中 B 队的完整兑换：只查 B 队、只续 B 队、码真的被扣掉。

        这里刻意不 mock _renew_existing_membership —— 扣码/写收据/释放邮箱占位
        全都发生在它内部的 extend_member_expiry 里。把它整个替换掉，测试就再也
        看不见"这张付过钱的码到底有没有被消耗"。
        """
        raw_token = "atm_multiteamchoice"
        token_id = await self._make_token(raw_token)
        hits = self._hits()
        lookup = AsyncMock(side_effect=self._lookup(hits))
        single = AsyncMock(return_value=hits[1])

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=lookup
        ), patch.object(
            access_tokens, "_find_existing_membership", new=single
        ):
            result = await access_tokens.redeem_access_token(
                access_tokens.RedeemAccessTokenRequest(
                    email=EMAIL, token=raw_token, team_id="team-b"
                ),
                Mock(),
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "renewed_member")
        self.assertEqual(result["team_id"], "team-b")
        # 已经选定 Team 后，只实时拉这一个队：多拉的每个队都是一次多余的上游请求，
        # 而且任何一个无关 Team 的会话失效都会把这次续期一起打成 503。
        self.assertEqual(
            [team["id"] for team in lookup.await_args.args[1]], ["team-b"]
        )
        # 拿到 claim 后的复核也只对着选中的队。
        self.assertEqual(
            [team["id"] for team in single.await_args.args[1]], ["team-b"]
        )

        # 钱的部分：码必须被扣掉、收据落到选中的队、邮箱占位释放。
        token_row, use_row, claim_count = await self._token_state(token_id)
        self.assertEqual(token_row["used_count"], 1)
        self.assertEqual(use_row["action"], "renewed_member")
        self.assertEqual(use_row["result"], "success")
        self.assertEqual(use_row["team_id"], "team-b")
        self.assertEqual(claim_count, 0)

        # 到期只写进了 B 队，A 队一行都没有。
        async with app_database.get_db() as db:
            rows = await (
                await db.execute(
                    "SELECT team_id FROM member_expiry WHERE lower(email) = ?", (EMAIL,)
                )
            ).fetchall()
        self.assertEqual([row["team_id"] for row in rows], ["team-b"])

    async def test_a_chosen_team_that_vanishes_returns_the_token_instead_of_inviting(self):
        """选中的成员身份在拿锁期间没了 —— 只能退码，绝不能改邀请。

        _renew_existing_membership 拿到 claim 后重新确认，发现人已被巡检/到期任务
        踢掉时返回 None。此时若继续往下走，就会进新邀请分支，而那个分支是按空位
        多少挑队的：用户花钱指定了 B 队，结果被塞进当时最空的 A 队。
        """
        raw_token = "atm_multiteamvanish"
        token_id = await self._make_token(raw_token)
        invite = AsyncMock()

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=self._lookup()
        ), patch.object(
            access_tokens, "_renew_existing_membership", new=AsyncMock(return_value=None)
        ), patch.object(
            access_tokens, "_invite_to_available_team", new=invite
        ):
            with self.assertRaises(HTTPException) as raised:
                await access_tokens.redeem_access_token(
                    access_tokens.RedeemAccessTokenRequest(
                        email=EMAIL, token=raw_token, team_id="team-b"
                    ),
                    Mock(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        invite.assert_not_awaited()
        token_row, use_row, claim_count = await self._token_state(token_id)
        self.assertEqual(token_row["used_count"], 0)
        self.assertEqual(use_row["action"], "renew_team_choice_invalid")
        self.assertEqual(use_row["error_message"], "team_choice_vanished")
        self.assertEqual(use_row["team_id"], "team-b")
        self.assertEqual(claim_count, 0)

    async def test_redeem_with_a_stale_team_choice_is_rejected_and_returns_the_token(self):
        raw_token = "atm_multiteamstale"
        token_id = await self._make_token(raw_token)

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=self._lookup()
        ), patch.object(
            access_tokens, "_renew_existing_membership", new=AsyncMock()
        ):
            with self.assertRaises(HTTPException) as raised:
                await access_tokens.redeem_access_token(
                    access_tokens.RedeemAccessTokenRequest(
                        email=EMAIL, token=raw_token, team_id="team-gone"
                    ),
                    Mock(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        token_row, use_row, claim_count = await self._token_state(token_id)
        self.assertEqual(token_row["used_count"], 0)
        self.assertEqual(use_row["action"], "renew_team_choice_invalid")
        self.assertEqual(use_row["error_message"], "team_choice_unknown")
        # 这个 id 不属于任何活跃 Team，不能原样写进审计行：之后的查询会拿这一列
        # 当作"这张码落在哪个队"的定位键读回去。
        self.assertIsNone(use_row["team_id"])
        self.assertEqual(claim_count, 0)

    async def test_a_still_active_team_the_email_left_is_rejected_as_not_found(self):
        """Team 还在、人不在了：这是"选择已失效"，与"Team 不存在"要分开留痕。"""
        raw_token = "atm_multiteamleft"
        token_id = await self._make_token(raw_token)

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=self._lookup([self._hits()[0]])
        ):
            with self.assertRaises(HTTPException) as raised:
                await access_tokens.redeem_access_token(
                    access_tokens.RedeemAccessTokenRequest(
                        email=EMAIL, token=raw_token, team_id="team-b"
                    ),
                    Mock(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        _, use_row, claim_count = await self._token_state(token_id)
        self.assertEqual(use_row["action"], "renew_team_choice_invalid")
        self.assertEqual(use_row["error_message"], "team_choice_not_found")
        self.assertEqual(use_row["team_id"], "team-b")
        self.assertEqual(claim_count, 0)


if __name__ == "__main__":
    unittest.main()
