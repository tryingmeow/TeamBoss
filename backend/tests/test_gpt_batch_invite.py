"""批量「添加 GPT 成员」选 Team 与"换 Team"边界的回归测试。

驱动 invite_gpt_member_any_team（主循环找真空位，超额循环在管理员确认过的 Team 上
超员）和它对单个 Team 调用的 _invite_to_team。

1. 候选：订阅已过期的 Team 不是候选；没有空位时要求超额确认；没有副作用的失败
   （没空位 / 上游明确拒绝）照常换下一个 Team。
2. 邮箱已在 Team A（缓存或现拉名单里），或 Team A 的名单拉不到，原来都会接着去
   Team B 再拉一次，同一个人在两个 Team 各占一个席位、各有一条到期记录。现在这两种
   情况都在 Team A 就终止（GptInviteFailed），主循环和超额循环都一样；结果不明确的
   邀请也同样终止，不再换 Team 重发。缓存名单里已在任何一个未过期 Team 的邮箱（哪怕
   那个 Team 已满、或排在候选后面）在试第一个 Team 之前就终止。
3. 调用方传进 _invite_to_team 的缓存快照可能是旧的：缓存"没有"不能当"不在"的证据，
   发邀请前在锁内现拉一次，拉不到则失败关闭。
4. 2xx 但本次邮箱在 ``errored_emails`` 里（分类见 test_invite_classification）：
   不算拉进来，不记到期。

经批量路由的结果判定与"仅重试失败邮箱"见 test_gpt_batch_invite_outcome；未结兑换
挡住批量拉人见 test_open_redemption_guards。
"""

import _isolation  # noqa: F401  must precede any app import
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import direct_call, start_temp_db_async
from _invite_fixtures import InviteClient, client_returning

from app.services import gpt_invites
from app.services.team_locks import release_default_seat_reservation, reserved_default_seats


EMAIL = "member@example.com"
TEAM_A = "g4-team-a"
TEAM_B = "g4-team-b"


class DummyClient:
    def invite_member(self, email: str, seat_type: str):
        return {"email": email, "seat_type": seat_type}


def _team(
    team_id: str,
    *,
    created_at: str,
    active_until: str | None = None,
    will_renew: int = 1,
    seats_entitled: int = 2,
) -> dict:
    """invite_gpt_member_any_team / _invite_to_team 读到的一行 Team。"""
    return {
        "id": team_id,
        "name": team_id,
        "owner_email": f"owner-{team_id}@example.com",
        "seats_in_use": 1,
        "seats_entitled": seats_entitled,
        "codex_count": 0,
        "chatgpt_count": 1,
        "created_at": created_at,
        "active_until": active_until,
        "will_renew": will_renew,
    }


# ── 1. 候选与没有副作用的失败 ─────────────────────────────────────────────────

class GptInvitesTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_team_is_not_an_invite_candidate(self):
        teams = [
            _team(
                "expired-team",
                created_at="2026-01-01T00:00:00+00:00",
                active_until="2020-01-01T00:00:00+00:00",
            ),
            _team("available-team", created_at="2026-01-02T00:00:00+00:00"),
        ]
        caches = {
            "expired-team": {"members": [], "pending_invites": []},
            "available-team": {"members": [], "pending_invites": []},
        }

        with patch.object(
            gpt_invites,
            "reserved_default_seats",
            new=AsyncMock(return_value=0),
        ):
            candidates = await gpt_invites._build_gpt_invite_candidates(teams, caches)

        self.assertEqual([item["id"] for item in candidates], ["available-team"])

    async def test_non_capacity_invite_error_is_not_reported_as_no_seat(self):
        teams = [_team("team-a", created_at="2026-01-01T00:00:00+00:00")]
        caches = {"team-a": {"members": [], "pending_invites": []}}

        with (
            patch.object(gpt_invites, "_load_active_team_rows", new=AsyncMock(return_value=teams)),
            patch.object(gpt_invites, "_load_member_caches", new=AsyncMock(return_value=caches)),
            patch.object(gpt_invites, "reserved_default_seats", new=AsyncMock(return_value=0)),
            patch.object(gpt_invites, "_team_with_unresolved_invite", new=AsyncMock(return_value=None)),
            patch.object(gpt_invites, "find_open_redemption", new=AsyncMock(return_value=None)),
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=DummyClient())),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(
                gpt_invites,
                "run_chatgpt_call",
                new=AsyncMock(return_value={"error": "invalid account", "_mutation_status": "rejected"}),
            ),
            patch.object(
                gpt_invites,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [], "pending_invites": []}),
            ),
            patch.object(gpt_invites, "log_operation", new=AsyncMock()),
        ):
            with self.assertRaises(gpt_invites.GptInviteFailed) as ctx:
                await gpt_invites.invite_gpt_member_any_team("user@example.com", None)

        self.assertEqual(ctx.exception.reason, "invalid account")

    async def test_uncertain_invite_stops_before_trying_another_team(self):
        teams = [
            _team("team-a", created_at="2026-01-01T00:00:00+00:00"),
            _team("team-b", created_at="2026-01-02T00:00:00+00:00"),
        ]
        caches = {
            "team-a": {"members": [], "pending_invites": []},
            "team-b": {"members": [], "pending_invites": []},
        }

        with (
            patch.object(gpt_invites, "_load_active_team_rows", new=AsyncMock(return_value=teams)),
            patch.object(gpt_invites, "_load_member_caches", new=AsyncMock(return_value=caches)),
            patch.object(gpt_invites, "reserved_default_seats", new=AsyncMock(return_value=0)),
            patch.object(gpt_invites, "_team_with_unresolved_invite", new=AsyncMock(return_value=None)),
            patch.object(gpt_invites, "find_open_redemption", new=AsyncMock(return_value=None)),
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=DummyClient())),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(
                gpt_invites,
                "run_chatgpt_call",
                new=AsyncMock(
                    return_value={
                        "error": "timeout",
                        "_mutation_status": "uncertain",
                    }
                ),
            ) as invite_call,
            patch.object(
                gpt_invites,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [], "pending_invites": []}),
            ),
            patch.object(gpt_invites, "record_uncertain_invite", new=AsyncMock()) as record_uncertain,
            patch.object(gpt_invites, "log_operation", new=AsyncMock()),
        ):
            with self.assertRaises(gpt_invites.GptInviteFailed) as ctx:
                await gpt_invites.invite_gpt_member_any_team("user@example.com", None)

        self.assertEqual(ctx.exception.team_id, "team-a")
        self.assertEqual(invite_call.await_count, 1)
        record_uncertain.assert_awaited_once()

    async def test_capacity_exhaustion_still_requests_overage_confirmation(self):
        teams = [_team("team-a", created_at="2026-01-01T00:00:00+00:00")]
        caches = {"team-a": {"members": [], "pending_invites": []}}

        with (
            patch.object(gpt_invites, "_load_active_team_rows", new=AsyncMock(return_value=teams)),
            patch.object(gpt_invites, "_load_member_caches", new=AsyncMock(return_value=caches)),
            patch.object(gpt_invites, "reserved_default_seats", new=AsyncMock(return_value=0)),
            patch.object(gpt_invites, "_team_with_unresolved_invite", new=AsyncMock(return_value=None)),
            patch.object(gpt_invites, "find_open_redemption", new=AsyncMock(return_value=None)),
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=DummyClient())),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(False, "no_gpt_seat: full"))),
            patch.object(
                gpt_invites,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [], "pending_invites": []}),
            ),
            patch.object(gpt_invites, "log_operation", new=AsyncMock()),
        ):
            with self.assertRaises(gpt_invites.NoGptSeatAvailable) as ctx:
                await gpt_invites.invite_gpt_member_any_team("user@example.com", None)

        self.assertEqual(ctx.exception.reason, "no_gpt_seat: full")


# ── 2. 已在 Team A / 拉不到 Team A 名单 → 终止，不换 Team ─────────────────────

class _RecordingClient:
    def __init__(self, team_id, invite_result=None):
        self.team_id = team_id
        self.invites = []
        self.invite_result = invite_result

    def invite_member(self, email, seat_type="default"):
        self.invites.append(email)
        if self.invite_result is not None:
            return dict(self.invite_result)
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


class GptBatchInviteStaysInTeamTest(unittest.IsolatedAsyncioTestCase):
    """真实 init_database() 建表，落在临时目录里；只替换上游客户端、现拉名单和现拉空位。"""

    async def asyncSetUp(self):
        await self._start_db()
        for team_id in (TEAM_A, TEAM_B):
            self.addCleanup(self._release, team_id)

    async def _start_db(self):
        self.db_path = await start_temp_db_async(self)

    async def _release(self, team_id):
        await release_default_seat_reservation(team_id, EMAIL)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_team(self, team_id, *, created_at, members=(), pending=()):
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, owner_email, access_token, device_id,
                                  seats_in_use, seats_entitled, codex_count, chatgpt_count,
                                  active_until, will_renew, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?, ?, 1, 5, 0, 1, NULL, 1, ?, ?)""",
            (team_id, team_id.upper(), f"owner-{team_id}@example.com", f"tok-{team_id}",
             f"dev-{team_id}", created_at, created_at),
        )
        conn.execute(
            "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, ?, ?, ?)",
            (team_id, json.dumps(list(members)), json.dumps(list(pending)), created_at),
        )
        conn.commit()
        conn.close()

    def _rows(self, sql, *params):
        conn = self._conn()
        rows = [dict(row) for row in conn.execute(sql, params)]
        conn.close()
        return rows

    def _expiry_rows(self, team_id):
        return self._rows("SELECT team_id, email, expires_at FROM member_expiry WHERE team_id = ?", team_id)

    def _reconciliation_rows(self):
        return self._rows("SELECT team_id, email, kind, resolved FROM pending_invite_reconciliations")

    def _teams(self, *, cache_a_members=()):
        # 候选按缓存空位多少排序：Team B 占两个别人的席位，保证总是先走到 Team A。
        self._insert_team(TEAM_A, created_at="2026-10-01T00:00:00+00:00", members=cache_a_members)
        self._insert_team(
            TEAM_B,
            created_at="2026-10-02T00:00:00+00:00",
            members=[{"email": f"other{i}@example.com", "seat_type": "default"} for i in (1, 2)],
        )

    async def _run(self, *, live, capacity=None, allow_overage=False, invite_results=None):
        """live / capacity: team_id -> 依次返回的值（Exception 实例则抛出）。"""
        invite_results = invite_results or {}
        clients = {t: _RecordingClient(t, invite_results.get(t)) for t in (TEAM_A, TEAM_B)}
        live = {t: list(v) for t, v in live.items()}
        capacity = {t: list(v) for t, v in (capacity or {}).items()}
        fetched = []

        async def _fetch(team_id, client):
            fetched.append(team_id)
            value = live[team_id].pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        async def _available(client, team_id, *, email):
            queue = capacity.get(team_id)
            return queue.pop(0) if queue else (True, "available=1")

        with (
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(side_effect=lambda t: clients[t])),
            patch.object(gpt_invites, "fetch_and_cache_members", new=AsyncMock(side_effect=_fetch)),
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(side_effect=_available)),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "notify_member_event", new=AsyncMock()),
        ):
            try:
                result = await gpt_invites.invite_gpt_member_any_team(
                    EMAIL,
                    None,
                    allow_overage=allow_overage,
                    # 超员确认绑定在计划上：确认时两个 Team 都列在管理员看到的计划里。
                    overage_team_ids=[TEAM_A, TEAM_B] if allow_overage else [],
                    action="invite_gpt_member",
                )
                return result, None, clients, fetched
            except (gpt_invites.GptInviteFailed, gpt_invites.NoGptSeatAvailable) as exc:
                return None, exc, clients, fetched

    async def _assert_team_b_untouched(self, clients):
        self.assertEqual(clients[TEAM_B].invites, [])
        self.assertEqual(self._expiry_rows(TEAM_B), [])
        self.assertEqual(await reserved_default_seats(TEAM_B), 0)
        self.assertEqual([r for r in self._reconciliation_rows() if r["team_id"] == TEAM_B], [])

    # 主循环

    async def test_cached_member_of_team_a_is_not_invited_into_team_b(self):
        self._teams(cache_a_members=[{"email": "Member@Example.com", "seat_type": "default"}])
        absent = {"members": [], "pending_invites": []}

        result, exc, clients, fetched = await self._run(live={TEAM_A: [], TEAM_B: [absent, absent]})

        self.assertIsNone(result)
        self.assertIsInstance(exc, gpt_invites.GptInviteFailed)
        self.assertEqual(exc.team_id, TEAM_A)
        self.assertIn(TEAM_A.upper(), exc.reason)
        self.assertEqual(fetched, [])
        self.assertEqual(clients[TEAM_A].invites, [])
        await self._assert_team_b_untouched(clients)
        self.assertEqual(self._expiry_rows(TEAM_A), [])

    async def test_cached_member_of_a_full_team_is_not_invited_into_team_b(self):
        # Team A 五个席位全满（含本人），不是候选，主循环根本不会走到它。
        self._teams(cache_a_members=[{"email": EMAIL, "seat_type": "default"}] + [
            {"email": f"a{i}@example.com", "seat_type": "default"} for i in range(4)
        ])
        absent = {"members": [], "pending_invites": []}

        result, exc, clients, fetched = await self._run(live={TEAM_A: [], TEAM_B: [absent, absent]})

        self.assertIsNone(result)
        self.assertIsInstance(exc, gpt_invites.GptInviteFailed)
        self.assertEqual(exc.team_id, TEAM_A)
        self.assertEqual(fetched, [])
        await self._assert_team_b_untouched(clients)

    async def test_live_member_of_team_a_is_not_invited_into_team_b(self):
        self._teams()
        absent = {"members": [], "pending_invites": []}
        in_a = {"members": [], "pending_invites": [{"email": EMAIL}]}

        result, exc, clients, fetched = await self._run(live={TEAM_A: [in_a], TEAM_B: [absent, absent]})

        self.assertIsNone(result)
        self.assertIsInstance(exc, gpt_invites.GptInviteFailed)
        self.assertEqual(exc.team_id, TEAM_A)
        self.assertEqual(fetched, [TEAM_A])
        self.assertEqual(clients[TEAM_A].invites, [])
        await self._assert_team_b_untouched(clients)

    async def test_team_a_lookup_failure_does_not_fall_through_to_team_b(self):
        self._teams()
        absent = {"members": [], "pending_invites": []}
        failure = HTTPException(status_code=502, detail="pagination did not terminate")

        result, exc, clients, fetched = await self._run(live={TEAM_A: [failure], TEAM_B: [absent, absent]})

        self.assertIsNone(result)
        self.assertIsInstance(exc, gpt_invites.GptInviteFailed)
        self.assertEqual(exc.team_id, TEAM_A)
        self.assertIn("pagination did not terminate", exc.reason)
        self.assertEqual(fetched, [TEAM_A])
        self.assertEqual(clients[TEAM_A].invites, [])
        await self._assert_team_b_untouched(clients)

    # 超额循环：两个 Team 在主循环里都没有空位，超额循环第二次走到 Team A

    async def test_overage_loop_live_member_of_team_a_is_not_invited_into_team_b(self):
        self._teams()
        absent = {"members": [], "pending_invites": []}
        in_a = {"members": [{"email": EMAIL, "seat_type": "default"}], "pending_invites": []}
        full = (False, "no_gpt_seat: full")

        result, exc, clients, fetched = await self._run(
            live={TEAM_A: [absent, in_a], TEAM_B: [absent, absent, absent]},
            capacity={TEAM_A: [full], TEAM_B: [full]},
            allow_overage=True,
        )

        self.assertIsNone(result)
        self.assertIsInstance(exc, gpt_invites.GptInviteFailed)
        self.assertEqual(exc.team_id, TEAM_A)
        self.assertEqual(fetched, [TEAM_A, TEAM_B, TEAM_A])
        self.assertEqual(clients[TEAM_A].invites, [])
        await self._assert_team_b_untouched(clients)

    async def test_overage_loop_team_a_lookup_failure_does_not_fall_through_to_team_b(self):
        self._teams()
        absent = {"members": [], "pending_invites": []}
        full = (False, "no_gpt_seat: full")

        result, exc, clients, fetched = await self._run(
            live={TEAM_A: [absent, RuntimeError("members page 3 failed")], TEAM_B: [absent, absent, absent]},
            capacity={TEAM_A: [full], TEAM_B: [full]},
            allow_overage=True,
        )

        self.assertIsNone(result)
        self.assertIsInstance(exc, gpt_invites.GptInviteFailed)
        self.assertEqual(exc.team_id, TEAM_A)
        self.assertIn("members page 3 failed", exc.reason)
        self.assertEqual(fetched, [TEAM_A, TEAM_B, TEAM_A])
        self.assertEqual(clients[TEAM_A].invites, [])
        await self._assert_team_b_untouched(clients)

    async def test_overage_loop_uncertain_invite_stays_with_team_a(self):
        self._teams()
        absent = {"members": [], "pending_invites": []}
        full = (False, "no_gpt_seat: full")

        result, exc, clients, fetched = await self._run(
            # Team A：主循环现拉、超额循环现拉、结果不明确后的复查 —— 都看不到这个人。
            live={TEAM_A: [absent, absent, absent], TEAM_B: [absent, absent, absent]},
            capacity={TEAM_A: [full], TEAM_B: [full]},
            allow_overage=True,
            invite_results={TEAM_A: {"error": "timed out", "_mutation_status": "uncertain"}},
        )

        self.assertIsNone(result)
        self.assertIsInstance(exc, gpt_invites.GptInviteFailed)
        self.assertEqual(exc.team_id, TEAM_A)
        self.assertEqual(clients[TEAM_A].invites, [EMAIL])
        await self._assert_team_b_untouched(clients)
        pending = self._reconciliation_rows()
        self.assertEqual([(r["team_id"], r["email"]) for r in pending], [(TEAM_A, EMAIL)])

    # 对照：Team A 没有副作用的失败（没空位 / 上游明确拒绝）仍然换下一个 Team

    async def test_capacity_or_rejection_in_team_a_still_moves_on_to_team_b(self):
        for label, kwargs in (
            ("no seat", {"capacity": {TEAM_A: [(False, "no_gpt_seat: full")]}}),
            ("rejected", {"invite_results": {TEAM_A: {"error": "invalid email",
                                                       "_mutation_status": "rejected"}}}),
        ):
            with self.subTest(label):
                await self._start_db()
                self._teams()
                await release_default_seat_reservation(TEAM_B, EMAIL)
                absent = {"members": [], "pending_invites": []}
                result, exc, clients, _ = await self._run(
                    live={TEAM_A: [absent], TEAM_B: [absent, absent]}, **kwargs
                )

                self.assertIsNone(exc)
                self.assertEqual(result["team_id"], TEAM_B)
                self.assertEqual(clients[TEAM_B].invites, [EMAIL])
                self.assertEqual(len(self._expiry_rows(TEAM_B)), 1)
                self.assertEqual(self._expiry_rows(TEAM_A), [])


# ── 3. 缓存快照不能当"不在"的证据 ─────────────────────────────────────────────

class GptBatchInviteStaleSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def _invite_to_team(self, live_snapshot=None, *, fetch_error=None):
        client = InviteClient()
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
                _team("team-a", created_at="2026-10-01T00:00:00+00:00", seats_entitled=5),
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


# ── 4. 2xx 但本次邮箱在 errored_emails 里：不算拉进来 ─────────────────────────

class GptBatchInviteErroredEmailTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_errored_email_is_not_recorded_as_added(self):
        client = client_returning(
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
                _team("team-a", created_at="2026-10-01T00:00:00+00:00", seats_entitled=5),
                EMAIL, None, check_capacity=True, action="invite_gpt_member",
            )

        self.assertIsNone(added)
        self.assertIn("Invalid email", error)
        record.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
