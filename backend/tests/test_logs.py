import unittest
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes.logs import (
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


if __name__ == "__main__":
    unittest.main()
