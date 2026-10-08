"""调度器/同步路径上三处资金缺陷的回归测试。

1. 同一次兑换被两条恢复路径各加一遍时长（30 天码变 60 天）。
2. 上游 seats_entitled 为 null/0/非正整数时被原样写库，patrol 据此把所有默认
   席位算成超员。
3. 自动踢人的邮箱查找把"结构不认识的 200"当成空列表，到期行被当成"人已不在"
   关掉。

每个用例断言的是修好之后的行为，不是实现细节。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.routes import access_tokens
from app.scheduler import _reconcile_pending_invites_sync
from app.services import member_expiry as member_expiry_module
from app.services.member_expiry import (
    extend_member_expiry,
    record_confirmed_invite_extension,
)

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
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO teams (id, name, status, access_token, device_id,
                                  created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', 'stub-token', 'stub-device',
                       '2026-10-01', '2026-10-01')"""
        )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _new_token_use(self, *, result="pending", grant="30d"):
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO access_tokens
               (token_hash, token_prefix, grant_expires_in, max_uses, used_count,
                disabled, created_at)
               VALUES (?, 'p', ?, 1, 1, 0, '2026-10-01')""",
            (f"h-{datetime.now(timezone.utc).timestamp()}", grant),
        )
        token_id = cur.lastrowid
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO access_token_uses
               (token_id, email, action, team_id, user_id, expires_at, result,
                created_at)
               VALUES (?, ?, 'invite_pending', 'team-1', NULL, NULL, ?, ?)""",
            (token_id, EMAIL, result, now_iso),
        )
        token_use_id = cur.lastrowid
        conn.commit()
        conn.close()
        return token_use_id


# ── 1. 两条恢复路径共用一次性凭据：时长只加一次 ───────────────────────────

class SingleCreditAcrossRecoveryPathsTest(_TempDbTest):
    """兑换邀请已在上游成功、本地 extend 写入连续失败 → 留下 kind='extend' 兜底行
    且兑换仍是 pending。之后 60 秒一轮的 reconcile_pending_redemptions 和数据同步里
    的 _reconcile_pending_invites_sync 都会去结算它，谁先谁后都只能加一次时长。
    """

    def _fail_primary_write(self):
        """跑真实的 record_confirmed_invite_extension，但让 extend 写入每次都失败。"""
        token_use_id = self._new_token_use()
        failing = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))
        with patch.object(member_expiry_module, "extend_member_expiry", failing), \
             patch.object(member_expiry_module, "_CONFIRM_WRITE_BACKOFF_SECONDS", 0):
            asyncio.run(
                record_confirmed_invite_extension(
                    "team-1",
                    "",
                    EMAIL,
                    "30d",
                    source="self_service",
                    token_use_id=token_use_id,
                    token_action="invited",
                )
            )
        conn = self._conn()
        rows = conn.execute(
            "SELECT kind, resolved FROM pending_invite_reconciliations WHERE token_use_id = ?",
            (token_use_id,),
        ).fetchall()
        use = conn.execute(
            "SELECT result FROM access_token_uses WHERE id = ?", (token_use_id,)
        ).fetchone()
        conn.close()
        self.assertEqual([(r["kind"], r["resolved"]) for r in rows], [("extend", 0)])
        self.assertEqual(use["result"], "pending")
        return token_use_id

    def _run_redemption_reconciler(self):
        snapshot = {"members": [{"id": "u1", "email": EMAIL}], "pending_invites": []}
        with patch.object(access_tokens, "fetch_and_cache_members",
                          AsyncMock(return_value=snapshot)), \
             patch.object(access_tokens, "_get_proxy_url", AsyncMock(return_value=None)), \
             patch.object(access_tokens, "ChatGPTClient", MagicMock()):
            return asyncio.run(access_tokens.reconcile_pending_redemptions())

    def _run_scheduler_sync(self):
        conn = self._conn()
        try:
            reconciled = _reconcile_pending_invites_sync(
                conn,
                "team-1",
                [{"id": "u1", "email": EMAIL}],
                [],
                datetime.now(timezone.utc).isoformat(),
            )
            conn.commit()
        finally:
            conn.close()
        return reconciled

    def _state(self, token_use_id):
        conn = self._conn()
        expiry_rows = conn.execute(
            "SELECT expires_at FROM member_expiry WHERE team_id = 'team-1' AND kicked = 0"
        ).fetchall()
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM pending_invite_reconciliations WHERE resolved = 0"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(len(expiry_rows), 1)
        return expiry_rows[0]["expires_at"], dict(use), unresolved

    def _assert_about_thirty_days(self, expires_iso):
        remaining = datetime.fromisoformat(expires_iso) - datetime.now(timezone.utc)
        self.assertGreater(remaining, timedelta(days=29, hours=23))
        self.assertLessEqual(remaining, timedelta(days=30))

    def test_reconciler_first_then_scheduler_does_not_add_again(self):
        token_use_id = self._fail_primary_write()

        counts = self._run_redemption_reconciler()
        self.assertEqual(counts["confirmed"], 1)
        credited, use, _ = self._state(token_use_id)
        self._assert_about_thirty_days(credited)
        self.assertEqual(use["result"], "success")

        self._run_scheduler_sync()
        after, use, unresolved = self._state(token_use_id)
        # 原先这里会变成 credited + 30 天：兜底行把同一笔购买又追加了一遍。
        self.assertEqual(after, credited)
        self.assertEqual(use["expires_at"], credited)
        self.assertEqual(unresolved, 0)

    def test_scheduler_first_then_reconciler_does_not_add_again(self):
        token_use_id = self._fail_primary_write()

        self._run_scheduler_sync()
        credited, use, unresolved = self._state(token_use_id)
        self._assert_about_thirty_days(credited)
        self.assertEqual(use["result"], "success")
        # 收据上记的到期时间必须就是写进 member_expiry 的那一个。
        self.assertEqual(use["expires_at"], credited)
        self.assertEqual(unresolved, 0)

        # 结清后的兑换不再出现在对账扫描里……
        counts = self._run_redemption_reconciler()
        # 只比这几个计数：对账任务还会报告别的计数（例如 uncertain），与本用例无关。
        self.assertEqual(
            {key: counts.get(key) for key in ("confirmed", "released", "waiting")},
            {"confirmed": 0, "released": 0, "waiting": 0},
        )
        # ……就算对账在调度器结清之前已经读到了它（并发），extend 也只会原样返回
        # 已结清的到期时间，不会再加。
        returned = asyncio.run(
            extend_member_expiry(
                "team-1", "", EMAIL, "30d",
                source="self_service", keep_permanent=True,
                token_use_id=token_use_id, token_action="invited",
            )
        )
        self.assertEqual(returned, credited)
        after, _, _ = self._state(token_use_id)
        self.assertEqual(after, credited)

    def test_receipt_records_the_expiry_actually_written_on_top_of_existing_time(self):
        token_use_id = self._fail_primary_write()
        far_future = (datetime.now(timezone.utc) + timedelta(days=200)).isoformat()
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', 'u1', ?, ?, 1, 0, '2026-01-01', 'self_service', '2026-01-01')""",
            (EMAIL, far_future),
        )
        conn.commit()
        conn.close()

        self._run_scheduler_sync()
        credited, use, _ = self._state(token_use_id)
        gained = datetime.fromisoformat(credited) - datetime.fromisoformat(far_future)
        self.assertGreater(gained, timedelta(days=29, hours=23))
        self.assertLessEqual(gained, timedelta(days=30))
        self.assertEqual(use["expires_at"], credited)

    def test_duplicate_fallback_rows_for_one_redemption_credit_once(self):
        """对账路径自己的 extend 也可能再失败一次，于是同一次兑换留下两行 'extend'。"""
        token_use_id = self._fail_primary_write()
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

        self._run_scheduler_sync()
        credited, use, unresolved = self._state(token_use_id)
        self._assert_about_thirty_days(credited)
        self.assertEqual(use["expires_at"], credited)
        self.assertEqual(unresolved, 0)

    def test_redemption_released_by_admin_is_never_credited(self):
        """管理员已核实退码（result='failed'）：之后人再出现在名单里，兜底行也不能
        把已经退掉的时长写回去。"""
        token_use_id = self._fail_primary_write()
        conn = self._conn()
        conn.execute(
            """UPDATE access_token_uses
               SET action = 'redeem_admin_released', result = 'failed', expires_at = NULL
               WHERE id = ?""",
            (token_use_id,),
        )
        conn.commit()
        conn.close()

        self._run_scheduler_sync()
        conn = self._conn()
        expiry_count = conn.execute("SELECT COUNT(*) FROM member_expiry").fetchone()[0]
        use = conn.execute(
            "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
            (token_use_id,),
        ).fetchone()
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM pending_invite_reconciliations WHERE resolved = 0"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(expiry_count, 0)
        self.assertEqual(use["result"], "failed")
        self.assertIsNone(use["expires_at"])
        self.assertEqual(unresolved, 0)



# ── 2. seats_entitled 只认正整数，不合格就保留上一次的值 ─────────────────────

# 上游契约是 JSON 整数。null / 0 / 负数一旦落库，patrol 把每个默认席位都算成超员；
# 数字字符串、浮点数、bool 说明响应结构变了，同样不认。
_BAD_ENTITLEMENTS = (None, 0, -3, "25", 25.0, True)


class _Missing:
    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()


# 真实的 /users 回复里至少有 owner。
_OWNER_ROW = {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}


def _subscription(entitled):
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


class _FakeSyncClient:
    subscription: dict = {}

    def __init__(self, access_token, team_id, device_id, proxy_url=None):
        self.team_id = team_id

    def get_subscription(self):
        return dict(_FakeSyncClient.subscription)

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


class SeatsEntitledGuardTest(_TempDbTest):
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

        _FakeSyncClient.subscription = subscription
        run_patrol = MagicMock(return_value={})
        with patch.object(app_scheduler, "ChatGPTClient", _FakeSyncClient), \
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
                patrolled = self._run_data_sync(_subscription(bad))
                self.assertEqual(self._entitled(), (5, "integer"))
                # 这一轮分母没刷新到可信值：巡逻照常撤陌生邀请、执行严格模式，
                # 但库里留着的 5 不能拿去判超员。
                self.assertIn("team-1", patrolled)
                self.assertIn("team-1", self.skip_over_quota)

    def test_scheduled_sync_writes_a_valid_entitlement(self):
        patrolled = self._run_data_sync(_subscription(7))
        self.assertEqual(self._entitled(), (7, "integer"))
        self.assertIn("team-1", patrolled)
        self.assertNotIn("team-1", self.skip_over_quota)

    def test_capacity_cache_keeps_previous_entitlement(self):
        from app.services.seat_capacity import update_capacity_cache

        seat_counts = {"seat_type_counts": {"default": 3, "usage_based": 0}}
        for bad in _BAD_ENTITLEMENTS:
            with self.subTest(seats_entitled=bad):
                self._set_entitled(5)
                asyncio.run(update_capacity_cache("team-1", _subscription(bad), seat_counts))
                self.assertEqual(self._entitled(), (5, "integer"))
        asyncio.run(update_capacity_cache("team-1", _subscription(9), seat_counts))
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
                                _Client(_subscription(bad))
                            )
                        )
            capacity, *_ = asyncio.run(
                seat_capacity.fetch_live_chatgpt_seat_capacity(_Client(_subscription(6)))
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
                        team_sync_service._fetch_overview(_Client(_subscription(bad)))
                    )
                    self.assertNotIn("seats_entitled", updates)
                    # 同一响应里的其它字段照常写。
                    self.assertEqual(updates["seats_in_use"], 3)
            updates, _ = asyncio.run(
                team_sync_service._fetch_overview(_Client(_subscription(8)))
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
            _Client.sub = _subscription(entitled)
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



# ── 3. 自动踢人的邮箱查找：结构不认识的 200 是"未知"，不是"人不在" ──────────

class _PagedClient:
    """按调用顺序吐出预设的 get_members / get_pending_invites 响应。"""

    def __init__(self, members_pages=None, invite_pages=None):
        self.members_pages = list(members_pages or [])
        self.invite_pages = list(invite_pages or [])

    def get_members(self, offset=0, limit=100):
        return self.members_pages.pop(0)

    def get_pending_invites(self, offset=0, limit=100):
        return self.invite_pages.pop(0)


def _full_page(n=100, key="items"):
    return {key: [{"id": f"u{i}", "email": f"other{i}@example.com",
                   "email_address": f"other{i}@example.com"} for i in range(n)]}


# 200 但结构不认识 / 不完整的名单：都不能证明这个人不在。
_UNRECOGNIZED_PAGES = {
    "empty object": [{}],
    "null items": [{"items": None}],
    "string items": [{"items": "nope"}],
    "list body": [[]],
    "null body": [None],
    "non-object entry": [{"items": ["redeemer@example.com"]}],
    "truncated before total": [dict(_full_page(), total=250), {"items": [], "total": 250}],
}


class AutoKickLookupFailsClosedTest(unittest.TestCase):
    def _direct(self):
        from app import scheduler as app_scheduler

        return patch.object(
            app_scheduler, "run_chatgpt_call_sync", lambda fn, *a, **kw: fn(*a, **kw)
        )

    def test_member_lookup_reports_unrecognized_replies_as_errors(self):
        from app.scheduler import _find_member_user_id_by_email

        with self._direct():
            for label, pages in _UNRECOGNIZED_PAGES.items():
                with self.subTest(reply=label):
                    user_id, error = _find_member_user_id_by_email(
                        _PagedClient(members_pages=list(pages)), EMAIL
                    )
                    self.assertIsNone(user_id)
                    self.assertTrue(error)

            # 找到了这个邮箱却没有 user id：同样不能当成"不在"。
            user_id, error = _find_member_user_id_by_email(
                _PagedClient(members_pages=[{"items": [{"email": EMAIL}]}]), EMAIL
            )
            self.assertIsNone(user_id)
            self.assertTrue(error)

            # 空的成员名单不完整（真实名单里至少有 owner），同样不能当成"不在"。
            user_id, error = _find_member_user_id_by_email(
                _PagedClient(members_pages=[{"items": [], "total": 0}]), EMAIL
            )
            self.assertIsNone(user_id)
            self.assertTrue(error)

            # 结构完整：真的不在 / 真的找到。
            self.assertEqual(
                _find_member_user_id_by_email(
                    _PagedClient(members_pages=[{"items": [dict(_OWNER_ROW)], "total": 1}]), EMAIL
                ),
                (None, None),
            )
            self.assertEqual(
                _find_member_user_id_by_email(
                    _PagedClient(members_pages=[_full_page(),
                                                {"items": [{"id": "u-redeemer", "email": EMAIL}]}]),
                    EMAIL,
                ),
                ("u-redeemer", None),
            )

    def test_pending_lookup_reports_unrecognized_replies_as_errors(self):
        from app.scheduler import _pending_invite_exists

        with self._direct():
            for label, pages in _UNRECOGNIZED_PAGES.items():
                with self.subTest(reply=label):
                    exists, error = _pending_invite_exists(
                        _PagedClient(invite_pages=list(pages)), EMAIL
                    )
                    self.assertFalse(exists)
                    self.assertTrue(error)

            self.assertEqual(
                _pending_invite_exists(_PagedClient(invite_pages=[{"invites": []}]), EMAIL),
                (False, None),
            )
            self.assertEqual(
                _pending_invite_exists(
                    _PagedClient(invite_pages=[{"items": [{"email_address": EMAIL}]}]), EMAIL
                ),
                (True, None),
            )


class AutoKickKeepsRowOnUnrecognizedReplyTest(_TempDbTest):
    """到期行没有 user_id，只能按邮箱查。查找拿到结构不认识的 200 时，这一行必须
    原样留着（下一轮重试），不能被当成"人已不在"关掉——关掉之后这个人会被重新
    检测成 detected、没有到期时间，白占席位。"""

    def _seed_expired_row(self):
        past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        conn = self._conn()
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked,
                first_seen_at, source, created_at)
               VALUES ('team-1', '', ?, ?, 1, 0, ?, 'self_service', ?)""",
            (EMAIL, past, past, past),
        )
        conn.commit()
        conn.close()

    def _run(self, members_pages, invite_pages):
        from app import scheduler as app_scheduler

        def _factory(*a, **kw):
            return _PagedClient(list(members_pages), list(invite_pages))

        with patch.object(app_scheduler, "ChatGPTClient", side_effect=_factory), \
             patch.object(app_scheduler, "run_chatgpt_call_sync",
                          lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app_scheduler, "notify_member_event_sync", lambda *a, **kw: None):
            app_scheduler.auto_kick_job()

        conn = self._conn()
        row = conn.execute(
            "SELECT kicked, kick_source FROM member_expiry WHERE email = ?", (EMAIL,)
        ).fetchone()
        log = conn.execute(
            """SELECT action, result, detail FROM operation_logs
               WHERE action IN ('auto_kick', 'auto_revoke_invite')
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        return dict(row), dict(log) if log else None

    def test_unrecognized_member_reply_leaves_the_row_for_next_round(self):
        self._seed_expired_row()
        row, log = self._run(members_pages=[{"total": 1}], invite_pages=[{"items": []}])
        self.assertEqual(row["kicked"], 0)
        self.assertEqual(log["result"], "failed")
        self.assertEqual(log["detail"], "lookup member by email")

    def test_unrecognized_invite_reply_leaves_the_row_for_next_round(self):
        self._seed_expired_row()
        row, log = self._run(members_pages=[{"items": [dict(_OWNER_ROW)]}], invite_pages=[{"invites": None}])
        self.assertEqual(row["kicked"], 0)
        self.assertEqual(log["result"], "failed")
        self.assertEqual(log["detail"], "lookup pending invite")

    def test_confirmed_absence_still_closes_the_row(self):
        self._seed_expired_row()
        row, log = self._run(members_pages=[{"items": [dict(_OWNER_ROW)]}], invite_pages=[{"items": []}])
        self.assertEqual(row["kicked"], 1)
        self.assertEqual(row["kick_source"], "auto_expire")
        self.assertEqual(log["detail"], "member or invite already absent")


if __name__ == "__main__":
    unittest.main()
