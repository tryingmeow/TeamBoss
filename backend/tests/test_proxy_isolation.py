"""A Team bound to a proxy never reaches chatgpt.com from any other exit.

Resolution has exactly two outcomes: a usable proxy URL, or ProxyUnavailableError.
``proxy_id`` empty is not a failure — that Team is configured for a direct
connection. Every other case (row gone, blank url, unreadable table) raises, so no
call site can silently fall back to the host's own IP.

The behavioral half of the invariant is checked on the refresh path, which holds
the long-lived session token: an unresolvable proxy makes it return a failed
outcome without sending the session anywhere.
"""

import _isolation  # noqa: F401  must precede any app import
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import insert_row, start_temp_db  # noqa: I001  (_isolation first)

from app.chatgpt_limiter import refresh_team_auth_sync
from app.proxy_resolve import ProxyUnavailableError, resolve_proxy_url_sync


def _conn_with_proxies(rows: list[tuple[int, str]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE proxies (id INTEGER PRIMARY KEY, url TEXT)")
    conn.executemany("INSERT INTO proxies (id, url) VALUES (?, ?)", rows)
    return conn


class ProxyResolutionTest(unittest.TestCase):
    def test_no_proxy_selected_resolves_to_direct(self):
        conn = _conn_with_proxies([])
        self.addCleanup(conn.close)
        for proxy_id in (None, 0, ""):
            self.assertIsNone(resolve_proxy_url_sync(conn, proxy_id))

    def test_selected_proxy_resolves_to_its_url(self):
        conn = _conn_with_proxies([(7, "http://user:pass@10.0.0.1:8080")])
        self.addCleanup(conn.close)
        self.assertEqual(resolve_proxy_url_sync(conn, 7), "http://user:pass@10.0.0.1:8080")

    def test_missing_row_raises_instead_of_going_direct(self):
        conn = _conn_with_proxies([(7, "http://10.0.0.1:8080")])
        self.addCleanup(conn.close)
        with self.assertRaises(ProxyUnavailableError):
            resolve_proxy_url_sync(conn, 9)

    def test_blank_url_raises_instead_of_going_direct(self):
        conn = _conn_with_proxies([(1, ""), (2, "   ")])
        self.addCleanup(conn.close)
        for proxy_id in (1, 2):
            with self.assertRaises(ProxyUnavailableError):
                resolve_proxy_url_sync(conn, proxy_id)

    def test_unreadable_table_raises_instead_of_going_direct(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        with self.assertRaises(ProxyUnavailableError):
            resolve_proxy_url_sync(conn, 7)


class RefreshKeepsTeamOnItsProxyTest(unittest.TestCase):
    def setUp(self):
        self.db_path = start_temp_db(self)
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        insert_row(conn, "teams", {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Proxied",
            "owner_email": "owner@example.com",
            "access_token": "access-token",
            "session_token": "session-token",
            "device_id": "device",
            "status": "active",
            "proxy_id": 404,  # 没有对应的 proxies 行
        })
        conn.commit()

    def test_unresolvable_proxy_fails_the_refresh_without_calling_upstream(self):
        with patch("app.chatgpt_client.ChatGPTClient.refresh_token") as refresh:
            outcome = refresh_team_auth_sync(
                "11111111-1111-1111-1111-111111111111",
                trigger="manual_token_refresh",
                force=True,
            )
        refresh.assert_not_called()
        self.assertEqual(outcome.status, "failed")
        self.assertIn("404", outcome.error or "")


if __name__ == "__main__":
    unittest.main()
