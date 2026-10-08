"""定时同步里两处资金路径缺陷的回归测试。

1. 本轮订阅没给出可用的 seats_entitled（null / 0 / 缺字段 / 订阅接口报错）时，
   整个 Team 被移出巡逻白名单：撤陌生邀请和严格模式跟着停掉，而它们根本不看
   席位数。席位数未知只该让"超员踢人"这一段跳过；管理员每 24 小时最多收到一次
   Telegram 提醒，复用 team_health_incidents 的去重机制。
2. 兑换兜底行（kind='extend'）的时长从行的创建时刻起算，兜底拖了多久，买家就
   少了多久。必须和正常续期同一条规则：max(现在, 现有到期) + 时长。

全部跑在 init_database() 建出的临时库上；ChatGPT 客户端和 Telegram 一律打桩，
不发任何网络请求。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app import scheduler as app_scheduler
from app.scheduler import _reconcile_pending_invites_sync
from app.services import patrol as patrol_service
from app.services import team_health_alerts, tg_notify, tg_summary
from app.services.member_expiry import extend_member_expiry

TEAM = "team-1"
OTHER_TEAM = "team-2"
EMAIL = "redeemer@example.com"


class _TempDbTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(
            app_database, "get_db_dir", return_value=self.tmpdir.name
        )
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        self.assertTrue(self.db_path.startswith(self.tmpdir.name))

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _logs(self, action, team_id=None):
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM operation_logs WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows if team_id is None or r["team_id"] == team_id]


# ── 1. 席位数未知：只跳过超员踢人，撤陌生邀请 / 严格模式照常，提醒 24h 一次 ──

def _subscription(entitled="absent"):
    sub = {
        "seats_in_use": 2,
        "billing_currency": "USD",
        "active_start": "2026-10-01T00:00:00+00:00",
        "active_until": "2026-11-01T00:00:00+00:00",
        "will_renew": True,
    }
    if entitled != "absent":
        sub["seats_entitled"] = entitled
    return sub


SUBSCRIPTION_CALL_ERROR = {"error": "HTTP 502: upstream unavailable"}


class _FakeSyncClient:
    """data_sync_job 看到的上游：一个 owner、一个外部混进来的成员、一个陌生邀请。"""

    subscriptions: dict = {}
    seat_counts_error = False

    def __init__(self, access_token, team_id, device_id, proxy_url=None):
        self.team_id = team_id

    def get_subscription(self):
        return dict(_FakeSyncClient.subscriptions[self.team_id])

    def get_seat_type_counts(self):
        if _FakeSyncClient.seat_counts_error:
            return {"error": "HTTP 500"}
        return {"seat_type_counts": {"default": 2, "usage_based": 0}}

    def get_members(self, offset=0, limit=100):
        return {
            "items": [
                {"id": f"u-owner-{self.team_id}", "email": f"owner@{self.team_id}.example",
                 "role": "account-owner", "seat_type": "default"},
                {"id": f"u-intruder-{self.team_id}", "email": f"intruder@{self.team_id}.example",
                 "role": "standard-user", "seat_type": "default"},
            ],
            "total": 2,
        }

    def get_pending_invites(self, offset=0, limit=100):
        return {
            "items": [
                {"id": f"inv-{self.team_id}", "email_address": f"stray@{self.team_id}.example",
                 "role": "standard-user", "seat_type": "default"},
            ],
            "total": 1,
        }


class _EntitlementBase(_TempDbTest):
    def setUp(self):
        super().setUp()
        self.upstream_writes: list[tuple[str, str]] = []
        self.alerts: list[str] = []
        self.other_notifications: list[str] = []
        _FakeSyncClient.subscriptions = {}
        _FakeSyncClient.seat_counts_error = False

        writes = self.upstream_writes

        class _RecordingPatrolClient:
            def __init__(self, *args, **kwargs):
                pass

            def remove_member(self, user_id):
                writes.append(("remove_member", user_id))
                return {"status": "ok"}

            def revoke_invite(self, email):
                writes.append(("revoke_invite", email))
                return {"status": "ok"}

        def _alert(text, **kwargs):
            self.alerts.append(text)
            return 1  # 投递成功，激活去重

        def _other(text, **kwargs):
            self.other_notifications.append(text)
            return 1

        def _direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        for target, name, value in (
            (app_scheduler, "ChatGPTClient", _FakeSyncClient),
            (app_scheduler, "run_chatgpt_call_sync", _direct),
            (app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None),
            (app_scheduler, "notify_member_event_sync", lambda *a, **kw: None),
            (app_scheduler, "sync_email_chat_commands_sync", lambda *a, **kw: None),
            (team_health_alerts, "notify_admins_sync", _alert),
            (tg_notify, "notify_admins_sync", _other),
            (tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None),
            (patrol_service, "ChatGPTClient", _RecordingPatrolClient),
            (patrol_service, "run_chatgpt_call_sync", _direct),
            (patrol_service, "notify_admins_sync", _other),
            (patrol_service, "sync_email_chat_commands_sync", lambda *a, **kw: None),
        ):
            p = patch.object(target, name, new=value)
            p.start()
            self.addCleanup(p.stop)

        conn = self._conn()
        for key, value in (
            ("patrol_kick_enabled", "1"),
            ("patrol_baseline_at", "2026-07-01T00:00:00+00:00"),
            ("patrol_strict_mode_enabled", "1"),
            # 严格模式候选 24 小时后才可处理：本轮只会被标记，不触发实时刷新。
            ("expiry_kick_mode", "delay_hours"),
            ("expiry_kick_delay_hours", "24"),
        ):
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, '2026-07-01')
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
        conn.commit()
        conn.close()

    def _add_team(self, team_id, *, stored_entitled, subscription, suspended=False):
        """一个已武装、已建巡逻基线的 Team。展示字段刚刷新过，本轮不会去拉。"""
        now = datetime.now(timezone.utc)
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, access_token, device_id, owner_email,
                                  seats_entitled, is_codex_enabled, display_synced_at,
                                  sync_failing_since, sync_suspended_at, sync_probe_at,
                                  created_at, updated_at)
               VALUES (?, ?, 'active', 'stub-token', 'stub-device', ?, ?, 0, ?, ?, ?, ?,
                       '2026-07-01', '2026-07-01')""",
            (
                team_id,
                f"Team {team_id}",
                f"owner@{team_id}.example",
                stored_entitled,
                now.isoformat(),
                (now - timedelta(days=3)).isoformat() if suspended else None,
                (now - timedelta(days=2)).isoformat() if suspended else None,
                (now - timedelta(hours=7)).isoformat() if suspended else None,
            ),
        )
        conn.execute(
            "INSERT INTO patrol_team_baselines (team_id, baseline_at) VALUES (?, ?)",
            (team_id, "2026-07-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()
        _FakeSyncClient.subscriptions[team_id] = subscription

    def _sync(self):
        app_scheduler.data_sync_job()

    def _stored_entitlement(self, team_id=TEAM):
        conn = self._conn()
        row = conn.execute(
            "SELECT seats_entitled FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        conn.close()
        return row["seats_entitled"]

    def _entitlement_alerts(self, team_id=TEAM):
        return [
            log for log in self._logs("team_health_alert", team_id)
            if "key=seats_entitled" in (log["detail"] or "")
            and log["result"] == "success"
        ]

    def _over_quota_actions(self, team_id=TEAM):
        return (
            self._logs("patrol_kick", team_id)
            + self._logs("patrol_would_kick", team_id)
            + [w for w in self.upstream_writes if w[0] == "remove_member"]
        )


class UnknownEntitlementKeepsOtherPatrolDutiesTest(_EntitlementBase):
    """库里的 seats_entitled 本来就未知（NULL），本轮上游也给不出可用值。"""

    def _assert_patrolled_without_over_quota(self, subscription):
        self._add_team(TEAM, stored_entitled=None, subscription=subscription)

        self._sync()

        # 上一次的值（这里是 NULL）原样保留，不会被写成 0。
        self.assertIsNone(self._stored_entitlement())
        # 陌生邀请照常撤销……
        self.assertEqual(
            self.upstream_writes, [("revoke_invite", f"stray@{TEAM}.example")]
        )
        self.assertEqual(
            [log["result"] for log in self._logs("patrol_revoke_invite", TEAM)],
            ["success"],
        )
        # ……严格模式照常处理（候选还在等待期内，本轮标记并通知）……
        self.assertEqual(
            [log["target_email"] for log in self._logs("patrol_strict_flagged", TEAM)],
            [f"intruder@{TEAM}.example"],
        )
        # ……只有超员踢人这一段跳过。
        self.assertEqual(len(self._logs("patrol_skip_invalid_entitlement", TEAM)), 1)
        self.assertEqual(self._over_quota_actions(), [])
        self.assertEqual(self._logs("patrol_job_error"), [])
        # 管理员收到一次 Telegram 提醒。
        self.assertEqual(len(self._entitlement_alerts()), 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn(f"Team {TEAM}", self.alerts[0])

    def test_null_entitlement(self):
        self._assert_patrolled_without_over_quota(_subscription(None))

    def test_zero_entitlement(self):
        self._assert_patrolled_without_over_quota(_subscription(0))

    def test_missing_entitlement(self):
        self._assert_patrolled_without_over_quota(_subscription())

    def test_subscription_call_error(self):
        self._assert_patrolled_without_over_quota(dict(SUBSCRIPTION_CALL_ERROR))


class EntitlementPatrolSafetyGuardsTest(_EntitlementBase):
    """放宽白名单不能顺带放宽别的执法前提。"""

    def test_unconfirmed_stored_entitlement_is_never_used_for_over_quota_kicks(self):
        # 库里有上一轮的合法值 1，成员按它算超员 1 人；但本轮上游没确认这个值。
        for index, subscription in enumerate(
            (_subscription(None), _subscription(), dict(SUBSCRIPTION_CALL_ERROR))
        ):
            team_id = f"team-stale-{index}"
            with self.subTest(subscription=subscription):
                self._add_team(team_id, stored_entitled=1, subscription=subscription)
                self._sync()
                self.assertEqual(self._stored_entitlement(team_id), 1)
                self.assertEqual(self._over_quota_actions(team_id), [])
                self.assertEqual(len(self._entitlement_alerts(team_id)), 1)

    def test_unconfirmed_stored_entitlement_keeps_stranger_revokes_and_strict_mode(self):
        # 库里有上一轮的合法值、本轮没确认：只跳过超员踢人，撤陌生邀请和严格模式照常。
        for index, subscription in enumerate(
            (_subscription(None), _subscription(), dict(SUBSCRIPTION_CALL_ERROR))
        ):
            team_id = f"team-stale-duties-{index}"
            with self.subTest(subscription=subscription):
                self._add_team(team_id, stored_entitled=1, subscription=subscription)
                self._sync()
                self.assertIn(
                    ("revoke_invite", f"stray@{team_id}.example"), self.upstream_writes
                )
                self.assertEqual(
                    [log["target_email"] for log in self._logs("patrol_strict_flagged", team_id)],
                    [f"intruder@{team_id}.example"],
                )
                self.assertEqual(
                    len(self._logs("patrol_skip_unconfirmed_entitlement", team_id)), 1
                )
                self.assertEqual(self._over_quota_actions(team_id), [])
                self.assertEqual(self._logs("patrol_job_error"), [])

    def test_suspended_team_still_failing_subscription_stays_out_of_patrol(self):
        self._add_team(
            TEAM, stored_entitled=None, subscription=dict(SUBSCRIPTION_CALL_ERROR),
            suspended=True,
        )

        self._sync()

        conn = self._conn()
        suspended_at = conn.execute(
            "SELECT sync_suspended_at FROM teams WHERE id = ?", (TEAM,)
        ).fetchone()["sync_suspended_at"]
        conn.close()
        self.assertIsNotNone(suspended_at)
        self.assertEqual(self.upstream_writes, [])
        self.assertEqual(self._logs("patrol_strict_flagged", TEAM), [])
        self.assertEqual(self._logs("patrol_skip_invalid_entitlement", TEAM), [])

    def test_seat_count_failure_still_keeps_team_out_of_patrol(self):
        _FakeSyncClient.seat_counts_error = True
        self._add_team(TEAM, stored_entitled=None, subscription=_subscription(None))

        self._sync()

        self.assertEqual(self.upstream_writes, [])
        self.assertEqual(self._logs("patrol_strict_flagged", TEAM), [])

    def test_fresh_valid_entitlement_patrols_normally_without_alert(self):
        self._add_team(TEAM, stored_entitled=None, subscription=_subscription(5))

        self._sync()

        self.assertEqual(self._stored_entitlement(), 5)
        self.assertEqual(
            self.upstream_writes, [("revoke_invite", f"stray@{TEAM}.example")]
        )
        self.assertEqual(self._logs("patrol_skip_invalid_entitlement", TEAM), [])
        self.assertEqual(self._entitlement_alerts(), [])
        self.assertEqual(self.alerts, [])


class EntitlementAlertThrottleTest(_EntitlementBase):
    """席位数未知的提醒：每个 Team 每 24 小时最多一条。"""

    def _backdate_last_alert(self, team_id, age):
        stamp = (datetime.now(timezone.utc) - age).isoformat()
        conn = self._conn()
        updated = conn.execute(
            """UPDATE team_health_incidents SET last_notified_at = ?
               WHERE team_id = ? AND alert_key = 'seats_entitled'""",
            (stamp, team_id),
        ).rowcount
        conn.commit()
        conn.close()
        self.assertEqual(updated, 1)

    def test_alert_fires_once_per_team_within_24h_and_again_after(self):
        self._add_team(TEAM, stored_entitled=None, subscription=_subscription(None))
        self._add_team(OTHER_TEAM, stored_entitled=7, subscription=dict(SUBSCRIPTION_CALL_ERROR))

        self._sync()
        self._sync()
        # 每个 Team 各一条，同一 Team 的第二轮静音。
        self.assertEqual(len(self._entitlement_alerts(TEAM)), 1)
        self.assertEqual(len(self._entitlement_alerts(OTHER_TEAM)), 1)
        self.assertEqual(len(self.alerts), 2)

        # 超过通用的 6 小时重复提醒间隔、但还没到 24 小时：仍然静音。
        self._backdate_last_alert(TEAM, timedelta(hours=23))
        self._sync()
        self.assertEqual(len(self._entitlement_alerts(TEAM)), 1)

        # 满 24 小时仍未恢复：再提醒一次。
        self._backdate_last_alert(TEAM, timedelta(hours=24, minutes=1))
        self._sync()
        self.assertEqual(len(self._entitlement_alerts(TEAM)), 2)
        self.assertEqual(len(self._entitlement_alerts(OTHER_TEAM)), 1)

    def test_recovery_in_between_does_not_reopen_the_24h_window(self):
        self._add_team(TEAM, stored_entitled=None, subscription=_subscription(None))
        self._sync()
        self.assertEqual(len(self._entitlement_alerts()), 1)

        # 下一轮拿到合法值：事件关闭（已提醒过，发一条恢复）。
        _FakeSyncClient.subscriptions[TEAM] = _subscription(4)
        self._sync()
        self.assertEqual(len(self._logs("team_health_recovery", TEAM)), 1)

        # 24 小时内又抖回未知：不再提醒。
        _FakeSyncClient.subscriptions[TEAM] = _subscription(None)
        self._sync()
        _FakeSyncClient.subscriptions[TEAM] = dict(SUBSCRIPTION_CALL_ERROR)
        self._sync()
        self.assertEqual(len(self._entitlement_alerts()), 1)
        self.assertEqual(len(self.alerts), 2)  # 一条提醒 + 一条恢复


# ── 2. 兜底行补记购买时长：max(现在, 现有到期) + 时长 ─────────────────────────

THIRTY_DAYS = timedelta(days=30)
DELAY = timedelta(days=10)


class FallbackCreditTest(_TempDbTest):
    """自助兑换的邀请已在上游成功，本地写到期失败，留下 kind='extend' 兜底行；
    10 天后数据同步才看到这个人并补记。"""

    def setUp(self):
        super().setUp()
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, access_token, device_id,
                                  created_at, updated_at)
               VALUES (?, 'Team 1', 'active', 'stub-token', 'stub-device',
                       '2026-07-01', '2026-07-01')""",
            (TEAM,),
        )
        conn.commit()
        conn.close()

    def _fallback_row(self, *, duration=THIRTY_DAYS, delay=DELAY):
        """按 member_expiry._persist_confirmed_membership 的形状落一行兜底 + 一次兑换。"""
        created = datetime.now(timezone.utc) - delay
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'p', ?, 1, 1, 0, '2026-07-01')""",
            (f"h-{created.timestamp()}", "30d" if duration else "never"),
        )
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result, created_at)
               VALUES (?, ?, 'invite_pending', ?, NULL, NULL, 'pending', ?)""",
            (cur.lastrowid, EMAIL, TEAM, created.isoformat()),
        )
        token_use_id = cur.lastrowid
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               VALUES (?, '', ?, ?, 'self_service', 'database is locked', 0, ?, ?, 'extend')""",
            (
                TEAM,
                EMAIL,
                (created + duration).isoformat() if duration else None,
                created.isoformat(),
                token_use_id,
            ),
        )
        conn.commit()
        conn.close()
        return token_use_id

    def _existing_expiry(self, expires_at, *, source="self_service", kicked=0):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES (?, 'u1', ?, ?, ?, ?, '2026-07-01', ?, '2026-07-01')""",
            (TEAM, EMAIL, expires_at, 1 if expires_at else 0, kicked, source),
        )
        conn.commit()
        conn.close()

    def _reconcile(self):
        conn = self._conn()
        try:
            count = _reconcile_pending_invites_sync(
                conn,
                TEAM,
                [{"id": "u1", "email": EMAIL}],
                [],
                datetime.now(timezone.utc).isoformat(),
            )
            conn.commit()
        finally:
            conn.close()
        return count

    def _state(self, token_use_id):
        conn = self._conn()
        rows = conn.execute(
            """SELECT expires_at, auto_kick FROM member_expiry
               WHERE team_id = ? AND kicked = 0""",
            (TEAM,),
        ).fetchall()
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM pending_invite_reconciliations WHERE resolved = 0"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(len(rows), 1)
        return dict(rows[0]), dict(use), unresolved

    def _assert_full_duration_from(self, expires_iso, base):
        gained = datetime.fromisoformat(expires_iso) - base
        # 兜底行的到期和 created_at 在落盘时先后取时间，差几毫秒属正常。
        self.assertGreater(gained, THIRTY_DAYS - timedelta(minutes=1))
        self.assertLessEqual(gained, THIRTY_DAYS + timedelta(minutes=1))

    def test_delayed_fallback_credits_full_duration_from_now(self):
        token_use_id = self._fallback_row()

        self.assertEqual(self._reconcile(), 1)

        expiry, use, unresolved = self._state(token_use_id)
        # 原先是 created_at + 30 天 = 只剩 20 天。
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))
        self.assertEqual(expiry["auto_kick"], 1)
        self.assertEqual(use, {"result": "success", "expires_at": expiry["expires_at"]})
        self.assertEqual(unresolved, 0)

    def test_delayed_fallback_over_detected_row_credits_from_now(self):
        # 同步先把这个人当成"外部发现"建了档（detected + NULL 不是永久授权）。
        self._existing_expiry(None, source="detected")
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, use, _ = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))
        self.assertEqual(expiry["auto_kick"], 1)
        self.assertEqual(use["expires_at"], expiry["expires_at"])

    def test_delayed_fallback_after_archived_membership_credits_from_now(self):
        # 上一段成员身份已 kicked=1 归档，没有任何已购时长需要保护。
        self._existing_expiry(
            (datetime.now(timezone.utc) + timedelta(days=90)).isoformat(), kicked=1,
        )
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, _, _ = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))

    def test_existing_future_expiry_is_extended_from_that_expiry(self):
        future = datetime.now(timezone.utc) + timedelta(days=100)
        self._existing_expiry(future.isoformat())
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, use, _ = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], future)
        self.assertEqual(use["expires_at"], expiry["expires_at"])

    def test_existing_past_expiry_credits_from_now(self):
        self._existing_expiry(
            (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        )
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, _, _ = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))

    def test_permanent_authorization_is_never_made_finite(self):
        self._existing_expiry(None, source="system")
        token_use_id = self._fallback_row()

        self._reconcile()

        expiry, use, unresolved = self._state(token_use_id)
        self.assertIsNone(expiry["expires_at"])
        self.assertEqual(expiry["auto_kick"], 0)
        self.assertEqual(use, {"result": "success", "expires_at": None})
        self.assertEqual(unresolved, 0)

    def test_permanent_purchase_grants_permanent(self):
        self._existing_expiry(
            (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        )
        token_use_id = self._fallback_row(duration=None)

        self._reconcile()

        expiry, use, _ = self._state(token_use_id)
        self.assertIsNone(expiry["expires_at"])
        self.assertIsNone(use["expires_at"])

    def test_second_pass_and_other_recovery_path_do_not_credit_again(self):
        token_use_id = self._fallback_row()
        self._reconcile()
        credited, _, _ = self._state(token_use_id)

        # 同一行再被扫一次（已 resolved），以及另一条恢复路径晚到：都不能再加。
        self.assertEqual(self._reconcile(), 0)
        returned = asyncio.run(
            extend_member_expiry(
                TEAM, "u1", EMAIL, "30d",
                source="self_service", keep_permanent=True,
                token_use_id=token_use_id, token_action="invited",
            )
        )
        after, use, unresolved = self._state(token_use_id)
        self.assertEqual(returned, credited["expires_at"])
        self.assertEqual(after, credited)
        self.assertEqual(use["expires_at"], credited["expires_at"])
        self.assertEqual(unresolved, 0)

    def test_duplicate_fallback_rows_for_one_redemption_credit_once(self):
        token_use_id = self._fallback_row()
        conn = self._conn()
        conn.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved,
                created_at, token_use_id, kind)
               SELECT team_id, user_id, email, expires_at, source, reason, 0,
                      created_at, token_use_id, kind
               FROM pending_invite_reconciliations WHERE token_use_id = ?""",
            (token_use_id,),
        )
        conn.commit()
        conn.close()

        self._reconcile()

        expiry, use, unresolved = self._state(token_use_id)
        self._assert_full_duration_from(expiry["expires_at"], datetime.now(timezone.utc))
        self.assertEqual(use["expires_at"], expiry["expires_at"])
        self.assertEqual(unresolved, 0)


if __name__ == "__main__":
    unittest.main()
