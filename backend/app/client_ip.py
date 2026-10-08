"""限流身份识别：从请求里取出一个**不可伪造**的客户端 IP。

自助兑换接口和后台登录接口都要按 IP 限流，两边必须用同一套判断，否则其中一边
的白名单逻辑失效就等于整体失效（后台登录尤其严重：伪造头能直接绕开失败锁定）。

这里是唯一认转发头的地方：run_server.py / Dockerfile 都关掉了 uvicorn 自带的
proxy headers 处理（它会在这段代码之前就按 X-Forwarded-For 改写 request.client，
信任范围也和 AUTO_TEAM_TRUSTED_PROXIES 是两套配置）。
"""

import ipaddress
import logging
import os
import time
from typing import Any, Optional

from fastapi import Request

logger = logging.getLogger(__name__)


# ── 可信反向代理 ────────────────────────────────────────────────────────────
# 只有来自这些来源的连接，才允许用 X-Real-IP / X-Forwarded-For 覆盖限流用的
# 客户端 IP。默认值 = 回环 + 内网私有段，覆盖两种既有部署形态：
#   * 宿主机 Nginx 反代 → 后端只监听 127.0.0.1，来源就是 127.0.0.1；
#   * docker compose 里 nginx 容器 → backend 容器，来源是 172.16/12 之类的桥接网段。
# 直接把端口暴露到公网时，公网来源不在白名单里，自带的 X-Real-IP 会被忽略，
# 攻击者也就没法用随机假 IP 绕开限流。设成空字符串 = 谁都不信，一律用直连 IP。
TRUSTED_PROXIES_ENV = "AUTO_TEAM_TRUSTED_PROXIES"
DEFAULT_TRUSTED_PROXIES = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"

_trusted_proxy_cache: Optional[tuple[str, tuple[Any, ...]]] = None


def _parse_trusted_proxies(raw: str) -> tuple[Any, ...]:
    networks: list[Any] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("%s 中的条目无法解析，已忽略: %r", TRUSTED_PROXIES_ENV, item)
    return tuple(networks)


def _trusted_proxy_networks() -> tuple[Any, ...]:
    """读取（并缓存）可信代理网段。环境变量变了会自动重新解析。"""
    global _trusted_proxy_cache
    raw = os.getenv(TRUSTED_PROXIES_ENV)
    if raw is None:
        raw = DEFAULT_TRUSTED_PROXIES

    cached = _trusted_proxy_cache
    if cached is not None and cached[0] == raw:
        return cached[1]

    networks = _parse_trusted_proxies(raw)
    _trusted_proxy_cache = (raw, networks)
    return networks


def normalize_ip(value: Optional[str]) -> str:
    """把一个字符串规范成 IP 文本；不是合法 IP 就返回空串。

    顺带把限流字典的 key 限制成合法 IP，避免有人用超长垃圾头把内存撑大。
    """
    candidate = (value or "").strip()
    if not candidate:
        return ""
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return ""
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped or address)


def is_trusted_proxy(peer: Optional[str]) -> bool:
    normalized = normalize_ip(peer)
    if not normalized:
        return False
    address = ipaddress.ip_address(normalized)
    return any(address in network for network in _trusted_proxy_networks())


# ── 身份坍缩检测 ────────────────────────────────────────────────────────────
# 直连来源可信时，解析出的"访客 IP"正常情况下几乎不可能等于代理自己的连接地址
# （peer）——除非反代和后端之间的这一跳本身就丢失了真实来源（典型情况：
# compose.yaml 把 web 端口绑在 127.0.0.1，Docker 走用户态 docker-proxy 转发，
# nginx 侧的 $remote_addr 从一开始就是网桥网关，而不是访客地址；nginx 又原样把
# $remote_addr 抄进 X-Real-IP）。单次命中可能是巧合（小内网部署里访客恰好也在
# 同一地址），所以攒够一批连续样本、且全部命中才报警；报警只打一次，不阻塞启动，
# 也不改变限流行为本身——发现方式和处理方式是两件事。
_COLLAPSE_SAMPLE_THRESHOLD = 20
_collapse_samples = 0
_collapse_last_ip = ""
_collapse_warned = False


def _is_collapsed_identity(ip: str, is_self: bool) -> bool:
    """这个请求有没有真实的每访客身份。

    判据不能只看 ``ip == peer``。compose 的实际拓扑是
    访客 → docker-proxy → nginx 容器 → 后端容器：后端看到的直连地址是 **nginx
    容器的 IP**，而 X-Real-IP 里是 **网桥网关**，两者本来就不相等。只比这一条，
    恰恰在最需要它的那个部署里永远不触发。

    真正的信号是"解析出来的访客地址本身就属于可信代理网段"——正常部署里访客是
    公网地址，绝不会落在 AUTO_TEAM_TRUSTED_PROXIES 里。判定必须**当场生效**而不
    是攒够样本再生效：攒样本期间登录锁定仍然有效，而攻击者只需要 5 个请求。

    代价：全部访客都在可信网段内的纯内网部署（例如把 10.0.0.0/8 设为可信代理），
    会被判成坍缩，从而不走按 IP 锁定，改用站点级递增冷却（见 admin.py 的
    _shared_identity_cooldown）。这种部署本来就不对公网暴露，而 10/min 的
    请求限流、外加冷却依然生效——比"面板能被任意匿名访客锁死"划算得多。
    """
    return bool(ip) and (is_self or is_trusted_proxy(ip))


def _note_identity_sample(peer: str, ip: str, collapsed: bool) -> None:
    """只负责告警节流：连续命中够多次才打一次日志，避免偶发抖动刷屏。"""
    global _collapse_samples, _collapse_warned, _collapse_last_ip

    if not collapsed or ip != _collapse_last_ip:
        _collapse_last_ip = ip if collapsed else ""
        _collapse_samples = 1 if collapsed else 0
        return

    _collapse_samples += 1
    if _collapse_warned or _collapse_samples < _COLLAPSE_SAMPLE_THRESHOLD:
        return

    _collapse_warned = True
    logger.warning(
        "检测到客户端身份坍缩：最近 %d 个经可信代理（%s）转发的请求，解析出的"
        "访客 IP 全部是同一个地址（%s），而它属于可信代理网段。这通常意味着 "
        "compose.yaml 把 web 端口绑在了 127.0.0.1，Docker 用户态转发（docker-proxy）"
        "导致后端看到的每个访客都是同一个网桥网关地址——全站共享一个限流身份。"
        "对这个共享身份，登录失败已从「按 IP 锁定」换成「站点级递增冷却」（连续失败"
        "5 次起逐次翻倍延迟，封顶 15 分钟，避免把所有访客连同管理员一起锁死），"
        "但请检查 docker/nginx.conf 的 set_real_ip_from "
        "是否覆盖了实际的反代来源，以及 compose.yaml 的端口绑定方式"
        "（docs/deployment.md「让后端收到真实访客 IP」一节有说明）。",
        _collapse_samples,
        peer,
        ip,
    )


def get_client_ip_info(request: Request) -> tuple[str, bool]:
    """
    从请求中获取用于限流计数的客户端 IP，以及一个"这个身份其实就是代理自己"的标记。

    安全要点一：转发头只有在**直连来源是可信代理**时才可信。否则谁都能自带一个
    X-Real-IP，每次换一个假值就绕开了限流（开源用户把端口直接暴露在公网时就是
    这种情况）。所以先看直连地址在不在 AUTO_TEAM_TRUSTED_PROXIES 里。

    安全要点二：即使来源可信，也不能取 X-Forwarded-For 的第一段。nginx 用的是
    `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`，会把真实来源
    **追加到调用方自带的 XFF 后面**——取第一段等于取攻击者自己写的值。

    所以：不可信来源 → 直接用直连地址；可信来源 → 优先 X-Real-IP（等于代理看到的
    $remote_addr），没有则取 XFF 里**最后一个合法 IP**（离本服务最近的那一跳），
    再退回直连地址。

    第二个返回值（is_proxy_self_identity）：为 True 表示"这个请求根本没有真实的
    每访客身份，大家共享同一个代理侧地址"。判据见 _note_identity_sample——它不只
    比对代理自身地址，还认"解析出的 IP 落在可信代理网段且连续重复"，因为 compose
    的多层容器拓扑里坍缩地址是网桥网关，并不等于后端看到的直连地址。调用方
    （登录失败锁定）据此决定：对这个共享身份改走站点级递增冷却，而不是按 IP 锁定。
    """
    peer = request.client.host if request.client else ""
    peer_norm = normalize_ip(peer)

    if not is_trusted_proxy(peer):
        return normalize_ip(peer) or (peer or "unknown")[:64], False

    ip = ""
    real_ip = normalize_ip(request.headers.get("X-Real-IP"))
    if real_ip:
        ip = real_ip
    else:
        x_forwarded_for = request.headers.get("X-Forwarded-For")
        if x_forwarded_for:
            for part in reversed(x_forwarded_for.split(",")):
                normalized = normalize_ip(part)
                if normalized:
                    ip = normalized
                    break

    if not ip:
        ip = peer_norm or "unknown"

    is_self = bool(peer_norm) and ip == peer_norm
    collapsed = _is_collapsed_identity(ip, is_self)
    _note_identity_sample(peer_norm, ip, collapsed)
    return ip, collapsed


def get_client_ip(request: Request) -> str:
    """从请求中获取用于限流计数的客户端 IP。详见 get_client_ip_info。"""
    return get_client_ip_info(request)[0]


def rate_limit_key(ip: str) -> str:
    """限流/失败锁定用的计数键：IPv4 按单个地址，IPv6 按所在的 /64 归并。

    一个 IPv6 接入（家宽、VPS、云主机）通常整段分到一个 /64，里面的 2^64 个地址可以
    随意换着用；按单个地址计数，每换一个地址就是一份全新的额度，等于没有限制。
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


# ── 限流实现 ────────────────────────────────────────────────────────────────
# 单个限流器最多跟踪多少个 IP。伪造来源刷请求时，窗口内的 key 不会被过期回收，
# 所以还需要这个硬上限兜底，保证进程内存有界（超出时淘汰最久未活动的条目：宁可
# 限流精度下降，也不能让内存无限增长）。
RATE_LIMIT_MAX_TRACKED_IPS = 10000


class RateLimiter:
    """内存限流器，使用滑动窗口算法（时间戳列表）。"""

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        *,
        max_tracked_ips: int = RATE_LIMIT_MAX_TRACKED_IPS,
    ):
        """
        max_requests: 时间窗口内最多允许的请求数
        window_seconds: 时间窗口大小（秒）
        max_tracked_ips: 同时跟踪的 IP 上限，超出后淘汰最久未活动的条目
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_tracked_ips = max(1, max_tracked_ips)
        self.requests: dict[str, list[float]] = {}  # {ip: [timestamp1, timestamp2, ...]}
        self._next_sweep = 0.0

    def is_allowed(self, ip: str) -> bool:
        """检查该 IP 是否被限流。返回 True 表示允许，False 表示被限流。"""
        now = time.time()
        self._sweep(now)

        cutoff = now - self.window_seconds
        # 只保留窗口内的请求；窗口外的旧时间戳在这里和 _sweep 里都会被丢掉。
        hits = [ts for ts in self.requests.get(ip, ()) if ts > cutoff]

        if len(hits) >= self.max_requests:
            self.requests[ip] = hits
            return False

        hits.append(now)
        self.requests[ip] = hits
        return True

    def _sweep(self, now: float) -> None:
        """回收过期的 IP 键。

        原实现只重写每个 key 的值、从不删除 key，被伪造来源刷一遍就会把 dict
        撑爆（每个假 IP 永久占一个条目）。这里定期整体清一遍：窗口内已经没有
        任何请求的 key 直接删掉；万一窗口内活跃 key 仍然超过上限，就按最后活动
        时间淘汰最旧的那批。
        """
        if now < self._next_sweep and len(self.requests) <= self.max_tracked_ips:
            return

        cutoff = now - self.window_seconds
        for key in list(self.requests):
            fresh = [ts for ts in self.requests[key] if ts > cutoff]
            if fresh:
                self.requests[key] = fresh
            else:
                del self.requests[key]

        overflow = len(self.requests) - self.max_tracked_ips
        if overflow > 0:
            victims = sorted(self.requests, key=lambda key: self.requests[key][-1])[:overflow]
            for key in victims:
                del self.requests[key]

        self._next_sweep = now + self.window_seconds
