import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes.logs import LOG_SEARCH_COLUMNS, add_log_search_condition


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


if __name__ == "__main__":
    unittest.main()
