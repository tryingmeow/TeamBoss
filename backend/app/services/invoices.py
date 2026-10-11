"""Stripe 发票的本地缓存与对账。

上游 ``GET /backend-api/invoices`` 返回完整的 Stripe invoice 对象，里面带着
真实的 customer_email / customer_name / customer_address。这些 PII 一律不落库：
入库前必须先过 :func:`sanitize_invoice`，只保留对账需要的字段。

金额上游给的是最小货币单位（THB 56100 = ฿561.00），入库时统一换算成主单位；
JPY / KRW 这类零小数货币本身就是主单位，不做除法。

对账全部在原币种内完成——推算月费和发票金额都是同一币种，直接相减，
不经过汇率，避免汇率波动制造假差异。换算成基准币只发生在展示层。
"""

import sqlite3
from datetime import datetime, timedelta, timezone

# Stripe 的零小数货币（金额不乘 100 传输），只列 FX 表里会出现的。
_ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW"}

# 推算月费 vs 发票金额的容差：超过 max(推算值的 1%, 原币 1.00) 才算不一致。
# 折扣尾差、税位舍入都在这个范围内，不值得打扰人。
MISMATCH_TOLERANCE_RATIO = 0.01
MISMATCH_TOLERANCE_MIN = 1.0

# 每个团队最多一天拉一次发票，发票本身也只在月度边界变化。
INVOICE_SYNC_INTERVAL_HOURS = 24

INVOICE_FETCH_LIMIT = 6


def minor_to_major(amount, currency: str):
    if amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if (currency or "").upper() in _ZERO_DECIMAL_CURRENCIES:
        return value
    return value / 100.0


def _epoch_to_iso(value):
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def sanitize_invoice(raw: dict):
    """从上游 invoice 对象里只取安全字段，PII 在这里被丢掉。

    Returns None if the object has no usable id.
    """
    if not isinstance(raw, dict):
        return None
    invoice_id = raw.get("id")
    if not invoice_id or not isinstance(invoice_id, str):
        return None

    currency = (raw.get("currency") or "").upper()

    # 发票级 period_start/period_end 实测常等于 created，真实的服务周期
    # 在行项目的 period 里，优先取它。
    period_start = raw.get("period_start")
    period_end = raw.get("period_end")
    description = None
    lines = raw.get("lines")
    if isinstance(lines, dict):
        line_items = lines.get("data")
        if isinstance(line_items, list) and line_items and isinstance(line_items[0], dict):
            first = line_items[0]
            desc = first.get("description")
            if isinstance(desc, str) and desc.strip():
                description = desc.strip()
            line_period = first.get("period")
            if isinstance(line_period, dict):
                period_start = line_period.get("start") or period_start
                period_end = line_period.get("end") or period_end

    hosted_url = raw.get("hosted_invoice_url")
    if not isinstance(hosted_url, str) or not hosted_url.startswith("https://"):
        hosted_url = None

    return {
        "invoice_id": invoice_id,
        "number": raw.get("number") if isinstance(raw.get("number"), str) else None,
        "status": (raw.get("status") or "").lower() or None,
        "currency": currency or None,
        "amount_due": minor_to_major(raw.get("amount_due"), currency),
        "amount_paid": minor_to_major(raw.get("amount_paid"), currency),
        "period_start": _epoch_to_iso(period_start),
        "period_end": _epoch_to_iso(period_end),
        "description": description,
        "hosted_invoice_url": hosted_url,
        "created_at": _epoch_to_iso(raw.get("created")),
    }


def store_invoices_sync(conn: sqlite3.Connection, team_id: str, raw_invoices, now: str) -> int:
    stored = 0
    for raw in raw_invoices or []:
        row = sanitize_invoice(raw)
        if row is None:
            continue
        conn.execute(
            """INSERT INTO invoices
               (team_id, invoice_id, number, status, currency, amount_due, amount_paid,
                period_start, period_end, description, hosted_invoice_url, created_at, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(team_id, invoice_id) DO UPDATE SET
                   number = excluded.number,
                   status = excluded.status,
                   currency = excluded.currency,
                   amount_due = excluded.amount_due,
                   amount_paid = excluded.amount_paid,
                   period_start = excluded.period_start,
                   period_end = excluded.period_end,
                   description = excluded.description,
                   hosted_invoice_url = excluded.hosted_invoice_url,
                   created_at = excluded.created_at,
                   fetched_at = excluded.fetched_at""",
            (team_id, row["invoice_id"], row["number"], row["status"], row["currency"],
             row["amount_due"], row["amount_paid"], row["period_start"], row["period_end"],
             row["description"], row["hosted_invoice_url"], row["created_at"], now),
        )
        stored += 1
    return stored


def _mark_invoice_attempt_sync(conn: sqlite3.Connection, team_id: str, now: str) -> None:
    """记一次"这个 team 的发票拉取已经尝试过了"。

    ``teams.invoices_synced_at`` 就是这个节流点：成功和失败都要落，否则一个上游
    持续报错的 team 会在**每一轮** data_sync 里被重试，24 小时一次的设计形同虚设。
    需要立刻重试的入口（用户展开发票子表）走 force，不看这个时间戳。
    """
    conn.execute("UPDATE teams SET invoices_synced_at = ? WHERE id = ?", (now, team_id))
    conn.commit()


def refresh_invoices_sync(conn: sqlite3.Connection, client, team_id: str, now: str,
                          run_call, limit: int = INVOICE_FETCH_LIMIT):
    """拉一次发票并入库。返回错误文本，成功返回 None。

    一次调用取的是这个 team 的一整批发票，要么全成要么全败，没有部分成功要处理。
    无论成败都推进节流时间戳（见 ``_mark_invoice_attempt_sync``）。
    """
    result = run_call(client.get_invoices, limit)
    if not isinstance(result, dict) or "error" in result:
        _mark_invoice_attempt_sync(conn, team_id, now)
        return (result or {}).get("error", "invalid response") if isinstance(result, dict) else "invalid response"
    items = result.get("data")
    if not isinstance(items, list):
        _mark_invoice_attempt_sync(conn, team_id, now)
        return "invalid response: missing data list"
    store_invoices_sync(conn, team_id, items, now)
    _mark_invoice_attempt_sync(conn, team_id, now)
    return None


def refresh_invoices_if_stale_sync(conn: sqlite3.Connection, client, team_id: str,
                                   now: str, run_call, force: bool = False):
    """节流版拉取。``force=True`` 跳过节流，给用户主动触发的入口用。"""
    if force:
        return refresh_invoices_sync(conn, client, team_id, now, run_call)
    row = conn.execute(
        "SELECT invoices_synced_at FROM teams WHERE id = ?", (team_id,)
    ).fetchone()
    if row is None:
        return None
    synced_at = row["invoices_synced_at"] if isinstance(row, sqlite3.Row) else row[0]
    if synced_at:
        try:
            synced_dt = datetime.fromisoformat(synced_at)
            if synced_dt.tzinfo is None:
                synced_dt = synced_dt.replace(tzinfo=timezone.utc)
            now_dt = datetime.fromisoformat(now)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            if now_dt - synced_dt < timedelta(hours=INVOICE_SYNC_INTERVAL_HOURS):
                return None
        except (ValueError, TypeError):
            pass
    return refresh_invoices_sync(conn, client, team_id, now, run_call)


def refresh_invoices_for_team_blocking(team_id: str, force: bool = False):
    """给路由层用的一次性拉取：自己开同步连接、自己组装客户端。

    这是用户主动触发的那个入口（展开某个 team 的发票子表）。``force=True`` 时
    绕过 24 小时节流：定时轮次会因为上一次失败而退避，用户点开的时候必须还能
    立刻重试一次，否则一个坏掉过一次的 team 在界面上要空一整天。任何失败都只
    返回错误文本，调用方照常返回库里已有的数据。
    """
    from ..chatgpt_limiter import run_chatgpt_call_sync
    from ..chatgpt_client import ChatGPTClient
    from ..database import get_db_path

    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        team = conn.execute(
            """SELECT t.id, t.status, t.access_token, t.device_id, t.proxy_id, p.url AS proxy_url
               FROM teams t LEFT JOIN proxies p ON p.id = t.proxy_id
               WHERE t.id = ?""",
            (team_id,),
        ).fetchone()
        if team is None or team["status"] != "active" or not team["access_token"]:
            return "team not eligible for invoice sync"
        # 绑了代理但 join 不出地址：这一次不拉发票，不能换成本机 IP 直连。
        if team["proxy_id"] and not (team["proxy_url"] or "").strip():
            return f"代理 #{team['proxy_id']} 不可用，已跳过发票同步"
        client = ChatGPTClient(
            team["access_token"], team["id"], team["device_id"],
            proxy_url=team["proxy_url"],
        )
        now = datetime.now(timezone.utc).isoformat()
        return refresh_invoices_if_stale_sync(
            conn, client, team_id, now, run_chatgpt_call_sync, force=force
        )
    finally:
        conn.close()


def classify_latest_invoice(latest: dict, monthly_total_native, billing_currency: str):
    """在原币种内给最新一期发票定性。

    返回 (reconciliation, display_amount, diff_native)：
    - reconciliation: 'unpaid' | 'over' | 'under' | 'match' | None(无法比对)
    - display_amount: 界面上代表这期发票的金额——已支付用实付，未支付用应付
    - diff_native: display_amount - 推算月费（仅在能比对时给出）
    """
    status = (latest.get("status") or "").lower()
    if status == "paid":
        display_amount = latest.get("amount_paid")
    else:
        display_amount = latest.get("amount_due")

    if status in ("open", "uncollectible"):
        return "unpaid", display_amount, None

    if (
        status != "paid"
        or display_amount is None
        or monthly_total_native is None
        or not billing_currency
        or (latest.get("currency") or "").upper() != billing_currency.upper()
    ):
        return None, display_amount, None

    diff = display_amount - monthly_total_native
    tolerance = max(MISMATCH_TOLERANCE_RATIO * abs(monthly_total_native), MISMATCH_TOLERANCE_MIN)
    if abs(diff) <= tolerance:
        return "match", display_amount, diff
    return ("over" if diff > 0 else "under"), display_amount, diff
