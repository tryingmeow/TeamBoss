import _isolation  # noqa: F401  must precede any app import
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import client_ip


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    def __init__(self, peer: str, headers: dict | None = None):
        self.client = _FakeClient(peer)
        self.headers = headers or {}


class GetClientIpInfoTest(unittest.TestCase):
    def test_untrusted_peer_uses_direct_connection_and_is_never_self_identity(self):
        # A public IP directly hitting the backend (no reverse proxy in front, or a
        # proxy outside AUTO_TEAM_TRUSTED_PROXIES) must never be treated as a proxy's
        # own identity, no matter what headers it sends.
        request = _FakeRequest("203.0.113.7", {"X-Real-IP": "203.0.113.7"})
        ip, is_self = client_ip.get_client_ip_info(request)
        self.assertEqual(ip, "203.0.113.7")
        self.assertFalse(is_self)

    def test_trusted_proxy_with_distinct_real_ip_is_not_self_identity(self):
        # Correctly configured deployment: the trusted proxy (bridge gateway) forwards
        # a real, distinct visitor IP via X-Real-IP. This must never be flagged as a
        # collapsed identity.
        request = _FakeRequest("172.18.0.1", {"X-Real-IP": "198.51.100.23"})
        ip, is_self = client_ip.get_client_ip_info(request)
        self.assertEqual(ip, "198.51.100.23")
        self.assertFalse(is_self)

    def test_trusted_proxy_whose_header_equals_its_own_address_is_self_identity(self):
        # This is the collapsed-identity bug: docker-proxy makes $remote_addr equal the
        # bridge gateway for every visitor, nginx copies that into X-Real-IP, and the
        # backend (correctly) trusts it because the peer is in the default trusted
        # range. The resolved "visitor" IP is then indistinguishable from the proxy's
        # own address.
        request = _FakeRequest("172.18.0.1", {"X-Real-IP": "172.18.0.1"})
        ip, is_self = client_ip.get_client_ip_info(request)
        self.assertEqual(ip, "172.18.0.1")
        self.assertTrue(is_self)

    def test_trusted_proxy_with_no_forwarding_headers_falls_back_to_peer_and_is_self_identity(self):
        request = _FakeRequest("127.0.0.1", {})
        ip, is_self = client_ip.get_client_ip_info(request)
        self.assertEqual(ip, "127.0.0.1")
        self.assertTrue(is_self)

    def test_get_client_ip_still_returns_plain_string(self):
        request = _FakeRequest("198.51.100.5", {"X-Real-IP": "198.51.100.5"})
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
                request = _FakeRequest("172.18.0.1", {"X-Real-IP": "172.18.0.1"})
                client_ip.get_client_ip_info(request)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("身份坍缩", captured.records[0].getMessage())
        self.assertTrue(client_ip._collapse_warned)

        # Further collapsed samples must not warn again (idempotent, not a log spam
        # source).
        with self.assertRaises(AssertionError):
            with self.assertLogs(client_ip.logger, level="WARNING"):
                request = _FakeRequest("172.18.0.1", {"X-Real-IP": "172.18.0.1"})
                client_ip.get_client_ip_info(request)

    def test_a_single_non_collapsed_sample_resets_the_streak(self):
        for _ in range(client_ip._COLLAPSE_SAMPLE_THRESHOLD - 1):
            request = _FakeRequest("172.18.0.1", {"X-Real-IP": "172.18.0.1"})
            client_ip.get_client_ip_info(request)
        self.assertFalse(client_ip._collapse_warned)

        # A real, distinct visitor IP breaks the streak (correctly configured
        # deployments must never trip the detector just because it once matched).
        request = _FakeRequest("172.18.0.1", {"X-Real-IP": "198.51.100.9"})
        client_ip.get_client_ip_info(request)
        self.assertEqual(client_ip._collapse_samples, 0)
        self.assertFalse(client_ip._collapse_warned)


if __name__ == "__main__":
    unittest.main()


class ComposeTopologyCollapseTest(unittest.TestCase):
    """compose 的真实拓扑：后端直连来源是 nginx 容器，头里是网桥网关。

    两者不相等，所以"解析出的 IP 等于代理自身地址"这一条在这里永远不成立——
    而这恰恰是身份坍缩最常发生的部署。判据必须认"访客地址落在可信代理网段"。
    """

    def test_bridge_gateway_in_header_is_collapsed(self):
        ip, collapsed = client_ip.get_client_ip_info(
            _FakeRequest("172.18.0.3", {"X-Real-IP": "172.18.0.1"})
        )
        self.assertEqual(ip, "172.18.0.1")
        self.assertTrue(collapsed)

    def test_real_public_visitor_behind_the_same_proxy_is_not_collapsed(self):
        ip, collapsed = client_ip.get_client_ip_info(
            _FakeRequest("172.18.0.3", {"X-Real-IP": "203.0.113.9"})
        )
        self.assertEqual(ip, "203.0.113.9")
        self.assertFalse(collapsed)

    def test_collapse_is_immediate_not_after_a_sample_streak(self):
        # 攒样本期间锁定仍然生效，而攻击者只需要 5 个请求，所以第一次就必须成立。
        _, collapsed = client_ip.get_client_ip_info(
            _FakeRequest("172.18.0.3", {"X-Real-IP": "172.18.0.1"})
        )
        self.assertTrue(collapsed)
