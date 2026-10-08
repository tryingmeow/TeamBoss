"""每个 Team 的超员策略设置：PATCH /api/teams/{team_id}/overage-policy。

只改本地一列：返回与 GET /api/teams 相同形状的 Team，记 set_overage_policy 日志，
非法值 422、不存在的 Team 404。全局 skip_overage_confirmation 照收不报错，但不再生效。
"""

import _isolation  # noqa: F401  must precede any app import
from _seat_fixtures import TempDbMixin

import asyncio
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import SettingsUpdate
from app.routes import settings as settings_route
from app.routes import team_policy, teams


TEAM = "prem-policy-team"


class OveragePolicyEndpointTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self._start_db()
        self.insert_team(TEAM, policy="confirm")
        app = FastAPI()
        app.include_router(teams.router)
        app.include_router(team_policy.router)
        self.http = TestClient(app)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)

    def test_set_and_get_round_trip(self):
        for policy, previous in (("forbid", "confirm"), ("auto", "forbid"), ("confirm", "auto")):
            with self.subTest(policy=policy):
                resp = self.http.patch(f"/api/teams/{TEAM}/overage-policy", json={"overage_policy": policy})
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                self.assertEqual(body["id"], TEAM)
                self.assertEqual(body["overage_policy"], policy)

                listed = {t["id"]: t for t in self.http.get("/api/teams").json()}
                self.assertEqual(set(body), set(listed[TEAM]), "形状必须和 GET /api/teams 的条目一致")
                self.assertEqual(listed[TEAM]["overage_policy"], policy)
                self.assertEqual(self.http.get(f"/api/teams/{TEAM}").json()["overage_policy"], policy)

                log = self.logs("set_overage_policy")[-1]
                self.assertEqual(log["team_id"], TEAM)
                self.assertEqual(log["result"], "success")
                self.assertEqual(log["detail"], f"overage_policy={policy}, previous={previous}")

    def test_invalid_value_is_422_and_nothing_changes(self):
        for bad in ("never", "", None, "AUTO"):
            with self.subTest(bad=bad):
                resp = self.http.patch(f"/api/teams/{TEAM}/overage-policy", json={"overage_policy": bad})
                self.assertEqual(resp.status_code, 422)
        self.assertEqual(self.http.get(f"/api/teams/{TEAM}").json()["overage_policy"], "confirm")
        self.assertEqual(self.logs("set_overage_policy"), [])

    def test_unknown_team_is_404(self):
        resp = self.http.patch("/api/teams/no-such-team/overage-policy", json={"overage_policy": "auto"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.logs("set_overage_policy"), [])

    def test_retired_global_toggle_is_still_accepted_but_changes_no_team(self):
        asyncio.run(settings_route.update_settings(SettingsUpdate(skip_overage_confirmation=True)))

        self.assertEqual(self.http.get(f"/api/teams/{TEAM}").json()["overage_policy"], "confirm")

    def test_retired_global_toggle_is_never_stored_or_reported_as_true(self):
        """旧页面读到 true 会在单个邀请里自动带上超员确认：存的、回的都只能是 false。"""
        conn = self._conn()
        conn.execute("UPDATE settings SET value = 'true' WHERE key = 'skip_overage_confirmation'")
        conn.commit()
        conn.close()
        self.assertEqual(
            asyncio.run(settings_route.get_settings())["skip_overage_confirmation"]["value"], "false",
            "库里还是旧的 true 时，GET 也必须回 false",
        )

        result = asyncio.run(settings_route.update_settings(SettingsUpdate(skip_overage_confirmation=True)))

        self.assertEqual(result["status"], "ok")
        conn = self._conn()
        stored = conn.execute(
            "SELECT value FROM settings WHERE key = 'skip_overage_confirmation'"
        ).fetchone()["value"]
        conn.close()
        self.assertEqual(stored, "false")
        self.assertEqual(
            asyncio.run(settings_route.get_settings())["skip_overage_confirmation"]["value"], "false"
        )


class PolicyChangeWaitsForInFlightInviteTest(TempDbMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await asyncio.to_thread(self._start_db)
        self.insert_team(TEAM, policy="auto")

    async def test_patch_waits_for_the_team_invite_lock(self):
        from app.models import OveragePolicyRequest
        from app.services.team_locks import team_invite_lock

        entered = asyncio.Event()
        release = asyncio.Event()

        async def _in_flight_invite():
            async with team_invite_lock(TEAM):
                entered.set()
                await release.wait()

        holder = asyncio.create_task(_in_flight_invite())
        await entered.wait()
        change = asyncio.create_task(
            team_policy.set_overage_policy(TEAM, OveragePolicyRequest(overage_policy="forbid"))
        )
        await asyncio.sleep(0.05)
        self.assertFalse(change.done(), "正在进行的邀请结束之前，策略不能改")
        release.set()
        await holder
        result = await change
        self.assertEqual(result["overage_policy"], "forbid")


if __name__ == "__main__":
    unittest.main()
