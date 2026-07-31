import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scheduler import _merge_team_cached_data
from app.team_sync_service import cached_default_seat_type


class TeamWorkspaceSettingsCacheTest(unittest.TestCase):
    def test_cached_default_preserves_not_cached_state(self):
        self.assertIsNone(cached_default_seat_type(json.dumps({"subscription": {}})))

    def test_cached_default_normalizes_api_values(self):
        self.assertEqual(
            cached_default_seat_type(json.dumps({
                "workspace_settings": {"default_seat_type": "usage_based"},
            })),
            "usage_based",
        )
        self.assertEqual(
            cached_default_seat_type(json.dumps({
                "workspace_settings": {"default_seat_type": None},
            })),
            "default",
        )

    def test_scheduled_merge_keeps_existing_keys_and_adds_workspace_settings(self):
        merged = json.loads(_merge_team_cached_data(
            json.dumps({"member_seat_usage": {"active_chatgpt": 1}}),
            {"subscription": {"seats_entitled": 2}},
            {"default_seat_type": "usage_based"},
            "2026-07-22T12:00:00+00:00",
        ))

        self.assertEqual(merged["member_seat_usage"]["active_chatgpt"], 1)
        self.assertEqual(merged["subscription"]["seats_entitled"], 2)
        self.assertEqual(
            merged["workspace_settings"]["default_seat_type"],
            "usage_based",
        )
        self.assertEqual(
            merged["workspace_settings_cached_at"],
            "2026-07-22T12:00:00+00:00",
        )

    def test_scheduled_merge_keeps_last_good_workspace_settings_on_error(self):
        merged = json.loads(_merge_team_cached_data(
            json.dumps({
                "workspace_settings": {"default_seat_type": "default"},
                "workspace_settings_cached_at": "old",
            }),
            {"subscription": {}},
            {"error": "temporary failure"},
            "new",
        ))

        self.assertEqual(merged["workspace_settings"]["default_seat_type"], "default")
        self.assertEqual(merged["workspace_settings_cached_at"], "old")


if __name__ == "__main__":
    unittest.main()
