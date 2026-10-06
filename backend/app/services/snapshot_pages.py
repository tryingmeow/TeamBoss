"""本文件是「成员 / 待接受邀请名单分页拉取何时算完整」的正本。

上游的成员名单（/users）和待接受邀请名单（/invites）都按 offset/limit 分页。只有一份
**完整**的名单才能拿来判断「某人不在」，进而放掉持久席位占用、关掉 member_expiry 行、
踢人或撤邀请。拉不全、读不懂的名单是未知状态：调用方保留上一份缓存，这次刷新算失败。

逐页的判定集中在 ``SnapshotPageAccumulator``，四处分页循环（member_cache_service 的
异步刷新、scheduler 的同步拉取、patrol 严格模式的动手前刷新、seat_capacity 卖座前现拉的
待接受邀请）都用它，规则只写这里。卖座那一处同样只认完整名单：拉不全 = 占用未知 = 没有空位。

- 每一页必须是对象；带 ``error`` 键 = 这一页失败（``SnapshotPageError.upstream_error``
  带着上游原样的错误，调用方沿用自己原来的报错文字）。
- 必须有一个列表字段（``items`` 或调用方给的备用键），每个条目都必须是对象。
- 一页的条目数不能超过请求的 ``limit``（多出来说明分页语义变了，按 offset 翻页会重复）。
- ``total``：缺失或 null 表示上游没报总数；报了就必须是非负整数（布尔不算）。同一次拉取
  里要么每页都报、要么每页都不报，报了就每页相同（中途变了 = 名单在翻页期间变过）。
- 报了总数：累计条数**恰好等于**总数才算完；超过总数、或者凑够之前来了短页，都是不完整。
- 没报总数：短页（不足 ``limit`` 条，含空页）就是最后一页。
- 翻页次数的上限由调用方的循环决定，翻到上限还没结束同样是不完整。
- 每一行都要认得出是谁：id 取 ``id``（去空白后非空的字符串），没有再取 ``user_id``；
  邮箱取 ``email``（去空白、转小写后非空），没有再取 ``email_address``。id 和邮箱都没有的行
  （例如 ``{}``）让整份名单不完整——它照样被算进条数、凑满 total，却对不上任何人。
- 同一份名单（一个累加器，跨页也算）里两行 id 相同、或邮箱相同（不分大小写），整份名单
  不完整。绝不悄悄去重：重复行同样凑了条数，掩盖的是没拉到的那个人；重复的 owner 还会
  让按人头数的席位判断多算一个，把正当的人当成超员。成员名单和邀请名单同一条规则。
- 成员名单不能是空的：真实的 /users 回复里至少有 owner。拉完一行都没有（``{"items": [],
  "total": 0}``，或不报总数的空页）说明这不是这个 Team 的真名单，按不完整处理，否则会把
  所有人判成缺席、清空缓存、放掉全部席位占用。规则由 ``require_items=True`` 打开：每一处
  /users 分页（异步刷新、scheduler 的数据同步 / 踢人监视 / 到期踢人的成员查找、patrol 严格
  模式刷新）都传它；邀请名单不传，空的邀请名单是正常的。
"""
from __future__ import annotations

from typing import Any, Optional


class SnapshotPageError(Exception):
    """这一页让整份名单不完整或读不懂。``str(exc)`` 是原因。"""

    def __init__(self, reason: str, *, upstream_error: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        # 这一页本身就是上游报错（带 error 键）时，原样的 error 值；否则为 None。
        self.upstream_error = upstream_error


def _page_total(data: dict) -> tuple[bool, Optional[int]]:
    """返回 (这一页报了总数, 总数)。报了却不是非负整数时抛 SnapshotPageError。"""
    raw = data.get("total")
    if raw is None:
        return False, None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise SnapshotPageError("malformed total in member/invite response")
    return True, raw


def _row_text(item: dict, *keys: str) -> str:
    """按顺序取第一个去空白后非空的字符串字段；都没有返回空串。"""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def row_identity(item: dict) -> tuple[str, str]:
    """一行名单的 (id, 小写邮箱)；拿不到的一项是空串。"""
    return _row_text(item, "id", "user_id"), _row_text(item, "email", "email_address").lower()


class SnapshotPageAccumulator:
    """按页累积一份名单，并判定它什么时候完整。

    用法（同步、异步一样）::

        pages = SnapshotPageAccumulator("users", limit=100)
        for _ in range(MAX_PAGES):
            data = call(offset=pages.next_offset, limit=pages.limit)
            if pages.add(data):          # 可能抛 SnapshotPageError
                return pages.items
        # 翻到上限还没完整：不完整
    """

    def __init__(self, *fallback_keys: str, limit: int, require_items: bool = False) -> None:
        self.item_keys = ("items",) + tuple(fallback_keys)
        self.limit = max(1, int(limit))
        # True = 这是成员名单，拉完却一行都没有算不完整（真实名单里至少有 owner）。
        self.require_items = bool(require_items)
        self.items: list[dict] = []
        self._pages = 0
        self._has_total: Optional[bool] = None
        self._total: Optional[int] = None
        self._ids: set[str] = set()
        self._emails: set[str] = set()

    @property
    def next_offset(self) -> int:
        return len(self.items)

    def add(self, data: Any) -> bool:
        """收下一页。返回 True = 名单已完整；False = 还要下一页。不完整 / 读不懂抛错。"""
        if not isinstance(data, dict):
            raise SnapshotPageError("non-object member/invite response")
        if "error" in data:
            raise SnapshotPageError(
                f"upstream error: {data['error']}", upstream_error=data["error"]
            )

        page_items = None
        for key in self.item_keys:
            candidate = data.get(key)
            if isinstance(candidate, list):
                page_items = candidate
                break
        if page_items is None:
            raise SnapshotPageError("unrecognized member/invite response structure")
        if not all(isinstance(item, dict) for item in page_items):
            raise SnapshotPageError("unrecognized member/invite entry structure")
        if len(page_items) > self.limit:
            raise SnapshotPageError("member/invite page larger than the requested limit")
        self._check_identities(page_items)

        has_total, total = _page_total(data)
        if self._pages == 0:
            self._has_total, self._total = has_total, total
        elif has_total != self._has_total or total != self._total:
            raise SnapshotPageError("member/invite total changed between pages")
        self._pages += 1

        self.items.extend(page_items)
        short_page = len(page_items) < self.limit
        if has_total:
            if len(self.items) == total:
                return self._complete()
            if len(self.items) > total:
                raise SnapshotPageError("more member/invite entries than the reported total")
            if short_page:
                raise SnapshotPageError("truncated member/invite response before reported total")
            return False
        return self._complete() if short_page else False

    def _complete(self) -> bool:
        if self.require_items and not self.items:
            raise SnapshotPageError("empty member list")
        return True

    def _check_identities(self, page_items: list[dict]) -> None:
        """每行要有 id 或邮箱；同一份名单里 id、邮箱都不能重复（跨页也算）。"""
        for item in page_items:
            row_id, email = row_identity(item)
            if not row_id and not email:
                raise SnapshotPageError("member/invite entry has neither id nor email")
            if row_id and row_id in self._ids:
                raise SnapshotPageError("duplicate member/invite id in the list")
            if email and email in self._emails:
                raise SnapshotPageError("duplicate member/invite email in the list")
            if row_id:
                self._ids.add(row_id)
            if email:
                self._emails.add(email)
