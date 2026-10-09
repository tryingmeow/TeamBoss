import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _request_fixtures import FakeRequest

from app import client_ip
from app.client_ip import rate_limit_key


class GetClientIpInfoTest(unittest.TestCase):
    def test_untrusted_peer_uses_direct_connection_and_is_never_self_identity(self):
        # A public IP directly hitting the backend (no reverse proxy in front, or a
        # proxy outside AUTO_TEAM_TRUSTED_PROXIES) must never be treated as a proxy's
        # own identity, no matter what headers it sends.
        request = FakeRequest("203.0.113.7", {"X-Real-IP": "203.0.113.7"})
        ip, is_self = client_ip.get_client_ip_info(request)
        self.assertEqual(ip, "203.0.113.7")
        self.assertFalse(is_self)

    def test_trusted_proxy_with_distinct_real_ip_is_not_self_identity(self):
        # Correctly configured deployment: the trusted proxy (bridge gateway) forwards
        # a real, distinct visitor IP via X-Real-IP. This must never be flagged as a
        # collapsed identity.
        request = FakeRequest("172.18.0.1", {"X-Real-IP": "198.51.100.23"})
        ip, is_self = client_ip.get_client_ip_info(request)
        self.assertEqual(ip, "198.51.100.23")
        self.assertFalse(is_self)

    def test_trusted_proxy_whose_header_equals_its_own_address_is_self_identity(self):
        # This is the collapsed-identity bug: docker-proxy makes $remote_addr equal the
        # bridge gateway for every visitor, nginx copies that into X-Real-IP, and the
        # backend (correctly) trusts it because the peer is in the default trusted
        # range. The resolved "visitor" IP is then indistinguishable from the proxy's
        # own address.
        request = FakeRequest("172.18.0.1", {"X-Real-IP": "172.18.0.1"})
        ip, is_self = client_ip.get_client_ip_info(request)
        self.assertEqual(ip, "172.18.0.1")
        self.assertTrue(is_self)

    def test_trusted_proxy_with_no_forwarding_headers_falls_back_to_peer_and_is_self_identity(self):
        request = FakeRequest("127.0.0.1", {})
        ip, is_self = client_ip.get_client_ip_info(request)
        self.assertEqual(ip, "127.0.0.1")
        self.assertTrue(is_self)

    def test_get_client_ip_still_returns_plain_string(self):
        request = FakeRequest("198.51.100.5", {"X-Real-IP": "198.51.100.5"})
        # 198.51.100.5 is not a trusted proxy by default, so this is just the direct peer.
        self.assertEqual(client_ip.get_client_ip(request), "198.51.100.5")


class CollapseDetectionWarningTest(unittest.TestCase):
    def setUp(self):
        # These counters are module-level singletons; reset them so this test's
        # outcome doesn't depend on what ran before it in the same process.
        client_ip._collapse_samples = 0
        client_ip._collapse_warned = False

    def tearDown(self):
        client_ip._collapse_samples = 0
        client_ip._collapse_warned = False

    def test_warns_once_after_threshold_consecutive_collapsed_samples(self):
        with self.assertLogs(client_ip.logger, level="WARNING") as captured:
            for _ in range(client_ip._COLLAPSE_SAMPLE_THRESHOLD):
                request = FakeRequest("172.18.0.1", {"X-Real-IP": "172.18.0.1"})
                client_ip.get_client_ip_info(request)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("身份坍缩", captured.records[0].getMessage())
        self.assertTrue(client_ip._collapse_warned)

        # Further collapsed samples must not warn again (idempotent, not a log spam
        # source).
        with self.assertRaises(AssertionError):
            with self.assertLogs(client_ip.logger, level="WARNING"):
                request = FakeRequest("172.18.0.1", {"X-Real-IP": "172.18.0.1"})
                client_ip.get_client_ip_info(request)

    def test_a_single_non_collapsed_sample_resets_the_streak(self):
        for _ in range(client_ip._COLLAPSE_SAMPLE_THRESHOLD - 1):
            request = FakeRequest("172.18.0.1", {"X-Real-IP": "172.18.0.1"})
            client_ip.get_client_ip_info(request)
        self.assertFalse(client_ip._collapse_warned)

        # A real, distinct visitor IP breaks the streak (correctly configured
        # deployments must never trip the detector just because it once matched).
        request = FakeRequest("172.18.0.1", {"X-Real-IP": "198.51.100.9"})
        client_ip.get_client_ip_info(request)
        self.assertEqual(client_ip._collapse_samples, 0)
        self.assertFalse(client_ip._collapse_warned)


class ComposeTopologyCollapseTest(unittest.TestCase):
    """compose 的真实拓扑：后端直连来源是 nginx 容器，头里是网桥网关。

    两者不相等，所以"解析出的 IP 等于代理自身地址"这一条在这里永远不成立——
    而这恰恰是身份坍缩最常发生的部署。判据必须认"访客地址落在可信代理网段"。
    """

    def test_bridge_gateway_in_header_is_collapsed(self):
        # 第一次请求就必须判成坍缩：攒样本期间锁定仍然生效，而攻击者只需要 5 个请求。
        ip, collapsed = client_ip.get_client_ip_info(
            FakeRequest("172.18.0.3", {"X-Real-IP": "172.18.0.1"})
        )
        self.assertEqual(ip, "172.18.0.1")
        self.assertTrue(collapsed)

    def test_real_public_visitor_behind_the_same_proxy_is_not_collapsed(self):
        ip, collapsed = client_ip.get_client_ip_info(
            FakeRequest("172.18.0.3", {"X-Real-IP": "203.0.113.9"})
        )
        self.assertEqual(ip, "203.0.113.9")
        self.assertFalse(collapsed)


class ProductionNginxHeadersTest(unittest.IsolatedAsyncioTestCase):
    """Production: stream :443 (proxy_protocol) -> http 127.0.0.1:8443 with
    real_ip_header proxy_protocol -> proxy_pass 127.0.0.1:18087 with
    X-Real-IP $remote_addr, X-Forwarded-For $proxy_add_x_forwarded_for,
    X-Forwarded-Proto https. The backend used to run with uvicorn
    proxy_headers=True (XFF rewrite of request.client); it now relies on
    client_ip.py alone. The attributed client IP must be identical.
    """

    CASES = [
        # (real visitor = nginx $remote_addr, X-Forwarded-For the visitor sent itself)
        ("198.51.100.77", None),
        ("198.51.100.78", "1.2.3.4"),
        ("2001:db8:1:2::abcd", None),
        ("2001:db8:1:2::abce", "9.9.9.9, 2001:db8:ffff::1"),
        ("203.0.113.5", "203.0.113.250"),
    ]

    @staticmethod
    def _scope(remote_addr: str, client_xff):
        xff = f"{client_xff}, {remote_addr}" if client_xff else remote_addr
        return {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/admin/login",
            "raw_path": b"/api/admin/login",
            "query_string": b"",
            "root_path": "",
            "server": ("127.0.0.1", 18087),
            "client": ("127.0.0.1", 50123),
            "headers": [
                (b"host", b"business.example"),
                (b"x-real-ip", remote_addr.encode()),
                (b"x-forwarded-for", xff.encode()),
                (b"x-forwarded-proto", b"https"),
            ],
        }

    async def _attributed_ip(self, scope, *, uvicorn_proxy_headers: bool) -> str:
        from starlette.requests import Request
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        seen = {}

        async def app(scope, receive, send):
            seen["ip"] = client_ip.get_client_ip(Request(scope))

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        entry = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1") if uvicorn_proxy_headers else app
        await entry(dict(scope), receive, send)
        return seen["ip"]

    async def test_same_client_ip_with_and_without_uvicorn_proxy_headers(self):
        for remote_addr, client_xff in self.CASES:
            with self.subTest(remote_addr=remote_addr, client_xff=client_xff):
                scope = self._scope(remote_addr, client_xff)
                before = await self._attributed_ip(scope, uvicorn_proxy_headers=True)
                after = await self._attributed_ip(scope, uvicorn_proxy_headers=False)
                self.assertEqual(before, client_ip.normalize_ip(remote_addr))
                self.assertEqual(after, before)

    def test_run_server_disables_uvicorn_proxy_headers(self):
        source = (Path(__file__).resolve().parents[1] / "run_server.py").read_text(encoding="utf-8")
        self.assertIn("proxy_headers=False", source)
        self.assertNotIn("proxy_headers=True", source)


class RateLimitKeyTest(unittest.TestCase):
    def test_ipv6_addresses_share_their_slash_64(self):
        self.assertEqual(rate_limit_key("2001:db8:1:2::1"), "2001:db8:1:2::/64")
        self.assertEqual(rate_limit_key("2001:db8:1:2:ffff:ffff:ffff:ffff"), "2001:db8:1:2::/64")
        self.assertNotEqual(rate_limit_key("2001:db8:1:3::1"), "2001:db8:1:2::/64")
        self.assertEqual(rate_limit_key("198.51.100.9"), "198.51.100.9")
        self.assertEqual(rate_limit_key("unknown"), "unknown")


if __name__ == "__main__":
    unittest.main()
