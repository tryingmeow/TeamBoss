import _isolation  # noqa: F401  must precede any app import
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scheduler import _merge_team_cached_data
from app.team_sync_service import cached_default_seat_type, cached_workspace_settings_at
from app.models import TeamResponse


class TeamWorkspaceSettingsCacheTest(unittest.TestCase):
    def test_workspace_timestamp_is_independent_from_full_sync(self):
        raw = json.dumps({
            "workspace_settings": {"default_seat_type": "usage_based"},
            "workspace_settings_cached_at": "2026-10-08T12:00:00+00:00",
            "overview_cached_at": "2026-10-07T12:00:00+00:00",
        })
        stamp = cached_workspace_settings_at(raw)
        self.assertEqual(stamp, "2026-10-08T12:00:00+00:00")
        model = TeamResponse.model_construct(workspace_settings_cached_at=stamp)
        self.assertEqual(model.model_dump()["workspace_settings_cached_at"], stamp)

    def test_missing_or_invalid_workspace_timestamp_stays_unknown(self):
        for raw in (None, "invalid", "[]", json.dumps({"workspace_settings_cached_at": "orphan"}),
                    json.dumps({"workspace_settings": {}, "workspace_settings_cached_at": 123}),
                    json.dumps({"workspace_settings": {}})):
            with self.subTest(raw=raw):
                self.assertIsNone(cached_workspace_settings_at(raw))

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
