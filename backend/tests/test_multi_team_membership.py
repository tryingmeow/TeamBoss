"""同一个人同时在多个 Team 里时，各 Team 的到期必须彼此独立。

一个邮箱可以被邀请进多个 Team，每个 Team 有自己的到期时间。踢人必须按
(team_id, email) 这一条记录走：A 队到期只从 A 队移除，不能顺手把 B 队的记录
也标成已踢，更不能拿 A 队的凭据去 B 队删人。
"""

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
    - 兑换续期：多 Team 时返回车队选项让用户自己点，兑换码不消耗；用户带
      team_id 重新提交后，服务端按实时成员列表重新核对该车队再续期。
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        await app_database.init_database()

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
                    "SELECT action, result, error_message FROM access_token_uses WHERE token_id = ?",
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

    async def test_redeem_without_a_choice_asks_which_team_and_returns_the_token(self):
        raw_token = "atm_multiteamtest"
        token_id = await self._make_token(raw_token)

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=AsyncMock(return_value=self._hits())
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
        self.assertEqual(use_row["result"], "failed")
        self.assertEqual(use_row["error_message"], "team_selection_required")
        self.assertEqual(claim_count, 0)

    async def test_redeem_with_a_chosen_team_renews_only_that_team(self):
        raw_token = "atm_multiteamchoice"
        await self._make_token(raw_token)
        hits = self._hits()
        renew = AsyncMock(
            return_value={
                "existing": hits[1],
                "action": "renewed_member",
                "expires_at": "2026-11-01T00:00:00+00:00",
            }
        )

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=AsyncMock(return_value=hits)
        ), patch.object(
            access_tokens, "_renew_existing_membership", new=renew
        ):
            result = await access_tokens.redeem_access_token(
                access_tokens.RedeemAccessTokenRequest(
                    email=EMAIL, token=raw_token, team_id="team-b"
                ),
                Mock(),
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["team_id"], "team-b")
        self.assertEqual(result["expires_at"], "2026-11-01T00:00:00+00:00")
        # 续期只能落到用户点的那个车队。
        self.assertIs(renew.await_args.args[0], hits[1])

    async def test_redeem_with_a_stale_team_choice_is_rejected_and_returns_the_token(self):
        raw_token = "atm_multiteamstale"
        token_id = await self._make_token(raw_token)

        with patch.object(
            access_tokens, "_check_rate_limit", new=AsyncMock()
        ), patch.object(
            access_tokens, "load_active_teams", new=AsyncMock(return_value=[TEAM_A, TEAM_B])
        ), patch.object(
            access_tokens, "_find_all_memberships", new=AsyncMock(return_value=self._hits())
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
        self.assertEqual(use_row["error_message"], "team_choice_not_found")
        self.assertEqual(claim_count, 0)


if __name__ == "__main__":
    unittest.main()
