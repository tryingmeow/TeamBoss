"""Session-refresh diagnostics and the re-import / in-flight-refresh race.

Diagnostics: every /api/auth/session call leaves one journald line and a compact
summary in operation_logs.detail, built only from hashes, lengths, booleans and
key/cookie names. Nothing here may ever contain a raw token, cookie value or
identity claim.

Race: a refresh can spend up to 60 s on the network. If the operator re-imports
the Team meanwhile, the refresh's result belongs to the old session and must not
overwrite the imported tokens or change status/auth_state.
"""

import asyncio
import hashlib
import io
import json
import secrets
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import jwt
import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPHeaderDict
from urllib3.response import HTTPResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import chatgpt_client as chatgpt_client_module
from app import chatgpt_limiter
from app import database as app_database
from app import team_service
from app.chatgpt_client import (
    ChatGPTClient,
    parse_set_cookie,
    reassemble_session_cookie,
)
from app.models import TeamSession


TEAM_ID = "00000000-0000-4000-8000-0000000d1a90"
COOKIE = "__Secure-next-auth.session-token"
OWNER_EMAIL = "owner-diag@example.com"
OWNER_USER_ID = "user-" + "Q" * 24


def _secret() -> str:
    return secrets.token_urlsafe(96)


def _sha8(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _access_token(delta: timedelta, *, finalizer: bool) -> str:
    now = datetime.now(timezone.utc)
    auth = {"chatgpt_account_id": TEAM_ID, "chatgpt_user_id": OWNER_USER_ID}
    if finalizer:
        auth["chatgpt_login_finalizer_auth_session_id"] = "finalizer-" + "Z" * 20
    return jwt.encode(
        {
            "iat": int(now.timestamp()),
            "exp": int((now + delta).timestamp()),
            "jti": secrets.token_hex(8),
            "https://api.openai.com/auth": auth,
            "https://api.openai.com/profile": {"email": OWNER_EMAIL},
        },
        key="",
        algorithm="none",
    )


def _session_response(status: int, body, set_cookies=()) -> requests.Response:
    """A real requests.Response on top of a real urllib3 response.

    Multiple Set-Cookie headers stay separate in raw.headers, exactly as they do
    on the wire, so this exercises the same getlist path as production.
    """
    headers = HTTPHeaderDict()
    headers.add("Content-Type", "application/json")
    for value in set_cookies:
        headers.add("Set-Cookie", value)
    raw = HTTPResponse(
        body=io.BytesIO(json.dumps(body).encode()),
        headers=headers,
        status=status,
        preload_content=False,
    )
    request = requests.Request("GET", "https://chatgpt.com/api/auth/session").prepare()
    return HTTPAdapter().build_response(request, raw)


def _session_body(access_token: str, session_token: str) -> dict:
    return {
        "user": {"id": OWNER_USER_ID, "email": OWNER_EMAIL, "name": "Owner"},
        "expires": "2026-12-01T00:00:00.000Z",
        "accessToken": access_token,
        "authProvider": "openai",
        "sessionToken": session_token,
    }


class _TempDbMixin:
    def _setup_db(self, access_token: str, session_token: str) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        self.assertTrue(self.db_path.startswith(self.tmpdir.name))

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO teams (id, name, access_token, session_token, device_id,
                                  status, created_at, updated_at)
               VALUES (?, 'diag', ?, ?, 'device-1', 'active', '2026-09-01', '2026-09-01')""",
            (TEAM_ID, access_token, session_token),
        )
        conn.commit()
        conn.close()

    def _team(self) -> tuple:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                """SELECT access_token, session_token, status,
                          COALESCE(auth_state, 'ok'), auth_state_since
                   FROM teams WHERE id = ?""",
                (TEAM_ID,),
            ).fetchone()
        finally:
            conn.close()

    def _refresh_logs(self) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                """SELECT action, result, detail, error_message FROM operation_logs
                   WHERE team_id = ? ORDER BY id""",
                (TEAM_ID,),
            ).fetchall()
        finally:
            conn.close()


class SetCookieParsingTest(unittest.TestCase):
    def test_chunks_are_joined_in_index_order_and_deletions_ignored(self):
        cookies = [
            (f"{COOKIE}.1", "BBB", False),
            ("__cf_bm", "cf", False),
            (f"{COOKIE}", "", True),
            (f"{COOKIE}.0", "AAA", False),
            (f"{COOKIE}.2", "", True),
        ]
        token, meta = reassemble_session_cookie(cookies)
        self.assertEqual(token, "AAABBB")
        self.assertEqual(
            meta,
            {"source": "chunks", "chunks": 2, "chunks_contiguous": True, "cleared": True},
        )

    def test_single_cookie_and_cleared_only(self):
        self.assertEqual(
            reassemble_session_cookie([(COOKIE, "ONE", False)])[0], "ONE"
        )
        token, meta = reassemble_session_cookie([(COOKIE, "", True)])
        self.assertIsNone(token)
        self.assertEqual(meta["source"], "none")
        self.assertTrue(meta["cleared"])

    def test_max_age_zero_is_a_deletion_and_expires_commas_are_harmless(self):
        self.assertEqual(
            parse_set_cookie(f"{COOKIE}.0=abc; Path=/; Max-Age=0; Secure"),
            (f"{COOKIE}.0", "abc", True),
        )
        self.assertEqual(
            parse_set_cookie("__cf_bm=xyz; Expires=Sat, 26 Sep 2026 05:00:00 GMT; Path=/"),
            ("__cf_bm", "xyz", False),
        )


class RefreshTokenDiagnosticsTest(unittest.TestCase):
    """ChatGPTClient.refresh_token against a real response object (no network)."""

    def _call(self, response, sent_session: str) -> dict:
        with patch.object(chatgpt_client_module.requests, "get", return_value=response) as get:
            result = ChatGPTClient.refresh_token(sent_session)
        get.assert_called_once()
        return result

    def test_chunked_set_cookie_is_reassembled_and_compared(self):
        sent = _secret()
        json_session = _secret()
        part_a, part_b = _secret(), _secret()
        access = _access_token(timedelta(days=10), finalizer=True)
        body = _session_body(access, json_session)
        response = _session_response(
            200,
            body,
            [
                f"{COOKIE}.0={part_a}; Path=/; Expires=Fri, 25 Dec 2026 00:00:00 GMT; HttpOnly; Secure",
                "__cf_bm=cfvalue123; Path=/; Expires=Sat, 26 Sep 2026 05:00:00 GMT",
                f"{COOKIE}.1={part_b}; Path=/; HttpOnly; Secure",
                f"{COOKIE}=; Path=/; Max-Age=0",
            ],
        )

        result = self._call(response, sent)

        # Existing contract: body fields untouched, so JSON sessionToken still wins.
        for key, value in body.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["set_cookie_session_token"], part_a + part_b)

        diag = result["refresh_diagnostics"]
        self.assertEqual(diag["http_status"], 200)
        self.assertEqual(
            diag["json_keys"],
            ["accessToken", "authProvider", "expires", "sessionToken", "user"],
        )
        self.assertIsNone(diag["error"])
        self.assertTrue(diag["access_present"])
        self.assertTrue(diag["access_finalizer_claim"])
        self.assertIsNotNone(diag["access_iat"])
        self.assertIsNotNone(diag["access_exp"])
        self.assertEqual(
            diag["set_cookie_names"],
            [f"{COOKIE}.0", "__cf_bm", f"{COOKIE}.1", COOKIE],
        )
        self.assertEqual(diag["sc_session_source"], "chunks")
        self.assertEqual(diag["sc_session_chunks"], 2)
        self.assertTrue(diag["sc_session_cleared"])
        self.assertEqual(diag["sc_session_len"], len(part_a + part_b))
        self.assertEqual(diag["sc_session_sha8"], _sha8(part_a + part_b))
        self.assertEqual(diag["json_session_len"], len(json_session))
        self.assertEqual(diag["json_session_sha8"], _sha8(json_session))
        self.assertEqual(diag["sent_session_len"], len(sent))
        self.assertEqual(diag["sent_session_sha8"], _sha8(sent))
        self.assertFalse(diag["sc_eq_json"])
        self.assertFalse(diag["sc_eq_sent"])
        self.assertFalse(diag["json_eq_sent"])

    def test_single_set_cookie_matching_json_session(self):
        session = _secret()
        access = _access_token(timedelta(days=10), finalizer=False)
        response = _session_response(
            200,
            _session_body(access, session),
            [f"{COOKIE}={session}; Path=/; HttpOnly; Secure"],
        )

        result = self._call(response, _secret())

        diag = result["refresh_diagnostics"]
        self.assertEqual(result["set_cookie_session_token"], session)
        self.assertEqual(diag["sc_session_source"], "single")
        self.assertTrue(diag["sc_eq_json"])
        self.assertFalse(diag["access_finalizer_claim"])

    def test_in_band_error_without_cookies(self):
        response = _session_response(200, {"error": "RefreshAccessTokenError"})

        result = self._call(response, _secret())

        self.assertEqual(result["error"], "RefreshAccessTokenError")
        self.assertEqual(result["status_code"], 200)
        self.assertIsNone(result["set_cookie_session_token"])
        diag = result["refresh_diagnostics"]
        self.assertEqual(diag["json_keys"], ["error"])
        self.assertEqual(diag["error"], "RefreshAccessTokenError")
        self.assertFalse(diag["access_present"])
        self.assertEqual(diag["set_cookie_names"], [])
        self.assertEqual(diag["sc_session_source"], "none")
        self.assertIsNone(diag["sc_eq_json"])

    def test_http_error_still_reports_status_and_cookie_names(self):
        response = _session_response(
            401, {"detail": "nope"}, [f"{COOKIE}=; Path=/; Max-Age=0"]
        )

        result = self._call(response, _secret())

        self.assertEqual(result["status_code"], 401)
        diag = result["refresh_diagnostics"]
        self.assertEqual(diag["http_status"], 401)
        self.assertEqual(diag["exc_type"], "HTTPError")
        self.assertEqual(diag["set_cookie_names"], [COOKIE])
        self.assertTrue(diag["sc_session_cleared"])

    def test_diagnostics_failure_never_breaks_the_refresh(self):
        body = {"accessToken": "a", "sessionToken": "s"}
        with patch.object(
            chatgpt_client_module,
            "summarize_session_refresh",
            side_effect=RuntimeError("boom"),
        ):
            result = self._call(_session_response(200, body), "sent")
        self.assertEqual(result["accessToken"], "a")
        self.assertEqual(result["sessionToken"], "s")
        self.assertEqual(result["refresh_diagnostics"], {"diag_error": "RuntimeError"})


class LimiterDiagnosticsLoggingTest(_TempDbMixin, unittest.TestCase):
    """End to end through refresh_team_auth_sync with only requests.get faked."""

    def setUp(self):
        self.sent_session = _secret()
        self.old_access = _access_token(timedelta(hours=-2), finalizer=True)
        self._setup_db(self.old_access, self.sent_session)

    def _run(self, response, *, trigger="scheduled_expiry_refresh", force=True, file_update=None):
        file_update = file_update or Mock(return_value=True)
        with (
            patch.object(chatgpt_client_module.requests, "get", return_value=response),
            patch.object(chatgpt_limiter, "update_session_file_tokens", file_update),
            self.assertLogs("app.chatgpt_limiter", level="WARNING") as captured,
        ):
            outcome = chatgpt_limiter.refresh_team_auth_sync(
                TEAM_ID, trigger=trigger, force=force
            )
        diag_lines = [m for m in captured.output if "auth_refresh_diag" in m]
        self.assertEqual(len(diag_lines), 1, captured.output)
        payload = json.loads(diag_lines[0].split("auth_refresh_diag ", 1)[1])
        return outcome, payload, "\n".join(captured.output), file_update

    def _assert_no_secret(self, text: str, secrets_: list[str]) -> None:
        for value in secrets_ + [OWNER_EMAIL, OWNER_USER_ID, "cfvalue123"]:
            window = 16
            for start in range(0, max(1, len(value) - window + 1)):
                chunk = value[start:start + window]
                self.assertNotIn(chunk, text, f"raw secret fragment leaked: {chunk!r}")

    def test_success_logs_one_line_and_detail_without_raw_secrets(self):
        json_session = _secret()
        part_a, part_b = _secret(), _secret()
        new_access = _access_token(timedelta(days=10), finalizer=True)
        response = _session_response(
            200,
            _session_body(new_access, json_session),
            [
                f"{COOKIE}.0={part_a}; Path=/; HttpOnly; Secure",
                f"{COOKIE}.1={part_b}; Path=/; HttpOnly; Secure",
                "__cf_bm=cfvalue123; Path=/",
            ],
        )

        outcome, payload, log_text, file_update = self._run(response)

        self.assertEqual(outcome.status, "refreshed")
        self.assertEqual(payload["team"], TEAM_ID[:8])
        self.assertEqual(payload["trigger"], "scheduled_expiry_refresh")
        self.assertEqual(payload["result"], "success")
        self.assertEqual(payload["http_status"], 200)
        self.assertIn("sessionToken", payload["json_keys"])
        self.assertIsNone(payload["error"])
        self.assertTrue(payload["access_present"])
        self.assertTrue(payload["access_changed"])
        self.assertTrue(payload["access_finalizer_claim"])
        self.assertTrue(payload["cur_finalizer_claim"])
        self.assertIsNotNone(payload["access_iat"])
        self.assertIsNotNone(payload["access_exp"])
        self.assertIsNotNone(payload["cur_access_exp"])
        self.assertEqual(payload["set_cookie_names"], [f"{COOKIE}.0", f"{COOKIE}.1", "__cf_bm"])
        self.assertEqual(payload["sc_session_sha8"], _sha8(part_a + part_b))
        self.assertEqual(payload["json_session_sha8"], _sha8(json_session))
        self.assertEqual(payload["sent_session_sha8"], _sha8(self.sent_session))
        self.assertFalse(payload["sc_eq_json"])
        self.assertTrue(payload["session_changed"])

        logs = self._refresh_logs()
        self.assertEqual(len(logs), 1)
        action, result, detail, error_message = logs[0]
        self.assertEqual((action, result, error_message), ("token_proactive_refresh", "success", None))
        self.assertIn("access_changed=1", detail)
        self.assertIn("diag: http=200", detail)
        self.assertIn(f"sc_session=chunks2:{len(part_a + part_b)}:{_sha8(part_a + part_b)}", detail)
        self.assertIn(f"json_session={len(json_session)}:{_sha8(json_session)}", detail)
        self.assertIn(f"sent_session={len(self.sent_session)}:{_sha8(self.sent_session)}", detail)
        self.assertIn("sc_eq_json=0", detail)
        self.assertIn("fin=1", detail)

        all_output = log_text + "\n" + "\n".join(str(v) for row in logs for v in row)
        self._assert_no_secret(
            all_output,
            [self.sent_session, json_session, part_a, part_b, part_a + part_b, new_access, self.old_access],
        )

        # Persisted token is still the JSON one, not the Set-Cookie one.
        access, session, status, auth_state, _ = self._team()
        self.assertEqual((access, session, status, auth_state), (new_access, json_session, "active", "ok"))
        file_update.assert_called_once_with(TEAM_ID, new_access, json_session)

    def test_refresh_access_token_error_logs_without_raw_secrets(self):
        part = _secret()
        response = _session_response(
            200,
            {"error": "RefreshAccessTokenError"},
            [f"{COOKIE}={part}; Path=/; HttpOnly; Secure"],
        )

        outcome, payload, log_text, _ = self._run(response)

        # 037d086 classification is unchanged: expired access + in-band error => rejected.
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.auth_rejected)
        self.assertEqual(self._team()[2:4], ("active", "rejected"))

        self.assertEqual(payload["result"], "failed")
        self.assertEqual(payload["error"], "RefreshAccessTokenError")
        self.assertEqual(payload["json_keys"], ["error"])
        self.assertFalse(payload["access_present"])
        self.assertFalse(payload["access_changed"])
        self.assertTrue(payload["auth_rejected"])
        self.assertFalse(payload["marked_token_expired"])
        self.assertEqual(payload["sc_session_source"], "single")
        self.assertEqual(payload["sc_session_sha8"], _sha8(part))
        self.assertFalse(payload["sc_eq_sent"])

        (action, result, detail, error_message), = self._refresh_logs()
        self.assertEqual((action, result, error_message), ("token_proactive_refresh", "failed", "RefreshAccessTokenError"))
        self.assertTrue(detail.startswith("trigger=scheduled_expiry_refresh; access_changed=0; diag: http=200"))
        self.assertIn("err=RefreshAccessTokenError", detail)
        self.assertIn(f"set_cookies={COOKIE}", detail)

        self._assert_no_secret(
            log_text + "\n" + detail,
            [self.sent_session, part, self.old_access],
        )

    def test_session_file_failure_is_logged_on_the_same_connection(self):
        new_access = _access_token(timedelta(days=10), finalizer=False)
        response = _session_response(200, _session_body(new_access, _secret()))

        outcome, _, _, _ = self._run(
            response, file_update=Mock(side_effect=OSError("disk full"))
        )

        self.assertEqual(outcome.status, "refreshed")
        logs = self._refresh_logs()
        self.assertEqual(
            [(action, result, error) for action, result, _, error in logs],
            [
                ("token_proactive_refresh", "success", None),
                ("session_file_update", "failed", "disk full"),
            ],
        )


class ReimportDuringRefreshTest(_TempDbMixin, unittest.TestCase):
    """The operator re-imports while a refresh of the old session is in flight."""

    def setUp(self):
        self.old_access = _access_token(timedelta(hours=-2), finalizer=True)
        self._setup_db(self.old_access, "old-session")
        self.imported_access = _access_token(timedelta(days=10), finalizer=True)
        self.imported_session = "imported-session"

    def _reimport(self) -> None:
        """The real re-import write path, upstream calls faked."""

        async def fake_run(func, *args, **kwargs):
            if getattr(func, "__name__", "") == "get_account_info":
                return {"accounts": {TEAM_ID: {"account": {"name": "diag"}}}}
            return {"error": "not needed for this test"}

        session = TeamSession(
            user={"email": OWNER_EMAIL},
            expires="2026-12-01T00:00:00Z",
            account={"id": TEAM_ID},
            accessToken=self.imported_access,
            sessionToken=self.imported_session,
        )
        with (
            patch.object(team_service, "run_chatgpt_call", new=fake_run),
            patch.object(team_service, "fetch_seat_pricing", new=AsyncMock(return_value={})),
            patch.object(team_service, "write_session_file"),
        ):
            asyncio.run(
                team_service.upsert_team_from_session(
                    session, log_action="reimport_team", expected_team_id=TEAM_ID
                )
            )

    def _in_flight(self, result: dict):
        """refresh_token stand-in: the import lands while the request is out."""

        def fake_refresh(session_token, proxy_url=None):
            self.assertEqual(session_token, "old-session")
            self._reimport()
            return dict(result)

        return fake_refresh

    def _refresh(self, result: dict, *, trigger: str):
        file_update = Mock(return_value=True)
        with (
            patch.object(ChatGPTClient, "refresh_token", staticmethod(self._in_flight(result))),
            patch.object(chatgpt_limiter, "update_session_file_tokens", file_update),
            self.assertLogs("app.chatgpt_limiter", level="WARNING") as captured,
        ):
            outcome = chatgpt_limiter.refresh_team_auth_sync(TEAM_ID, trigger=trigger, force=True)
        return outcome, file_update, captured.output

    def _assert_import_intact(self) -> None:
        self.assertEqual(
            self._team(),
            (self.imported_access, self.imported_session, "active", "ok", None),
        )

    def _last_refresh_log(self) -> tuple:
        rows = [r for r in self._refresh_logs() if r[0].startswith("token_") or r[0] == "refresh_token"]
        return rows[-1]

    def test_new_tokens_from_the_old_session_do_not_overwrite_the_import(self):
        outcome, file_update, output = self._refresh(
            {"accessToken": "old-session-new-access", "sessionToken": "old-session-rotated"},
            trigger="scheduled_expiry_refresh",
        )

        self.assertEqual(outcome.status, "superseded")
        self.assertFalse(outcome.auth_rejected)
        self._assert_import_intact()
        file_update.assert_not_called()
        action, result, detail, error = self._last_refresh_log()
        self.assertEqual((action, result, error), ("token_proactive_refresh", "superseded", None))
        self.assertIn("superseded=1", detail)
        self.assertTrue(any('"result":"superseded"' in line for line in output))

    def test_stale_refresh_access_token_error_leaves_auth_state_untouched(self):
        outcome, _, _ = self._refresh(
            {"error": "RefreshAccessTokenError", "status_code": 200},
            trigger="api_401_retry",
        )

        self.assertEqual(outcome.status, "superseded")
        self.assertFalse(outcome.auth_rejected)
        self._assert_import_intact()

    def test_stale_session_401_does_not_mark_token_expired(self):
        outcome, _, _ = self._refresh(
            {"error": "401 Client Error: Unauthorized", "status_code": 401},
            trigger="api_401_retry",
        )

        self.assertEqual(outcome.status, "superseded")
        self._assert_import_intact()

    def test_stale_same_token_answer_does_not_mark_rejected(self):
        outcome, _, _ = self._refresh(
            {"accessToken": self.old_access, "sessionToken": "old-session"},
            trigger="api_401_retry",
        )

        self.assertEqual(outcome.status, "superseded")
        self._assert_import_intact()

    def test_team_deleted_mid_refresh_is_not_found_not_retried(self):
        def delete_team(session_token, proxy_url=None):
            conn = sqlite3.connect(self.db_path)
            conn.execute("DELETE FROM teams WHERE id = ?", (TEAM_ID,))
            conn.commit()
            conn.close()
            return {"error": "RefreshAccessTokenError", "status_code": 200}

        with (
            patch.object(ChatGPTClient, "refresh_token", staticmethod(delete_team)),
            patch.object(chatgpt_limiter, "update_session_file_tokens", Mock(return_value=True)),
        ):
            outcome = chatgpt_limiter.refresh_team_auth_sync(
                TEAM_ID, trigger="api_401_retry", force=True
            )

        self.assertEqual(outcome.status, "not_found")
        self.assertFalse(outcome.retry_original)

    def test_superseded_is_not_a_failure_and_starts_no_cooldown(self):
        self._refresh(
            {"error": "RefreshAccessTokenError", "status_code": 200},
            trigger="api_401_retry",
        )
        fresh = _access_token(timedelta(days=12), finalizer=True)
        refresh = Mock(return_value={"accessToken": fresh, "sessionToken": "next-session"})

        with (
            patch.object(ChatGPTClient, "refresh_token", refresh),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
            self.assertLogs("app.chatgpt_limiter", level="WARNING"),
        ):
            outcome = chatgpt_limiter.refresh_team_auth_sync(TEAM_ID, trigger="api_401_retry")

        refresh.assert_called_once_with(self.imported_session, None)
        self.assertEqual(outcome.status, "refreshed")

    def _client_accepting_only_the_import(self, access_token: str):
        imported_access = self.imported_access

        class Client(ChatGPTClient):
            calls = 0

            def get_subscription(self):
                Client.calls += 1
                if self.access_token == imported_access:
                    return {"ok": True}
                return {"error": "401 Client Error: Unauthorized", "status_code": 401}

        return Client(access_token, TEAM_ID, "device-1")

    def _call_with_in_flight_import(self, client):
        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                staticmethod(self._in_flight({"error": "RefreshAccessTokenError", "status_code": 200})),
            ),
            patch.object(chatgpt_limiter, "update_session_file_tokens", return_value=True),
            patch.object(chatgpt_limiter, "report_team_failure_sync") as report_failure,
            patch.object(chatgpt_limiter, "report_team_recovery_sync") as report_recovery,
            self.assertLogs("app.chatgpt_limiter", level="WARNING"),
        ):
            result = chatgpt_limiter.run_chatgpt_call_sync(client.get_subscription)
        return result, report_failure, report_recovery

    def test_proactive_path_switches_to_the_imported_token(self):
        # Stored token already expired, so the proactive refresh is the one in flight.
        client = self._client_accepting_only_the_import(self.old_access)

        result, report_failure, _ = self._call_with_in_flight_import(client)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.access_token, self.imported_access)
        report_failure.assert_not_called()
        self._assert_import_intact()

    def test_401_retry_path_retries_with_the_imported_token(self):
        # Not yet expired: no proactive refresh, the 401 triggers the in-flight refresh.
        live_old_access = _access_token(timedelta(days=5), finalizer=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE teams SET access_token = ? WHERE id = ?", (live_old_access, TEAM_ID))
        conn.commit()
        conn.close()
        client = self._client_accepting_only_the_import(live_old_access)

        result, report_failure, report_recovery = self._call_with_in_flight_import(client)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(type(client).calls, 2)
        self.assertEqual(client.access_token, self.imported_access)
        report_failure.assert_not_called()
        report_recovery.assert_called_once_with(TEAM_ID, "chatgpt_auth", source="api_401_retry")
        self._assert_import_intact()

    def test_normal_refresh_without_reimport_is_unchanged(self):
        new_access = _access_token(timedelta(days=10), finalizer=True)
        file_update = Mock(return_value=True)
        with (
            patch.object(
                ChatGPTClient,
                "refresh_token",
                return_value={"accessToken": new_access, "sessionToken": "rotated"},
            ),
            patch.object(chatgpt_limiter, "update_session_file_tokens", file_update),
            self.assertLogs("app.chatgpt_limiter", level="WARNING"),
        ):
            outcome = chatgpt_limiter.refresh_team_auth_sync(
                TEAM_ID, trigger="api_401_retry", force=True
            )

        self.assertEqual(outcome.status, "refreshed")
        self.assertEqual(self._team(), (new_access, "rotated", "active", "ok", None))
        file_update.assert_called_once_with(TEAM_ID, new_access, "rotated")
        action, result, detail, _ = self._last_refresh_log()
        self.assertEqual((action, result), ("token_auto_refresh", "success"))
        self.assertIn("diag: missing=1", detail)


if __name__ == "__main__":
    unittest.main()
