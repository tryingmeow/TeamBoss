"""Team 健康告警（team_health_alerts）：失败去重、未恢复定期再提醒、没送达就重试、恢复只发一次；
以及成员名单刷新成功只解除登录类（chatgpt_auth）告警。"""
import _isolation  # noqa: F401  must precede any app import
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app import member_cache_service
from app.services import team_health_alerts


class TeamHealthAlertsTest(unittest.TestCase):
    def setUp(self):
        self.db_path = start_temp_db(self)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO teams (id, name, status) VALUES ('team-1', 'Test Team', 'active')"
        )
        conn.commit()
        conn.close()

        self.original_notify = team_health_alerts.notify_admins_sync
        self.messages: list[str] = []
        team_health_alerts.notify_admins_sync = self._notify

    def tearDown(self):
        team_health_alerts.notify_admins_sync = self.original_notify

    def _notify(self, text: str) -> int:
        self.messages.append(text)
        return 1

    def _incident(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM team_health_incidents WHERE team_id='team-1' AND alert_key='chatgpt_auth'"
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def test_unrecovered_incident_is_reminded_after_the_repeat_interval(self):
        """静音有期限：超过间隔仍未恢复必须再提醒一次。

        只发一次的话，深夜发生的故障会被后来的消息顶走，第二天没人知道还坏着。
        """
        team_health_alerts.report_team_failure_sync(
            "team-1", "chatgpt_auth", "401 Unauthorized", source="test"
        )
        self.assertEqual(len(self.messages), 1)

        # 间隔内的重复失败仍然静音。
        team_health_alerts.report_team_failure_sync(
            "team-1", "chatgpt_auth", "401 Unauthorized", source="test"
        )
        self.assertEqual(len(self.messages), 1)

        # 把上次提醒时间往前拨到超过间隔，再来一次失败就该提醒了。
        stale = (
            datetime.now(timezone.utc)
            - team_health_alerts.REPEAT_ALERT_INTERVAL
            - timedelta(minutes=1)
        ).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """UPDATE team_health_incidents
               SET last_notified_at = ?, first_failed_at = ?
               WHERE team_id='team-1' AND alert_key='chatgpt_auth'""",
            (stale, stale),
        )
        conn.commit()
        conn.close()

        reminded = team_health_alerts.report_team_failure_sync(
            "team-1", "chatgpt_auth", "401 Unauthorized", source="test"
        )

        self.assertEqual(reminded["notified"], 1)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("仍未恢复", self.messages[1])

    def test_failure_is_deduplicated_and_recovery_is_sent_once(self):
        first = team_health_alerts.report_team_failure_sync(
            "team-1",
            "chatgpt_auth",
            "401 Unauthorized",
            source="test",
        )
        repeated = team_health_alerts.report_team_failure_sync(
            "team-1",
            "chatgpt_auth",
            "401 Unauthorized again",
            source="test",
        )

        self.assertEqual(first["notified"], 1)
        self.assertEqual(repeated["reason"], "deduplicated")
        self.assertEqual(len(self.messages), 1)
        self.assertIn("🚨 Team 异常", self.messages[0])
        self.assertIn("🏢 Team：Test Team", self.messages[0])
        self.assertEqual(self._incident()["failure_count"], 2)

        recovered = team_health_alerts.report_team_recovery_sync(
            "team-1",
            "chatgpt_auth",
            source="test",
        )
        repeated_recovery = team_health_alerts.report_team_recovery_sync(
            "team-1",
            "chatgpt_auth",
            source="test",
        )

        self.assertEqual(recovered["notified"], 1)
        self.assertEqual(repeated_recovery["reason"], "no_open_incident")
        self.assertEqual(len(self.messages), 2)
        self.assertIn("✅ Team 恢复", self.messages[1])
        self.assertEqual(self._incident()["status"], "resolved")

    def test_undelivered_alert_is_retried_on_next_failure(self):
        deliveries = [0, 1]

        def flaky_notify(text: str) -> int:
            self.messages.append(text)
            return deliveries.pop(0)

        team_health_alerts.notify_admins_sync = flaky_notify
        first = team_health_alerts.report_team_failure_sync(
            "team-1", "chatgpt_auth", "401", source="test"
        )
        second = team_health_alerts.report_team_failure_sync(
            "team-1", "chatgpt_auth", "401", source="test"
        )

        self.assertEqual(first["reason"], "not_delivered")
        self.assertEqual(second["reason"], "sent")
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self._incident()["notified"], 1)

    def test_undelivered_recovery_stays_open_and_retries(self):
        team_health_alerts.report_team_failure_sync(
            "team-1", "chatgpt_auth", "401", source="test"
        )
        deliveries = [0, 1]

        def flaky_notify(text: str) -> int:
            self.messages.append(text)
            return deliveries.pop(0)

        team_health_alerts.notify_admins_sync = flaky_notify
        first = team_health_alerts.report_team_recovery_sync(
            "team-1", "chatgpt_auth", source="test"
        )
        self.assertEqual(first["reason"], "not_delivered")
        self.assertEqual(self._incident()["status"], "open")

        second = team_health_alerts.report_team_recovery_sync(
            "team-1", "chatgpt_auth", source="test"
        )
        self.assertEqual(second["reason"], "sent")
        self.assertEqual(self._incident()["status"], "resolved")

    def test_auth_error_detection(self):
        self.assertTrue(team_health_alerts.is_auth_error("401 Client Error"))
        self.assertTrue(team_health_alerts.is_auth_error("Unauthorized"))
        self.assertFalse(team_health_alerts.is_auth_error("Read timed out"))


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
