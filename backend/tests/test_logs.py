import _isolation  # noqa: F401  must precede any app import
import unittest
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app import database
from app.routes.logs import (
    MAX_LOG_FILTER_VALUES,
    get_logs,
    LOG_SEARCH_COLUMNS,
    MEMBER_LOG_ACTIONS,
    MEMBER_LOG_ACTION_PREFIXES,
    add_log_scope_condition,
    add_log_search_condition,
)


class LogSearchTest(unittest.TestCase):
    def test_empty_query_does_not_add_condition(self):
        conditions = []
        params = []

        add_log_search_condition(conditions, params, "   ")

        self.assertEqual(conditions, [])
        self.assertEqual(params, [])

    def test_search_query_checks_all_log_columns(self):
        conditions = []
        params = []

        add_log_search_condition(conditions, params, "Kick")

        self.assertEqual(len(conditions), 1)
        self.assertIn("LOWER(COALESCE(l.action, '')) LIKE ?", conditions[0])
        self.assertIn("LOWER(COALESCE(l.detail, '')) LIKE ?", conditions[0])
        self.assertIn("LOWER(COALESCE(l.created_at, '')) LIKE ?", conditions[0])
        self.assertIn("LOWER(COALESCE(t.name, '')) LIKE ?", conditions[0])
        self.assertIn("LOWER(COALESCE(t.owner_email, '')) LIKE ?", conditions[0])
        self.assertEqual(params, ["%kick%"] * len(LOG_SEARCH_COLUMNS))

    def test_member_scope_uses_explicit_action_classification(self):
        conditions = []
        params = []

        add_log_scope_condition(conditions, params, "members")

        self.assertEqual(len(conditions), 1)
        self.assertIn("l.action IN", conditions[0])
        self.assertNotIn("target_email IS NOT NULL", conditions[0])
        self.assertEqual(
            params,
            list(MEMBER_LOG_ACTIONS)
            + [f"{prefix}%" for prefix in MEMBER_LOG_ACTION_PREFIXES],
        )
        self.assertIn("remove_member", params)
        self.assertNotIn("add_team", params)
        self.assertNotIn("reimport_team", params)

    def test_member_scope_and_search_are_combined_in_parameter_order(self):
        conditions = []
        params = []

        add_log_scope_condition(conditions, params, "members")
        add_log_search_condition(conditions, params, "Alice")

        scope_param_count = len(MEMBER_LOG_ACTIONS) + len(MEMBER_LOG_ACTION_PREFIXES)
        self.assertEqual(len(conditions), 2)
        self.assertEqual(params[scope_param_count:], ["%alice%"] * len(LOG_SEARCH_COLUMNS))

    def test_unknown_or_empty_scope_does_not_add_condition(self):
        for scope in (None, "", "teams"):
            conditions = []
            params = []
            add_log_scope_condition(conditions, params, scope)
            self.assertEqual(conditions, [])
            self.assertEqual(params, [])

    def test_member_scope_selects_targetless_member_actions_and_excludes_team_imports(self):
        conditions = []
        params = []
        add_log_scope_condition(conditions, params, "members")

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE operation_logs (action TEXT, target_email TEXT)")
            conn.executemany(
                "INSERT INTO operation_logs (action, target_email) VALUES (?, ?)",
                (
                    ("remove_member", None),
                    ("member_watch_invite", "member@example.com"),
                    ("self_service_renew", "member@example.com"),
                    ("add_team", "owner@example.com"),
                    ("reimport_team", "owner@example.com"),
                    ("sync_team", None),
                ),
            )
            rows = conn.execute(
                f"SELECT l.action FROM operation_logs l WHERE {conditions[0]} ORDER BY l.action",
                params,
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(
            [row[0] for row in rows],
            ["member_watch_invite", "remove_member", "self_service_renew"],
        )


class LogRouteQueryTest(unittest.IsolatedAsyncioTestCase):
    """Real SQL against the isolated DB: one request must equal the old union of requests."""

    ROWS = [
        # (action, target_email, detail, result, trigger_type, created_at)
        ("remove_member", "alice@example.com", "kick", "success", "manual", "2026-01-01 10:00:00"),
        ("invite_member", "bob@example.com", "welcome", "success", "auto", "2026-01-02 10:00:00"),
        ("set_expiry", "carol@example.com", "30d", "failed", "manual", "2026-01-03 10:00:00"),
        ("add_team", "owner@example.com", "imported", "success", "manual", "2026-01-04 10:00:00"),
        ("login", None, "ALICE logged in", "success", "system", "2026-01-05 10:00:00"),
        ("sync_team", None, None, "failed", "auto", "2026-01-06 10:00:00"),
    ]

    async def asyncSetUp(self):
        await database.init_database()
        async with database.get_db() as db:
            await db.execute("DELETE FROM operation_logs")
            await db.executemany(
                "INSERT INTO operation_logs (action, target_email, detail, result, trigger_type, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                self.ROWS,
            )
            await db.commit()

    async def get(self, **kw):
        args = dict(
            team_id=None, action=None, scope=None, q=None, q_action=None, page=1, per_page=50
        )
        args.update(kw)
        res = await get_logs(**args)
        return res

    async def ids(self, **kw):
        kw.setdefault("per_page", 1000)
        return {row["id"] for row in (await self.get(**kw))["logs"]}

    async def actions(self, **kw):
        return sorted(row["action"] for row in (await self.get(per_page=1000, **kw))["logs"])

    async def test_single_q_unchanged(self):
        self.assertEqual(await self.actions(q=["Alice"]), ["login", "remove_member"])

    async def test_repeated_q_is_or(self):
        self.assertEqual(
            await self.actions(q=["bob@", "carol@", "  ", "bob@"]),
            ["invite_member", "set_expiry"],
        )

    async def test_q_action_is_ored_with_q(self):
        self.assertEqual(
            await self.actions(q=["bob@"], q_action=["add_team,sync_team"]),
            ["add_team", "invite_member", "sync_team"],
        )
        self.assertEqual(await self.actions(q_action=["add_team", "login"]), ["add_team", "login"])

    async def test_action_list_is_in_filter_and_anded(self):
        self.assertEqual(await self.actions(action=["add_team,login"]), ["add_team", "login"])
        self.assertEqual(await self.actions(action=["add_team", "login"]), ["add_team", "login"])
        self.assertEqual(await self.actions(action=["login"]), ["login"])
        self.assertEqual(await self.actions(action=["add_team", "login"], q=["alice"]), ["login"])

    async def test_member_scope_still_anded_with_search(self):
        self.assertEqual(
            await self.actions(scope="members", q=["alice"], q_action=["login", "add_team"]),
            ["remove_member"],
        )

    async def test_caps_reject_oversized_requests(self):
        many = [f"t{i}" for i in range(MAX_LOG_FILTER_VALUES + 1)]
        for kw in ({"q": many}, {"q_action": many}, {"action": many}, {"action": [",".join(many)]}):
            with self.assertRaises(HTTPException) as ctx:
                await self.get(**kw)
            self.assertEqual(ctx.exception.status_code, 422)
        ok = many[:MAX_LOG_FILTER_VALUES]
        await self.get(q=ok, q_action=ok, action=ok)
        # duplicates and blanks do not count toward the cap
        await self.get(q=["x"] * 200 + ["", " "])

    async def test_values_are_parameterized(self):
        await self.get(q=["'; DROP TABLE operation_logs; --"], q_action=["x') OR 1=1 --"])
        self.assertEqual(len(await self.ids()), len(self.ROWS))

    async def test_per_page_allows_1000(self):
        self.assertEqual((await self.get(per_page=1000))["per_page"], 1000)

    async def test_one_combined_request_equals_old_union(self):
        text = "alice"
        old_requests = [
            dict(q=[text]),
            dict(action=["add_team"]),
            dict(action=["login"]),
            dict(q=["failed"]),
        ]
        union: set[int] = set()
        for req in old_requests:
            union |= await self.ids(**req)
        combined = await self.ids(q=[text, "failed"], q_action=["add_team", "login"])
        self.assertEqual(combined, union)
        scoped_union: set[int] = set()
        for req in old_requests:
            scoped_union |= await self.ids(scope="members", **req)
        self.assertEqual(
            await self.ids(scope="members", q=[text, "failed"], q_action=["add_team", "login"]),
            scoped_union,
        )
        page = await self.get(q=[text, "failed"], q_action=["add_team", "login"], per_page=2)
        self.assertEqual(page["total"], len(union))
        self.assertEqual(len(page["logs"]), 2)

    async def test_rows_with_the_same_timestamp_page_newest_id_first(self):
        # The old client-side merge ordered by created_at, then id, both descending.
        async with database.get_db() as db:
            await db.executemany(
                "INSERT INTO operation_logs (action, created_at) VALUES (?, '2026-02-01 00:00:00')",
                [("tie_a",), ("tie_b",), ("tie_c",)],
            )
            await db.commit()
        pages = [
            [row["action"] for row in (await self.get(q_action=["tie_a", "tie_b", "tie_c"], per_page=1, page=n))["logs"]]
            for n in (1, 2, 3)
        ]
        self.assertEqual(pages, [["tie_c"], ["tie_b"], ["tie_a"]])


if __name__ == "__main__":
    unittest.main()
