"""app.chatgpt_limiter: token refresh around ChatGPT calls, and when a Team's auth counts as rejected."""

import _isolation  # noqa: F401  must precede any app import
import sqlite3
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import sys
import jwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app.chatgpt_client import ChatGPTClient
from app import chatgpt_limiter


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


class SuccessfulClient(ChatGPTClient):
    def __init__(self, access_token: str, team_id: str = "team-1"):
        super().__init__(access_token, team_id, "device-1")
        self.calls = 0

    def get_subscription(self) -> dict:
        self.calls += 1
        return {"ok": True, "authorization": self.session.headers["authorization"]}


class _TeamDbCase(unittest.TestCase):
    """A temp database holding one Team ("team-1", session "session-1"). Holds no tests."""

    def setUp(self):
        self.db_path = start_temp_db(self)

    def _insert_team(self, access_token: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO teams (id, access_token, session_token, status) VALUES (?, ?, ?, ?)",
            ("team-1", access_token, "session-1", "active"),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _token_expiring_in(delta: timedelta) -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "iat": int(now.timestamp()),
                "exp": int((now + delta).timestamp()),
            },
            key="",
            algorithm="none",
        )


class ChatGPTLimiterTest(_TeamDbCase):
    def test_proactively_refreshes_access_token_before_expiry(self):
        old_token = self._token_expiring_in(timedelta(hours=12))
        new_token = self._token_expiring_in(timedelta(days=10))
        self._insert_team(old_token)
        client = SuccessfulClient(old_token)
        refresh = Mock(
            return_value={"accessToken": new_token, "sessionToken": "new-session"}
        )

        with (
            patch.object(ChatGPTClient, "refresh_token", refresh),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["authorization"], f"Bearer {new_token}")
        refresh.assert_called_once_with("session-1", None)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT access_token, session_token, status FROM teams WHERE id = 'team-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (new_token, "new-session", "active"))

    def test_proactive_unchanged_token_keeps_live_session_active(self):
        old_token = self._token_expiring_in(timedelta(hours=12))
        self._insert_team(old_token)
        client = SuccessfulClient(old_token)

        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                return_value={
                    "accessToken": old_token,
                    "sessionToken": "rotated-session",
                },
            ),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["ok"], True)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT access_token, session_token, status FROM teams WHERE id = 'team-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (old_token, "rotated-session", "active"))

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
        self.assertEqual(team, ("old-token", "session-1", "active"))
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
        self.assertEqual(row, ("old-token", "rotated-session", "active"))

    def test_upstream_revoked_token_is_flagged_without_leaving_the_active_scan(self):
        """会话应答、但交回同一个 token 且业务接口 401 = 上游吊销。

        必须留下可观测的痕迹（``auth_state='rejected'``），同时 ``status`` 保持
        ``active`` —— scheduler / patrol / 成员缓存都按 ``status = 'active'`` 取
        Team，一旦改掉状态这个 Team 就会掉出定时同步、再也不会重试。
        """
        self._insert_team("old-token")
        client = AlwaysUnauthorizedClient("old-token")

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
            ),
            patch.object(chatgpt_limiter, "report_team_failure_sync"),
            patch.object(chatgpt_limiter, "report_team_recovery_sync"),
        ):
            chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT status, auth_state, auth_state_since FROM teams WHERE id = 'team-1'"
        ).fetchone()
        conn.close()

        self.assertEqual(row[0], "active")
        self.assertEqual(row[1], "rejected")
        self.assertIsNotNone(row[2])

    def test_auth_rejected_clears_once_a_new_access_token_arrives(self):
        self._insert_team("old-token")

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
                chatgpt_limiter, "update_session_file_tokens", return_value=True
            ),
            patch.object(chatgpt_limiter, "report_team_failure_sync"),
            patch.object(chatgpt_limiter, "report_team_recovery_sync"),
        ):
            chatgpt_limiter.run_chatgpt_call_sync(
                AlwaysUnauthorizedClient("old-token").get_subscription
            )

        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                return_value={
                    "accessToken": "fresh-token",
                    "sessionToken": "fresh-session",
                },
            ),
            patch.object(
                chatgpt_limiter, "update_session_file_tokens", return_value=True
            ),
            patch.object(chatgpt_limiter, "report_team_failure_sync"),
            patch.object(chatgpt_limiter, "report_team_recovery_sync"),
        ):
            chatgpt_limiter.refresh_team_auth_sync(
                "team-1",
                trigger="manual_token_refresh",
                force=True,
            )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT status, auth_state, auth_state_since FROM teams WHERE id = 'team-1'"
        ).fetchone()
        conn.close()

        self.assertEqual(row, ("active", "ok", None))

    def test_explicit_session_401_marks_rejected_access_as_expired(self):
        self._insert_team("old-token")
        client = AlwaysUnauthorizedClient("old-token")

        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                return_value={
                    "error": "401 Client Error: Unauthorized",
                    "status_code": 401,
                },
            ),
            patch.object(chatgpt_limiter, "report_team_failure_sync"),
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["status_code"], 401)
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM teams WHERE id = 'team-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "token_expired")

    def test_transient_session_refresh_error_does_not_kill_team(self):
        self._insert_team("old-token")
        client = AlwaysUnauthorizedClient("old-token")

        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                return_value={"error": "503 Server Error", "status_code": 503},
            ),
            patch.object(chatgpt_limiter, "report_team_failure_sync"),
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["status_code"], 401)
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM teams WHERE id = 'team-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "active")

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

    def test_proactive_cooldown_does_not_block_the_401_retry_path(self):
        """主动刷新的 1 小时冷却不得顺延到业务 401 的自救路径上。

        ``partial``（session 轮换、access token 未变）是主动刷新的常见结果，会写下
        一条负面日志。若冷却时长按历史 action 取，此后一小时内的真 401 会被一并挡
        掉，Team 持续 401 且不再尝试刷新。
        """
        self._insert_team("old-token")
        attempted_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """INSERT INTO operation_logs
                   (team_id, action, result, trigger_type, created_at)
               VALUES (?, 'token_proactive_refresh', 'partial', 'auto_refresh', ?)""",
            ("team-1", attempted_at.isoformat()),
        )
        conn.commit()

        now = datetime.now(timezone.utc)
        try:
            proactive = chatgpt_limiter._cooldown_until(
                conn, "team-1", now, "scheduled_expiry_refresh"
            )
            reactive = chatgpt_limiter._cooldown_until(
                conn, "team-1", now, "api_401_retry"
            )
        finally:
            conn.close()

        # 主动刷新自己仍受 1 小时冷却约束。
        self.assertIsNotNone(proactive)
        # 但 30 分钟前的那次尝试早已越过 401 路径的 10 分钟冷却。
        self.assertIsNone(reactive)

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


# NextAuth 刷新失败时的真实形状：HTTP 200，body 带 error、不带新 token。
# ChatGPTClient.refresh_token 会给它补上 status_code=200（见 test_chatgpt_transport 的 RefreshTokenClientShapeTest）。
REFRESH_ACCESS_TOKEN_ERROR = {"error": "RefreshAccessTokenError", "status_code": 200}


class AuthRejectedClassificationTest(_TeamDbCase):
    """access token 用不了 + 会话端点明确交不出新 token => auth_state='rejected'。"""

    def _auth_row(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT status, COALESCE(auth_state, 'ok'), auth_state_since "
                "FROM teams WHERE id = 'team-1'"
            ).fetchone()
        finally:
            conn.close()

    def _refresh(self, refresh_result, *, trigger: str, force: bool = False):
        with (
            patch.object(ChatGPTClient, "refresh_token", return_value=refresh_result),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
        ):
            return chatgpt_limiter.refresh_team_auth_sync(
                "team-1", trigger=trigger, force=force
            )

    def test_expired_token_and_refresh_access_token_error_marks_rejected_end_to_end(self):
        """生产上的形状：JWT exp 已过，每次刷新都是 RefreshAccessTokenError。"""
        expired = self._token_expiring_in(timedelta(hours=-12))
        self._insert_team(expired)
        client = AlwaysUnauthorizedClient(expired)

        with (
            patch.object(
                ChatGPTClient, "refresh_token", return_value=dict(REFRESH_ACCESS_TOKEN_ERROR)
            ),
            patch.object(chatgpt_limiter, "report_team_failure_sync") as report_failure,
            patch.object(chatgpt_limiter, "report_team_recovery_sync"),
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(result["status_code"], 401)
        status, auth_state, since = self._auth_row()
        # status 必须保持 active，否则 Team 掉出 scheduler 扫描、永远不会自愈。
        self.assertEqual((status, auth_state), ("active", "rejected"))
        self.assertIsNotNone(since)
        report_failure.assert_called_once()

    def test_proactive_path_marks_expired_token_rejected(self):
        self._insert_team(self._token_expiring_in(timedelta(hours=-1)))

        outcome = self._refresh(
            dict(REFRESH_ACCESS_TOKEN_ERROR), trigger="scheduled_expiry_refresh"
        )

        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.auth_rejected)
        self.assertEqual(self._auth_row()[:2], ("active", "rejected"))

    def test_reactive_401_path_marks_rejected_even_before_jwt_exp(self):
        """业务接口 401 本身就证明 token 用不了，不必等 JWT exp。"""
        token = self._token_expiring_in(timedelta(days=5))
        self._insert_team(token)
        client = AlwaysUnauthorizedClient(token)

        with (
            patch.object(
                ChatGPTClient, "refresh_token", return_value=dict(REFRESH_ACCESS_TOKEN_ERROR)
            ),
            patch.object(chatgpt_limiter, "report_team_failure_sync") as report_failure,
            patch.object(chatgpt_limiter, "report_team_recovery_sync"),
        ):
            chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)

        self.assertEqual(self._auth_row()[:2], ("active", "rejected"))
        self.assertIn("需重新导入", report_failure.call_args.args[2])

    def test_manual_refresh_path_marks_expired_token_rejected(self):
        self._insert_team(self._token_expiring_in(timedelta(hours=-1)))

        outcome = self._refresh(
            dict(REFRESH_ACCESS_TOKEN_ERROR), trigger="manual_token_refresh", force=True
        )

        self.assertTrue(outcome.auth_rejected)
        self.assertEqual(self._auth_row()[:2], ("active", "rejected"))

    def test_session_returning_the_same_expired_token_marks_rejected(self):
        """200、无 error，但交回的还是那个已过期的 token：同样是换不出新 token。"""
        expired = self._token_expiring_in(timedelta(hours=-1))
        self._insert_team(expired)

        outcome = self._refresh(
            {"accessToken": expired, "sessionToken": "rotated-session"},
            trigger="scheduled_expiry_refresh",
        )

        self.assertEqual(outcome.status, "session_rotated")
        self.assertTrue(outcome.auth_rejected)
        self.assertEqual(self._auth_row()[:2], ("active", "rejected"))

    def test_rejection_since_is_kept_across_repeated_detections(self):
        self._insert_team(self._token_expiring_in(timedelta(hours=-1)))
        self._refresh(dict(REFRESH_ACCESS_TOKEN_ERROR), trigger="manual_token_refresh", force=True)
        first_since = self._auth_row()[2]

        self._refresh(dict(REFRESH_ACCESS_TOKEN_ERROR), trigger="manual_token_refresh", force=True)

        self.assertEqual(self._auth_row(), ("active", "rejected", first_since))

    def test_refresh_error_while_token_still_usable_stays_ok(self):
        """token 还没过期、也没被 401：主动刷新失败只是机会没抓住，不算失效。"""
        self._insert_team(self._token_expiring_in(timedelta(hours=12)))

        outcome = self._refresh(
            dict(REFRESH_ACCESS_TOKEN_ERROR), trigger="scheduled_expiry_refresh"
        )

        self.assertEqual(outcome.status, "failed")
        self.assertFalse(outcome.auth_rejected)
        self.assertEqual(self._auth_row(), ("active", "ok", None))

    def test_transient_refresh_failures_never_mark_rejected(self):
        """超时、断网、403、429、5xx：就算 token 已过期也只是这次没问成。"""
        transient_results = {
            "timeout": {
                "error": "HTTPSConnectionPool(host='chatgpt.com', port=443): Read timed out. (read timeout=60)"
            },
            "connection": {"error": "('Connection aborted.', RemoteDisconnected('closed'))"},
            "403": {"error": "403 Client Error: Forbidden", "status_code": 403},
            "429": {"error": "429 Client Error: Too Many Requests", "status_code": 429},
            "500": {"error": "500 Server Error", "status_code": 500},
            "503": {"error": "503 Server Error", "status_code": 503},
            "525": {"error": "525 Server Error: <none>", "status_code": 525},
        }
        self._insert_team(self._token_expiring_in(timedelta(hours=-1)))

        for name, refresh_result in transient_results.items():
            for trigger in ("api_401_retry", "scheduled_expiry_refresh"):
                with self.subTest(failure=name, trigger=trigger):
                    outcome = self._refresh(refresh_result, trigger=trigger, force=True)
                    self.assertEqual(outcome.status, "failed")
                    self.assertFalse(outcome.auth_rejected)
                    self.assertEqual(self._auth_row(), ("active", "ok", None))

    def test_successful_refresh_clears_rejection(self):
        self._insert_team(self._token_expiring_in(timedelta(hours=-1)))
        self._refresh(dict(REFRESH_ACCESS_TOKEN_ERROR), trigger="manual_token_refresh", force=True)
        self.assertEqual(self._auth_row()[1], "rejected")

        outcome = self._refresh(
            {
                "accessToken": self._token_expiring_in(timedelta(days=10)),
                "sessionToken": "fresh-session",
            },
            trigger="api_401_retry",
            force=True,
        )

        self.assertEqual(outcome.status, "refreshed")
        self.assertFalse(outcome.auth_rejected)
        self.assertEqual(self._auth_row(), ("active", "ok", None))


if __name__ == "__main__":
    unittest.main()
