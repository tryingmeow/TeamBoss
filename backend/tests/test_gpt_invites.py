import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from curl_cffi.const import CurlECode
from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import Timeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chatgpt_client import ChatGPTClient
from app.services import gpt_invites


class DummyClient:
    def invite_member(self, email: str, seat_type: str):
        return {"email": email, "seat_type": seat_type}


def _team(
    team_id: str,
    *,
    created_at: str,
    active_until: str | None = None,
    will_renew: int = 1,
) -> dict:
    return {
        "id": team_id,
        "name": team_id,
        "owner_email": f"owner-{team_id}@example.com",
        "seats_in_use": 1,
        "seats_entitled": 2,
        "codex_count": 0,
        "chatgpt_count": 1,
        "created_at": created_at,
        "active_until": active_until,
        "will_renew": will_renew,
    }


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

    async def test_member_of_one_team_is_not_invited_into_the_next_candidate(self):
        teams = [
            _team("team-a", created_at="2026-01-01T00:00:00+00:00"),
            _team("team-b", created_at="2026-01-02T00:00:00+00:00"),
        ]
        caches = {
            "team-a": {
                "members": [{"email": "user@example.com", "seat_type": "default"}],
                "pending_invites": [],
            },
            "team-b": {"members": [], "pending_invites": []},
        }

        with (
            patch.object(gpt_invites, "_load_active_team_rows", new=AsyncMock(return_value=teams)),
            patch.object(gpt_invites, "_load_member_caches", new=AsyncMock(return_value=caches)),
            patch.object(gpt_invites, "reserved_default_seats", new=AsyncMock(return_value=0)),
            patch.object(gpt_invites, "get_team_client", new=AsyncMock(return_value=DummyClient())) as get_client,
            patch.object(gpt_invites, "_live_gpt_available", new=AsyncMock(return_value=(True, "available=1"))),
            patch.object(gpt_invites, "run_chatgpt_call", new=AsyncMock(return_value={"invited": []})) as invite_call,
            patch.object(
                gpt_invites,
                "fetch_and_cache_members",
                new=AsyncMock(return_value={"members": [], "pending_invites": []}),
            ),
            patch.object(gpt_invites, "add_member_watch", new=AsyncMock()),
            patch.object(gpt_invites, "log_operation", new=AsyncMock()),
            # 不 mock 的话会真的写进 backend/data/app.db（生产库）
            patch.object(gpt_invites, "record_confirmed_invite", new=AsyncMock(return_value=None)) as record,
            patch.object(gpt_invites, "reserve_default_seat", new=AsyncMock()) as reserve,
        ):
            # 已在 team-a 的人不能再被拉进 team-b：一个人占两个 Team 的席位和到期记录。
            with self.assertRaises(gpt_invites.GptInviteFailed) as ctx:
                await gpt_invites.invite_gpt_member_any_team("user@example.com", None)

        self.assertEqual(ctx.exception.team_id, "team-a")
        get_client.assert_not_awaited()
        invite_call.assert_not_awaited()
        record.assert_not_awaited()
        reserve.assert_not_awaited()

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


class InviteMutationClassificationTest(unittest.TestCase):
    def setUp(self):
        self.client = ChatGPTClient("access", "team-1", "device-1")

    def test_empty_success_response_is_still_confirmed(self):
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("empty")
        self.client.session.post = Mock(return_value=response)

        result = self.client.invite_member("user@example.com")

        self.assertEqual(result["_mutation_status"], "confirmed")

    def test_timeout_is_uncertain(self):
        self.client.session.post = Mock(
            side_effect=Timeout("timed out", CurlECode.OPERATION_TIMEDOUT)
        )

        result = self.client.invite_member("user@example.com")

        self.assertEqual(result["_mutation_status"], "uncertain")

    def test_clear_400_rejection_can_be_retried(self):
        response = Response()
        response.status_code = 400
        response.ok = False
        self.client.session.post = Mock(return_value=response)

        result = self.client.invite_member("user@example.com")

        self.assertEqual(result["_mutation_status"], "rejected")


if __name__ == "__main__":
    unittest.main()
