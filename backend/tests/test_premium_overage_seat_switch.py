"""切换席位（PATCH /api/teams/{team_id}/members/{user_id}/seat）按超员策略把关。

旧代码切到 ChatGPT 从不查空位：满员 Team 里 Codex → ChatGPT 会让 ChatGPT 直接加购扣费。
现在切到计费类型走和邀请同一套检查；切到 Codex 不查；当前席位类型不认识的成员一律不动。
上游 change_seat_type 是唯一的写操作，被拒时绝不能发。
"""

from test_premium_overage_support import (  # noqa: I001  (_isolation first)
    FakeTeamClient,
    TempDbMixin,
    capacity_entries,
    direct_call,
)

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.models import ChangeSeatRequest
from app.routes import members
from app.services import seat_capacity
from app.services.team_locks import reserved_seats, try_acquire_member_operation_sync


TEAM = "prem-seat-team"
USER_ID = "user-123"
EMAIL = "seat.member@example.com"


def _snapshot(seat_type="usage_based", *, user_id=USER_ID, email=EMAIL):
    return {
        "members": [{"id": user_id, "email": email, "seat_type": seat_type, "status": "active"}],
        "pending_invites": [],
    }


def _full_client(**kwargs):
    return FakeTeamClient(seats_entitled=2, counts={"default": 2, "usage_based": 1}, **kwargs)


class _SeatHarness(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._start_db()
        self.track_reservation(TEAM, EMAIL)

    def switch(self, client, target, *, policy=None, allow_overage=False, snapshots=None, current="usage_based"):
        """跑真实的 change_seat；``snapshots`` 依次作为每次现拉名单的结果。"""
        if policy is not None:
            self.set_policy(TEAM, policy)
        self.client = client
        queue = list(snapshots) if snapshots is not None else None

        async def _fetch(team_id, _client):
            if queue is None:
                # 切换成功后的刷新：返回已切换后的名单。
                return _snapshot(target if client.mutations else current)
            value = queue.pop(0) if queue else _snapshot(target if client.mutations else current)
            if isinstance(value, Exception):
                raise value
            return value

        patches = [
            patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
            patch.object(members, "fetch_and_cache_members", new=AsyncMock(side_effect=_fetch)),
            patch.object(members, "run_chatgpt_call", new=direct_call),
            patch.object(seat_capacity, "run_chatgpt_call", new=direct_call),
            patch.object(members, "add_member_watch", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(
                members.change_seat(TEAM, USER_ID, ChangeSeatRequest(seat_type=target, allow_overage=allow_overage))
            ), None
        except HTTPException as exc:
            return None, exc
        finally:
            for p in patches:
                p.stop()

    def assert_no_mutation(self):
        self.assertEqual(self.client.mutations, [], "被拒时绝不能调用 change_seat_type")

    def claims(self):
        conn = self._conn()
        rows = conn.execute("SELECT * FROM member_operation_claims").fetchall()
        conn.close()
        return rows


class SwitchToChatGPTTest(_SeatHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="confirm")

    def test_codex_to_chatgpt_on_full_confirm_team_asks(self):
        _result, exc = self.switch(_full_client(), "default")

        self.assertEqual(exc.status_code, 409)
        detail = exc.detail
        self.assertEqual(detail["code"], "require_overage_confirmation")
        self.assertEqual(detail["operation"], "seat_switch")
        self.assertEqual(detail["seat_type"], "default")
        self.assertEqual(detail["message"], "切换到 ChatGPT 会让 ChatGPT 自动加购 1 个 ChatGPT 席位并扣费。")
        self.assert_no_mutation()
        log = self.logs("change_seat")[-1]
        self.assertEqual(log["result"], "skipped")
        self.assertIn("reason=overage_needs_confirmation", log["detail"])
        self.assertEqual(self.claims(), [], "成员操作占用必须释放")

    def test_confirmed_switch_proceeds_without_capacity_read(self):
        result, exc = self.switch(_full_client(), "default", allow_overage=True)

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.client.mutations, [("change_seat_type", USER_ID, "default")])
        self.assertEqual(self.client.capacity_reads, 0)
        log = self.logs("change_seat")[-1]
        self.assertEqual(log["result"], "success")
        for part in ("seat_type=default", "from_seat_type=usage_based", "allow_overage=True",
                     "policy=confirm", "overage=True", f"user_id={USER_ID}"):
            self.assertIn(part, log["detail"])

    def test_free_seat_switch_proceeds(self):
        client = FakeTeamClient(seats_entitled=3, counts={"default": 2, "usage_based": 1})

        result, exc = self.switch(client, "default")

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(client.mutations, [("change_seat_type", USER_ID, "default")])
        self.assertEqual(client.capacity_reads, 1)

    def test_forbid_full_team_is_refused(self):
        _result, exc = self.switch(_full_client(), "default", policy="forbid", allow_overage=True)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "overage_forbidden")
        self.assertIn("禁止超员", exc.detail["message"])
        self.assert_no_mutation()

    def test_live_capacity_failure_fails_closed(self):
        for policy, code in (("forbid", "overage_forbidden"), ("confirm", "require_overage_confirmation")):
            with self.subTest(policy=policy):
                _result, exc = self.switch(_full_client(fail_reads=True), "default", policy=policy)
                self.assertEqual(exc.status_code, 409)
                self.assertEqual(exc.detail["code"], code)
                self.assertTrue(exc.detail["capacity"]["capacity_unknown"])
                self.assert_no_mutation()

    def test_auto_policy_switches_without_asking(self):
        result, exc = self.switch(_full_client(), "default", policy="auto")

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(self.client.mutations, [("change_seat_type", USER_ID, "default")])
        self.assertEqual(self.client.capacity_reads, 0)


class SwitchToPremiumTest(_SeatHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="confirm")

    def test_free_premium_seat_switches_and_reserves_until_snapshot_catches_up(self):
        client = _full_client(seat_capacity=capacity_entries(default=(2, 0), prolite=(1, 1)))
        stale = _snapshot("default")

        result, exc = self.switch(
            client, "prolite", current="default", snapshots=[stale, stale, stale]
        )

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(client.mutations, [("change_seat_type", USER_ID, "prolite")])
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "prolite")), 1)

    def test_premium_is_reserved_even_when_the_refresh_already_shows_it(self):
        client = _full_client(seat_capacity=capacity_entries(prolite=(1, 1)))

        result, exc = self.switch(
            client, "prolite", current="default",
            snapshots=[_snapshot("default"), _snapshot("default"), _snapshot("prolite")],
        )

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(client.mutations, [("change_seat_type", USER_ID, "prolite")])
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "prolite")), 1)

    def test_chatgpt_is_not_reserved_when_the_refresh_already_shows_it(self):
        result, exc = self.switch(
            _full_client(), "default", policy="auto",
            snapshots=[_snapshot("usage_based"), _snapshot("usage_based"), _snapshot("default")],
        )

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "default")), 0)

    def test_no_premium_entry_is_full(self):
        client = _full_client(seat_capacity=capacity_entries(default=(2, 0)))

        _result, exc = self.switch(client, "prolite", current="default", snapshots=[_snapshot("default")] * 2)

        self.assertEqual(exc.detail["code"], "require_overage_confirmation")
        self.assertEqual(exc.detail["seat_type"], "prolite")
        self.assertIn("Premium", exc.detail["message"])
        self.assert_no_mutation()


class SwitchToCodexAndGuardsTest(_SeatHarness):
    def setUp(self):
        super().setUp()
        self.insert_team(TEAM, policy="forbid")

    def test_switch_to_codex_never_checks(self):
        result, exc = self.switch(_full_client(), "usage_based", current="default",
                                  snapshots=[_snapshot("default"), _snapshot("default")])

        self.assertIsNone(exc, getattr(exc, "detail", None))
        self.assertEqual(self.client.mutations, [("change_seat_type", USER_ID, "usage_based")])
        self.assertEqual(self.client.reads, [], "切到 Codex 空出席位，不读容量")
        self.assertEqual(asyncio.run(reserved_seats(TEAM, "usage_based")), 0)

    def test_unknown_current_seat_type_is_never_switched(self):
        _result, exc = self.switch(_full_client(), "usage_based", snapshots=[_snapshot("automation")] * 2)

        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "seat_type_unknown")
        self.assertIn("其他（automation）", exc.detail["message"])
        self.assert_no_mutation()
        self.assertEqual(self.logs("change_seat")[-1]["result"], "skipped")

    def test_same_seat_type_is_a_no_op(self):
        result, exc = self.switch(_full_client(), "usage_based", snapshots=[_snapshot("usage_based")] * 2)

        self.assertIsNone(exc)
        self.assertTrue(result["unchanged"])
        self.assert_no_mutation()

    def test_member_mid_claim_is_busy(self):
        conn = self._conn()
        owner = try_acquire_member_operation_sync(conn, TEAM, email=EMAIL, operation="patrol_kick")
        conn.close()
        self.assertIsNotNone(owner)

        _result, exc = self.switch(_full_client(), "default", policy="auto",
                                   snapshots=[_snapshot("usage_based")] * 2)

        self.assertEqual(exc.status_code, 409)
        self.assertIn("正在变更", exc.detail)
        self.assert_no_mutation()
        self.assertEqual(self.client.reads, [])

    def test_member_list_failure_is_502_and_nothing_changes(self):
        _result, exc = self.switch(_full_client(), "usage_based", snapshots=[RuntimeError("page 2 failed")])

        self.assertEqual(exc.status_code, 502)
        self.assert_no_mutation()

    def test_missing_member_is_404(self):
        other = _snapshot("usage_based", user_id="someone-else", email="x@example.com")
        _result, exc = self.switch(_full_client(), "usage_based", snapshots=[other])

        self.assertEqual(exc.status_code, 404)
        self.assert_no_mutation()


class UnknownTargetRejectedTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._start_db()
        self.insert_team(TEAM, policy="auto")

    def test_unknown_target_seat_type_is_422_before_anything_runs(self):
        app = FastAPI()
        app.include_router(members.router)
        get_client = AsyncMock()
        with patch.object(members, "get_team_client", new=get_client):
            with TestClient(app) as http:
                for target in ("automation", "", "PROLITE"):
                    with self.subTest(target=target):
                        resp = http.patch(
                            f"/api/teams/{TEAM}/members/{USER_ID}/seat",
                            json={"seat_type": target, "allow_overage": True},
                        )
                        self.assertEqual(resp.status_code, 422)
        get_client.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
