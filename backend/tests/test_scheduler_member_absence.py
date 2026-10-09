"""A member counts as gone only when the upstream proves it.

An unrecognised or incomplete member / invite list is an error, never an empty
Team: the member-cache parser raises on it, and the auto-kick lookups report it
so the expired row is kept for the next round instead of being closed as "no
longer there". A member_expiry row written after the sync took its snapshot is
never judged absent from that snapshot.
"""

import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _redemption_fixtures import EMAIL, ConnectedTeamCase

from app import member_cache_service
from app import scheduler as app_scheduler

# 真实的 /users 回复里至少有 owner。
_OWNER_ROW = {"id": "u-owner", "email": "owner@example.com", "role": "account-owner"}


# ── 自动踢人的邮箱查找：结构不认识的 200 是"未知"，不是"人不在" ──────────────

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


class AutoKickKeepsRowOnUnrecognizedReplyTest(ConnectedTeamCase):
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


# ── 上游名单残缺不能归一化成"Team 为空" ─────────────────────────────────────

class MalformedUpstreamFailsClosedTest(unittest.TestCase):
    def test_empty_list_is_still_an_empty_team(self):
        self.assertEqual(member_cache_service._api_items({"items": []}, "users"), [])

    def test_missing_and_null_list_fields_raise(self):
        for payload in ({}, {"items": None}, {"total": 3}, []):
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPException) as ctx:
                    member_cache_service._api_items(payload, "users")
                self.assertEqual(ctx.exception.status_code, 502)


# ── 快照拍完之后才写进来的成员行不参与"缺席→踢"的判定 ──────────────────────

class AbsenceJudgementSkipsRowsNewerThanSnapshotTest(unittest.TestCase):
    """反向缺席判定的时间界限。

    成员名单在 ``snapshot_taken_at`` 那一刻拍下，``expiry_rows`` 在那之后才读。
    夹在中间落地的一次兑换写出的行当然不在名单里，判它缺席会 kicked=1，下一轮再
    以 source='detected'、expires_at=NULL、auto_kick=0 重新插一条——付过钱的到期
    时间没了，人正好长成 patrol 的踢人目标。
    """

    def _judge(self, row_created_at, snapshot_taken_at):
        return app_scheduler._too_new_to_judge_absent(row_created_at, snapshot_taken_at)

    def test_row_created_after_the_snapshot_is_too_new_to_judge(self):
        snapshot = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(self._judge("2026-09-11T12:00:01+00:00", snapshot))

    def test_row_created_before_the_snapshot_is_judged_normally(self):
        snapshot = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(self._judge("2026-09-11T11:59:59+00:00", snapshot))

    def test_legacy_row_without_timestamps_is_still_judged(self):
        snapshot = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(self._judge(None, snapshot))

    def test_the_absence_branch_is_the_only_caller_and_is_wired_up(self):
        source = Path(app_scheduler.__file__).read_text(encoding="utf-8")
        self.assertIn("COALESCE(created_at, first_seen_at) AS row_created_at", source)
        self.assertIn("if _too_new_to_judge_absent(", source)
        # 快照时刻必须在拉名单之前取，否则这个界限本身就是错的。
        snapshot_line = source.index("snapshot_taken_at = datetime.now(timezone.utc)")
        members_line = source.index("members_items, m_err = _fetch_all_api_items_sync(")
        self.assertLess(snapshot_line, members_line)


if __name__ == "__main__":
    unittest.main()
