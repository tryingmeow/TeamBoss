"""Premium 巡逻踢人复核修复（K1–K3）的回归测试。

- K1：快照按"开始拉取的时间"（member_cache.fetch_started_at）定先后。开始得早的刷新不能覆盖
  开始得晚的快照；Premium 否决拿 TeamBoss 的席位改动和开始时间比，不和写入时间比。每个完整快照
  的写入方都随快照给持久席位占用对账。
- K2：TeamBoss 以前拉过 / 卖过席位的人（system / self_service 记录或兑换，开着关着都算），
  重新被检测成外部 Premium 成员时不踢，只进限频的席位提醒。
- K3：Premium 的"别一次踢一片"护栏数全部外部成员，不看严格模式；一个 Team 一轮在 Premium
  和超员两条路上一共最多踢 NON_STRICT_KICK_ABS_CAP 个。

所有上游调用都是记录调用的假客户端，绝不触网。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import contextlib
import json
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_premium_patrol import (  # noqa: I001  (_isolation first)
    OWNER,
    RecordingClient,
    _Base,
    _live,
    _member,
)

from app import member_cache_service
from app import scheduler as app_scheduler
from app.services import patrol

KEEPER = _member("keeper@example.com", "u-k", source="system")
LIVE_OWNER = _live("owner@example.com", "u-owner", "usage_based", owner=True)
LIVE_KEEPER = _live("keeper@example.com", "u-k", "default")
FUTURE_START = "2999-01-01T00:00:00.000000+00:00"


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _direct_call(fn, *args, **kwargs):
    return fn(*args, **kwargs)


class _Fixture(_Base):
    def _cache_row(self, team_id):
        conn = self._conn()
        row = conn.execute(
            "SELECT members_json, updated_at, fetch_started_at FROM member_cache WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def _cached_seat(self, team_id, user_id):
        row = self._cache_row(team_id)
        for m in json.loads(row["members_json"]):
            if m.get("id") == user_id:
                return m.get("seat_type")
        return None

    def _log(self, team_id, action, target_email, detail, result, created_at=None):
        conn = self._conn()
        conn.execute(
            """INSERT INTO operation_logs (team_id, action, target_email, detail, result,
                                           trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, 'manual', ?)""",
            (team_id, action, target_email, detail, result, created_at or _now()),
        )
        conn.commit()
        conn.close()

    def _team_row(self, team_id):
        conn = self._conn()
        row = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        conn.close()
        return row

    def _hold(self, team_id, email, *, age=timedelta(hours=1)):
        conn = self._conn()
        conn.execute(
            """INSERT INTO seat_holds (team_id, email, seat_type, source, created_at)
               VALUES (?, ?, 'prolite', 'test', ?)""",
            (team_id, email, (datetime.now(timezone.utc) - age).isoformat()),
        )
        conn.commit()
        conn.close()

    def _holds(self, team_id):
        conn = self._conn()
        rows = conn.execute("SELECT email FROM seat_holds WHERE team_id = ?", (team_id,)).fetchall()
        conn.close()
        return {r["email"] for r in rows}

    def _async_refresh(self, team_id, client):
        with patch.object(member_cache_service, "run_chatgpt_call", _direct_call):
            return asyncio.run(member_cache_service._fetch_and_cache_members_impl(team_id, client))


# ═══ K1：快照按开始拉取的时间定先后 ═══════════════════════════════════════════

class StaleRefreshTest(_Fixture):
    def _premium_outsider_team(self, team_id):
        outsider = _member("outsider@example.com", "u-p", seat_type="prolite")
        # 早一个小时的一份快照：外部成员在 Premium 上。
        self._armed_team(team_id, [OWNER, KEEPER, outsider])
        earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._cache(team_id, [OWNER, KEEPER, outsider], updated_at=earlier)
        return outsider

    def test_stale_refresh_written_after_admin_downgrade_does_not_kick(self):
        # 修复清单 K1 的场景：一次刷新先读到他在 Premium，卡在拉邀请上；管理员这时把他切回
        # ChatGPT（日志落在刷新开始之后）；卡住的刷新随后才把旧名单写进缓存。写入时间比日志晚，
        # 但名单是开始那一刻的：不能拿它踢人。
        team_id = "team-k1"
        self._premium_outsider_team(team_id)
        fixture = self

        class StallingClient:
            def get_members(self, offset=0, limit=100):
                items = [LIVE_OWNER, LIVE_KEEPER, _live("outsider@example.com", "u-p", "prolite")]
                return {"items": items if offset == 0 else [], "total": 3}

            def get_pending_invites(self, offset=0, limit=100):
                time.sleep(0.002)
                fixture._log(
                    team_id, "change_seat", "outsider@example.com",
                    "user_id=u-p, seat_type=default, from_seat_type=prolite, policy=confirm",
                    "success",
                )
                time.sleep(0.002)
                return {"items": [], "total": 0}

        self._async_refresh(team_id, StallingClient())
        row = self._cache_row(team_id)
        self.assertEqual(self._cached_seat(team_id, "u-p"), "prolite")
        self.assertLess(row["fetch_started_at"], row["updated_at"])

        result = self._patrol(dry_run=False)

        self.assertEqual(self._calls("remove_member"), [])
        self.assertEqual(result["kicked"], 0)
        deferred = [l for l in self._logs("patrol_kick") if l["result"] == "skipped"]
        self.assertEqual(len(deferred), 1)
        self.assertIn("deferred", deferred[0]["detail"])
        self.assertIsNone(self._kicked(team_id, "u-p")[1])

    def test_older_started_refresh_does_not_overwrite_newer_snapshot(self):
        # 刷新 A 先开始、读到 Premium；它卡住期间管理员切回 ChatGPT，另一次刷新 B（开始得更晚）
        # 写进了 ChatGPT。A 最后才写：不能盖掉 B。
        team_id = "team-k1b"
        self._premium_outsider_team(team_id)
        fixture = self

        class SlowClient:
            def get_members(self, offset=0, limit=100):
                items = [LIVE_OWNER, LIVE_KEEPER, _live("outsider@example.com", "u-p", "prolite")]
                return {"items": items if offset == 0 else [], "total": 3}

            def get_pending_invites(self, offset=0, limit=100):
                fixture._log(
                    team_id, "change_seat", "outsider@example.com",
                    "user_id=u-p, seat_type=default, from_seat_type=prolite, policy=confirm",
                    "success",
                )
                RecordingClient.live_members = [
                    LIVE_OWNER, LIVE_KEEPER, _live("outsider@example.com", "u-p", "default"),
                ]
                conn = fixture._conn()
                ok, err, _ = patrol._refresh_team_snapshot_sync(conn, fixture._team_row(team_id))
                conn.close()
                assert ok, err
                fixture.newer = fixture._cache_row(team_id)
                return {"items": [], "total": 0}

        self._async_refresh(team_id, SlowClient())

        row = self._cache_row(team_id)
        self.assertEqual(row, self.newer)
        self.assertEqual(self._cached_seat(team_id, "u-p"), "default")

        self._patrol(dry_run=False)
        self.assertEqual(self._calls("remove_member"), [])


class SnapshotWritersTest(_Fixture):
    """每个写完整快照的地方：记下开始时间；库里的快照开始得更晚时不覆盖；随快照对账席位占用。"""

    def _run_async_writer(self, team_id):
        class Client(RecordingClient):
            pass
        self._async_refresh(team_id, Client())

    def _run_patrol_refresh(self, team_id):
        conn = self._conn()
        ok, err, _ = patrol._refresh_team_snapshot_sync(conn, self._team_row(team_id))
        conn.close()
        self.assertTrue(ok, err)

    def _run_member_watch(self, team_id):
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_watch (team_id, reason, target_email, started_at, expires_at, done)
               VALUES (?, 'invite', 'keeper@example.com', ?, ?, 0)""",
            (team_id, _now(), (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()),
        )
        conn.commit()
        conn.close()
        with patch.object(app_scheduler, "ChatGPTClient", RecordingClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "edit_message_sync", lambda *a, **kw: None):
            app_scheduler.member_watch_job()

    def _run_data_sync(self, team_id):
        from app.services import tg_notify, tg_summary

        conn = self._conn()
        conn.execute("UPDATE teams SET display_synced_at = ? WHERE id = ?", (_now(), team_id))
        conn.commit()
        conn.close()

        class SyncClient(RecordingClient):
            def get_subscription(self):
                return {"seats_entitled": 5, "seats_in_use": 2}

            def get_seat_type_counts(self):
                return {"seat_type_counts": {"default": 1, "usage_based": 1}}

        with patch.object(app_scheduler, "ChatGPTClient", SyncClient), \
             patch.object(app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "refresh_invoices_if_stale_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_recovery_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "report_team_failure_sync", lambda *a, **kw: None), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None), \
             patch.object(patrol, "run_patrol", lambda *a, **kw: {}), \
             patch.object(tg_notify, "notify_admins_sync", lambda *a, **kw: 0), \
             patch.object(tg_summary, "maybe_send_summary_sync", lambda *a, **kw: None):
            app_scheduler.data_sync_job()

    def _writers(self):
        return {
            "member_cache_service": self._run_async_writer,
            "patrol_refresh": self._run_patrol_refresh,
            "member_watch_job": self._run_member_watch,
            "data_sync_job": self._run_data_sync,
        }

    def _retire(self, team_id):
        # 定时任务扫全部 active Team：跑完的子用例把自己的 Team 停掉，免得下一个子用例再碰它。
        conn = self._conn()
        conn.execute("UPDATE teams SET status = 'retired' WHERE id = ?", (team_id,))
        conn.commit()
        conn.close()

    def _setup_team(self, team_id, *, cached_start):
        self._team(team_id)
        self._expiry(team_id, "keeper@example.com", "u-k", source="system")
        self._cache(team_id, [OWNER, KEEPER, _member("gone@example.com", "u-gone")],
                    updated_at=cached_start)
        RecordingClient.live_members = [LIVE_OWNER, LIVE_KEEPER]

    def test_newer_started_snapshot_is_kept(self):
        for name, run in self._writers().items():
            with self.subTest(writer=name):
                team_id = f"team-newer-{name}"
                self._setup_team(team_id, cached_start=FUTURE_START)
                before = self._cache_row(team_id)

                run(team_id)

                self.assertEqual(self._cache_row(team_id), before)
                self._retire(team_id)

    def test_writer_records_fetch_start_and_reconciles_seat_holds(self):
        for name, run in self._writers().items():
            with self.subTest(writer=name):
                team_id = f"team-write-{name}"
                self._setup_team(team_id, cached_start=None)
                conn = self._conn()
                conn.execute("UPDATE member_cache SET fetch_started_at = NULL WHERE team_id = ?",
                             (team_id,))
                conn.commit()
                conn.close()
                # 一小时前占下、现在已在名单里的席位：这份完整快照之后应当放掉。
                self._hold(team_id, "keeper@example.com")
                started_after = _now()

                run(team_id)

                row = self._cache_row(team_id)
                emails = {m["email"] for m in json.loads(row["members_json"])}
                self.assertEqual(emails, {"owner@example.com", "keeper@example.com"})
                self.assertIsNotNone(row["fetch_started_at"])
                self.assertGreaterEqual(row["fetch_started_at"], started_after)
                self.assertEqual(self._holds(team_id), set())
                self._retire(team_id)


if __name__ == "__main__":
    unittest.main()
