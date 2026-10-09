"""seats_entitled across sync paths, and what an unknown value does to patrol.

Only a positive JSON integer is ever stored; every write path (scheduled data
sync, capacity cache, manual Team sync, session import) keeps the previous value
otherwise, and the live pre-invite capacity check treats it as a fetch error.
When this round cannot confirm the value, the Team skips only the over-quota
kicks: stranger-invite revokes and strict mode still run, and the admins get at
most one Telegram alert per Team per 24 hours. Display-only overview failures
never count as enforcement failures.

Everything runs on a database built by ``init_database()``; the ChatGPT client
and Telegram are stubbed and no request leaves the process.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db
from _redemption_fixtures import ConnectedTeamCase

from app import scheduler as app_scheduler
from app.scheduler import _classify_overview_failures
from app.services import patrol as patrol_service
from app.services import team_health_alerts, tg_notify, tg_summary

TEAM = "team-1"
OTHER_TEAM = "team-2"


# ── seats_entitled 只认正整数，不合格就保留上一次的值 ─────────────────────────

# 上游契约是 JSON 整数。null / 0 / 负数一旦落库，patrol 把每个默认席位都算成超员；
# 数字字符串、浮点数、bool 说明响应结构变了，同样不认。
_BAD_ENTITLEMENTS = (None, 0, -3, "25", 25.0, True)


class _Missing:
    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()


# 真实的 /users 回复里至少有 owner。
_OWNER_ROW = {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}


def _guard_subscription(entitled):
    sub = {
        "seats_in_use": 3,
        "billing_currency": "USD",
        "active_start": "2026-10-01T00:00:00+00:00",
        "active_until": "2026-11-01T00:00:00+00:00",
        "will_renew": True,
    }
    if entitled is not _MISSING:
        sub["seats_entitled"] = entitled
    return sub


class _GuardSyncClient:
    subscription: dict = {}

    def __init__(self, access_token, team_id, device_id, proxy_url=None):
        self.team_id = team_id

    def get_subscription(self):
        return dict(_GuardSyncClient.subscription)

    def get_seat_type_counts(self):
        return {"seat_type_counts": {"default": 3, "usage_based": 0}}

    def get_members(self, offset=0, limit=100):
        return {"items": [dict(_OWNER_ROW)], "total": 1}

    def get_pending_invites(self, offset=0, limit=100):
        return {"items": [], "total": 0}



class PositiveSeatCountTest(unittest.TestCase):
    def test_only_positive_json_integers_are_accepted(self):
        from app.services.seat_capacity import positive_seat_count

        self.assertEqual(positive_seat_count(1), 1)
        self.assertEqual(positive_seat_count(25), 25)
        for bad in _BAD_ENTITLEMENTS + (False, 1.0, "abc", [], {}):
            with self.subTest(value=bad):
                self.assertIsNone(positive_seat_count(bad))


class SeatsEntitledGuardTest(ConnectedTeamCase):
    def setUp(self):
        super().setUp()
        conn = self._conn()
        conn.execute(
            "UPDATE teams SET seats_entitled = 5, display_synced_at = ? WHERE id = 'team-1'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        conn.close()

    def _set_entitled(self, value):
        conn = self._conn()
        conn.execute("UPDATE teams SET seats_entitled = ? WHERE id = 'team-1'", (value,))
        conn.commit()
        conn.close()

    def _entitled(self, team_id="team-1"):
        conn = self._conn()
        row = conn.execute(
            "SELECT seats_entitled, typeof(seats_entitled) AS t FROM teams WHERE id = ?",
            (team_id,),
        ).fetchone()
        conn.close()
        return row["seats_entitled"], row["t"]

    def _run_data_sync(self, subscription):
        from app import scheduler as app_scheduler
        from app.services import patrol as patrol_service
        from app.services import tg_notify, tg_summary

        _GuardSyncClient.subscription = subscription
        run_patrol = MagicMock(return_value={})
        with patch.object(app_scheduler, "ChatGPTClient", _GuardSyncClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync",
                          lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_recovery_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_failure_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None), \
             patch.object(patrol_service, "run_patrol", run_patrol), \
             patch.object(tg_notify, "notify_admins_sync", lambda *a, **kw: None), \
             patch.object(tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None):
            app_scheduler.data_sync_job()
        run_patrol.assert_called_once()
        self.skip_over_quota = set(run_patrol.call_args.kwargs.get("skip_over_quota_team_ids", ()))
        return set(run_patrol.call_args.kwargs["allow_team_ids"])

    def test_scheduled_sync_keeps_previous_entitlement_and_skips_over_quota(self):
        for bad in _BAD_ENTITLEMENTS + (_MISSING,):
            with self.subTest(seats_entitled=bad):
                self._set_entitled(5)
                patrolled = self._run_data_sync(_guard_subscription(bad))
                self.assertEqual(self._entitled(), (5, "integer"))
                # 这一轮分母没刷新到可信值：巡逻照常撤陌生邀请、执行严格模式，
                # 但库里留着的 5 不能拿去判超员。
                self.assertIn("team-1", patrolled)
                self.assertIn("team-1", self.skip_over_quota)

    def test_scheduled_sync_writes_a_valid_entitlement(self):
        patrolled = self._run_data_sync(_guard_subscription(7))
        self.assertEqual(self._entitled(), (7, "integer"))
        self.assertIn("team-1", patrolled)
        self.assertNotIn("team-1", self.skip_over_quota)

    def test_capacity_cache_keeps_previous_entitlement(self):
        from app.services.seat_capacity import update_capacity_cache

        seat_counts = {"seat_type_counts": {"default": 3, "usage_based": 0}}
        for bad in _BAD_ENTITLEMENTS:
            with self.subTest(seats_entitled=bad):
                self._set_entitled(5)
                asyncio.run(update_capacity_cache("team-1", _guard_subscription(bad), seat_counts))
                self.assertEqual(self._entitled(), (5, "integer"))
        asyncio.run(update_capacity_cache("team-1", _guard_subscription(9), seat_counts))
        self.assertEqual(self._entitled(), (9, "integer"))

    def test_live_capacity_with_invalid_entitlement_is_a_fetch_error(self):
        """邀请前的实时容量判断不能把不合格的 seats_entitled 当成一个可信数字
        （null → 0 个空位只是碰巧安全；"25"/True 会被当成 25/1 个席位）。"""
        from app.services import seat_capacity

        class _Client:
            def __init__(self, sub):
                self.sub = sub

            def get_subscription(self):
                return dict(self.sub)

            def get_seat_type_counts(self):
                return {"seat_type_counts": {"default": 3, "usage_based": 0}}

            def get_pending_invites(self, offset=0, limit=100):
                return {"items": [], "total": 0}

        async def _direct(fn, *a, **kw):
            return fn(*a, **kw)

        with patch.object(seat_capacity, "run_chatgpt_call", _direct):
            for bad in _BAD_ENTITLEMENTS + (_MISSING,):
                with self.subTest(seats_entitled=bad):
                    with self.assertRaises(seat_capacity.SeatCapacityFetchError):
                        asyncio.run(
                            seat_capacity.fetch_live_chatgpt_seat_capacity(
                                _Client(_guard_subscription(bad))
                            )
                        )
            capacity, *_ = asyncio.run(
                seat_capacity.fetch_live_chatgpt_seat_capacity(_Client(_guard_subscription(6)))
            )
        self.assertEqual(capacity.seats_entitled, 6)
        self.assertEqual(capacity.available, 3)

    def test_manual_team_sync_does_not_write_invalid_entitlement(self):
        from app import team_sync_service

        class _Client:
            team_id = "team-1"

            def __init__(self, sub):
                self.sub = sub

            def get_subscription(self):
                return dict(self.sub)

            def get_remaining_balance(self):
                return {"balance": 0}

            def get_seat_type_counts(self):
                return {"seat_type_counts": {"default": 3, "usage_based": 0}}

            def get_payment_methods(self):
                return {"payment_methods": []}

            def get_account_info(self):
                return {"accounts": {"team-1": {"account": {"name": "Team 1"}}}}

        async def _direct(fn, *a, **kw):
            return fn(*a, **kw)

        with patch.object(team_sync_service, "run_chatgpt_call", _direct), \
             patch.object(team_sync_service, "fetch_seat_pricing", AsyncMock(return_value={})):
            for bad in _BAD_ENTITLEMENTS + (_MISSING,):
                with self.subTest(seats_entitled=bad):
                    updates, _ = asyncio.run(
                        team_sync_service._fetch_overview(_Client(_guard_subscription(bad)))
                    )
                    self.assertNotIn("seats_entitled", updates)
                    # 同一响应里的其它字段照常写。
                    self.assertEqual(updates["seats_in_use"], 3)
            updates, _ = asyncio.run(
                team_sync_service._fetch_overview(_Client(_guard_subscription(8)))
            )
        self.assertEqual(updates["seats_entitled"], 8)

    def test_session_import_does_not_write_invalid_entitlement(self):
        import jwt

        from app import team_service
        from app.models import TeamSession

        team_uuid = "11111111-2222-3333-4444-555555555555"
        token = jwt.encode(
            {"https://api.openai.com/auth": {"chatgpt_account_id": team_uuid},
             "exp": int(datetime.now(timezone.utc).timestamp()) + 3600},
            "test-signing-key-not-a-secret-0000000000",
            algorithm="HS256",
        )

        class _Client:
            sub: dict = {}

            def __init__(self, *a, **kw):
                pass

            def get_account_info(self):
                return {"accounts": {team_uuid: {"account": {"name": "Imported"}}}}

            def get_subscription(self):
                return dict(_Client.sub)

            def get_remaining_balance(self):
                return {"balance": 0}

            def get_seat_type_counts(self):
                return {"seat_type_counts": {"default": 3, "usage_based": 0}}

            def get_payment_methods(self):
                return {"payment_methods": []}

        async def _direct(fn, *a, **kw):
            return fn(*a, **kw)

        session = TeamSession(
            user={"email": "owner@example.com"},
            expires="2099-01-01T00:00:00Z",
            account={"id": team_uuid},
            accessToken=token,
            sessionToken="stub-session",
        )

        def _import(entitled):
            _Client.sub = _guard_subscription(entitled)
            with patch.object(team_service, "ChatGPTClient", _Client), \
                 patch.object(team_service, "run_chatgpt_call", _direct), \
                 patch.object(team_service, "fetch_seat_pricing", AsyncMock(return_value={})), \
                 patch.object(team_service, "write_session_file", lambda *a, **kw: None):
                asyncio.run(team_service.upsert_team_from_session(session))

        # 新 Team：不合格的值不能被当成数字写进去（留 NULL = 未知）。
        _import(0)
        self.assertEqual(self._entitled(team_uuid), (None, "null"))
        _import(4)
        self.assertEqual(self._entitled(team_uuid), (4, "integer"))
        # 已有 Team：保留上一次的合法值。
        for bad in _BAD_ENTITLEMENTS:
            with self.subTest(seats_entitled=bad):
                _import(bad)
                self.assertEqual(self._entitled(team_uuid), (4, "integer"))


# ── 席位数未知：只跳过超员踢人，撤陌生邀请 / 严格模式照常，提醒 24h 一次 ──

class _SyncDbCase(unittest.TestCase):
    def setUp(self):
        self.db_path = start_temp_db(self)

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


class _EntitlementBase(_SyncDbCase):
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


# ── 纯展示接口失败不参与挂起判定 ────────────────────────────────────────────

class OverviewFailureClassificationTest(unittest.TestCase):
    def test_display_only_failures_are_not_enforcement_failures(self):
        overview, display, enforcement = _classify_overview_failures(
            subscription={"seats_entitled": 5},
            seat_counts={"default": 3},
            balance_info={"error": "boom"},
            payment_methods={"error": "boom"},
            account_info={"error": "boom"},
        )
        self.assertEqual(overview, ["balance", "payment_methods", "account_info"])
        self.assertEqual(display, ["balance", "payment_methods", "account_info"])
        self.assertEqual(enforcement, [])

    def test_subscription_and_seat_counts_are_enforcement_inputs(self):
        _overview, display, enforcement = _classify_overview_failures(
            subscription={"error": "401"},
            seat_counts={"error": "401"},
            balance_info=None,
            payment_methods=None,
            account_info=None,
        )
        self.assertEqual(enforcement, ["subscription", "seat_counts"])
        self.assertEqual(display, [])

    def test_throttled_calls_are_not_failures(self):
        overview, display, enforcement = _classify_overview_failures(
            subscription={"seats_entitled": 5},
            seat_counts={"default": 3},
            balance_info=None,
            payment_methods=None,
            account_info=None,
        )
        self.assertEqual((overview, display, enforcement), ([], [], []))


if __name__ == "__main__":
    unittest.main()
