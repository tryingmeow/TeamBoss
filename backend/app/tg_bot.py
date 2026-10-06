"""Telegram 长轮询机器人：管理员和成员分级命令。

设计要点：
- 自包含：直接用 sqlite3 读 settings / tg_users / tg_pairing_codes，requests 调 Telegram
  以及本地后端 HTTP API，不 import 业务 service 模块，避免循环依赖（参照 services/tg_notify.py）。
- 容错：轮询循环里任何异常都吞掉继续，绝不能让线程挂掉；本地 API 调用失败只回一句错误文本。
- 幂等：start_bot_thread()/stop_bot_thread() 可重复调用；tg_bot_enabled != '1' 或 token 为空时
  只做轻量 sleep 轮询，绝不请求 Telegram。
- 鉴权：未绑定 chat 只认配对和帮助；tg_users 中的管理员可使用后台命令；
  tg_member_bindings 中的成员只能查询自己。
"""

import sqlite3
import threading
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from os import getenv
from typing import Callable, Optional

import requests

from .database import get_db_path
from .seat_types import OVERAGE_POLICY_LABELS, normalize_overage_policy, seat_type_label
from .tg_format import detail_card, overview_panel
from .services.tg_member_bindings import (
    claim_member_pairing_code_sync,
    member_emails_for_chat_sync,
    normalize_member_email,
)
from .services.tg_commands import sync_all_commands_sync, sync_chat_commands_sync
from .services.tg_notify import send_message_sync

TG_API = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 30          # Telegram getUpdates 长轮询秒数
_IDLE_SLEEP = 5             # 未开启/无 token 时的轮询间隔
_GETUPDATES_HTTP_TIMEOUT = _POLL_TIMEOUT + 10
_SEND_TIMEOUT = 15
_API_HTTP_TIMEOUT = 15

_BACKEND_PORT = getenv("AUTO_TEAM_BACKEND_PORT", "18087")
_API_BASE = f"http://127.0.0.1:{_BACKEND_PORT}"

_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_lifecycle_lock = threading.Lock()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# settings / sqlite helpers（同步，供轮询线程直接用）
# ---------------------------------------------------------------------------

def _get_setting(key: str) -> Optional[str]:
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
        finally:
            conn.close()
        if row and row[0] is not None:
            value = str(row[0]).strip()
            return value or None
    except Exception:
        pass
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_user(chat_id: str) -> Optional[dict]:
    try:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT * FROM tg_users WHERE chat_id = ? AND disabled = 0", (str(chat_id),)
            )
            row = cur.fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _find_member_emails(chat_id: str) -> list[str]:
    return member_emails_for_chat_sync(chat_id)


# ---------------------------------------------------------------------------
# 向导状态机（处理多步命令）
# ---------------------------------------------------------------------------

_WIZARD_TTL = 300  # 秒，超时自动清空
_wizards: dict[str, dict] = {}  # chat_id -> wizard state。单线程轮询，无需加锁

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_DURATION_RE = re.compile(r"^\d+[mhd]$", re.IGNORECASE)
_NEVER = {"never", "none", "永久", "forever", "infinite"}

_WIZ_INVALID = "❌ 输入无效，操作已重置。\n↩️ 发送 /invite 或 /kick 重新开始。"
_WIZ_CANCEL_HINT = "↩️ 回复 q 取消"

_LIST_CAP = 30


def _member_email(m: dict) -> str:
    """提取成员邮箱或名字。"""
    return m.get("email") or m.get("name") or "(无邮箱)"


def _numbered_lines(rows: list, label_fn) -> str:
    """格式化编号列表。"""
    return "\n".join(f"{i}. {label_fn(r)}" for i, r in enumerate(rows, 1))


def _trunc_note(total: int) -> str:
    """超出限制时的截断提示。"""
    if total > _LIST_CAP:
        return f"\n…共 {total} 条，仅显示前 {_LIST_CAP} 条；可输入关键词精确查询。"
    return ""


def _member_label(m: dict) -> str:
    """编号列表用的一行标签：邮箱 ·Team。"""
    return f"👤 {_member_email(m)}  ·  🏢 {m.get('team_name') or '?'}"


def _mask_owner_email(email: str) -> str:
    """车主邮箱脱敏：本地部分前 5 位 + 服务商名（如 gmail / outlook）。"""
    email = (email or "").strip()
    if not email or "@" not in email:
        return email or "?"
    local, _, domain = email.partition("@")
    provider = domain.split(".", 1)[0] if domain else ""
    ell = "…" if len(local) > 5 else ""
    head = local[:5]
    return f"{head}{ell}@{provider}" if provider else f"{head}{ell}"


def _reset_wizard(chat_id: str) -> bool:
    """清空该 chat 的向导状态。返回是否原来存在。"""
    return _wizards.pop(chat_id, None) is not None


def reset_conversation_state() -> None:
    """Drop in-progress conversations after switching to a different Bot."""
    _wizards.clear()


def _get_wizard(chat_id: str) -> Optional[dict]:
    """取出该 chat 的向导状态；若超时则删除并返回 None。"""
    w = _wizards.get(chat_id)
    if w is None:
        return None
    if time.time() - w.get("updated_at", 0) > _WIZARD_TTL:
        _wizards.pop(chat_id, None)
        return None
    return w


def _touch(w: dict) -> None:
    """更新向导的 updated_at 时间戳。"""
    w["updated_at"] = time.time()


def _api_delete(path: str) -> dict:
    """DELETE 请求到本地后端。"""
    headers = {}
    api_key = _admin_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    resp = requests.delete(f"{_API_BASE}{path}", headers=headers, timeout=_API_HTTP_TIMEOUT)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


def _extract_api_error(exc: Exception) -> str:
    """从异常中提取 API 错误信息。"""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            d = resp.json().get("detail")
            # 结构化的 409（超员策略等）给的是对象，回给人看的只有 message 那句话。
            if isinstance(d, dict) and d.get("message"):
                return str(d["message"])[:300]
            if d:
                return str(d)[:300]
        except Exception:
            pass
        return f"HTTP {resp.status_code}"
    if isinstance(exc, requests.RequestException):
        # 连不上/超时的 requests 异常文本里带着本地后端地址和完整请求路径
        # （HTTPConnectionPool(host='127.0.0.1', port=...) ... url: /api/...），
        # 这些回复也会发给成员，只记服务端日志。
        logger.warning("local backend request failed: %r", exc)
        if isinstance(exc, requests.Timeout):
            return "服务响应超时，请稍后查看结果，确认后再重试"
        return "服务暂时不可用，请稍后再试"
    return str(exc)[:300]


def _normalize_duration(raw: str) -> Optional[str]:
    """规范化有效期字符串。返回 'never' 或如 '30d'；非法返回 None。"""
    v = raw.strip().lower()
    if v in _NEVER:
        return "never"
    if _DURATION_RE.fullmatch(v) and int(v[:-1]) > 0:
        return v
    return None


def _sorted_teams(teams: list) -> list[dict]:
    """排序 Team：有空位的(active < seats)在前，满员的在后，组内保持原顺序。"""
    indexed = [(i, t) for i, t in enumerate(teams)]
    active = teams[0].get("active_chatgpt", 0) if teams else 0
    seats = teams[0].get("seats_entitled", 0) if teams else 0

    def sort_key(p):
        idx, t = p
        a = t.get("active_chatgpt") or 0
        s = t.get("seats_entitled") or 0
        is_full = 0 if a < s else 1
        return (is_full, idx)

    sorted_indexed = sorted(indexed, key=sort_key)
    return [t for _, t in sorted_indexed]


def _team_lines(teams: list[dict]) -> str:
    """格式化向导 Team 列表，用图标区分可用和已满。"""
    lines = []
    for i, t in enumerate(teams, 1):
        name = t.get("name") or t.get("team_id") or "?"
        active = t.get("active_chatgpt") or 0
        seats = t.get("seats_entitled") or 0
        is_full = active >= seats
        icon = "🔴" if is_full else "🟢"
        state = "已满" if is_full else f"空余 {max(seats - active, 0)}"
        lines.append(f"{i}. {icon} {name}\n   └ 💺 GPT：{active} / {seats} · {state}")
    return "\n".join(lines)


# 配对码错误锁定：同一个 chat 在 _PAIR_FAILURE_WINDOW 内输错 _PAIR_MAX_BAD_CODES 次
# （码根本不存在），之后 _PAIR_LOCKOUT_SECONDS 内这个 chat 的 /pair 一律拒绝、不查库。
# 管理员配对码能直接换来后台权限，不能让人对着机器人无限次猜。只由轮询线程访问。
_PAIR_MAX_BAD_CODES = 5
_PAIR_FAILURE_WINDOW = 3600
_PAIR_LOCKOUT_SECONDS = 1800
_pair_failures: dict[str, dict] = {}  # chat_id -> {"count", "first_at", "locked_until"}


def _pair_locked(chat_id: str, now: float) -> bool:
    entry = _pair_failures.get(str(chat_id))
    return bool(entry) and entry.get("locked_until", 0) > now


def _record_bad_pair_code(chat_id: str, now: float) -> bool:
    """记一次错误配对码；返回这一次之后是否进入锁定。"""
    if len(_pair_failures) > 1000:
        for key in list(_pair_failures):
            entry = _pair_failures[key]
            if entry.get("locked_until", 0) <= now and now - entry["first_at"] > _PAIR_FAILURE_WINDOW:
                del _pair_failures[key]

    key = str(chat_id)
    entry = _pair_failures.get(key)
    if entry is None or now - entry["first_at"] > _PAIR_FAILURE_WINDOW:
        entry = {"count": 0, "first_at": now, "locked_until": 0}
        _pair_failures[key] = entry
    entry["count"] += 1
    if entry["count"] >= _PAIR_MAX_BAD_CODES:
        entry["locked_until"] = now + _PAIR_LOCKOUT_SECONDS
        return True
    return False


_PAIR_LOCKED_TEXT = f"配对码错误次数过多，请 {_PAIR_LOCKOUT_SECONDS // 60} 分钟后再试。"


def _notify_admins_of_new_admin(recipients: list[str], chat_id: str, username: Optional[str], paired_at: str) -> None:
    """有人用配对码拿到管理员身份时，告诉此前所有管理员。

    配对码一旦外泄，这条通知是现有管理员唯一能及时发现的信号。
    """
    who = username or "（未知）"  # Telegram 用户名，没有时是对方自己填的名字
    text = (
        "🔔 有新的 Telegram 管理员完成了配对\n"
        f"👤 账号：{who}\n"
        f"🆔 Chat ID：{chat_id}\n"
        f"🕒 时间：{paired_at}\n"
        "如果这不是你们自己的操作，请立即到后台的 Telegram 设置里停用这个账号，并吊销未使用的配对码。"
    )
    for recipient in recipients:
        try:
            _send(recipient, text)
        except Exception:
            logger.exception("failed to notify an admin about a new admin pairing")


def _pair(chat_id: str, username: Optional[str], code: str) -> str:
    """尝试用配对码注册当前 chat。返回要回复的文本。"""
    code = (code or "").strip().upper()
    if not code:
        return "配对码不能为空。发送 /pair <配对码> 完成注册。"

    if _pair_locked(chat_id, time.time()):
        return _PAIR_LOCKED_TEXT

    member_result = claim_member_pairing_code_sync(chat_id, username, code)
    if member_result is not None:
        return member_result

    try:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            # Claim code + grant admin in one write transaction.  The code must
            # be won first: otherwise a concurrent loser could still be inserted
            # into tg_users before discovering that its conditional UPDATE lost.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tg_pairing_codes WHERE code = ?", (code,)
            ).fetchone()
            if not row:
                if _record_bad_pair_code(chat_id, time.time()):
                    return _PAIR_LOCKED_TEXT
                return "配对码无效，请核对后重试。"
            if row["disabled"]:
                return "配对码已被吊销。"
            if row["used_by_chat_id"]:
                return "配对码已被使用过。"

            expires_at = row["expires_at"]
            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > exp_dt:
                        return "配对码已过期，请向管理员索取新的配对码。"
                except Exception:
                    pass

            now = _now_iso()
            cursor = conn.execute(
                """UPDATE tg_pairing_codes
                   SET used_by_chat_id = ?, used_at = ?
                   WHERE id = ? AND used_by_chat_id IS NULL AND disabled = 0""",
                (str(chat_id), now, row["id"]),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return "配对码已被其他用户同时使用，请稍后重试或向管理员索取新的配对码。"

            existing_admins = [
                str(r[0])
                for r in conn.execute(
                    "SELECT chat_id FROM tg_users WHERE disabled = 0 AND chat_id != ?",
                    (str(chat_id),),
                ).fetchall()
            ]
            conn.execute(
                """INSERT INTO tg_users (chat_id, username, paired_at, created_at, disabled)
                   VALUES (?, ?, ?, ?, 0)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     username = excluded.username,
                     paired_at = excluded.paired_at""",
                (str(chat_id), username, now, now),
            )
            conn.commit()
            _pair_failures.pop(str(chat_id), None)

            try:
                sync_chat_commands_sync(str(chat_id), conn=conn)
            except Exception:
                logger.exception("failed to sync Telegram commands after successful admin pairing")
        finally:
            conn.close()
    except Exception:
        # 这条路径任何 Telegram 用户都能触达（还没配对成功就是陌生人）。裸异常会把
        # 数据库绝对路径、表名列名带出去，只记服务端日志。
        logger.exception("admin pairing failed")
        return "注册失败，请联系管理员。"

    _notify_admins_of_new_admin(existing_admins, str(chat_id), username, now)
    return "✅ 管理员注册成功。发送 /help 查看管理命令。"


# ---------------------------------------------------------------------------
# 本地后端 API 调用
# ---------------------------------------------------------------------------

def _admin_api_key() -> Optional[str]:
    return _get_setting("admin_api_key")


def _api_get(path: str, params: Optional[dict] = None, *, timeout: int = _API_HTTP_TIMEOUT) -> dict:
    headers = {}
    api_key = _admin_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    resp = requests.get(f"{_API_BASE}{path}", headers=headers, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _api_patch(path: str, body: dict) -> dict:
    headers = {}
    api_key = _admin_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    resp = requests.patch(f"{_API_BASE}{path}", headers=headers, json=body, timeout=_API_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _api_post(path: str, body: Optional[dict] = None, *, timeout: int = _API_HTTP_TIMEOUT) -> dict:
    headers = {}
    api_key = _admin_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    resp = requests.post(
        f"{_API_BASE}{path}", headers=headers, json=body, timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# 命令实现（已注册用户）。每个 handler: (user: dict, args: str) -> str
# ---------------------------------------------------------------------------

def _is_idle(team: dict) -> bool:
    return (team.get("active_chatgpt") or 0) < (team.get("seats_entitled") or 0)


def _format_team_line(team: dict) -> str:
    name = team.get("name") or team.get("team_id") or "?"
    active = team.get("active_chatgpt", 0) or 0
    seats = team.get("seats_entitled", 0) or 0
    codex = "开启 ✅" if team.get("codex_enabled") else "关闭 ⏸️"
    risk = team.get("risk", "ok")
    icon = {"ok": "🟢", "watch": "🟡", "over": "🔴"}.get(risk, "")
    risk_label = {"ok": "正常", "watch": "观察", "over": "超员"}.get(risk, risk)
    return detail_card(
        f"{icon} {name}",
        (
            f"💺 GPT 席位：{active} / {seats}",
            f"💻 Codex：{codex}",
            f"🧭 风险状态：{risk_label}",
        ),
    )


def _render_status(data: dict, filt: str) -> str:
    teams = data.get("teams") or []
    if filt == "idle":
        teams = [t for t in teams if _is_idle(t)]
    elif filt == "busy":
        teams = [t for t in teams if not _is_idle(t)]

    if not teams:
        return f"没有符合条件（{filt}）的 Team。"

    filter_label = {"all": "全部", "idle": "有空位", "busy": "已满"}[filt]
    patrol_state = "开启 ✅" if data.get("kick_enabled") else "关闭 ⏸️"
    summary = overview_panel(
        (
            f"🏢 当前筛选：{filter_label}",
            f"📋 Team 数量：{len(teams)}",
            f"🛡️ 自动巡逻：{patrol_state}",
        )
    )
    cards = [_format_team_line(team) for team in teams]
    return "\n\n".join(("📊 TeamBoss 状态", summary, *cards))


def _status_verify_worker(chat_id: str, message_id: int, filt: str, cached_text: str) -> None:
    stamp = datetime.now().strftime("%H:%M")
    try:
        data = _api_get("/api/patrol/status", params={"refresh": "true"}, timeout=120)
        fresh_text = _render_status(data, filt)
        _edit_message(chat_id, message_id, fresh_text + f"\n\n✅ 已核对官方 · {stamp}")
    except Exception:
        logger.warning("tg status verify worker failed", exc_info=False)
        _edit_message(chat_id, message_id, cached_text + f"\n\n⚠️ 官方核对失败，以上为缓存 · {stamp}")


def _start_status(user: dict, chat_id: str, args: str) -> None:
    """状态查询：缓存秒发 + 官方实时核对后原地编辑。"""
    filt = (args or "").strip().lower() or "all"
    if filt not in ("all", "idle", "busy"):
        filt = "all"

    try:
        data = _api_get("/api/patrol/status")
    except Exception as exc:
        _send(chat_id, f"获取状态失败：{exc}")
        return

    cached_text = _render_status(data, filt)
    text = cached_text + "\n\n⏳ 系统已请求 API 核对……"
    message_id = _send_returning_id(chat_id, text)
    if message_id is None:
        return  # 连缓存都没发出去，放弃后续核对

    threading.Thread(
        target=_status_verify_worker,
        args=(chat_id, message_id, filt, cached_text),
        name="tg-status-verify",
        daemon=True,
    ).start()


def cmd_watch(user: dict, args: str) -> str:
    try:
        data = _api_get("/api/patrol/status")
    except Exception as exc:
        return f"获取状态失败：{exc}"

    teams = [t for t in (data.get("teams") or []) if t.get("risk") in ("watch", "over")]
    if not teams:
        return "✅ 风险巡检\n\n当前没有观察或超员 Team。"

    teams.sort(key=lambda team: 0 if team.get("risk") == "over" else 1)
    watch_count = sum(1 for team in teams if team.get("risk") == "watch")
    over_count = len(teams) - watch_count
    summary = overview_panel(
        (
            f"📋 风险 Team：{len(teams)}",
            f"🔴 超员：{over_count}",
            f"🟡 观察：{watch_count}",
        )
    )
    cards: list[str] = []
    for t in teams:
        name = t.get("name") or t.get("team_id") or "?"
        active = t.get("active_chatgpt", 0) or 0
        seats = t.get("seats_entitled", 0) or 0
        risk = t.get("risk") or "watch"
        icon = "🔴" if risk == "over" else "🟡"
        risk_label = "超员" if risk == "over" else "观察"
        rows = [
            f"💺 GPT 席位：{active} / {seats}",
            f"💻 Codex：{'开启 ✅' if t.get('codex_enabled') else '关闭 ⏸️'}",
            f"🧭 风险状态：{risk_label}",
        ]
        if risk == "over":
            for cand in t.get("detected_over") or []:
                email = cand.get("email") or "未知成员"
                seat_type = seat_type_label(cand.get("seat_type"))
                rows.append(f"👤 待处理：{email} · {seat_type}")
        cards.append(detail_card(f"{icon} {name}", rows))
    return "\n\n".join(("⚠️ 风险 Team", summary, *cards))


def cmd_billing(user: dict, args: str) -> str:
    try:
        data = _api_get("/api/finance/overview")
    except Exception as exc:
        return f"获取预计月支出失败：{exc}"

    base = data.get("base_currency") or ""
    total = data.get("monthly_total_base")
    total_str = f"{total:.2f}" if isinstance(total, (int, float)) else "未知"
    teams = sorted(
        data.get("teams") or [],
        key=lambda t: (t.get("monthly_total_base") or 0),
        reverse=True,
    )
    billable = [t for t in teams if (t.get("monthly_total_base") or 0) > 0]
    listed = billable[:20]
    alerts = data.get("alerts") or []
    summary = overview_panel(
        (
            f"💰 月度总支出：{total_str} {base}".rstrip(),
            f"🏢 计费 Team：{len(billable)}",
            f"🚨 财务告警：{len(alerts)}",
        )
    )
    sections = ["💳 预计月支出", summary]

    if listed:
        spend_rows = []
        for index, team in enumerate(listed, 1):
            name = team.get("name") or team.get("team_id") or "未知 Team"
            amount = team.get("monthly_total_base") or 0
            spend_rows.append(f"{index}. {name}  ·  {amount:.2f} {base}".rstrip())
        if len(billable) > len(listed):
            spend_rows.append(f"…另有 {len(billable) - len(listed)} 个 Team，请到后台查看。")
        sections.append("🏷️ Team 支出\n" + "\n".join(spend_rows))
    else:
        sections.append("📭 暂无计费 Team")

    if alerts:
        alert_labels = {
            "low_balance": "Credit 不足",
            "discount_expiring": "折扣到期",
            "token_expired": "Token 过期",
            "subscription_expired": "订阅到期",
        }
        alert_cards = []
        for alert in alerts[:5]:
            alert_type = alert.get("type") or "unknown"
            label = alert_labels.get(alert_type, alert_type)
            team_name = alert.get("team_name") or "未知 Team"
            alert_cards.append(
                detail_card(
                    f"🔸 {team_name}",
                    (f"🏷️ 类型：{label}", f"📝 详情：{alert.get('detail') or '无'}"),
                )
            )
        if len(alerts) > 5:
            alert_cards.append(f"…另有 {len(alerts) - 5} 条告警，请到后台查看。")
        sections.append("🚨 财务告警\n\n" + "\n\n".join(alert_cards))
    else:
        sections.append("✅ 当前无财务告警")
    return "\n\n".join(sections)


def _logs_reply(args: str, *, scope: Optional[str] = None, title: str) -> str:
    q = (args or "").strip()
    params: dict = {"per_page": 10, "page": 1}
    if scope:
        params["scope"] = scope
    if q:
        params["q"] = q
    try:
        data = _api_get("/api/logs", params=params)
    except Exception as exc:
        return f"获取日志失败：{exc}"

    logs = data.get("logs") or []
    if not logs:
        return "没有匹配的日志。"

    cards = []
    for log in logs:
        ts = log.get("created_at") or "时间未知"
        team = log.get("team_name") or log.get("team_id") or "系统"
        action = log.get("action") or "未知操作"
        target = log.get("target_email") or "—"
        result = log.get("result") or "未知"
        result_icon = "✅" if result == "success" else "⚠️" if result == "dryrun" else "❌"
        cards.append(
            detail_card(
                f"🕐 {ts}",
                (
                    f"🏢 Team：{team}",
                    f"⚙️ 操作：{action}",
                    f"👤 对象：{target}",
                    f"{result_icon} 结果：{result}",
                ),
            )
        )
    query_note = f" · 关键词「{q}」" if q else ""
    return "\n\n".join((f"🧾 {title} · {len(logs)} 条{query_note}", *cards))


def cmd_logs(user: dict, args: str) -> str:
    return _logs_reply(args, title="最近操作日志")


def cmd_member_logs(user: dict, args: str) -> str:
    return _logs_reply(args, scope="members", title="最近人员日志")


def cmd_team(user: dict, args: str) -> str:
    name_q = (args or "").strip()
    if not name_q:
        return "用法：/team <Team 名关键字>"
    needle = name_q.lower()

    try:
        data = _api_get("/api/patrol/status")
    except Exception as exc:
        return f"获取状态失败：{exc}"

    teams = [t for t in (data.get("teams") or []) if needle in (t.get("name") or "").lower()]
    if not teams:
        return f"没有找到名字包含「{name_q}」的 Team。"

    try:
        finance_data = _api_get("/api/finance/overview")
        finance_base_currency = finance_data.get("base_currency") or ""
        finance_by_id = {
            ft.get("team_id"): ft for ft in (finance_data.get("teams") or [])
        }
    except Exception:
        finance_base_currency = ""
        finance_by_id = {}

    cards: list[str] = []
    for t in teams[:5]:
        name = t.get("name") or t.get("team_id") or "?"
        active = t.get("active_chatgpt", 0) or 0
        seats = t.get("seats_entitled", 0) or 0
        risk = t.get("risk") or "ok"
        risk_icon = {"ok": "🟢", "watch": "🟡", "over": "🔴"}.get(risk, "⚪")
        risk_label = {"ok": "正常", "watch": "观察", "over": "超员"}.get(risk, risk)
        rows = [
            f"💺 GPT 席位：{active} / {seats}",
            f"💻 Codex：{'开启 ✅' if t.get('codex_enabled') else '关闭 ⏸️'}",
            f"🧭 风险状态：{risk_label}",
        ]
        if t.get("risk") == "over":
            for cand in t.get("detected_over") or []:
                rows.append(f"👤 待处理：{cand.get('email') or '未知成员'}")
        fin = finance_by_id.get(t.get("team_id")) if finance_by_id else None
        if fin:
            currency = fin.get("billing_currency") or ""
            subscription_label = {
                "renewing": "正常续费",
                "nonrenewing": "到期不续费",
                "expired": "已到期",
                "stale": "数据未同步",
            }.get(fin.get("subscription_status"), "未知")
            rows.extend((
                f"💳 Credit：{fin.get('balance') if fin.get('balance') is not None else '未知'} {currency}".rstrip(),
                f"📅 到期：{fin.get('active_until') or '未知'}",
                f"🔁 订阅：{subscription_label}",
                f"💰 月费：{fin.get('monthly_total_base') if fin.get('monthly_total_base') is not None else '未知'} {finance_base_currency}".rstrip(),
            ))
        cards.append(detail_card(f"{risk_icon} {name}", rows))

    suffix = " · 仅显示前 5 个" if len(teams) > 5 else ""
    return "\n\n".join((f"🔎 Team 查询 · {len(teams)} 个{suffix}", *cards))


def cmd_patrol(user: dict, args: str) -> str:
    sub = (args or "").strip().lower()
    if sub == "on":
        try:
            _api_post("/api/patrol/activate", timeout=180)
        except Exception as exc:
            return f"操作失败：{exc}"
        return "✅ 已豁免当前成员并开启巡逻自动踢人。"
    if sub == "off":
        try:
            _api_patch("/api/patrol/settings", {"kick_enabled": False})
        except Exception as exc:
            return f"操作失败：{exc}"
        return "✅ 巡逻自动踢人已关闭。"

    # 默认 / status
    try:
        data = _api_get("/api/patrol/status")
    except Exception as exc:
        return f"获取状态失败：{exc}"
    kick = "开启 ✅" if data.get("kick_enabled") else "关闭 ⏸️"
    baseline = data.get("baseline_at") or "未设置"
    exempt_n = len(data.get("exempt_team_ids") or [])
    return "\n\n".join((
        "🛡️ 巡逻状态",
        overview_panel(
            (
                f"🤖 自动踢人：{kick}",
                f"🕐 最近保护：{baseline}",
                f"🛡️ 豁免 Team：{exempt_n}",
            )
        ),
    ))


def cmd_token(user: dict, args: str) -> str:
    """生成一次性兑换码。用法: /token <天数>，如 /token 30"""
    days_str = (args or "").strip()
    if not days_str:
        return "用法: /token <天数>，例如 /token 30 生成有效期 30 天的兑换码"

    if not days_str.isdigit():
        return "❌ 天数必须是正整数。例如 /token 30"

    days = int(days_str)
    if days <= 0 or days > 36500:
        return "❌ 天数应在 1-36500 之间。"

    duration = f"{days}d"
    try:
        result = _api_post(
            "/api/access-tokens",
            {
                "grant_expires_in": duration,
                "token_ttl": "7d",
                "note": None,
            },
        )
        token = result.get("token")
        if not token:
            return "❌ 生成兑换码失败：没有返回 token"

        return detail_card(
            "✅ 兑换码已生成",
            (
                f"🔑 Code：{token}",
                f"⏳ 有效期：{days} 天",
                f"📋 Code 前缀：{result.get('token_prefix') or '?'}",
            ),
        ) + "\n\n💡 可复制上述 Code 转发给成员使用。"
    except Exception as exc:
        return f"❌ 生成兑换码失败：{_extract_api_error(exc)}"


_HELP_ADMIN = (
    "🤖 TeamBoss 管理助手\n\n"
    "📊 查询与监控\n"
    "├ /status [all|idle|busy] · Team 状态\n"
    "├ /watch · 风险与超员\n"
    "├ /team <名字> · Team 详情\n"
    "├ /billing · 预计月支出概览\n"
    "└ /logs [关键词] · 操作日志\n\n"
    "👥 成员与车主\n"
    "├ /info <邮箱或关键词> · 成员详情\n"
    "├ /members · 成员列表\n"
    "├ /owners · 车主列表\n"
    "└ /m_logs [关键词] · 人员日志\n\n"
    "⚙️ 管理操作\n"
    "├ /invite <邮箱> · 邀请成员\n"
    "├ /kick · 移除成员\n"
    "├ /token <天数> · 生成兑换码\n"
    "├ /patrol on|off|status · 自动巡逻\n"
    "└ /pair <配对码> · 绑定身份\n\n"
    "💡 向导中发送 /q 或 q 可取消操作。"
)
_HELP_MEMBER = (
    "👤 TeamBoss 成员助手\n\n"
    "├ /info · 查询我的成员状态、服务到期与系统宽限\n"
    "├ /pair <配对码> · 继续绑定其他邮箱\n"
    "└ /help · 查看本帮助"
)
_HELP_PUBLIC = (
    "👋 欢迎使用 TeamBoss\n\n"
    "当前 Telegram 尚未绑定身份。\n\n"
    "├ /pair <配对码> · 绑定管理员或成员身份\n"
    "├ /start <配对码> · 使用配对码开始\n"
    "└ /help · 查看本帮助"
)


def cmd_help(user: dict, args: str) -> str:
    return _HELP_ADMIN


# ---------------------------------------------------------------------------
# /invite 向导
# ---------------------------------------------------------------------------

def _start_invite(user: dict, chat_id: str, args: str) -> str:
    """启动拉人向导。"""
    email = args.strip().lower()
    if not email or not EMAIL_RE.match(email):
        return "用法:/invite <邮箱>,例如 /invite bob@example.com"

    try:
        data = _api_get("/api/patrol/status")
    except Exception as exc:
        return f"获取 Team 失败:{exc}"

    teams = data.get("teams") or []
    teams = _sorted_teams(teams)
    if not teams:
        return "当前没有在用 Team。"

    _wizards[chat_id] = {"flow": "invite", "step": "pick_team", "email": email, "teams": teams}
    w = _wizards[chat_id]
    _touch(w)
    return (
        f"✉️ 邀请成员\n"
        f"└ 👤 {email}\n\n"
        f"🏢 请选择目标 Team（回复序号）\n"
        f"{_team_lines(teams)}\n\n"
        f"{_WIZ_CANCEL_HINT}"
    )


def _team_overage_policy(team_id: Optional[str]) -> str:
    """现读 Team 的超员策略（只用来决定向导怎么问；真正把关在服务端邀请接口）。

    读不到按默认的「超员需确认」：满了就先问，不会替管理员默认加购。
    """
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            row = conn.execute(
                "SELECT overage_policy FROM teams WHERE id = ?", (str(team_id or ""),)
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return normalize_overage_policy(None)
    return normalize_overage_policy(row[0] if row else None)


def _team_cached_full(team: dict) -> bool:
    active = team.get("active_chatgpt", 0) or 0
    seats = team.get("seats_entitled", 0) or 0
    return active >= seats


def _invite_forbidden_text(team: dict) -> Optional[str]:
    """Team 设为禁止超员且（按缓存）已满时的拒绝文案；否则 None。"""
    if not _team_cached_full(team):
        return None
    if _team_overage_policy(team.get("team_id")) != "forbid":
        return None
    # 延迟导入：本模块不在导入期依赖业务 service（见模块说明）。文案和服务端 409 同一个正本。
    from .services.overage_policy import refusal_message

    message = refusal_message(
        policy="forbid", team_name=team.get("name") or "?", seat_type="default"
    )
    return f"⛔ 未邀请\n\n{message}"


def _step_invite(chat_id: str, w: dict, text: str) -> Optional[str]:
    """向导步骤处理。"""
    text = text.strip()
    step = w.get("step")

    if step == "pick_team":
        teams = w.get("teams", [])
        try:
            idx = int(text) - 1
            if idx < 0 or idx >= len(teams):
                raise ValueError()
        except (ValueError, TypeError):
            _reset_wizard(chat_id)
            return _WIZ_INVALID

        w["team"] = teams[idx]
        refusal = _invite_forbidden_text(w["team"])
        if refusal:
            _reset_wizard(chat_id)
            return refusal
        w["step"] = "input_time"
        _touch(w)
        return (
            "✅ 已选择 Team\n"
            f"└ 🏢 {w['team'].get('name', '?')}\n\n"
            "⏳ 请输入有效期\n"
            "例如：30d / 12h / 7d / never\n\n"
            f"{_WIZ_CANCEL_HINT}"
        )

    elif step == "input_time":
        dur = _normalize_duration(text)
        if dur is None:
            _reset_wizard(chat_id)
            return _WIZ_INVALID

        w["expires_in"] = dur
        w["step"] = "confirm"
        _touch(w)

        team = w.get("team", {})
        name = team.get("name", "?")
        email = w.get("email", "?")
        refusal = _invite_forbidden_text(team)
        if refusal:
            _reset_wizard(chat_id)
            return refusal
        is_full = _team_cached_full(team)
        policy = _team_overage_policy(team.get("team_id"))
        # 卡片上提示了会加购扣费、管理员回 1，就算确认了超员（allow_overage）。
        w["allow_overage"] = is_full

        confirm_msg = detail_card(
            "📋 请确认邀请信息",
            (
                f"🏢 Team：{name}",
                f"👤 邮箱：{email}",
                f"⏳ 有效期：{dur}",
            ),
        )
        if is_full:
            confirm_msg += (
                f"\n\n⚠️ 该 Team 的 ChatGPT 席位已满（{OVERAGE_POLICY_LABELS.get(policy, policy)}）："
                "继续会让 ChatGPT 自动加购 1 个 ChatGPT 席位并扣费。"
            )
        confirm_msg += "\n\n回复 1 确认 · 回复 0 取消"
        return confirm_msg

    elif step == "confirm":
        if text == "0":
            _reset_wizard(chat_id)
            return "已取消。"
        elif text == "1":
            team = w.get("team", {})
            team_id = team.get("team_id")
            name = team.get("name", "?")
            email = w.get("email", "?")
            expires_in = w.get("expires_in", "never")
            refusal = _invite_forbidden_text(team)
            if refusal:
                _reset_wizard(chat_id)
                return refusal
            allow_overage = bool(w.get("allow_overage", _team_cached_full(team)))

            _reset_wizard(chat_id)
            msg_id = _send_returning_id(chat_id, f"⏳ 系统已请求 API 发送邀请……")
            threading.Thread(
                target=_invite_worker,
                args=(chat_id, msg_id, team_id, email, expires_in, allow_overage, name),
                name="tg-invite",
                daemon=True,
            ).start()
            return None
        else:
            _reset_wizard(chat_id)
            return _WIZ_INVALID

    _reset_wizard(chat_id)
    return _WIZ_INVALID


def _update_watch_tg_info(team_id: str, target_email: str, reason: str,
                         chat_id: str, message_id: int) -> None:
    """将 TG 消息 ID 写入最近的 member_watch 记录，供 watch job 编辑回复。"""
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            conn.execute("""
                UPDATE member_watch
                SET tg_chat_id = ?, tg_message_id = ?
                WHERE team_id = ? AND target_email = ? AND reason = ? AND done = 0
                  AND tg_chat_id IS NULL
            """, (chat_id, message_id, team_id, target_email.lower(), reason))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("failed to update watch tg info", exc_info=False)


def _api_error_detail(exc: Exception) -> Optional[dict]:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        detail = resp.json().get("detail")
    except Exception:
        return None
    return detail if isinstance(detail, dict) else None


def _overage_confirmation_prompt(exc, chat_id, team_id, email, expires_in, allow_overage, name) -> Optional[str]:
    """缓存说没满、服务端现拉发现满了（超员需确认）：转成和满员时一样的确认步骤。

    只在这个 chat 当前没有别的向导时挂上确认步骤（setdefault 不覆盖管理员新开的操作）；
    回 1 后带 allow_overage 重新调用邀请接口。其他错误返回 None，照常显示失败。
    """
    from .services.overage_policy import CODE_NEEDS_CONFIRMATION

    detail = _api_error_detail(exc)
    if not detail or detail.get("code") != CODE_NEEDS_CONFIRMATION or allow_overage:
        return None
    message = str(detail.get("message") or "该 Team 已满，继续会让 ChatGPT 自动加购并扣费。")
    state = {
        "flow": "invite",
        "step": "confirm",
        "email": email,
        "team": {"team_id": team_id, "name": name},
        "expires_in": expires_in,
        "allow_overage": True,
    }
    _touch(state)
    if _wizards.setdefault(str(chat_id), state) is not state:
        return detail_card(
            "⚠️ 未邀请：需要确认超员",
            (f"👤 成员：{email}", f"🏢 Team：{name}", f"📝 {message}"),
        ) + "\n\n你正在进行别的操作；如确认加购，请重新发送 /invite。"
    return detail_card(
        "⚠️ 需要确认超员",
        (f"👤 成员：{email}", f"🏢 Team：{name}", f"⏳ 有效期：{expires_in}", f"📝 {message}"),
    ) + "\n\n回复 1 确认加购 · 回复 0 取消"


def _invite_worker(chat_id, msg_id, team_id, email, expires_in, allow_overage, name) -> None:
    try:
        resp = _api_post(
            f"/api/teams/{team_id}/members/invite",
            {
                "email": email,
                "seat_type": "default",
                "expires_in": expires_in,
                "allow_overage": allow_overage,
            },
        )
        resp = resp if isinstance(resp, dict) else {}
        # 有效期以服务端合并后实际落库的为准（可能保留了更晚的到期或永久授权），
        # 不是这次填写的值。
        shown_expiry = resp.get("expiry_display") or expires_in
        text = detail_card(
            "✅ 邀请已重发" if resp.get("resent") else "✅ 邀请已发送",
            (f"👤 成员：{email}", f"🏢 Team：{name}", f"⏳ 有效期：{shown_expiry}"),
        ) + "\n\n⏳ 正在等待成员接受……"
        if msg_id is not None:
            _update_watch_tg_info(team_id, email, "invite", chat_id, msg_id)
    except Exception as e:
        text = _overage_confirmation_prompt(e, chat_id, team_id, email, expires_in, allow_overage, name)
        if text is None:
            text = detail_card("❌ 邀请失败", (f"👤 成员：{email}", f"📝 原因：{_extract_api_error(e)}"))

    if msg_id is None:
        logger.warning("tg invite worker: no message_id to edit, result=%s", text)
        return
    _edit_message(chat_id, msg_id, text)


# ---------------------------------------------------------------------------
# /kick 向导（重写：从成员列表直接踢人+搜索）
# ---------------------------------------------------------------------------

def _start_kick(user: dict, chat_id: str, args: str) -> str:
    """启动踢人向导。"""
    try:
        data = _api_get("/api/users/members", params={"status": "joined"})
    except Exception as exc:
        return f"获取成员失败:{exc}"

    items = data.get("items") or []
    candidates = [m for m in items if (m.get("actions") or {}).get("kick")]
    if not candidates:
        return "当前没有可踢成员。"

    shown = candidates[:_LIST_CAP]
    _wizards[chat_id] = {"flow": "kick", "step": "pick", "candidates": shown}
    w = _wizards[chat_id]
    _touch(w)

    return (
        "🥾 移除成员\n\n"
        "回复序号选择，或输入关键词搜索：\n"
        + _numbered_lines(shown, _member_label)
        + _trunc_note(len(candidates))
        + f"\n\n{_WIZ_CANCEL_HINT}"
    )


def _step_kick(chat_id: str, w: dict, text: str) -> Optional[str]:
    """向导步骤处理。"""
    text = text.strip()
    step = w.get("step")

    if step == "pick":
        if text.isdigit():
            idx = int(text) - 1
            cand = w.get("candidates", [])
            if 0 <= idx < len(cand):
                target = cand[idx]
                w["target"] = target
                w["step"] = "confirm"
                _touch(w)
                return detail_card(
                    "⚠️ 请确认移除成员",
                    (
                        f"👤 成员：{_member_email(target)}",
                        f"🏢 Team：{target.get('team_name') or '?'}",
                    ),
                ) + "\n\n回复 1 确认 · 回复 0 取消"
            else:
                _reset_wizard(chat_id)
                return _WIZ_INVALID
        else:
            # 关键词搜索，不重置
            q = text
            try:
                data = _api_get("/api/users/members", params={"status": "joined", "q": q})
            except Exception as exc:
                _reset_wizard(chat_id)
                return f"查询失败:{_extract_api_error(exc)}"

            items = data.get("items") or []
            cand = [m for m in items if (m.get("actions") or {}).get("kick")]
            if not cand:
                return f"🔍 没有匹配「{q}」的成员。\n\n请更换关键词，或回复 q 取消。"

            shown = cand[:_LIST_CAP]
            w["candidates"] = shown
            _touch(w)
            return (
                f"🔍「{q}」匹配 {len(cand)} 人\n\n"
                + _numbered_lines(shown, _member_label)
                + _trunc_note(len(cand))
                + "\n\n回复序号选择 · 输入关键词重搜 · 回复 q 取消"
            )

    elif step == "confirm":
        if text == "1":
            _reset_wizard(chat_id)
            target = w.get("target", {})
            path = (target.get("actions") or {}).get("kick")
            if not path:
                return "❌ 该成员无踢人入口,已重置。"

            email = _member_email(target)
            team_name = target.get("team_name") or "?"
            tid = target.get("team_id")
            msg_id = _send_returning_id(chat_id, f"⏳ 系统已请求 API 踢人……")
            threading.Thread(
                target=_kick_worker,
                args=(chat_id, msg_id, path, email, team_name, tid),
                name="tg-kick",
                daemon=True,
            ).start()
            return None
        elif text == "0":
            _reset_wizard(chat_id)
            return "已取消。"
        else:
            _reset_wizard(chat_id)
            return _WIZ_INVALID

    _reset_wizard(chat_id)
    return _WIZ_INVALID


def _kick_worker(chat_id, msg_id, path, email, team_name, team_id=None) -> None:
    try:
        _api_delete(path)
        text = detail_card(
            "✅ 移除请求已提交",
            (f"👤 成员：{email}", f"🏢 Team：{team_name}"),
        ) + "\n\n⏳ 正在等待官方确认……"
        if msg_id is not None and team_id:
            _update_watch_tg_info(team_id, email, "kick", chat_id, msg_id)
    except Exception as e:
        text = detail_card("❌ 移除成员失败", (f"👤 成员：{email}", f"📝 原因：{_extract_api_error(e)}"))

    if msg_id is None:
        logger.warning("tg kick worker: no message_id to edit, result=%s", text)
        return
    _edit_message(chat_id, msg_id, text)


# ---------------------------------------------------------------------------
# /members 向导（仅管理员）：缓存列表秒发 + 官方全量核对后编辑
# ---------------------------------------------------------------------------

def _member_list_render(items, cached):
    tag = " · 缓存" if cached else ""
    if not items:
        return "👥 成员列表\n\n当前没有已加入成员。"
    shown = items[:_LIST_CAP]
    return (
        f"👥 已加入成员 · {len(items)} 人{tag}\n\n"
        + _numbered_lines(shown, _member_label)
        + _trunc_note(len(items))
        + "\n\n🔍 输入关键词查询 · 回复 q 取消"
    )


def _member_query_render(q):
    def render(items, cached):
        tag = " · 缓存" if cached else ""
        if not items:
            return f"🔍 成员搜索\n\n没有匹配「{q}」的成员。"
        shown = items[:_LIST_CAP]
        return (
            f"🔍「{q}」匹配 {len(items)} 人{tag}\n\n"
            + _numbered_lines(shown, _member_label)
            + _trunc_note(len(items))
        )
    return render


def _start_members(user: dict, chat_id: str, args: str) -> None:
    """启动成员列表查询：缓存秒发 + 官方全量核对编辑。"""
    try:
        data = _api_get("/api/users/members", params={"status": "joined"})
    except Exception as exc:
        _send(chat_id, f"获取成员失败:{exc}")
        return

    items = data.get("items") or []
    if not items:
        _send(chat_id, "当前没有成员。")
        return

    _wizards[chat_id] = {"flow": "members_query", "step": "query"}
    _touch(_wizards[chat_id])
    _send_list_with_verify(
        chat_id, "/api/users/members", {"status": "joined"}, items, _member_list_render, full_scan=True
    )


def _step_members_query(chat_id: str, w: dict, text: str) -> None:
    """成员列表查询（一次性）：缓存秒发 + 官方定向核对编辑。"""
    q = text.strip()
    try:
        data = _api_get("/api/users/members", params={"status": "joined", "q": q})
    except Exception as exc:
        _reset_wizard(chat_id)
        _send(chat_id, f"查询失败:{_extract_api_error(exc)}")
        return

    items = data.get("items") or []
    _reset_wizard(chat_id)
    if not items:
        _send(chat_id, f"没有匹配「{q}」的成员。")
        return

    _send_list_with_verify(
        chat_id, "/api/users/members", {"status": "joined", "q": q}, items, _member_query_render(q), full_scan=False
    )


# ---------------------------------------------------------------------------
# /owners 向导（仅管理员）：缓存列表秒发 + 官方全量核对后编辑
# ---------------------------------------------------------------------------

def _owner_list_render(items, cached):
    tag = " · 缓存" if cached else ""
    if not items:
        return "👑 车主列表\n\n当前没有车主。"
    shown = items[:_LIST_CAP]
    return (
        f"👑 车主列表 · {len(items)} 人{tag}\n\n"
        + _numbered_lines(shown, _member_label)
        + _trunc_note(len(items))
        + "\n\n🔍 输入关键词查询 · 回复 q 取消"
    )


def _owner_query_render(q):
    def render(items, cached):
        tag = " · 缓存" if cached else ""
        if not items:
            return f"🔍 车主搜索\n\n没有匹配「{q}」的车主。"
        shown = items[:_LIST_CAP]
        return (
            f"🔍「{q}」匹配 {len(items)} 人{tag}\n\n"
            + _numbered_lines(shown, _member_label)
            + _trunc_note(len(items))
        )
    return render


def _start_owners(user: dict, chat_id: str, args: str) -> None:
    """启动车主列表查询：缓存秒发 + 官方全量核对编辑。"""
    try:
        data = _api_get("/api/users/owners", params={})
    except Exception as exc:
        _send(chat_id, f"获取车主失败:{exc}")
        return

    items = data.get("items") or []
    if not items:
        _send(chat_id, "当前没有车主。")
        return

    _wizards[chat_id] = {"flow": "owners_query", "step": "query"}
    _touch(_wizards[chat_id])
    _send_list_with_verify(
        chat_id, "/api/users/owners", {}, items, _owner_list_render, full_scan=True
    )


def _step_owners_query(chat_id: str, w: dict, text: str) -> None:
    """车主列表查询（一次性）：缓存秒发 + 官方全量核对编辑（owners 接口无 team_id）。"""
    q = text.strip()
    try:
        data = _api_get("/api/users/owners", params={"q": q})
    except Exception as exc:
        _reset_wizard(chat_id)
        _send(chat_id, f"查询失败:{_extract_api_error(exc)}")
        return

    items = data.get("items") or []
    _reset_wizard(chat_id)
    if not items:
        _send(chat_id, f"没有匹配「{q}」的车主。")
        return

    _send_list_with_verify(
        chat_id, "/api/users/owners", {"q": q}, items, _owner_query_render(q), full_scan=True
    )


# ---------------------------------------------------------------------------
# /info 查人（公共）：缓存秒发 + 官方实时核对后原地编辑
# ---------------------------------------------------------------------------

_INFO_SHOW_CAP = 10
_INFO_DISPLAY_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def _info_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _info_local(value: Optional[datetime]) -> str:
    if value is None:
        return "未知"
    return value.astimezone(_INFO_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


def _member_expiry_lines(expiry: dict, *, now: Optional[datetime] = None) -> list[str]:
    expires_at = _info_datetime(expiry.get("expires_at"))
    effective_kick_at = _info_datetime(expiry.get("effective_kick_at"))
    if expires_at is None:
        return ["📅 服务到期：永不"]

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lines = [f"📅 服务到期：{_info_local(expires_at)}（北京时间）"]
    if effective_kick_at is None or effective_kick_at <= expires_at:
        lines.append("⏱️ 系统宽限：无")
        return lines

    if expires_at <= now < effective_kick_at:
        lines.extend(
            (
                "⚠️ 到期状态：处于系统宽限期",
                f"🗑️ 预计移除：{_info_local(effective_kick_at)}（北京时间）",
                "ℹ️ 宽限期是系统缓冲，不计入购买时长",
            )
        )
        return lines
    if now >= effective_kick_at:
        lines.extend(
            (
                "🚨 到期状态：已到移除时间，等待系统处理",
                "ℹ️ 宽限期不计入购买时长",
            )
        )
        return lines

    delta = effective_kick_at - expires_at
    hours = delta.total_seconds() / 3600
    if hours.is_integer():
        grace_label = f"到期后 {int(hours)} 小时"
    else:
        grace_label = f"至 {_info_local(effective_kick_at)}"
    lines.append(f"⏱️ 系统宽限：{grace_label}（不计入购买时长）")
    return lines


def _info_blocks(items: list) -> str:
    """渲染成员信息块（最多前 10 条），服务到期与系统宽限分开显示。"""
    blocks = []
    for m in items[:_INFO_SHOW_CAP]:
        exp = m.get("expiry") or {}
        label = m.get("status_label") or m.get("status") or ""
        owner = _mask_owner_email(m.get("owner_email") or "")
        status = m.get("status") or ""
        icon = {"joined": "🟢", "pending": "🟡"}.get(status, "⚪")
        rows = [
            f"🏢 Team：{m.get('team_name') or '?'}",
            f"👑 车主：{owner}",
            f"💺 席位：{seat_type_label(m.get('seat_type'))}",
            *_member_expiry_lines(exp),
        ]
        blocks.append(detail_card(f"{icon} {_member_email(m)} · {label}", rows))
    return "\n\n".join(blocks)


def _info_header(q: str, count: int, *, cached: bool) -> str:
    tag = " · 缓存" if cached else ""
    more = " · 仅显示前 10 条" if count > _INFO_SHOW_CAP else ""
    return f"🔎「{q}」匹配 {count} 条{tag}{more}\n\n"


def _info_render(q):
    def render(items, cached):
        if not items:
            return f"🔎 成员搜索\n\n没有在册且匹配「{q}」的成员。"
        return _info_header(q, len(items), cached=cached) + _info_blocks(items)
    return render


def _member_info_items(emails: list[str]) -> list[dict]:
    items: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for email in emails:
        normalized = normalize_member_email(email)
        data = _api_get("/api/users/members", params={"q": normalized})
        exact = [
            item
            for item in (data.get("items") or [])
            if normalize_member_email(item.get("email")) == normalized
            and item.get("status") in {"joined", "pending"}
        ]
        for item in exact:
            key = (
                str(item.get("team_id") or ""),
                normalize_member_email(item.get("email")),
                str(item.get("status") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return items


def _handle_info(user: Optional[dict], chat_id: str, args: str, member_emails: list[str]) -> None:
    """Administrators may search globally; members may only query themselves."""
    q = (args or "").strip()
    is_admin = bool(user)

    if q and not is_admin:
        _send(chat_id, "成员账号不能查询其他邮箱。直接发送 /info 查询自己的成员信息。")
        return

    if not q:
        if not member_emails:
            if is_admin:
                _send(chat_id, "用法:/info <邮箱或关键词>,例如 /info bob@example.com")
            else:
                _send(chat_id, "当前 Telegram 尚未绑定成员邮箱，请向管理员索取绑定指令。")
            return
        try:
            items = _member_info_items(member_emails)
        except Exception as exc:
            _send(chat_id, f"查询失败:{_extract_api_error(exc)}")
            return
        if not items:
            _send(chat_id, "绑定邮箱当前没有待接受或已加入的成员记录。")
            return
        _send(chat_id, f"👤 我的成员信息 · {len(items)} 条\n\n" + _info_blocks(items))
        return

    try:
        data = _api_get("/api/users/members", params={"q": q})
    except Exception as exc:
        _send(chat_id, f"查询失败:{_extract_api_error(exc)}")
        return

    items = data.get("items") or []
    if not items:
        _send(chat_id, f"没有找到匹配「{q}」的成员。")
        return

    _send_list_with_verify(
        chat_id, "/api/users/members", {"q": q}, items, _info_render(q), full_scan=False
    )


COMMANDS: dict[str, Callable[[dict, str], str]] = {
    "watch": cmd_watch,
    "billing": cmd_billing,
    "logs": cmd_logs,
    "m_logs": cmd_member_logs,
    "team": cmd_team,
    "patrol": cmd_patrol,
    "token": cmd_token,
    "help": cmd_help,
}


# ---------------------------------------------------------------------------
# 消息分发
# ---------------------------------------------------------------------------

def _parse_command(text: str) -> tuple[Optional[str], str]:
    text = (text or "").strip()
    if not text.startswith("/"):
        return None, ""
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:]
    if "@" in cmd:  # /help@MyBot -> help
        cmd = cmd.split("@", 1)[0]
    cmd = cmd.lower()
    args = parts[1] if len(parts) > 1 else ""
    return cmd, args


def _send(chat_id: str, text: str) -> bool:
    token = _get_setting("tg_bot_token")
    if not token or not chat_id or not text:
        return False
    return send_message_sync(chat_id, text, token=token)


def _send_returning_id(chat_id: str, text: str) -> Optional[int]:
    """发消息并返回 message_id;失败返回 None（用于随后 editMessageText 原地编辑）。"""
    token = _get_setting("tg_bot_token")
    if not token or not chat_id or not text:
        return None
    try:
        resp = requests.post(
            TG_API.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=_SEND_TIMEOUT,
        )
        if not resp.ok:
            return None
        return (resp.json().get("result") or {}).get("message_id")
    except Exception:
        return None


def _edit_message(chat_id: str, message_id: int, text: str) -> bool:
    """原地编辑已发出的消息文本;失败返回 False。"""
    token = _get_setting("tg_bot_token")
    if not token or message_id is None or not text:
        return False
    try:
        resp = requests.post(
            TG_API.format(token=token, method="editMessageText"),
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=_SEND_TIMEOUT,
        )
        return resp.ok
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 通用：缓存秒发 + 官方实时核对后原地编辑
#   render_fn(items, cached) -> str  负责渲染正文（含空态文案），页脚由本模块统一贴
#   full_scan=True 刷所有 Team（贼慢，全量列表用）；False 只刷命中 items 的 Team（查人用）
# ---------------------------------------------------------------------------

def _send_list_with_verify(chat_id, base_path, base_params, cache_items, render_fn, *, full_scan):
    cache_text = render_fn(cache_items, True) + "\n\n⏳ 正在从官方实时核对……"
    message_id = _send_returning_id(chat_id, cache_text)
    if message_id is None:
        return  # 连缓存都没发出去，放弃后续核对
    threading.Thread(
        target=_verify_worker,
        args=(chat_id, message_id, base_path, dict(base_params), cache_items, render_fn, full_scan),
        name="tg-verify",
        daemon=True,
    ).start()


def _official_refetch(base_path, base_params, cache_items, full_scan):
    """官方实时刷新，返回 (items, had_error)。"""
    if full_scan:
        try:
            data = _api_get(base_path, params={**base_params, "refresh": "true"})
        except Exception:
            return [], True
        return (data.get("items") or []), bool(data.get("errors"))

    team_ids = sorted({m.get("team_id") for m in cache_items if m.get("team_id")})
    merged: list = []
    seen: set = set()
    had_error = False
    for tid in team_ids:
        try:
            data = _api_get(base_path, params={**base_params, "team_id": tid, "refresh": "true"})
        except Exception:
            had_error = True
            continue
        if data.get("errors"):
            had_error = True
        for m in data.get("items") or []:
            key = (m.get("team_id"), (m.get("email") or "").lower(), m.get("user_id") or "")
            if key not in seen:
                seen.add(key)
                merged.append(m)
    return merged, had_error


def _verify_worker(chat_id, message_id, base_path, base_params, cache_items, render_fn, full_scan):
    try:
        merged, had_error = _official_refetch(base_path, base_params, cache_items, full_scan)
        stamp = datetime.now().strftime("%H:%M")
        if had_error:
            body = render_fn(merged or cache_items, False)
            footer = f"⚠️ 官方核对失败,以上为缓存 · {stamp}"
        else:
            body = render_fn(merged, False)
            footer = f"✅ 已核对官方 · {stamp}"
        _edit_message(chat_id, message_id, body + f"\n\n{footer}")
    except Exception:
        logger.warning("tg verify worker failed", exc_info=False)


def _handle_message(message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    chat_id = str(chat_id)

    # 管理身份只绑定个人私聊。若把群聊 chat_id 当成身份，群内任何成员都会
    # 共享同一授权上下文，配对码即使已经失效也无法撤销这次错误绑定。
    if chat.get("type") != "private":
        _send(chat_id, "为保护管理权限，本机器人仅支持个人私聊。请私聊机器人完成配对和操作。")
        return

    from_user = message.get("from") or {}
    username = from_user.get("username") or from_user.get("first_name") or ""
    text = message.get("text") or ""

    cmd, args = _parse_command(text)
    user = _find_user(chat_id)
    member_emails = _find_member_emails(chat_id)

    # 未注册用户：只允许配对和查看绑定帮助。
    if not user and not member_emails:
        if cmd in ("start", "pair"):
            _send(chat_id, _pair(chat_id, username, args) if args else _HELP_PUBLIC)
        elif cmd == "help":
            _send(chat_id, _HELP_PUBLIC)
        else:
            _send(chat_id, "未授权，请向管理员索取配对码，然后发 /pair <码> 注册")
        return

    # 已注册的操作员或成员也可以继续用新配对码绑定其他邮箱/更新权限。
    if cmd == "pair":
        _send(chat_id, _pair(chat_id, username, args))
        return

    if cmd == "start":
        _send(chat_id, "你已经完成注册，发送 /help 查看可用命令。")
        return

    # /info：管理员可按关键词全局查；成员只能查绑定到本人的邮箱。
    if cmd == "info":
        _reset_wizard(chat_id)
        try:
            _handle_info(user, chat_id, args, member_emails)
        except Exception:
            # 成员也能走到这里，异常原文不回给对方。
            logger.exception("tg /info failed")
            _send(chat_id, "查询失败，请稍后再试。")
        return

    # 只有成员身份、没有操作员权限时，不能进入任何后台命令。
    if not user:
        if cmd == "help":
            _send(chat_id, _HELP_MEMBER)
        else:
            _send(chat_id, "成员账号仅可使用 /info、/pair 和 /help。")
        return

    # 操作员注册用户分支

    # /q 或纯文本 q 取消当前向导；/cancel 仅作隐藏兼容别名
    if cmd in ("q", "cancel") or (cmd is None and text.strip().lower() == "q"):
        had = _reset_wizard(chat_id)
        _send(chat_id, "已取消当前操作。" if had else "当前没有进行中的操作。")
        return

    # /invite 拉人向导
    if cmd == "invite":
        try:
            reply = _start_invite(user, chat_id, args)
        except Exception as exc:
            _reset_wizard(chat_id)
            reply = f"操作出错:{exc}"
        _send(chat_id, reply)
        return

    # /kick 踢人向导
    if cmd == "kick":
        try:
            reply = _start_kick(user, chat_id, args)
        except Exception as exc:
            _reset_wizard(chat_id)
            reply = f"操作出错:{exc}"
        _send(chat_id, reply)
        return

    # /members 成员列表查询
    if cmd == "members":
        try:
            _start_members(user, chat_id, args)
        except Exception as exc:
            _reset_wizard(chat_id)
            _send(chat_id, f"操作出错:{exc}")
        return

    # /owners 车主列表查询
    if cmd == "owners":
        try:
            _start_owners(user, chat_id, args)
        except Exception as exc:
            _reset_wizard(chat_id)
            _send(chat_id, f"操作出错:{exc}")
        return

    # /status 状态查询：缓存秒发 + 官方实时核对后原地编辑
    if cmd == "status":
        try:
            _start_status(user, chat_id, args)
        except Exception as exc:
            _send(chat_id, f"操作出错:{exc}")
        return

    # 斜杠命令（其他已知/未知命令）
    if cmd is not None:
        handler = COMMANDS.get(cmd)
        if handler:
            # 真命令打断向导
            _reset_wizard(chat_id)
            try:
                reply = handler(user, args)
            except Exception as exc:
                reply = f"命令执行出错：{exc}"
            _send(chat_id, reply)
        else:
            # 未知斜杠命令，不重置向导
            _send(chat_id, "未知指令，发 /help 查看可用命令。")
        return

    # 纯文本（cmd is None）：若在向导中则继续向导，否则提示未知指令
    w = _get_wizard(chat_id)
    if w:
        reply = None
        try:
            flow = w.get("flow")
            if flow == "invite":
                reply = _step_invite(chat_id, w, text)
            elif flow == "kick":
                reply = _step_kick(chat_id, w, text)
            elif flow == "members_query":
                _step_members_query(chat_id, w, text)  # 自行发送(缓存+编辑)
            elif flow == "owners_query":
                _step_owners_query(chat_id, w, text)   # 自行发送(缓存+编辑)
            else:
                _reset_wizard(chat_id)
                reply = _WIZ_INVALID
        except Exception as exc:
            _reset_wizard(chat_id)
            reply = f"操作出错:{exc}"
        if reply:
            _send(chat_id, reply)
    else:
        _send(chat_id, "未知指令，发 /help 查看可用命令。")


# ---------------------------------------------------------------------------
# 长轮询循环 + 线程生命周期
# ---------------------------------------------------------------------------

def _get_updates(token: str, offset: Optional[int]) -> list:
    params: dict = {"timeout": _POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(
        TG_API.format(token=token, method="getUpdates"),
        params=params,
        timeout=_GETUPDATES_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        return []
    return data.get("result") or []


def _poll_loop(stop_event: threading.Event) -> None:
    offset: Optional[int] = None
    registered_token: Optional[str] = None
    logger.info("Telegram bot polling thread started")
    while not stop_event.is_set():
        try:
            enabled = _get_setting("tg_bot_enabled") == "1"
            token = _get_setting("tg_bot_token")
            if not enabled or not token:
                stop_event.wait(_IDLE_SLEEP)
                continue

            if token != registered_token:
                # Telegram update ids are scoped to one Bot. Reusing the previous
                # Bot's cursor can skip every update from the replacement Bot.
                offset = None
                sync_all_commands_sync(token=token)
                registered_token = token

            updates = _get_updates(token, offset)
            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = update_id + 1
                # 编辑过的消息不能当新消息处理：把向导里的“3”改成“1”会被当成一次新的
                # 确认回复（踢人确认、重放 /invite /token）。只处理原始 message；
                # edited_message / channel_post 等其它更新类型只推进 offset 后丢弃。
                message = update.get("message")
                if not message:
                    continue
                try:
                    _handle_message(message)
                except Exception as exc:
                    logger.warning(
                        "Telegram update handler failed update_id=%s type=%s",
                        update.get("update_id"),
                        type(exc).__name__,
                    )
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning(
                "Telegram getUpdates failed type=%s status=%s",
                type(exc).__name__,
                status,
            )
            stop_event.wait(_IDLE_SLEEP)
    logger.info("Telegram bot polling thread stopped")


def start_bot_thread() -> None:
    """启动轮询线程（守护线程）。幂等：已在跑则直接返回。"""
    global _thread, _stop_event
    with _lifecycle_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event = threading.Event()
        _thread = threading.Thread(
            target=_poll_loop, args=(_stop_event,), name="tg-bot-poll", daemon=True
        )
        _thread.start()


def is_bot_thread_alive() -> bool:
    with _lifecycle_lock:
        return bool(_thread is not None and _thread.is_alive())


def stop_bot_thread() -> None:
    """停止轮询线程。幂等：未启动过也安全。"""
    global _thread, _stop_event
    with _lifecycle_lock:
        if _stop_event is not None:
            _stop_event.set()
        if _thread is not None:
            _thread.join(timeout=5)
        _thread = None
        _stop_event = None
