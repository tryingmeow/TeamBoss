import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import member_cache_service
from app.services import team_health_alerts


class MemberCacheIncidentTest(unittest.IsolatedAsyncioTestCase):
    async def test_member_refresh_does_not_resolve_full_sync_incident(self):
        snapshot = {"members": [], "pending_invites": []}

        with (
            patch.object(
                member_cache_service,
                "_fetch_and_cache_members_impl",
                new=AsyncMock(return_value=snapshot),
            ),
            patch.object(
                team_health_alerts,
                "report_team_recovery",
                new=AsyncMock(),
            ) as report_recovery,
        ):
            result = await member_cache_service.fetch_and_cache_members("team-1", object())

        self.assertEqual(result, snapshot)
        report_recovery.assert_awaited_once_with(
            "team-1",
            "chatgpt_auth",
            source="member_refresh",
        )


if __name__ == "__main__":
    unittest.main()
