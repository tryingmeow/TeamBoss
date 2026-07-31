import asyncio
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chatgpt_client import ChatGPTClient
from app import chatgpt_limiter
from app import database as app_database


class FakeClient(ChatGPTClient):
    def __init__(self, access_token: str, team_id: str = "team-1"):
        super().__init__(access_token, team_id, "device-1")
        self.calls = 0

    def get_subscription(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {"error": "401 Client Error: Unauthorized", "status_code": 401}
        return {"ok": True, "authorization": self.session.headers["authorization"]}


class AlwaysUnauthorizedClient(ChatGPTClient):
    def __init__(self, access_token: str, team_id: str = "team-1"):
        super().__init__(access_token, team_id, "device-1")

    def get_subscription(self) -> dict:
        return {"error": "401 Client Error: Unauthorized", "status_code": 401}


class ChatGPTLimiterTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_dir = self.tmpdir.name

        # Patch get_db_dir to return temp directory
        self.db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.db_dir)
        self.db_dir_patch.start()

        # Initialize the database using the patched paths
        asyncio.run(app_database.init_database())

        # Get the actual database path after initialization
        self.db_path = app_database.get_db_path()

    def tearDown(self):
        self.db_dir_patch.stop()
        self.tmpdir.cleanup()

    def _insert_team(self, access_token: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO teams (id, access_token, session_token, status) VALUES (?, ?, ?, ?)",
            ("team-1", access_token, "session-1", "active"),
        )
        conn.commit()
        conn.close()

    def test_refreshes_token_and_retries_once_after_401(self):
        self._insert_team("old-token")
        client = FakeClient("old-token")
        refresh_calls = []

        def fake_refresh(session_token, proxy_url=None):
            refresh_calls.append((session_token, proxy_url))
            return {"accessToken": "new-token", "sessionToken": "new-session"}

        with (
            patch.object(ChatGPTClient, "refresh_token", staticmethod(fake_refresh)),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
            patch.object(chatgpt_limiter, "report_team_failure_sync") as report_failure,
            patch.object(chatgpt_limiter, "report_team_recovery_sync") as report_recovery,
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["authorization"], "Bearer new-token")
        self.assertEqual(refresh_calls, [("session-1", None)])
        report_failure.assert_not_called()
        report_recovery.assert_called_once_with(
            "team-1", "chatgpt_auth", source="api_401_retry"
        )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT access_token, session_token, status FROM teams WHERE id = 'team-1'").fetchone()
        conn.close()
        self.assertEqual(row, ("new-token", "new-session", "active"))

    def test_reuses_db_token_refreshed_by_another_request(self):
        self._insert_team("new-token")
        client = FakeClient("old-token")

        with (
            patch.object(ChatGPTClient, "refresh_token", side_effect=AssertionError("should not refresh")),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
            patch.object(chatgpt_limiter, "report_team_failure_sync") as report_failure,
            patch.object(chatgpt_limiter, "report_team_recovery_sync") as report_recovery,
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["authorization"], "Bearer new-token")
        report_failure.assert_not_called()
        report_recovery.assert_called_once()

    def test_reports_only_after_retry_still_returns_401(self):
        self._insert_team("old-token")
        client = AlwaysUnauthorizedClient("old-token")

        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                return_value={"accessToken": "new-token", "sessionToken": "new-session"},
            ),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
            patch.object(chatgpt_limiter, "report_team_failure_sync") as report_failure,
            patch.object(chatgpt_limiter, "report_team_recovery_sync") as report_recovery,
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["status_code"], 401)
        report_failure.assert_called_once()
        self.assertEqual(report_failure.call_args.args[:2], ("team-1", "chatgpt_auth"))
        self.assertIn("重试仍失败", report_failure.call_args.args[2])
        report_recovery.assert_not_called()

    def test_unchanged_tokens_are_not_reported_as_refresh_success(self):
        self._insert_team("old-token")
        client = AlwaysUnauthorizedClient("old-token")
        refresh = Mock(
            return_value={"accessToken": "old-token", "sessionToken": "session-1"}
        )

        with (
            patch.object(ChatGPTClient, "refresh_token", refresh),
            patch.object(
                chatgpt_limiter,
                "update_session_file_tokens",
            ) as update_session_file,
            patch.object(chatgpt_limiter, "report_team_failure_sync") as report_failure,
            patch.object(chatgpt_limiter, "report_team_recovery_sync") as report_recovery,
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["status_code"], 401)
        refresh.assert_called_once_with("session-1", None)
        update_session_file.assert_not_called()
        report_recovery.assert_not_called()
        self.assertIn("未返回新的 access token", report_failure.call_args.args[2])

        conn = sqlite3.connect(self.db_path)
        team = conn.execute(
            "SELECT access_token, session_token, status FROM teams WHERE id = 'team-1'"
        ).fetchone()
        log = conn.execute(
            """SELECT result, detail FROM operation_logs
               WHERE team_id = 'team-1' AND action = 'token_auto_refresh'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        self.assertEqual(team, ("old-token", "session-1", "token_expired"))
        self.assertEqual(log[0], "unchanged")
        self.assertIn("access_changed=0", log[1])
        self.assertIn("session_changed=0", log[1])

    def test_session_only_rotation_is_persisted_without_retrying_access_token(self):
        self._insert_team("old-token")
        client = FakeClient("old-token")

        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                return_value={
                    "accessToken": "old-token",
                    "sessionToken": "rotated-session",
                },
            ),
            patch.object(
                chatgpt_limiter,
                "update_session_file_tokens",
                return_value=True,
            ) as update_session_file,
            patch.object(chatgpt_limiter, "report_team_failure_sync") as report_failure,
            patch.object(chatgpt_limiter, "report_team_recovery_sync") as report_recovery,
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["status_code"], 401)
        self.assertEqual(client.calls, 1)
        update_session_file.assert_called_once_with(
            "team-1",
            "old-token",
            "rotated-session",
        )
        report_recovery.assert_not_called()
        self.assertIn("仅轮换 session token", report_failure.call_args.args[2])

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT access_token, session_token, status FROM teams WHERE id = 'team-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("old-token", "rotated-session", "token_expired"))

    def test_failed_refresh_uses_durable_cooldown_but_manual_force_bypasses_it(self):
        self._insert_team("old-token")
        refresh = Mock(
            side_effect=[
                {"error": "RefreshAccessTokenError"},
                {"accessToken": "new-token", "sessionToken": "new-session"},
            ]
        )

        with (
            patch.object(ChatGPTClient, "refresh_token", refresh),
            patch.object(
                chatgpt_limiter,
                "update_session_file_tokens",
                return_value=True,
            ),
        ):
            failed = chatgpt_limiter.refresh_team_auth_sync(
                "team-1",
                trigger="api_401_retry",
            )
            cooled_down = chatgpt_limiter.refresh_team_auth_sync(
                "team-1",
                trigger="api_401_retry",
            )
            forced = chatgpt_limiter.refresh_team_auth_sync(
                "team-1",
                trigger="manual_token_refresh",
                force=True,
            )

        self.assertEqual(failed.status, "failed")
        self.assertEqual(cooled_down.status, "cooldown")
        self.assertEqual(forced.status, "refreshed")
        self.assertEqual(refresh.call_count, 2)

    def test_concurrent_401s_make_one_session_refresh_request(self):
        self._insert_team("old-token")
        clients = [FakeClient("old-token"), FakeClient("old-token")]
        refresh = Mock()

        def delayed_refresh(session_token, proxy_url=None):
            time.sleep(0.1)
            return {"accessToken": "new-token", "sessionToken": "new-session"}

        refresh.side_effect = delayed_refresh
        with (
            patch.object(ChatGPTClient, "refresh_token", refresh),
            patch.object(
                chatgpt_limiter,
                "update_session_file_tokens",
                return_value=True,
            ),
            patch.object(chatgpt_limiter, "report_team_failure_sync"),
            patch.object(chatgpt_limiter, "report_team_recovery_sync"),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(
                executor.map(
                    chatgpt_limiter.run_chatgpt_call_sync,
                    [client.get_subscription for client in clients],
                )
            )

        self.assertEqual(refresh.call_count, 1)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertTrue(
            all(result["authorization"] == "Bearer new-token" for result in results)
        )


if __name__ == "__main__":
    unittest.main()
