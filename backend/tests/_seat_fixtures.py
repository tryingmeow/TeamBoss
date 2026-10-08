"""Seat / overage / redemption harnesses shared by several test modules (no test cases here).

Every upstream is a stand-in that records calls; nothing here touches ChatGPT. Read
endpoints return fixed data; FakeTeamClient's write endpoints only record, so a test can
assert ``client.mutations == []`` when a request is refused.
"""

import _isolation  # noqa: F401  must precede any app import
from _fixtures import direct_call, start_temp_db

import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import database as app_database
from app.models import InviteMemberRequest
from app.routes import access_tokens, gpt_members, members
from app.services import gpt_invites, seat_capacity, team_locks
from app.services.team_locks import release_default_seat_reservation


# ── Team upstream stand-in and temp DB (overage policy, seat holds) ──────────

class FakeTeamClient:
    """一个 Team 的上游替身。

    * 读：``get_subscription`` / ``get_seat_type_counts`` / ``get_pending_invites``。
      ``fail_reads=True`` 时订阅接口回 ``{"error": ...}``（= 读失败）。
    * 写：``invite_member`` / ``change_seat_type`` 只记录到 ``mutations`` 并回成功
      （``invite_result`` 给了就回它，例如上游明确拒绝）。
      其他写接口一律记录并抛错，让误调用的用例当场失败。
    """

    def __init__(
        self,
        *,
        seats_entitled=2,
        counts=None,
        seat_capacity=None,
        pending=(),
        fail_reads=False,
        invite_result=None,
    ):
        self.seats_entitled = seats_entitled
        self.counts = dict(counts if counts is not None else {"default": 0, "usage_based": 0})
        self.seat_capacity = seat_capacity
        self.pending = list(pending)
        self.fail_reads = fail_reads
        self.invite_result = invite_result
        self.reads: list[str] = []
        self.mutations: list[tuple] = []

    # ---- 读 ----
    def get_subscription(self):
        self.reads.append("get_subscription")
        if self.fail_reads:
            return {"error": "simulated read failure"}
        sub = {
            "seats_entitled": self.seats_entitled,
            "seats_in_use": sum(self.counts.values()),
        }
        if self.seat_capacity is not None:
            sub["seat_capacity"] = self.seat_capacity
        return sub

    def get_seat_type_counts(self):
        self.reads.append("get_seat_type_counts")
        # 上游总是给出全部四种类型（没人的是 0）；Premium 空位要用到 prolite 的在用人数。
        counts = {"default": 0, "usage_based": 0, "automation": 0, "prolite": 0}
        counts.update(self.counts)
        return {"seat_type_counts": counts}

    def get_pending_invites(self, offset=0, limit=100):
        self.reads.append("get_pending_invites")
        return {"items": list(self.pending), "total": len(self.pending)}

    # ---- 写 ----
    def invite_member(self, email, seat_type="default"):
        self.mutations.append(("invite_member", email, seat_type))
        if self.invite_result is not None:
            return dict(self.invite_result)
        return {
            "account_invites": [{"email_address": email}],
            "errored_emails": [],
            "_mutation_status": "confirmed",
        }

    def change_seat_type(self, user_id, seat_type):
        self.mutations.append(("change_seat_type", user_id, seat_type))
        return {"id": user_id, "seat_type": seat_type}

    def _forbidden(self, name, *args):
        self.mutations.append((name, *args))
        raise AssertionError(f"unexpected upstream mutation: {name}")

    def remove_member(self, *args):
        self._forbidden("remove_member", *args)

    def revoke_invite(self, *args):
        self._forbidden("revoke_invite", *args)

    def resend_invite(self, *args):
        self._forbidden("resend_invite", *args)

    @property
    def capacity_reads(self) -> int:
        return self.reads.count("get_subscription")


def capacity_entries(**available_by_type):
    """``seat_capacity`` 上游形态：capacity_entries(default=(paid, available), prolite=(...))。"""
    return [
        {"type": seat_type, "paid": paid, "held": 0, "renewal_requested": paid, "available": available}
        for seat_type, (paid, available) in available_by_type.items()
    ]


class TempDbMixin:
    """每个用例一份 init_database() 建的临时库；进程内的席位预留在收尾时清掉。"""

    def _start_db(self):
        self.db_path = start_temp_db(self)
        self._reserved: list[tuple[str, str]] = []

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def insert_team(
        self,
        team_id,
        *,
        policy="confirm",
        seats_entitled=2,
        members=(),
        pending=(),
        seat_capacity=None,
        created_at="2026-10-01T00:00:00+00:00",
    ):
        active_default = sum(
            1 for m in members if (m.get("seat_type") or "default") == "default"
        )
        codex = sum(1 for m in members if m.get("seat_type") == "usage_based")
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, owner_email, access_token, device_id,
                                  seats_in_use, seats_entitled, codex_count, chatgpt_count,
                                  active_until, will_renew, created_at, updated_at,
                                  overage_policy, seat_capacity_json)
               VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, ?)""",
            (
                team_id, f"{team_id}-name", f"owner-{team_id}@example.com", f"tok-{team_id}",
                f"dev-{team_id}", len(members), seats_entitled, codex, active_default,
                created_at, created_at, policy,
                json.dumps(seat_capacity) if seat_capacity is not None else None,
            ),
        )
        conn.execute(
            "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, ?, ?, ?)",
            (team_id, json.dumps(list(members)), json.dumps(list(pending)), created_at),
        )
        conn.commit()
        conn.close()

    def set_policy(self, team_id, policy):
        conn = self._conn()
        conn.execute("UPDATE teams SET overage_policy = ? WHERE id = ?", (policy, team_id))
        conn.commit()
        conn.close()

    def logs(self, action=None):
        conn = self._conn()
        if action:
            rows = conn.execute(
                "SELECT team_id, action, target_email, detail, result, error_message "
                "FROM operation_logs WHERE action = ? ORDER BY id",
                (action,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT team_id, action, target_email, detail, result, error_message "
                "FROM operation_logs ORDER BY id"
            ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def track_reservation(self, team_id, email):
        """登记一个可能被预留的 (team, email)，收尾时释放，避免泄漏到别的测试模块。"""
        self._reserved.append((team_id, email))
        self.addCleanup(lambda: asyncio.run(release_default_seat_reservation(team_id, email)))


# ── Seat holds and member snapshots ───────────────────────────────────────

HOLDS_TEAM = "r2-holds-team"
HOLDS_USER_ID = "user-switch"
HOLDS_EMAIL = "switch.member@example.com"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


class SeatHoldsCase(TempDbMixin, unittest.TestCase):
    """TempDbMixin with reservations cleared per test, plus seat_holds / member_cache readers."""

    def setUp(self):
        self._start_db()
        team_locks._reservations.clear()
        self.addCleanup(team_locks._reservations.clear)

    def put_hold(self, email, seat_type, created_at, team_id=HOLDS_TEAM):
        conn = self._conn()
        conn.execute(
            """INSERT INTO seat_holds (team_id, email, seat_type, source, created_at)
               VALUES (?, ?, ?, 'test', ?)""",
            (team_id, email, seat_type, created_at),
        )
        conn.commit()
        conn.close()

    def holds(self, team_id=HOLDS_TEAM):
        conn = self._conn()
        rows = conn.execute(
            "SELECT email, seat_type, created_at FROM seat_holds WHERE team_id = ? ORDER BY email",
            (team_id,),
        ).fetchall()
        conn.close()
        return [tuple(row) for row in rows]

    def cache_row(self, team_id=HOLDS_TEAM):
        conn = self._conn()
        row = conn.execute(
            "SELECT members_json, pending_json, updated_at, fetch_started_at FROM member_cache "
            "WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None


class _ListClient(FakeTeamClient):
    """读接口按 offset 回预设的成员 / 邀请分页；change_seat_type 超时（结果不明）。"""

    def __init__(self, *, member_pages=None, invite_pages=None, **kwargs):
        super().__init__(**kwargs)
        self.member_pages = member_pages if member_pages is not None else []
        self.invite_pages = invite_pages if invite_pages is not None else [{"items": [], "total": 0}]
        self.switch_result = {"error": "Request timed out"}

    @staticmethod
    def _page(pages, offset, limit):
        index = offset // limit
        if index < len(pages):
            return json.loads(json.dumps(pages[index]))
        return {"items": []}

    def get_members(self, offset=0, limit=100):
        self.reads.append("get_members")
        return self._page(self.member_pages, offset, limit)

    def get_pending_invites(self, offset=0, limit=100):
        self.reads.append("get_pending_invites")
        return self._page(self.invite_pages, offset, limit)

    def change_seat_type(self, user_id, seat_type):
        self.mutations.append(("change_seat_type", user_id, seat_type))
        return dict(self.switch_result)

    def show(self, *people):
        self.member_pages = [{"items": list(people), "total": len(people)}]


@contextlib.contextmanager
def _patched(patches):
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


# ── Redemption capacity (real redeem_access_token flow) ───────────────────

REDEEM_EMAIL = "premium.redeemer@example.com"
REDEEM_CREATED = "2026-09-01T00:00:00+00:00"
NO_CHATGPT_SEAT = "没有可用 ChatGPT 席位，请联系管理员"


def _filler(count, seat_type="usage_based", prefix="filler"):
    """一页不占计费席位的待接受邀请（Codex），用来把第一页填满。"""
    return [
        {"email_address": f"{prefix}{i}@example.com", "seat_type": seat_type}
        for i in range(count)
    ]


class PagedClient:
    """``pages[i]`` 是 offset = i × limit 时的回复；超出范围返回空列表。"""

    def __init__(self, subscription, counts, pages):
        self.subscription = subscription
        self.counts = counts
        self.pages = pages
        self.pending_calls: list[tuple[int, int]] = []

    def get_subscription(self):
        return self.subscription

    def get_seat_type_counts(self):
        return {"seat_type_counts": dict(self.counts)}

    def get_pending_invites(self, offset=0, limit=100):
        self.pending_calls.append((offset, limit))
        index = offset // limit
        if index < len(self.pages):
            return self.pages[index]
        return {"items": []}


def _premium_subscription(*, paid=1, available=1):
    return {
        "seats_entitled": 5 + paid,
        "seats_in_use": 0,
        "seat_capacity": [
            {"type": "default", "paid": 5, "available": 5},
            {"type": "prolite", "paid": paid, "available": available},
        ],
    }


class RedeemFlowCase(unittest.IsolatedAsyncioTestCase):
    """Drives the real redeem_access_token against a temp DB; self.upstream feeds a fake ChatGPTClient."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AUTO_TEAM_DATA_DIR": self._tmp.name}, clear=False)
        self._env.start()
        await app_database.init_database()
        team_locks._reservations.clear()
        self.addCleanup(team_locks._reservations.clear)

        self.upstream: dict[str, dict] = {}
        self.invites: list[tuple[str, str, str]] = []
        self.holds_at_invite: list[list[tuple]] = []
        self.invite_result: dict = {"_mutation_status": "confirmed"}
        self.invite_raises: Exception | None = None
        test = self

        class FakeClient:
            def __init__(self, access_token, team_id, device_id, proxy_url=None):
                self.team_id = team_id

            def get_subscription(self):
                return json.loads(json.dumps(test.upstream[self.team_id]["subscription"]))

            def get_seat_type_counts(self):
                return {"seat_type_counts": dict(test.upstream[self.team_id]["counts"])}

            def get_pending_invites(self, offset=0, limit=100):
                pages = test.upstream[self.team_id]["pages"]
                index = offset // limit
                return json.loads(json.dumps(pages[index])) if index < len(pages) else {"items": []}

            def invite_member(self, email, seat_type="default", role="standard-user"):
                test.invites.append((self.team_id, email, seat_type))
                conn = sqlite3.connect(app_database.get_db_path())
                try:
                    test.holds_at_invite.append(
                        conn.execute("SELECT team_id, email, seat_type FROM seat_holds").fetchall()
                    )
                finally:
                    conn.close()
                if test.invite_raises is not None:
                    raise test.invite_raises
                return dict(test.invite_result)

        async def fake_fetch(team_id, client):
            return {"members": [], "pending_invites": []}

        self.notify_admins = AsyncMock(return_value=1)
        for module, target, value in (
            (access_tokens, "_check_rate_limit", AsyncMock()),
            (access_tokens, "_get_proxy_url", AsyncMock(return_value=None)),
            (access_tokens, "ChatGPTClient", FakeClient),
            (access_tokens, "fetch_and_cache_members", fake_fetch),
            (access_tokens, "add_member_watch", AsyncMock()),
            (access_tokens, "run_chatgpt_call", direct_call),
            (seat_capacity, "run_chatgpt_call", direct_call),
            (access_tokens, "notify_admins", self.notify_admins),
            (access_tokens, "_premium_notice_sent", {}),
            (access_tokens, "notify_member_event", AsyncMock(return_value=1)),
            (
                access_tokens,
                "_redeem_lookup_budget",
                access_tokens._RedeemLookupBudget(
                    per_code=100, per_code_window=3600, global_limit=1000, global_window=600
                ),
            ),
        ):
            p = patch.object(module, target, new=value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _exec(self, sql, params=()):
        async with app_database.get_db() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.lastrowid

    async def _rows(self, sql, params=()):
        async with app_database.get_db() as db:
            cursor = await db.execute(sql, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def _team(self, team_id="team-a", *, cached_premium=1):
        await self._exec(
            """INSERT INTO teams
               (id, name, status, access_token, device_id, seats_entitled, chatgpt_count,
                seat_capacity_json, overage_policy, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?, 5, 0, ?, 'auto', ?, ?)""",
            (
                team_id,
                f"Team {team_id}",
                f"tok-{team_id}",
                f"dev-{team_id}",
                json.dumps({"prolite": {"paid": cached_premium, "available": cached_premium}}),
                REDEEM_CREATED,
                REDEEM_CREATED,
            ),
        )

    def _premium_upstream(self, team_id="team-a", *, paid=1, available=1, in_use=0, pages=None):
        self.upstream[team_id] = {
            "subscription": _premium_subscription(paid=paid, available=available),
            "counts": {"default": 0, "usage_based": 0, "prolite": in_use},
            "pages": pages if pages is not None else [{"items": []}],
        }

    def _chatgpt_upstream(self, team_id="team-a", *, entitled=2, active=1, pages=None):
        self.upstream[team_id] = {
            "subscription": {"seats_entitled": entitled, "seats_in_use": active},
            "counts": {"default": active, "usage_based": 0},
            "pages": pages if pages is not None else [{"items": []}],
        }

    async def _token(self, raw, seat_type):
        return int(
            await self._exec(
                """INSERT INTO access_tokens
                   (token_hash, token_prefix, grant_expires_in, max_uses,
                    used_count, disabled, created_at, seat_type)
                   VALUES (?, ?, '30d', 1, 0, 0, ?, ?)""",
                (hashlib.sha256(raw.encode()).hexdigest(), raw[:12], REDEEM_CREATED, seat_type),
            )
        )

    async def _redeem(self, raw, *, email=REDEEM_EMAIL):
        return await access_tokens.redeem_access_token(
            access_tokens.RedeemAccessTokenRequest(email=email, token=raw, team_id=None),
            object(),
        )

    async def _assert_refused_unconsumed(self, raw, seat_type, detail, *, email=REDEEM_EMAIL):
        token_id = await self._token(raw, seat_type)
        with self.assertRaises(HTTPException) as raised:
            await self._redeem(raw, email=email)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, detail)
        token = (await self._rows("SELECT used_count FROM access_tokens WHERE id = ?", (token_id,)))[0]
        self.assertEqual(token["used_count"], 0)
        uses = await self._rows("SELECT result FROM access_token_uses WHERE token_id = ?", (token_id,))
        self.assertTrue(uses)
        self.assertTrue(all(use["result"] not in ("pending", "uncertain", "success") for use in uses))

    async def _hold_rows(self):
        return await self._rows("SELECT team_id, email, seat_type FROM seat_holds ORDER BY email")


# ── Single-team invite and batch auto-assign under the overage policy ──────

INVITE_TEAM = "prem-inv-team"
INVITE_EMAIL = "new.member@example.com"
ABSENT = {"members": [], "pending_invites": []}


def _full_default_client(**kwargs):
    """ChatGPT 席位已满：2 个已付、2 个在用。"""
    return FakeTeamClient(seats_entitled=2, counts={"default": 2, "usage_based": 0}, **kwargs)


class InviteHarness(TempDbMixin, unittest.TestCase):
    """Runs the real single-team invite route for INVITE_EMAIL into INVITE_TEAM against FakeTeamClient."""

    def setUp(self):
        self._start_db()
        self.track_reservation(INVITE_TEAM, INVITE_EMAIL)

    def invite(self, client, *, policy=None, seat_type="default", allow_overage=False, snapshot=ABSENT,
               refreshed=None, confirmation=None):
        """``snapshot``：邀请前现拉的名单；``refreshed``：邀请成功后刷新拿到的名单（默认同前）。"""
        if policy is not None:
            self.set_policy(INVITE_TEAM, policy)
        self.client = client
        patches = [
            patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(
                members,
                "fetch_and_cache_members",
                new=AsyncMock(side_effect=[snapshot, snapshot if refreshed is None else refreshed]),
            ),
            patch.object(members, "run_chatgpt_call", new=direct_call),
            patch.object(seat_capacity, "run_chatgpt_call", new=direct_call),
            patch.object(members, "add_member_watch", new=AsyncMock()),
            patch.object(members, "notify_member_event", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(
                members.invite_member(
                    INVITE_TEAM,
                    InviteMemberRequest(
                        email=INVITE_EMAIL, expires_in="30d", seat_type=seat_type, allow_overage=allow_overage,
                        overage_confirmation=confirmation,
                    ),
                )
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

    def assert_refused(self, exc, code, *, seat_type="default", unknown=False):
        self.assertIsNotNone(exc, "应当被超员策略挡下")
        self.assertEqual(exc.status_code, 409)
        detail = exc.detail
        self.assertEqual(detail["code"], code)
        self.assertEqual(detail["team_id"], INVITE_TEAM)
        self.assertEqual(detail["team_name"], f"{INVITE_TEAM}-name")
        self.assertEqual(detail["seat_type"], seat_type)
        self.assertEqual(detail["operation"], "invite")
        self.assertEqual(detail["capacity"]["seat_type"], seat_type)
        self.assertEqual(detail["capacity"]["available"], 0)
        self.assertIs(detail["capacity"]["capacity_unknown"], unknown)
        self.assertEqual(self.client.mutations, [], "被拒时绝不能发上游写请求")
        log = self.logs("invite_member")[-1]
        self.assertEqual(log["result"], "skipped")
        reason = "overage_forbidden" if code == "overage_forbidden" else "overage_needs_confirmation"
        self.assertIn(f"seat_type={seat_type}", log["detail"])
        self.assertIn(f"reason={reason}", log["detail"])
        self.assertEqual(log["error_message"], detail["message"])
        return detail

    def assert_invited(self, response, exc, seat_type="default"):
        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(response["status"], "ok")
        self.assertEqual(self.client.mutations, [("invite_member", INVITE_EMAIL, seat_type)])
        return self.logs("invite_member")[-1]


def _members(n, prefix):
    return [{"email": f"{prefix}{i}@example.com", "seat_type": "default", "status": "active"} for i in range(n)]


class BatchHarness(TempDbMixin, unittest.TestCase):
    """Runs the real batch auto-assign route over teams added with team(); one FakeTeamClient per team."""

    def setUp(self):
        self._start_db()
        self.clients: dict[str, FakeTeamClient] = {}

    def team(self, team_id, *, policy, seats=1, used=1, created_at, live_used=None, **kwargs):
        """缓存里 ``used`` 个 ChatGPT 成员；现拉时 ``live_used``（默认同缓存）个。"""
        self.insert_team(
            team_id, policy=policy, seats_entitled=seats, members=_members(used, team_id),
            created_at=created_at, **kwargs,
        )
        live = used if live_used is None else live_used
        self.clients[team_id] = FakeTeamClient(
            seats_entitled=seats, counts={"default": live, "usage_based": 0}
        )

    def submit(self, emails, *, allow_overage=False, overage_team_ids=None, overage_seat_limit=None):
        for team_id in self.clients:
            for email in emails:
                self.track_reservation(team_id, email)
        patches = [
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(side_effect=lambda t: self.clients[t])),
            patch.object(gpt_invites, "fetch_and_cache_members", new=AsyncMock(return_value=ABSENT)),
            patch.object(gpt_invites, "run_chatgpt_call", new=direct_call),
            patch.object(seat_capacity, "run_chatgpt_call", new=direct_call),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "notify_member_event", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(
                gpt_members.invite_gpt_members(
                    gpt_members.InviteGptMembersRequest(
                        emails=emails,
                        expires_in="30d",
                        allow_overage=allow_overage,
                        **({} if overage_team_ids is None else {"overage_team_ids": overage_team_ids}),
                        **({} if overage_seat_limit is None else {"overage_seat_limit": overage_seat_limit}),
                    )
                )
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

    def invited(self, team_id):
        return [m[1] for m in self.clients[team_id].mutations if m[0] == "invite_member"]

    def all_mutations(self):
        return {t: c.mutations for t, c in self.clients.items() if c.mutations}


# ── Renewal reminders ─────────────────────────────────────────────────────

NOW = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)
IN_WINDOW = "2026-10-24T06:00:00Z"  # 正好 3 天后：窗口含右端
OUT_OF_WINDOW = "2026-10-24T06:00:01Z"


def _capacity(**entries):
    """_capacity(default=(paid, available[, renewal_requested]))"""
    out = {}
    for seat_type, values in entries.items():
        entry = {"paid": values[0], "available": values[1]}
        if len(values) > 2:
            entry["renewal_requested"] = values[2]
        out[seat_type] = entry
    return json.dumps(out)


def _renewal_team(**overrides):
    team = {
        "id": "t1",
        "name": "Lab",
        "owner_email": "owner.long@example.com",
        "active_until": IN_WINDOW,
        "will_renew": 1,
        "billing_period": "monthly",
        "price_per_seat": 780.0,
        "billing_currency": "THB",
        "seat_capacity_json": _capacity(default=(2, 1, 2), prolite=(0, 0, 0)),
        "seat_type_counts_json": json.dumps({"default": 1, "prolite": 0, "usage_based": 1}),
        "sync_suspended_at": None,
    }
    team.update(overrides)
    return team
