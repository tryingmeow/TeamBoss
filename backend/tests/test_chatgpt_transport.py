"""ChatGPT transport: curl_cffi with browser impersonation, outcome classification unchanged.

chatgpt.com's Cloudflare edge answers python-requests' TLS fingerprint with an HTML
403 on the admin/billing endpoints, so ChatGPTClient talks through curl_cffi. The
money path (redeem, patrol kicks, pending-invite sync) classifies a call from the
result dict alone: an upstream HTTP status lands in ``status_code``; a transport
failure (timeout, reset, DNS, proxy, TLS, HTTP/2 stream error) has no
``status_code`` and an invite stays "uncertain". curl_cffi attaches a half-read
response to transport exceptions, and these tests pin that it never leaks into
``status_code``.
"""

import _isolation  # noqa: F401  must precede any app import
import json
import socketserver
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlECode
from curl_cffi.requests import Response
from curl_cffi.requests import exceptions as cx
from curl_cffi.requests.exceptions import Timeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import chatgpt_client as chatgpt_client_module
from app.chatgpt_client import IMPERSONATE, ChatGPTClient
from app.chatgpt_limiter import _is_unauthorized_result

URL = "https://chatgpt.com/backend-api/accounts/team-1/invites"


def _response(status: int, body: bytes = b"{}", reason: str = "") -> Response:
    """A curl_cffi Response as Session._parse_response fills it (HTTP/2: no reason)."""
    response = Response()
    response.url = URL
    response.status_code = status
    response.ok = 200 <= status < 400
    response.reason = reason
    response.content = body
    return response


def _transport_failures() -> dict[str, Exception]:
    """Every curl_cffi transport exception, each carrying the partial response curl attaches."""
    return {
        "connect timeout": cx.Timeout("curl: (28) timed out", CurlECode.OPERATION_TIMEDOUT, _response(0, b"")),
        "read timeout after 400 headers": cx.Timeout(
            "curl: (28) timed out", CurlECode.OPERATION_TIMEDOUT, _response(400, b"")
        ),
        "connection refused": cx.ConnectionError("curl: (7)", CurlECode.COULDNT_CONNECT, _response(0, b"")),
        "dns": cx.DNSError("curl: (6)", CurlECode.COULDNT_RESOLVE_HOST, _response(0, b"")),
        "proxy": cx.ProxyError("curl: (97)", CurlECode.PROXY, _response(0, b"")),
        "tls": cx.SSLError("curl: (35)", CurlECode.SSL_CONNECT_ERROR, _response(0, b"")),
        "reset after 403 headers": cx.ConnectionError("curl: (56)", CurlECode.RECV_ERROR, _response(403, b"")),
        "empty reply": cx.ConnectionError("curl: (52)", CurlECode.GOT_NOTHING, _response(0, b"")),
        # curl_cffi maps HTTP/2 stream errors and truncated bodies to HTTPError
        # subclasses; they are transport failures, not upstream answers.
        "http2 stream error after 400 headers": cx.HTTPError(
            "curl: (92)", CurlECode.HTTP2_STREAM, _response(400, b"")
        ),
        "truncated body after 404 headers": cx.IncompleteRead(
            "curl: (18)", CurlECode.PARTIAL_FILE, _response(404, b"")
        ),
        "too many redirects": cx.TooManyRedirects("curl: (47)", CurlECode.TOO_MANY_REDIRECTS, _response(302, b"")),
        "bare request exception": cx.RequestException("boom"),
    }


class SessionSetupTest(unittest.TestCase):
    def test_session_impersonates_browser_and_keeps_proxy_and_headers(self):
        proxy = "http://user:pass@127.0.0.1:9"
        client = ChatGPTClient("access-1", "team-1", "device-1", proxy)

        self.assertIsInstance(client.session, curl_requests.Session)
        self.assertEqual(client.session.impersonate, IMPERSONATE)
        self.assertEqual(client.session.proxies, {"http": proxy, "https": proxy})
        headers = client.session.headers
        self.assertEqual(headers["authorization"], "Bearer access-1")
        self.assertEqual(headers["chatgpt-account-id"], "team-1")
        self.assertEqual(headers["oai-device-id"], "device-1")
        self.assertIn("Chrome/", headers["user-agent"])

        client.update_access_token("access-2")
        self.assertEqual(client.session.headers["authorization"], "Bearer access-2")

    def test_no_proxy_means_no_proxies(self):
        client = ChatGPTClient("access-1", "team-1", "device-1")
        self.assertFalse(client.session.proxies)

    def test_refresh_token_impersonates_with_proxy_and_timeout(self):
        proxy = "http://user:pass@127.0.0.1:9"
        body = json.dumps({"accessToken": "a", "sessionToken": "s"}).encode()
        with patch.object(chatgpt_client_module.curl_requests, "get", return_value=_response(200, body)) as get:
            ChatGPTClient.refresh_token("session-1", proxy)
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["impersonate"], IMPERSONATE)
        self.assertEqual(kwargs["proxies"], {"http": proxy, "https": proxy})
        self.assertEqual(kwargs["timeout"], 60)


class RefreshTokenClientShapeTest(unittest.TestCase):
    """refresh_token 必须让调用方分得清"上游明确答复"和"这次没问成"。"""

    @staticmethod
    def _http_response(status_code: int, body=None):
        response = Response()
        response.status_code = status_code
        response.ok = 200 <= status_code < 400
        response.url = "https://chatgpt.com/api/auth/session"
        response.reason = "test"
        response.content = json.dumps(body if body is not None else {}).encode()
        return response

    def _call(self, **get_kwargs):
        with patch.object(chatgpt_client_module.curl_requests, "get", **get_kwargs) as get:
            result = ChatGPTClient.refresh_token("session-1")
        get.assert_called_once()
        return result

    # refresh_token 只追加诊断字段，原有字段一个不少、值不变。
    ADDED_KEYS = {"refresh_diagnostics", "set_cookie_session_token"}

    def _without_added(self, result: dict) -> dict:
        self.assertTrue(self.ADDED_KEYS <= set(result))
        return {k: v for k, v in result.items() if k not in self.ADDED_KEYS}

    def test_in_band_refresh_error_carries_its_2xx_status(self):
        result = self._call(
            return_value=self._http_response(200, {"error": "RefreshAccessTokenError"})
        )
        self.assertEqual(
            self._without_added(result),
            {"error": "RefreshAccessTokenError", "status_code": 200},
        )

    def test_successful_session_body_is_returned_unchanged(self):
        body = {"accessToken": "a", "sessionToken": "s"}
        result = self._call(return_value=self._http_response(200, dict(body)))
        self.assertEqual(self._without_added(result), body)

    def test_timeout_has_no_status_code(self):
        # curl_cffi 超时时会把收了一半的响应挂在异常上；那不是上游答复。
        partial = Response()
        partial.status_code = 401
        timeout = Timeout(
            "curl: (28) Operation timed out", CurlECode.OPERATION_TIMEDOUT, partial
        )
        result = self._call(side_effect=timeout)
        self.assertIn("error", result)
        self.assertNotIn("status_code", result)

    def test_http_errors_keep_their_own_status(self):
        for status_code in (401, 403, 429, 503):
            with self.subTest(status_code=status_code):
                result = self._call(return_value=self._http_response(status_code))
                self.assertEqual(result["status_code"], status_code)


class InviteClassificationTest(unittest.TestCase):
    """invite_member's confirmed / rejected / uncertain, the redeem money path."""

    def setUp(self):
        self.client = ChatGPTClient("access", "team-1", "device-1")

    def _invite_with(self, **post_kwargs) -> dict:
        self.client.session.post = Mock(**post_kwargs)
        return self.client.invite_member("user@example.com")

    def test_transport_failures_are_uncertain_without_status(self):
        for name, exc in _transport_failures().items():
            with self.subTest(failure=name):
                result = self._invite_with(side_effect=exc)
                self.assertEqual(result["_mutation_status"], "uncertain")
                self.assertNotIn("status_code", result)

    def test_clear_client_rejections(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                result = self._invite_with(return_value=_response(status, b"<html>blocked</html>"))
                self.assertEqual(result["_mutation_status"], "rejected")
                self.assertEqual(result["status_code"], status)

    def test_statuses_that_may_have_executed_stay_uncertain(self):
        for status in (408, 409, 425, 429, 500, 502, 503, 525):
            with self.subTest(status=status):
                result = self._invite_with(return_value=_response(status))
                self.assertEqual(result["_mutation_status"], "uncertain")
                self.assertEqual(result["status_code"], status)

    def test_2xx_without_per_email_result_is_confirmed_even_with_bad_body(self):
        for body in (b"", b"not json", b"{}", b'{"account_invites": [{"email_address": "user@example.com"}]}'):
            with self.subTest(body=body):
                result = self._invite_with(return_value=_response(200, body))
                self.assertEqual(result["_mutation_status"], "confirmed")
                self.assertNotIn("error", result)

    def test_2xx_whose_account_invites_omits_our_email_is_uncertain(self):
        # A 2xx may have reached OpenAI, so it is never "rejected"; but upstream did
        # not say it invited this address either (see classify_invite_response).
        for body in (b'{"account_invites": []}', b'{"account_invites": [{"email_address": "other@example.com"}]}'):
            with self.subTest(body=body):
                result = self._invite_with(return_value=_response(200, body))
                self.assertEqual(result["_mutation_status"], "uncertain")
                self.assertNotIn("status_code", result)
                self.assertIn("error", result)


class ReadAndKickShapeTest(unittest.TestCase):
    """GETs and the kick/revoke calls keep the {error, status_code?} shape."""

    def setUp(self):
        self.client = ChatGPTClient("access", "team-1", "device-1")

    def test_cloudflare_html_403_is_an_http_error_with_status(self):
        self.client.session.get = Mock(return_value=_response(403, b"<html>Unable to load site</html>"))
        result = self.client.get_seat_type_counts()
        self.assertEqual(result["status_code"], 403)
        self.assertTrue(result["error"].startswith("403 Client Error: Forbidden for url: https://chatgpt.com/"))

    def test_success_returns_parsed_json(self):
        self.client.session.get = Mock(return_value=_response(200, b'{"items": [], "total": 0}'))
        self.assertEqual(self.client.get_pending_invites(), {"items": [], "total": 0})

    def test_transport_failures_have_no_status_on_every_call(self):
        calls = {
            "get_subscription": lambda: self.client.get_subscription(),
            "get_seat_type_counts": lambda: self.client.get_seat_type_counts(),
            "get_members": lambda: self.client.get_members(),
            "get_pending_invites": lambda: self.client.get_pending_invites(),
            "remove_member": lambda: self.client.remove_member("user-1"),
            "revoke_invite": lambda: self.client.revoke_invite("user@example.com"),
        }
        for name, exc in _transport_failures().items():
            for method in ("get", "post", "patch", "delete"):
                setattr(self.client.session, method, Mock(side_effect=exc))
            for call_name, call in calls.items():
                with self.subTest(failure=name, call=call_name):
                    result = call()
                    self.assertIn("error", result)
                    self.assertNotIn("status_code", result)

    def test_kick_rejection_keeps_status(self):
        self.client.session.delete = Mock(return_value=_response(404))
        result = self.client.remove_member("user-1")
        self.assertEqual(result["status_code"], 404)

    def test_error_text_keeps_requests_format(self):
        cases = {
            (401, ""): "401 Client Error: Unauthorized for url: ",
            (401, "Unauthorized"): "401 Client Error: Unauthorized for url: ",
            (503, ""): "503 Server Error: Service Unavailable for url: ",
            (525, ""): "525 Server Error:  for url: ",
        }
        for (status, reason), prefix in cases.items():
            with self.subTest(status=status, reason=reason):
                self.client.session.get = Mock(return_value=_response(status, reason=reason))
                result = self.client.get_members()
                self.assertEqual(result["error"], prefix + URL)

    def test_401_is_still_recognised_as_unauthorized(self):
        self.client.session.get = Mock(return_value=_response(401))
        result = self.client.get_members()
        self.assertTrue(_is_unauthorized_result(result))
        # Text-only fallback (status_code absent) still matches as before.
        self.assertTrue(_is_unauthorized_result({"error": result["error"]}))


class _ProxyHandler(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Proxy-Authorization")))
        payload = b'{"seat_counts": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class _StallAfterHeaders(socketserver.BaseRequestHandler):
    """Sends a 400 status line and headers, then never sends the promised body."""

    def handle(self):
        self.request.recv(65536)
        self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 100\r\n\r\n")
        self.server.release.wait(10)


class LocalSocketTest(unittest.TestCase):
    """Real curl transfers against 127.0.0.1 only; nothing leaves the box."""

    def _serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_requests_go_through_the_configured_authenticated_proxy(self):
        _ProxyHandler.seen = []
        port = self._serve(HTTPServer(("127.0.0.1", 0), _ProxyHandler))
        client = ChatGPTClient("access", "team-1", "device-1", f"http://user:pass@127.0.0.1:{port}")
        client.base_url = "http://chatgpt.invalid"

        result = client.get_seat_type_counts()

        self.assertEqual(result, {"seat_counts": []})
        self.assertEqual(len(_ProxyHandler.seen), 1)
        path, proxy_auth = _ProxyHandler.seen[0]
        self.assertEqual(path, "http://chatgpt.invalid/backend-api/accounts/team-1/users/seat_type_counts")
        self.assertTrue(proxy_auth and proxy_auth.startswith("Basic "))

    def test_real_timeout_after_4xx_headers_stays_uncertain(self):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _StallAfterHeaders)
        server.daemon_threads = True
        server.release = threading.Event()
        self.addCleanup(server.release.set)
        port = self._serve(server)
        client = ChatGPTClient("access", "team-1", "device-1")
        client.base_url = f"http://127.0.0.1:{port}"
        real_post = client.session.post
        client.session.post = lambda *a, **kw: real_post(*a, **{**kw, "timeout": 1})

        result = client.invite_member("user@example.com")

        self.assertEqual(result["_mutation_status"], "uncertain")
        self.assertNotIn("status_code", result)


if __name__ == "__main__":
    unittest.main()
