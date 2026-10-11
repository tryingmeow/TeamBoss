"""Team 出口代理的解析：绑了代理就必须从那个出口走，解析不出来一律报错。

一个 Team 绑代理，意思是它对 chatgpt.com 的所有请求只能从那个 IP 出去。解析
失败时返回 ``None``，调用方会原样构造一个不带 proxies 的客户端，于是这一次请求
从本机 IP 直连——同一个 ChatGPT 账号的出口 IP 中途换掉，正是绑代理要避免的事。
所以这里只有两种结果：拿到可用地址，或者抛 :class:`ProxyUnavailableError`。

``proxy_id`` 为空是另一回事：这个 Team 本来就没选代理，直连是它的配置，返回
``None``。代理被删除时 ``routes/proxies.py`` 会把引用它的 Team 一起置空，所以
"有 proxy_id 但查不到行"不是正常状态，而是解析失败。
"""

from __future__ import annotations

import sqlite3

from .database import get_db


class ProxyUnavailableError(RuntimeError):
    """Team 绑了代理，但当前解析不出可用的代理地址。"""

    def __init__(self, proxy_id, reason: str):
        self.proxy_id = proxy_id
        self.reason = reason
        super().__init__(f"代理 #{proxy_id} 不可用（{reason}），已阻止直连")


def _checked_url(proxy_id, row) -> str:
    if row is None:
        raise ProxyUnavailableError(proxy_id, "proxy not found")
    url = (row["url"] or "").strip()
    if not url:
        raise ProxyUnavailableError(proxy_id, "empty proxy url")
    return url


def resolve_proxy_url_sync(conn: sqlite3.Connection, proxy_id) -> str | None:
    """同步连接版。``proxy_id`` 为空返回 None（没选代理），其余失败全部抛错。"""
    if not proxy_id:
        return None
    try:
        row = conn.execute("SELECT url FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
    except sqlite3.Error as exc:
        raise ProxyUnavailableError(proxy_id, f"lookup failed: {type(exc).__name__}") from exc
    return _checked_url(proxy_id, row)


async def resolve_proxy_url(proxy_id) -> str | None:
    """异步版，语义与 :func:`resolve_proxy_url_sync` 相同。"""
    if not proxy_id:
        return None
    async with get_db() as db:
        cursor = await db.execute("SELECT url FROM proxies WHERE id = ?", (proxy_id,))
        row = await cursor.fetchone()
    return _checked_url(proxy_id, row)
