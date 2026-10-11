import asyncio
import json
import logging
import sqlite3
import os
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .chatgpt_client import ChatGPTClient
from .chatgpt_limiter import run_chatgpt_call_sync
from .database import get_db_path
from .member_cache_service import snapshot_fetch_started_now, store_member_snapshot_sync
from .proxy_resolve import ProxyUnavailableError, resolve_proxy_url_sync
from .services.invoices import refresh_invoices_if_stale_sync
from .services.pricing import account_billing_updates, subscription_billing_updates, fetch_seat_pricing_sync
from .seat_types import is_known_seat_type, normalize_seat_type
from .services.snapshot_pages import SnapshotPageAccumulator, SnapshotPageError
from .services.seat_capacity import (
    chatgpt_count_from_seat_counts,
    member_seat_usage_from_members,
    positive_seat_count,
    seat_counts_column_updates,
    seat_type_count_from_seat_counts,
    subscription_column_updates,
)
from .services.renewal_reminders import run_renewal_idle_seat_reminders_sync
from .services.tg_member_bindings import (
    deactivate_member_binding_if_inactive_sync,
    run_member_expiry_reminders_sync,
)
from .services.tg_commands import sync_email_chat_commands_sync
from .services.tg_notify import edit_message_sync, notify_member_event_sync
from .services.team_health_alerts import (
    is_auth_error,
    report_team_failure_sync,
    report_team_recovery_sync,
)
from .services.team_locks import member_operation_claim_sync

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(
    # APScheduler 3.11 默认 misfire_grace_time=1（秒）：member_watch_job(30s 间隔)/
    # auto_kick_job(60s 间隔) 做分页网络调用、单次请求超时 60s，还排在 4 个并发许可
    # 后面，一次运行超过自己的间隔太正常了；默认值会把超期的下一次触发直接丢弃
    # （悄无声息地漏跑），而不是照常延后执行。300s 给够一到两轮排队+超时的余量，
    # 同时仍能在任务真正卡死时体现出来。max_instances/coalesce 沿用 APScheduler
    # 该版本的既有默认值（max_instances=1, coalesce=True），不在此改动。
    job_defaults={"misfire_grace_time": 300},
)
APP_LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 纯展示字段的刷新间隔。余额、卡号、Team 名、折扣、默认席位类型、单席位价格都是
# 周级慢变量，却占了每轮同步 9 个请求里的 5 个。按团队限流到这个间隔，把 chatgpt.com
# 的请求量压掉一半以上。
#
# 明确不在限流范围内、仍然每轮实时拉取的：get_subscription（seats_entitled 是 patrol
# 算 over_by 的分母）、get_seat_type_counts（codex/chatgpt 席位计数）、成员列表与待邀请
# 列表（识别 source == 'detected' 的外部拉人）。这三样是超员判定与乱拉人检测的输入，
# 拿旧值会直接削弱这个功能本身。
DISPLAY_SYNC_INTERVAL_HOURS = 6


def _display_sync_due(display_synced_at: str | None, now: datetime) -> bool:
    """展示字段是否到期需要刷新。解析不了的时间戳一律当作到期，宁可多拉一次。"""
    if not display_synced_at:
        return True
    try:
        last = datetime.fromisoformat(display_synced_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(hours=DISPLAY_SYNC_INTERVAL_HOURS)


# 连续同步失败达到这个小时数，就把这个 Team 的定时同步挂起：不再每 15 分钟
# 打一轮 chatgpt.com 请求，也不再重复播报同一条失败。上游把 token 吊销以后，
# 重试改变不了任何结果，只是每天上千次注定 401 的请求和上百条重复告警。
SYNC_FAILURE_SUSPEND_HOURS = 24

# 挂起期间的探活间隔。完全不打请求就永远发现不了上游恢复，所以保留这一根
# 最低频的探针：恢复正常的那一轮会自动解除挂起。
SYNC_SUSPENDED_PROBE_HOURS = 6


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sync_probe_due(sync_probe_at: str | None, now: datetime) -> bool:
    """挂起的 Team 这一轮要不要探活。解析不了的时间戳一律当作到期。"""
    last = _parse_ts(sync_probe_at)
    if last is None:
        return True
    return (now - last) >= timedelta(hours=SYNC_SUSPENDED_PROBE_HOURS)


def _record_team_sync_outcome(
    conn,
    team_id: str,
    *,
    ok: bool,
    now: datetime,
    failing_since: str | None,
    suspended_at: str | None,
    last_full_sync_at: str | None,
) -> str | None:
    """记录这一轮这个 Team 的同步成败，返回 'suspended' / 'resumed' / None。

    失败起点优先用已有的 ``sync_failing_since``；没有就回填 ``last_full_sync_at``
    ——那就是"最后一次全部接口都成功"的时刻，本来就是这条连续失败的起点。少了
    这一步，升级上线会把一个已经坏了半个月的 Team 的计时器归零，再白等一天。
    """
    try:
        if ok:
            if failing_since or suspended_at:
                conn.execute(
                    "UPDATE teams SET sync_failing_since = NULL, sync_suspended_at = NULL, "
                    "sync_probe_at = NULL WHERE id = ?",
                    (team_id,),
                )
                conn.commit()
                return "resumed" if suspended_at else None
            return None

        if suspended_at:
            conn.execute(
                "UPDATE teams SET sync_probe_at = ? WHERE id = ?",
                (now.isoformat(), team_id),
            )
            conn.commit()
            return None

        since = _parse_ts(failing_since) or _parse_ts(last_full_sync_at) or now
        if (now - since) >= timedelta(hours=SYNC_FAILURE_SUSPEND_HOURS):
            conn.execute(
                "UPDATE teams SET sync_failing_since = ?, sync_suspended_at = ?, "
                "sync_probe_at = ? WHERE id = ?",
                (since.isoformat(), now.isoformat(), now.isoformat(), team_id),
            )
            conn.commit()
            return "suspended"

        conn.execute(
            "UPDATE teams SET sync_failing_since = COALESCE(sync_failing_since, ?) WHERE id = ?",
            (since.isoformat(), team_id),
        )
        conn.commit()
        return None
    except sqlite3.OperationalError:
        # 迁移还没跑到的旧库：同步挂起只是节流，不该让同步流程本身失败。
        return None


def _merge_team_cached_data(
    raw: str | None,
    overview: dict,
    workspace_settings: dict,
    cached_at: str,
) -> str:
    try:
        cached_data = json.loads(raw) if raw else {}
        if not isinstance(cached_data, dict):
            cached_data = {}
    except (TypeError, ValueError):
        cached_data = {}

    cached_data.update(overview)
    cached_data["overview_cached_at"] = cached_at
    if isinstance(workspace_settings, dict) and "error" not in workspace_settings:
        cached_data["workspace_settings"] = workspace_settings
        cached_data["workspace_settings_cached_at"] = cached_at
    return json.dumps(cached_data, ensure_ascii=False)


def _get_sync_db():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _log_operation_sync(team_id, action, target_email=None, detail=None,
                        result=None, error_message=None, trigger_type="scheduler"):
    try:
        conn = _get_sync_db()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO operation_logs
               (team_id, action, target_email, detail, result, error_message, trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (team_id, action, target_email, detail, result, error_message, trigger_type, now)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _fetch_all_api_items_sync(method, *fallback_keys, limit=100, max_items=10000, require_items=False):
    """分页拉完整份成员/邀请名单，返回 ``(items, error)``。

    只有拿到完整的名单才返回 ``(items, None)``——调用方会据此判"某人不在"（反向检测、
    到期踢人的邮箱查找、踢人监视）。何时算完整只按 ``SnapshotPageAccumulator``
    （snapshot_pages 正本）：认不出的 200、条数与 total 对不上、total 中途变了、翻到上限
    还没结束，都返回 error，那是未知状态，不是空名单。上游报错页原样返回它的 error。
    成员名单（get_members）传 ``require_items=True``：拉完是空的同样是 error。
    """
    pages = SnapshotPageAccumulator(*fallback_keys, limit=limit, require_items=require_items)
    while pages.next_offset < max_items:
        data = run_chatgpt_call_sync(method, offset=pages.next_offset, limit=pages.limit)
        try:
            if pages.add(data):
                return pages.items, None
        except SnapshotPageError as exc:
            if isinstance(data, dict) and "error" in data and data["error"]:
                return None, exc.upstream_error
            return None, str(exc)
    # 翻到上限还没结束：没见到的人不能判为缺席。
    return None, "member/invite list exceeds the paging limit"


def _parse_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _get_kick_settings(conn):
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN ('expiry_kick_mode', 'expiry_kick_delay_hours')"
    ).fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    mode = settings.get("expiry_kick_mode", "delay_hours")
    if mode == "day_start":
        mode = "day_end"
    if mode not in {"delay_hours", "day_end"}:
        mode = "delay_hours"
    try:
        delay_hours = int(settings.get("expiry_kick_delay_hours", "0"))
    except (TypeError, ValueError):
        delay_hours = 0
    delay_hours = min(max(delay_hours, 0), 720)
    return mode, delay_hours


# 绑了代理却解析不出来时抛 ProxyUnavailableError：这一轮跳过这个 Team，
# 不能换成本机 IP 直连（见 proxy_resolve）。
_get_proxy_url_sync = resolve_proxy_url_sync


def _effective_kick_at(expires_at, mode, delay_hours):
    if mode == "day_end":
        local = expires_at.astimezone(APP_LOCAL_TZ)
        return local.replace(hour=23, minute=59, second=0, microsecond=0).astimezone(timezone.utc)
    return expires_at + timedelta(hours=delay_hours)


# auto_kick_job 拿下面两个查找的"没找到"直接关掉到期行（"人已不在"）。所以
# "没找到"只能来自一份完整、结构可识别的名单：拉取走 _fetch_all_api_items_sync，
# 它对认不出的 200、截断、翻页超限一律返回 error，调用方原样保留这一行、下一轮
# 重试。把认不出的 200 当成空页会让到期行被关掉，人随后被重新检测成 detected、
# 没有到期时间，继续占着席位。


def _find_member_user_id_by_email(client: ChatGPTClient, email: str):
    if not email:
        return None, None
    email = email.lower()
    members, error = _fetch_all_api_items_sync(
        client.get_members, "users", limit=100, require_items=True
    )
    if error:
        return None, error
    for member in members:
        if str(member.get("email") or "").lower() == email:
            user_id = member.get("id") or member.get("user_id")
            if not user_id:
                # 人在名单里却拿不到 id：既踢不了，也绝不能当成"不在"。
                return None, "member entry has no user id"
            return user_id, None
    return None, None


def _pending_invite_exists(client: ChatGPTClient, email: str):
    if not email:
        return False, None
    email = email.lower()
    invites, error = _fetch_all_api_items_sync(
        client.get_pending_invites, "invites", limit=100
    )
    if error:
        return False, error
    for invite in invites:
        invite_email = invite.get("email_address") or invite.get("email") or ""
        if str(invite_email).lower() == email:
            return True, None
    return False, None


def _mark_expiry_done(conn, row_id, kicked_at, user_id=None, kick_source=None, email=None):
    if user_id:
        conn.execute(
            "UPDATE member_expiry SET kicked = 1, kicked_at = ?, kick_source = ?, user_id = ? WHERE id = ? AND kicked = 0",
            (kicked_at, kick_source, user_id, row_id),
        )
    else:
        conn.execute(
            "UPDATE member_expiry SET kicked = 1, kicked_at = ?, kick_source = ? WHERE id = ? AND kicked = 0",
            (kicked_at, kick_source, row_id),
        )
    deactivate_member_binding_if_inactive_sync(conn, email, now_iso=kicked_at)
    conn.commit()
    sync_email_chat_commands_sync(email or "", conn=conn)


def _add_member_watch_sync(conn, team_id, reason, target_email=None, target_user_id=None):
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=30)).isoformat()
    now_iso = now.isoformat()
    target_email = (target_email or "").strip().lower() or None
    target_user_id = (target_user_id or "").strip() or None

    if target_email:
        conn.execute(
            "UPDATE member_watch SET done = 1 WHERE team_id = ? AND target_email = ? AND done = 0",
            (team_id, target_email),
        )
    elif target_user_id:
        conn.execute(
            "UPDATE member_watch SET done = 1 WHERE team_id = ? AND target_user_id = ? AND done = 0",
            (team_id, target_user_id),
        )

    conn.execute(
        """INSERT INTO member_watch
           (team_id, reason, target_email, target_user_id, started_at, expires_at, done)
           VALUES (?, ?, ?, ?, ?, ?, 0)""",
        (team_id, reason, target_email, target_user_id, now_iso, expires_at),
    )
    conn.commit()


def _reactivate_or_insert_detected_member(conn, team_id, user_id, email, now):
    """Track a detected member without reusing kicked audit rows."""
    uid = user_id or ""
    normalized_email = (email or "").strip().lower()
    row = conn.execute(
        """SELECT id FROM member_expiry
           WHERE team_id = ?
             AND kicked = 0
             AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
           ORDER BY COALESCE(created_at, first_seen_at) DESC, id DESC
           LIMIT 1""",
        (team_id, uid, uid, normalized_email, normalized_email),
    ).fetchone()
    if row:
        # source 只在这一行本来就是"外部发现"时才写 'detected'。复用的是一条
        # kicked=0 的已有记录：它可能是付过钱的 self_service / system 行，只是上游
        # user_id 漂移导致这一轮按邮箱重新匹配上。无条件改写会把自己人降级成外人
        # ——管理端的"检测到的成员"列表里出现一个等着被手工清掉的付费用户。
        conn.execute(
            """UPDATE member_expiry
               SET user_id = ?, email = ?, kicked = 0, kicked_at = NULL, kick_source = NULL,
                   first_seen_at = COALESCE(first_seen_at, ?),
                   source = CASE
                       WHEN COALESCE(source, '') IN ('', 'detected') THEN 'detected'
                       ELSE source
                   END
               WHERE id = ?""",
            (uid, normalized_email, now, row["id"]),
        )
        return False

    conn.execute(
        """INSERT INTO member_expiry
           (team_id, user_id, email, expires_at, auto_kick, kicked,
            first_seen_at, source, created_at)
           VALUES (?, ?, ?, NULL, 0, 0, ?, 'detected', ?)""",
        (team_id, uid, normalized_email, now, now),
    )
    return True


def _too_new_to_judge_absent(row_created_at, snapshot_taken_at) -> bool:
    """这行成员记录是不是"比本轮快照还新，还没资格判缺席"。

    成员名单在 snapshot_taken_at 那一刻拍下，member_expiry 是之后才读的。夹在中间
    落地的一次兑换写出的行当然不在名单里；判它缺席会 kicked=1 + kick_source
    ='detected'，下一轮 _reactivate_or_insert_detected_member 只复用 kicked=0 的行，
    于是补插一条 source='detected'、expires_at=NULL、auto_kick=0 的新行——付过钱的
    到期时间没了，人反而正好长成 patrol 的踢人目标。

    这里只排除"太新、判不了"的行，不是给所有人加宽限期：时间戳缺失的历史行照旧
    参与判定。
    """
    created = _parse_datetime(row_created_at)
    return bool(created and created > snapshot_taken_at)


def _purchased_duration(row):
    """``kind='extend'`` 兜底行代表的购买时长，其它行返回 None。

    这类行的 ``expires_at`` 是落盘那一刻算出的 ``now + duration``（见
    ``member_expiry._persist_confirmed_membership``），所以购买时长就是
    ``expires_at - created_at``。行内没有单独的时长列，这是还原它的唯一途径。
    """
    try:
        if (row["kind"] or "backfill") != "extend":
            return None
    except (IndexError, KeyError):
        return None
    created = _parse_datetime(row["created_at"])
    target = _parse_datetime(row["expires_at"])
    if not created or not target:
        return None
    delta = target - created
    if delta <= timedelta(0):
        return None
    return delta


def _credit_purchased_duration(current_expires_at, purchased, now):
    """把一段买到的时长记到成员身上：max(现有到期, 现在) + 时长。

    与 member_expiry.extend_member_expiry 是同一条续期规则：现有到期还在未来就从它
    往后加；已经过去、无法解析、或者根本没有（没有记录 / detected 未授权）就从现在
    起算。兜底行从落盘到被同步看到可能拖了好几天，这段时间不能算进成员的时长里。
    永久授权（已授权来源 + NULL）由调用方先挡掉，绝不走到这里。
    """
    base = _parse_datetime(now) or datetime.now(timezone.utc)
    current = _parse_datetime(current_expires_at)
    if current is not None and current > base:
        base = current
    return (base + purchased).isoformat()


def _resolve_token_use_reconciliations(conn, token_use_id, now):
    """撤掉某次兑换名下的全部兜底行（barrier / extend / backfill）。

    只在这次兑换已经是终态时调用。同一次兑换可能同时留下好几行：uncertain 分支
    立的 kind='barrier'，加上每次本地写入失败各留的一行 'extend'。兑换一旦结清，
    access_tokens.reconcile_pending_redemptions 只扫 pending/uncertain，再也够不到
    它。屏障若留在 resolved=0：auto_kick_job 永远对这个 (team,email) 返回 defer
    而跳过踢人，patrol 又因为 source='self_service' 不碰它——一份无限期免费的
    会员；多出来的 'extend' 行则会在下一轮把同一笔购买再加一遍。谁结清了
    token_use_id，谁就在同一个事务里把它名下的行一起撤掉。
    """
    conn.execute(
        """UPDATE pending_invite_reconciliations
           SET resolved = 1, resolved_at = ?
           WHERE token_use_id = ? AND resolved = 0""",
        (now, token_use_id),
    )


def _reconcile_pending_invites_sync(conn, team_id, members, pending_invites, now):
    """Backfill confirmed invites before unknown-member detection runs.

    A row in ``pending_invite_reconciliations`` means the remote invite
    succeeded but the primary local write did not.  Only resolve a row after
    the target is visible in a successful live snapshot; until then the row
    remains an explicit patrol safety barrier.
    """
    # kind='barrier' 的行只是"结果未定的自助邀请"借这张表挡巡逻：它的 expires_at
    # 是 NULL，按下面的回填语义会被当成永久直接落库。这类行的时长结算只能走
    # access_tokens.reconcile_pending_redemptions 的累加语义，这里必须原样留着。
    rows = conn.execute(
        """SELECT id, user_id, email, expires_at, source, token_use_id,
                  COALESCE(kind, 'backfill') AS kind, created_at
           FROM pending_invite_reconciliations
           WHERE team_id = ? AND resolved = 0
             AND COALESCE(kind, 'backfill') != 'barrier'
           ORDER BY id""",
        (team_id,),
    ).fetchall()
    if not rows:
        return 0

    member_ids = set()
    member_id_by_email = {}
    member_email_by_id = {}
    for member in members or []:
        user_id = member.get("id") or member.get("user_id") or ""
        email = (member.get("email") or "").strip().lower()
        if user_id:
            member_ids.add(user_id)
            if email:
                member_email_by_id[user_id] = email
        if email:
            member_id_by_email[email] = user_id

    pending_emails = {
        (invite.get("email_address") or invite.get("email") or "").strip().lower()
        for invite in (pending_invites or [])
    }
    pending_emails.discard("")

    reconciled = 0
    began_here = False
    for row in rows:
        stored_user_id = row["user_id"] or ""
        stored_email = (row["email"] or "").strip().lower()
        live_user_id = (
            stored_user_id
            if stored_user_id and stored_user_id in member_ids
            else member_id_by_email.get(stored_email, "")
        )
        live_email = stored_email or member_email_by_id.get(live_user_id, "")
        is_present = bool(live_user_id) or bool(live_email and live_email in pending_emails)
        if not is_present:
            continue

        source = (row["source"] or "system").strip() or "system"
        # This queue only records invites initiated by this application.  A
        # stale/invalid "detected" value must never turn a confirmed invite
        # into a patrol target.
        if source == "detected":
            source = "system"

        # 下面是"读现有到期 → 算新值 → 写回"，必须先拿写锁再读：否则读到的基准
        # 可能已被并发的续期改过，写回时把别人刚加的时长覆盖掉。legacy 事务模式下
        # 只有 DML 才会隐式开事务，所以这里显式 BEGIN IMMEDIATE；已经在事务里
        # （本轮前面的行写过）说明写锁早已在手。出错时由 data_sync_job 回滚。
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            began_here = True
        # 拿到写锁后重读：同一次兑换的另一条兜底行可能已在本轮前面一并撤掉了。
        still_open = conn.execute(
            "SELECT 1 FROM pending_invite_reconciliations WHERE id = ? AND resolved = 0",
            (row["id"],),
        ).fetchone()
        if not still_open:
            continue

        # 带兑换凭据的行：先认领凭据，再决定写不写到期时间。同一次兑换还有别的
        # 结清路径（access_tokens.reconcile_pending_redemptions 和管理员收尾经
        # extend_member_expiry 记账，管理员核实退码），它们在结清收据的同一事务里
        # 撤掉这次兑换名下的全部行；兑换结清之后不会再有它名下的新行写进来（见
        # member_expiry.insert_pending_invite_reconciliation_row），但库里可能留着
        # 这条检查之前写下的旧行。这种行要是照常回填，会把同一笔购买再追加一遍
        # （30 天码变 60 天），或给已退的码记时长。所以凭据认领不到（rowcount=0）
        # 就只撤掉这次兑换的兜底行，绝不碰 member_expiry。
        token_use_id = row["token_use_id"]
        if token_use_id is not None:
            claimed = conn.execute(
                """UPDATE access_token_uses
                   SET action = COALESCE(NULLIF(action, ''), 'invited'),
                       team_id = ?, user_id = ?,
                       result = 'success', error_message = NULL
                   WHERE id = ? AND result IN ('pending', 'uncertain')""",
                (team_id, live_user_id, token_use_id),
            ).rowcount
            if claimed != 1:
                logger.warning(
                    "invite reconciliation: token_use_id=%s already settled by "
                    "another path; resolving fallback row %s without crediting "
                    "(team=%s)",
                    token_use_id, row["id"], team_id,
                )
                _resolve_token_use_reconciliations(conn, token_use_id, now)
                reconciled += 1
                continue

        existing = conn.execute(
            """SELECT id, expires_at, source FROM member_expiry
               WHERE team_id = ? AND kicked = 0
                 AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
               ORDER BY COALESCE(created_at, first_seen_at) DESC, id DESC
               LIMIT 1""",
            (team_id, live_user_id, live_user_id, live_email, live_email),
        ).fetchone()
        # kind='extend'：这行代表一次"买到的时长"，不是一个到期时刻。行内的
        # expires_at 是落盘那一刻的 now + 时长；照抄它，兜底拖了几天成员就少几天。
        purchased = _purchased_duration(row)
        if existing:
            pending_expires = _parse_datetime(row["expires_at"])
            current_expires = _parse_datetime(existing["expires_at"])
            if row["expires_at"] is None:
                # 这次确认的邀请本身就是永久。
                resolved_expires = None
            elif existing["expires_at"] is None and existing["source"] != "detected":
                # 已授权的永久记录不能被有限时长兜底降级。detected + NULL
                # 只是“外部发现、尚未授权”，不代表真正的永久购买。
                resolved_expires = None
            elif purchased is not None:
                # 按 extend_member_expiry 的语义追加：max(现有到期, 现在) + 时长
                # （detected + NULL 没有已购时长，从现在起算）。只取 max(行内到期,
                # 现有到期) 会让一个到期时间更远的成员把这次授予的时长整个吃掉，
                # 而兑换还被置成 success——用户和管理员都看不到任何异常。
                resolved_expires = _credit_purchased_duration(
                    existing["expires_at"], purchased, now
                )
            elif existing["expires_at"] is None:
                resolved_expires = row["expires_at"]
            elif pending_expires and current_expires:
                resolved_expires = max(pending_expires, current_expires).isoformat()
            else:
                # 无法解析时保留当前正式记录，避免陈旧兜底值覆盖它。
                resolved_expires = existing["expires_at"]
            auto_kick = 1 if resolved_expires else 0
            conn.execute(
                """UPDATE member_expiry
                   SET user_id = ?, email = ?, expires_at = ?, auto_kick = ?,
                       source = ?, kicked = 0, kicked_at = NULL, kick_source = NULL
                   WHERE id = ?""",
                (
                    live_user_id,
                    live_email,
                    resolved_expires,
                    auto_kick,
                    source,
                    existing["id"],
                ),
            )
        else:
            if purchased is not None:
                # 没有现行记录（上一段已 kicked=1 归档也算）：没有已购时长要保护，
                # 和 extend_member_expiry 一样从现在起算。
                resolved_expires = _credit_purchased_duration(None, purchased, now)
            else:
                resolved_expires = row["expires_at"]
            auto_kick = 1 if resolved_expires else 0
            conn.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked,
                    first_seen_at, source, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    team_id,
                    live_user_id,
                    live_email,
                    resolved_expires,
                    auto_kick,
                    now,
                    source,
                    now,
                ),
            )

        # 凭据已在上面认领（同一事务）；这里把收据上的到期时间补成刚写进
        # member_expiry 的那个值，并释放邮箱锁。
        if token_use_id is not None:
            conn.execute(
                "UPDATE access_token_uses SET expires_at = ? WHERE id = ?",
                (resolved_expires, token_use_id),
            )
            conn.execute(
                "DELETE FROM redemption_email_claims WHERE token_use_id = ?",
                (token_use_id,),
            )
            _resolve_token_use_reconciliations(conn, token_use_id, now)

        conn.execute(
            """UPDATE pending_invite_reconciliations
               SET resolved = 1, resolved_at = ?
               WHERE id = ? AND resolved = 0""",
            (now, row["id"]),
        )
        reconciled += 1

    if began_here and reconciled == 0:
        # 开了写事务却一行没动（重读发现都已被撤掉）：调用方只在 reconciled>0
        # 时提交，这里自己放掉写锁，别让它拖到后面的提交点。
        conn.commit()
    return reconciled


def _pending_invite_reconciliation_reject_sync(conn, team_id, user_id="", email=""):
    normalized_user_id = user_id or ""
    normalized_email = (email or "").strip().lower()
    try:
        row = conn.execute(
            """SELECT 1 FROM pending_invite_reconciliations
               WHERE team_id = ? AND resolved = 0
                 AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
               LIMIT 1""",
            (
                team_id,
                normalized_user_id,
                normalized_user_id,
                normalized_email,
                normalized_email,
            ),
        ).fetchone()
    except sqlite3.Error:
        return "deferred: invite reconciliation state is unavailable"
    if row:
        return "deferred: confirmed invite reconciliation is still pending"
    return None


def _cached_unknown_seat_type_sync(conn, team_id, user_id, email):
    """到期目标在成员快照里是不是注册表外的席位类型（如 automation）。

    返回 ``(status, 席位类型原值)``，status 为 member / pending；快照读不到、快照里没有
    这个人、或席位类型认识，都返回 None，到期踢人照旧处理（它本来就不看席位类型）。
    只有快照明确写着未知类型才跳过：TeamBoss 对这类席位什么都不做。
    """
    try:
        row = conn.execute(
            "SELECT members_json, pending_json FROM member_cache WHERE team_id = ?",
            (team_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    user_id = str(user_id or "")
    email = (email or "").strip().lower()
    for status, raw in (("member", row["members_json"]), ("pending", row["pending_json"])):
        try:
            items = json.loads(raw or "[]")
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            # 邀请的 id 是邀请号，不是用户 id，只能按邮箱对。
            item_user_id = str(item.get("id") or item.get("user_id") or "") if status == "member" else ""
            item_email = (item.get("email") or "").strip().lower()
            if not ((user_id and item_user_id == user_id) or (email and item_email == email)):
                continue
            if not is_known_seat_type(item.get("seat_type")):
                return status, normalize_seat_type(item.get("seat_type"))
    return None


def _latest_log_detail_sync(conn, team_id, action, target_email):
    try:
        row = conn.execute(
            """SELECT detail FROM operation_logs
               WHERE team_id = ? AND action = ? AND target_email IS ?
               ORDER BY id DESC LIMIT 1""",
            (team_id, action, target_email),
        ).fetchone()
    except sqlite3.Error:
        return None
    return row["detail"] if row else None


def auto_kick_job():
    try:
        conn = _get_sync_db()
        now_dt = datetime.now(timezone.utc)
        mode, delay_hours = _get_kick_settings(conn)

        cursor = conn.execute(
            "SELECT me.*, t.access_token, t.device_id, t.proxy_id FROM member_expiry me "
            "JOIN teams t ON me.team_id = t.id "
            "WHERE me.kicked = 0 AND me.auto_kick = 1 AND t.status = 'active'"
        )
        expired = cursor.fetchall()

        for row in expired:
            team_id = row["team_id"]
            user_id = row["user_id"]
            email = row["email"]
            access_token = row["access_token"]
            device_id = row["device_id"]
            try:
                proxy_url = _get_proxy_url_sync(conn, row["proxy_id"])
            except ProxyUnavailableError as exc:
                # 代理解析不出来就不动这个人：这一刀从本机 IP 发出去，等于临时换了出口。
                skip_detail = f"reason=proxy_unavailable, {exc}"
                if _latest_log_detail_sync(conn, team_id, "auto_kick", email) != skip_detail:
                    _log_operation_sync(team_id, "auto_kick", email, skip_detail, "skipped")
                continue
            row_id = row["id"]

            try:
                reconciliation_reject = _pending_invite_reconciliation_reject_sync(
                    conn, team_id, user_id, email
                )
                if reconciliation_reject:
                    _log_operation_sync(
                        team_id,
                        "auto_kick",
                        email,
                        reconciliation_reject,
                        "skipped",
                    )
                    continue

                expires_at = _parse_datetime(row["expires_at"])
                if not expires_at:
                    _log_operation_sync(team_id, "auto_kick", email,
                                        "invalid expires_at", "failed", row["expires_at"])
                    continue

                kick_at = _effective_kick_at(expires_at, mode, delay_hours)
                if kick_at > now_dt:
                    continue

                # 注册表外的席位类型（如 automation）：不踢、不撤，只记一条（同样的跳过不每分钟重复记）。
                unknown_seat = _cached_unknown_seat_type_sync(conn, team_id, user_id, email)
                if unknown_seat:
                    unknown_status, unknown_type = unknown_seat
                    skip_action = "auto_kick" if unknown_status == "member" else "auto_revoke_invite"
                    skip_detail = f"seat_type={unknown_type}, reason=unknown_seat_type"
                    if _latest_log_detail_sync(conn, team_id, skip_action, email) != skip_detail:
                        _log_operation_sync(team_id, skip_action, email, skip_detail, "skipped")
                    continue

                client = ChatGPTClient(access_token, team_id, device_id, proxy_url=proxy_url)

                resolved_user_id = user_id
                if not resolved_user_id and email:
                    resolved_user_id, lookup_error = _find_member_user_id_by_email(client, email)
                    if lookup_error:
                        _log_operation_sync(team_id, "auto_kick", email,
                                            "lookup member by email", "failed", lookup_error)
                        continue

                if resolved_user_id:
                    with member_operation_claim_sync(
                        conn,
                        team_id,
                        email=email,
                        user_id=resolved_user_id,
                        operation="auto_kick",
                    ) as acquired:
                        if not acquired:
                            _log_operation_sync(team_id, "auto_kick", email,
                                                "member operation already in progress", "skipped")
                            continue

                        # claim 拿到后再读一次；如果续期刚刚先完成，这里必须看到
                        # 新到期时间并放弃远端删除。
                        fresh_row = conn.execute(
                            "SELECT * FROM member_expiry WHERE id = ? AND kicked = 0 AND auto_kick = 1",
                            (row_id,),
                        ).fetchone()
                        if not fresh_row:
                            _log_operation_sync(team_id, "auto_kick", email,
                                                "record already kicked or modified", "skipped")
                            continue

                        fresh_expires_at = _parse_datetime(fresh_row["expires_at"])
                        if fresh_expires_at:
                            fresh_kick_at = _effective_kick_at(fresh_expires_at, mode, delay_hours)
                            if fresh_kick_at > now_dt:
                                _log_operation_sync(team_id, "auto_kick", email,
                                                    "expires_at updated after initial scan", "skipped")
                                continue

                        result = run_chatgpt_call_sync(client.remove_member, resolved_user_id)

                        if "error" in result:
                            _log_operation_sync(team_id, "auto_kick", email,
                                                f"user_id={resolved_user_id}", "failed", result["error"])
                            continue

                        kicked_at = datetime.now(timezone.utc).isoformat()
                        _mark_expiry_done(
                            conn,
                            row_id,
                            kicked_at,
                            resolved_user_id,
                            kick_source="auto_expire",
                            email=email,
                        )
                        _add_member_watch_sync(conn, team_id, "kick", target_email=email, target_user_id=resolved_user_id)
                        _log_operation_sync(team_id, "auto_kick", email,
                                            f"user_id={resolved_user_id}", "success")
                        notify_member_event_sync(
                            "到期自动踢人", team_id, email=email, source="auto_expire"
                        )
                    continue

                pending_exists, invite_lookup_error = _pending_invite_exists(client, email)
                if invite_lookup_error:
                    _log_operation_sync(team_id, "auto_revoke_invite", email,
                                        "lookup pending invite", "failed", invite_lookup_error)
                    continue

                if pending_exists:
                    with member_operation_claim_sync(
                        conn,
                        team_id,
                        email=email,
                        operation="auto_revoke_invite",
                    ) as acquired:
                        if not acquired:
                            _log_operation_sync(team_id, "auto_revoke_invite", email,
                                                "member operation already in progress", "skipped")
                            continue

                        fresh_row = conn.execute(
                            "SELECT * FROM member_expiry WHERE id = ? AND kicked = 0 AND auto_kick = 1",
                            (row_id,),
                        ).fetchone()
                        if not fresh_row:
                            _log_operation_sync(team_id, "auto_revoke_invite", email,
                                                "record already kicked or modified", "skipped")
                            continue

                        fresh_expires_at = _parse_datetime(fresh_row["expires_at"])
                        if fresh_expires_at:
                            fresh_kick_at = _effective_kick_at(fresh_expires_at, mode, delay_hours)
                            if fresh_kick_at > now_dt:
                                _log_operation_sync(team_id, "auto_revoke_invite", email,
                                                    "expires_at updated after initial scan", "skipped")
                                continue

                        result = run_chatgpt_call_sync(client.revoke_invite, email)
                        if "error" in result:
                            _log_operation_sync(team_id, "auto_revoke_invite", email,
                                                None, "failed", result["error"])
                            continue
                        kicked_at = datetime.now(timezone.utc).isoformat()
                        _mark_expiry_done(
                            conn, row_id, kicked_at, kick_source="auto_expire", email=email
                        )
                        _add_member_watch_sync(conn, team_id, "kick", target_email=email)
                        _log_operation_sync(team_id, "auto_revoke_invite", email,
                                            "pending invite revoked", "success")
                        notify_member_event_sync(
                            "到期自动撤邀请", team_id, email=email, source="auto_expire"
                        )
                    continue

                if not email:
                    _log_operation_sync(team_id, "auto_kick", email,
                                        "missing user_id and email", "failed", "No member identifier")
                    continue

                kicked_at = datetime.now(timezone.utc).isoformat()
                _mark_expiry_done(
                    conn, row_id, kicked_at, kick_source="auto_expire", email=email
                )
                _log_operation_sync(team_id, "auto_kick", email,
                                    "member or invite already absent", "success")
            except Exception as e:
                _log_operation_sync(team_id, "auto_kick", email,
                                    f"user_id={user_id}", "failed", str(e))

        conn.close()
    except Exception as e:
        _log_operation_sync(None, "auto_kick_job_error", None, None, "failed", str(e))


# 执行链路的输入：patrol 用它们判超员、挑人。任何一项失败，这一轮就不算刷新过。
_ENFORCEMENT_OVERVIEW_KEYS = ("subscription", "seat_counts")


def _classify_overview_failures(
    *, subscription, seat_counts, balance_info, payment_methods, account_info
):
    """把 overview 的子接口失败拆成 (全部, 纯展示, 执行链路) 三份。

    被限流跳过的接口传 None，不算失败——否则每轮都会误报 partial，并压着
    last_full_sync_at 不更新。

    为什么要单独拆出执行链路：挂起判定（连续 24 小时失败就停止定时请求）只能看
    这几项。一个成员名单、订阅、席位数全都正常、只有余额接口持续报错的 Team，按
    "全绿才算成功"会在 24 小时后被挂起，而手动同步按钮用的是同一套全绿判定，于是
    它再也恢复不了。展示接口失败照常播报（合并播报 + 一行 warning 日志），仅此而已。
    """
    overview_failures: list[str] = []
    display_failures: list[str] = []
    enforcement_failures: list[str] = []

    def _failed(payload):
        return payload is not None and "error" in payload

    for key, payload in (
        ("subscription", subscription),
        ("balance", balance_info),
        ("seat_counts", seat_counts),
        ("payment_methods", payment_methods),
        ("account_info", account_info),
    ):
        if not _failed(payload):
            continue
        overview_failures.append(key)
        if key in _ENFORCEMENT_OVERVIEW_KEYS:
            enforcement_failures.append(key)
        else:
            display_failures.append(key)

    return overview_failures, display_failures, enforcement_failures


# 席位数没经本轮确认时提醒管理员：复用 team_health_incidents 的去重，同一 Team
# 24 小时最多一条，中间恢复过也不重开窗口（订阅接口偶发报错会来回抖）。
ENTITLEMENT_ALERT_KEY = "seats_entitled"
ENTITLEMENT_ALERT_INTERVAL = timedelta(hours=24)


def _admit_team_with_unconfirmed_entitlement(conn, team_id, problem, allow_ids, skip_over_quota_ids):
    """成员快照是新的、但本轮没确认 seats_entitled：放进巡逻白名单，并提醒管理员。

    撤陌生邀请和严格模式只看成员/邀请快照，照常执行；只有超员踢人拿 seats_entitled
    当分母，这一段必须跳过：

    * 库里的值不是正整数（未知）：run_patrol 自己跳过超员那一段
      （patrol_skip_invalid_entitlement）。
    * 库里还留着之前的合法值：run_patrol 只认库里的值，会拿这个本轮没确认过的旧值
      判超员、踢人，所以同时放进 skip_over_quota_ids（patrol_skip_unconfirmed_entitlement）。
      白名单和跳过集合在这一处一起决定，不能只放其一。
    """
    row = conn.execute(
        "SELECT seats_entitled FROM teams WHERE id = ?", (team_id,)
    ).fetchone()
    stored = row["seats_entitled"] if row else None
    allow_ids.add(team_id)
    if positive_seat_count(stored) is None:
        detail = (
            f"本轮订阅没给出可用的席位数（{problem}），库里也没有有效值。"
            "巡逻照常撤陌生邀请、执行严格模式，只跳过超员踢人。"
        )
    else:
        skip_over_quota_ids.add(team_id)
        detail = (
            f"本轮订阅没给出可用的席位数（{problem}）。库里保留的上次值 {stored} "
            "未经本轮确认，不拿它判超员：巡逻照常撤陌生邀请、执行严格模式，"
            "只跳过超员踢人，直到拿到有效值。"
        )
    report_team_failure_sync(
        team_id,
        ENTITLEMENT_ALERT_KEY,
        detail,
        source="scheduled_data_sync",
        notify_interval=ENTITLEMENT_ALERT_INTERVAL,
    )


def data_sync_job():
    sync_completed = False
    failed_team_ids: set[str] = set()
    # 已挂起的 Team：本轮根本没打请求，既不算失败（不能一直压着摘要不发），
    # 也不能让 patrol 拿着冻住的快照去判超员。
    suspended_team_ids: set[str] = set()
    # 巡逻白名单：本轮成员/邀请快照完整刷新、且没挂起的 team（规则见每个 team 末尾的
    # 白名单判定；席位数没确认的只跳过超员踢人）。没进这个集合的 team，这一轮巡逻一个都不碰。
    refreshed_team_ids: set[str] = set()
    # 白名单里席位数本轮没经上游确认、库里却留着旧合法值的 team：巡逻只跳过超员踢人。
    unconfirmed_entitlement_team_ids: set[str] = set()
    newly_suspended: list[tuple[str, str]] = []  # (team_id, team_name)
    resumed_from_suspension: list[tuple[str, str]] = []
    teams_with_overview_failures: list[tuple[str, str, list[str]]] = []  # (team_id, team_name, failed_keys)
    try:
        conn = _get_sync_db()
        cursor = conn.execute(
            "SELECT id, name, access_token, device_id, proxy_id, country_code, display_synced_at, "
            "sync_failing_since, sync_suspended_at, sync_probe_at, last_full_sync_at "
            "FROM teams WHERE status = 'active'"
        )
        teams = cursor.fetchall()

        for team in teams:
            team_id = team["id"]
            team_name = team["name"] or team_id
            access_token = team["access_token"]
            device_id = team["device_id"]

            failing_since = team["sync_failing_since"]
            suspended_at = team["sync_suspended_at"]
            last_full_sync_at = team["last_full_sync_at"]
            round_ok = False
            # 成员/邀请快照完整拿到，且除订阅外的执行输入都没报错。订阅只喂超员踢人
            # 的分母，它出问题不该连撤陌生邀请、严格模式一起停掉（见白名单判定）。
            # round_ok 仍要求订阅也成功：挂起计时照旧。
            snapshot_ok = False

            # 挂起中且没到探活点：这一轮对这个 Team 一个请求都不发。
            if suspended_at and not _sync_probe_due(team["sync_probe_at"], datetime.now(timezone.utc)):
                suspended_team_ids.add(team_id)
                continue

            try:
                # 在 try 里面解析：代理不可用时这一轮按同步失败处理（下面的 except
                # 会记 data_sync failed 并上报），而不是改成本机 IP 直连。
                proxy_url = _get_proxy_url_sync(conn, team["proxy_id"])
                client = ChatGPTClient(access_token, team_id, device_id, proxy_url=proxy_url)
                now_dt = datetime.now(timezone.utc)
                refresh_display = _display_sync_due(team["display_synced_at"], now_dt)

                # patrol 的输入，每轮必拉。
                subscription = run_chatgpt_call_sync(client.get_subscription)
                seat_counts = run_chatgpt_call_sync(client.get_seat_type_counts)

                # 纯展示字段，限流。跳过时保持 None：下面每一处写库都以
                # "is not None" 为前提，所以跳过的这一轮不会碰到对应的列，
                # 既不会写旧值也不会写 NULL。
                balance_info = None
                payment_methods = None
                account_info = None
                workspace_settings = None
                if refresh_display:
                    balance_info = run_chatgpt_call_sync(client.get_remaining_balance)
                    payment_methods = run_chatgpt_call_sync(client.get_payment_methods)
                    account_info = run_chatgpt_call_sync(client.get_account_info)
                    workspace_settings = run_chatgpt_call_sync(client.get_workspace_settings)

                now = now_dt.isoformat()

                # Track overview sub-interface failures for later notification
                # 被限流跳过的接口不算失败，否则每轮都会误报 partial 并压着
                # last_full_sync_at 不更新。
                overview_failures, display_failures, enforcement_failures = (
                    _classify_overview_failures(
                        subscription=subscription,
                        seat_counts=seat_counts,
                        balance_info=balance_info,
                        payment_methods=payment_methods,
                        account_info=account_info,
                    )
                )

                updates = []
                params = []

                # seats_entitled 是 patrol 算 over_by 的分母。这一轮没拿到合法值
                # （null / 0 / 非正整数 / 缺失 / 订阅接口报错）时库里保留上一次的值，
                # 但那已经不是本轮确认过的输入，不能拿去判超员（见白名单判定）。
                entitlement_fresh = False
                entitlement_problem = "订阅接口报错"
                if "error" not in subscription:
                    entitlement_problem = (
                        "响应里没有 seats_entitled"
                        if "seats_entitled" not in subscription
                        else "seats_entitled 不是正整数"
                    )
                    # 缺字段不写、seats_entitled 只认正整数：规则见
                    # seat_capacity.subscription_column_updates。
                    for col, value in subscription_column_updates(
                        subscription, team_id=team_id
                    ).items():
                        updates.append(f"{col} = ?")
                        params.append(value)
                    entitlement_fresh = isinstance(subscription, dict) and (
                        positive_seat_count(subscription.get("seats_entitled")) is not None
                    )

                if balance_info is not None and "error" not in balance_info:
                    balance_value = balance_info.get("balance")
                    updates.append("balance = ?")
                    params.append(str(balance_value) if balance_value is not None else None)

                official_codex = None
                official_chatgpt = None
                if "error" not in seat_counts:
                    official_codex = seat_type_count_from_seat_counts(
                        seat_counts, "usage_based"
                    )
                    official_chatgpt = chatgpt_count_from_seat_counts(seat_counts)
                    if official_codex is not None:
                        updates.append("codex_count = ?")
                        params.append(official_codex)
                    if official_chatgpt is not None:
                        updates.append("chatgpt_count = ?")
                        params.append(official_chatgpt)
                    # 分类型计数原样（含未知类型）给界面用；seat_capacity_json 由上面的
                    # subscription_column_updates 写。规则见 seat_capacity。
                    for col, value in seat_counts_column_updates(seat_counts).items():
                        updates.append(f"{col} = ?")
                        params.append(value)

                if payment_methods is not None and "error" not in payment_methods:
                    methods = payment_methods.get("payment_methods", [])
                    if methods:
                        card = methods[0].get("card", {})
                        updates.extend(["card_last4 = ?", "card_brand = ?", "payment_method_id = ?"])
                        params.extend([card.get("last4"), card.get("brand"), methods[0].get("id")])

                billing_updates = account_billing_updates(account_info, team_id)
                billing_updates.update(subscription_billing_updates(subscription))
                for key, value in billing_updates.items():
                    updates.append(f"{key} = ?")
                    params.append(value)

                if refresh_display and "error" not in subscription:
                    pricing_updates = fetch_seat_pricing_sync(
                        client,
                        subscription,
                        fallback_country_code=team["country_code"],
                        run_call=run_chatgpt_call_sync,
                    )
                    if "country_code" in pricing_updates:
                        updates.append("country_code = ?")
                        params.append(pricing_updates["country_code"])
                    if "price_per_seat" in pricing_updates:
                        updates.append("price_per_seat = ?")
                        params.append(pricing_updates["price_per_seat"])
                    if "premium_price_per_seat" in pricing_updates:
                        updates.append("premium_price_per_seat = ?")
                        params.append(pricing_updates["premium_price_per_seat"])
                    if "price_period" in pricing_updates:
                        updates.append("price_period = ?")
                        params.append(pricing_updates["price_period"])
                    if "billing_symbol" in pricing_updates:
                        updates.append("billing_symbol = ?")
                        params.append(pricing_updates["billing_symbol"])
                    if "billing_period" in pricing_updates:
                        updates.append("billing_period = ?")
                        params.append(pricing_updates["billing_period"])

                current_cache_row = conn.execute(
                    "SELECT cached_data FROM teams WHERE id = ?", (team_id,)
                ).fetchone()
                # 跳过的接口不进 merge，_merge_team_cached_data 保留上一轮的值。
                fresh_payload = {"subscription": subscription, "seat_counts": seat_counts}
                if balance_info is not None:
                    fresh_payload["balance"] = balance_info
                if payment_methods is not None:
                    fresh_payload["payment_methods"] = payment_methods
                if account_info is not None:
                    fresh_payload["account_info"] = account_info
                cached = _merge_team_cached_data(
                    current_cache_row["cached_data"] if current_cache_row else None,
                    fresh_payload,
                    workspace_settings,
                    now,
                )
                updates.extend(["cached_data = ?", "updated_at = ?"])
                params.extend([cached, now])

                # 只有真拉了展示接口、且它们都没报错，才推进限流时间戳；
                # 拉失败就让下一轮继续重试，不要白等 6 小时。只看展示接口
                # 自己的失败：subscription/seat_counts 抖一下不该把限流打回去。
                if refresh_display and not display_failures:
                    updates.append("display_synced_at = ?")
                    params.append(now)

                # Track sync result: only update last_full_sync_at if ALL overview interfaces succeeded
                if not overview_failures:
                    updates.append("last_full_sync_at = ?")
                    params.append(now)
                    updates.append("last_sync_partial_failures = ?")
                    params.append(None)
                else:
                    # Record which interfaces failed
                    updates.append("last_sync_partial_failures = ?")
                    params.append(json.dumps(overview_failures, ensure_ascii=False))
                    # Also record operation log for this team's failure
                    failure_detail = f"overview sub-interface failures: {', '.join(overview_failures)}"
                    _log_operation_sync(team_id, "data_sync", None, "scheduled", "partial", failure_detail)
                    teams_with_overview_failures.append((team_id, team_name, overview_failures))

                if updates:
                    params.append(team_id)
                    conn.execute(
                        f"UPDATE teams SET {', '.join(updates)} WHERE id = ?", params
                    )
                    conn.commit()

                # ── 计费快照 ──
                try:
                    team_row = conn.execute(
                        """SELECT billing_currency, billing_period, price_period, price_per_seat,
                                  premium_price_per_seat, seat_capacity_json,
                                  seats_entitled, seats_in_use,
                                  codex_count, chatgpt_count, discount_amount, discount_expires_at,
                                  discount_start_in_num_periods, balance,
                                  active_until, will_renew
                           FROM teams WHERE id = ?""",
                        (team_id,)
                    ).fetchone()

                    if team_row:
                        from .services.pricing import team_monthly_cost

                        snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        # 和财务页同一算法（含真实 Premium 部分，年付记月均），趋势图和总览对得上。
                        # Prices only count when they match a known billing period (see
                        # pricing.priced_period). Otherwise record that honestly as a NULL
                        # monthly_total instead of a fabricated 0, which would be
                        # indistinguishable from a real free/fully-discounted Team.
                        cost = team_monthly_cost(dict(team_row))
                        monthly_total = cost.monthly_total

                        conn.execute(
                            """INSERT INTO billing_snapshots
                               (team_id, snapshot_date, billing_currency, billing_period, price_per_seat,
                                premium_price_per_seat, premium_seats_paid,
                                seats_entitled, seats_in_use, codex_count, chatgpt_count,
                                discount_amount, monthly_total, balance, active_until,
                                will_renew, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(team_id, snapshot_date) DO UPDATE SET
                                   billing_currency = excluded.billing_currency,
                                   billing_period = excluded.billing_period,
                                   price_per_seat = excluded.price_per_seat,
                                   premium_price_per_seat = excluded.premium_price_per_seat,
                                   premium_seats_paid = excluded.premium_seats_paid,
                                   seats_entitled = excluded.seats_entitled,
                                   seats_in_use = excluded.seats_in_use,
                                   codex_count = excluded.codex_count,
                                   chatgpt_count = excluded.chatgpt_count,
                                   discount_amount = excluded.discount_amount,
                                   monthly_total = excluded.monthly_total,
                                   balance = excluded.balance,
                                   active_until = excluded.active_until,
                                   will_renew = excluded.will_renew,
                                   created_at = excluded.created_at""",
                            (team_id, snapshot_date, team_row["billing_currency"],
                             team_row["billing_period"],
                             cost.price_per_seat, cost.premium_price_per_seat,
                             cost.premium_seats_paid, team_row["seats_entitled"],
                             team_row["seats_in_use"], team_row["codex_count"],
                             team_row["chatgpt_count"], team_row["discount_amount"], monthly_total,
                             team_row["balance"], team_row["active_until"],
                             team_row["will_renew"], now)
                        )
                        conn.commit()
                except Exception:
                    pass

                # ── 发票缓存：每团队最多一天拉一次，PII 在入库前就被丢掉 ──
                try:
                    refresh_invoices_if_stale_sync(
                        conn, client, team_id, now, run_chatgpt_call_sync
                    )
                except Exception:
                    pass

                # ── 成员检测：发现未跟踪的手动拉入成员 ──
                # 快照时刻：下面这两次拉取之后写进 member_expiry 的行，不可能出现在
                # 这份名单里。反向缺席判定必须以此为界（见 absence 分支）。
                snapshot_taken_at = datetime.now(timezone.utc)
                # 同一时刻也是这份快照的 fetch_started_at（写缓存时只让开始得更晚的覆盖更早的）。
                fetch_started_at = snapshot_taken_at.isoformat(timespec="microseconds")
                members_items, m_err = _fetch_all_api_items_sync(
                    client.get_members, "users", require_items=True
                )
                pending_items, p_err = _fetch_all_api_items_sync(client.get_pending_invites, "invites")

                if not m_err and not p_err:
                    owner_email = ""
                    try:
                        owner_row = conn.execute("SELECT owner_email FROM teams WHERE id = ?", (team_id,)).fetchone()
                        owner_email = (owner_row["owner_email"] or "").strip().lower() if owner_row else ""
                    except Exception:
                        pass

                    reconciled_count = _reconcile_pending_invites_sync(
                        conn, team_id, members_items, pending_items, now
                    )
                    if reconciled_count > 0:
                        # Make the trusted source durable before detection or
                        # any patrol pass can classify this snapshot.
                        conn.commit()
                        _log_operation_sync(
                            team_id,
                            "invite_reconciliation",
                            None,
                            f"reconciled {reconciled_count} confirmed invite(s)",
                            "success",
                        )

                    expiry_rows = conn.execute(
                        "SELECT id, user_id, email, "
                        "COALESCE(created_at, first_seen_at) AS row_created_at "
                        "FROM member_expiry WHERE team_id = ? AND kicked = 0",
                        (team_id,)
                    ).fetchall()
                    tracked_ids = set()
                    tracked_emails = set()
                    for er in expiry_rows:
                        if er["user_id"]:
                            tracked_ids.add(er["user_id"])
                        if er["email"]:
                            tracked_emails.add(er["email"].lower())

                    api_member_ids = set()
                    api_member_emails = set()
                    for m in (members_items or []):
                        uid = m.get("id") or m.get("user_id") or ""
                        m_email = (m.get("email") or "").strip().lower()
                        if uid:
                            api_member_ids.add(uid)
                        if m_email:
                            api_member_emails.add(m_email)

                    api_pending_emails = set()
                    for inv in (pending_items or []):
                        inv_email = (inv.get("email_address") or inv.get("email") or "").strip().lower()
                        if inv_email:
                            api_pending_emails.add(inv_email)

                    new_count = 0
                    detected_added: list[str] = []
                    for m in (members_items or []):
                        uid = m.get("id") or m.get("user_id") or ""
                        m_email = (m.get("email") or "").strip().lower()
                        if m.get("role") == "account-owner" or m_email == owner_email:
                            continue
                        if uid in tracked_ids or m_email in tracked_emails:
                            continue
                        if not uid and not m_email:
                            continue
                        if _reactivate_or_insert_detected_member(conn, team_id, uid, m_email, now):
                            new_count += 1
                            detected_added.append(m_email or uid)
                        if uid:
                            tracked_ids.add(uid)
                        if m_email:
                            tracked_emails.add(m_email)

                    for inv in (pending_items or []):
                        inv_email = (inv.get("email_address") or inv.get("email") or "").strip().lower()
                        if not inv_email or inv_email in tracked_emails:
                            continue
                        if _reactivate_or_insert_detected_member(conn, team_id, "", inv_email, now):
                            new_count += 1
                            detected_added.append(inv_email)
                        tracked_emails.add(inv_email)

                    if new_count > 0:
                        conn.commit()
                        _log_operation_sync(team_id, "member_detect", None,
                                            f"detected {new_count} untracked member(s)", "success")
                        for detected_target in detected_added:
                            notify_member_event_sync(
                                "检测到外部拉人",
                                team_id,
                                email=detected_target,
                                source="detected",
                            )

                    # 反向检测：API 中已消失（手动踢人/撤邀请）→ 标记 kicked
                    absent_count = 0
                    detected_absent: list[str] = []
                    for er in expiry_rows:
                        uid = er["user_id"] or ""
                        email = (er["email"] or "").strip().lower()
                        if email == owner_email:
                            continue
                        if not uid and not email:
                            continue

                        still_present = (
                            (uid and uid in api_member_ids)
                            or (email and email in api_member_emails)
                            or (email and email in api_pending_emails)
                        )
                        if still_present:
                            continue

                        # 这份名单是 snapshot_taken_at 那一刻的。在那之后才写进来的
                        # 行（典型场景：同一时刻正在落地的一次自助兑换）当然不在名单
                        # 里，它"缺席"只是因为太新，还没资格判。误判的代价是整条链：
                        # 本轮 kicked=1/kick_source='detected'，下一轮
                        # _reactivate_or_insert_detected_member 只复用 kicked=0 的行，
                        # 于是补插一条 source='detected'、expires_at=NULL、auto_kick=0
                        # 的新行——付过钱的到期时间没了，人反而正好长成 patrol 的踢人
                        # 目标。这里只排除"太新、判不了"的行，不是给所有人加宽限期。
                        if _too_new_to_judge_absent(
                            er["row_created_at"], snapshot_taken_at
                        ):
                            continue

                        conn.execute(
                            "UPDATE member_expiry SET kicked = 1, kicked_at = ?, kick_source = 'detected' WHERE id = ?",
                            (now, er["id"]),
                        )
                        deactivate_member_binding_if_inactive_sync(
                            conn, email, now_iso=now
                        )
                        absent_count += 1
                        detected_absent.append(email or uid)

                    if absent_count > 0:
                        conn.commit()
                        for detected_target in detected_absent:
                            sync_email_chat_commands_sync(detected_target, conn=conn)
                        _log_operation_sync(
                            team_id, "member_detect_absent", None,
                            f"marked {absent_count} absent member(s) as kicked", "success",
                        )
                        for detected_target in detected_absent:
                            notify_member_event_sync(
                                "检测到外部踢人",
                                team_id,
                                email=detected_target,
                                source="detected",
                            )

                    # 刷新 member_cache
                    all_expiry = conn.execute(
                        "SELECT * FROM member_expiry WHERE team_id = ? AND kicked = 0", (team_id,)
                    ).fetchall()
                    exp_map = {}
                    for er in all_expiry:
                        if er["user_id"]:
                            exp_map[er["user_id"]] = dict(er)
                        if er["email"]:
                            exp_map[er["email"].lower()] = dict(er)

                    cached_members = []
                    for m in (members_items or []):
                        uid = m.get("id") or m.get("user_id") or ""
                        c_email = m.get("email") or ""
                        ei = exp_map.get(uid) or exp_map.get(c_email.lower())
                        cached_members.append({
                            "id": uid, "email": c_email,
                            "name": m.get("name"),
                            "role": m.get("role", "standard-user"),
                            "seat_type": m.get("seat_type", "default"),
                            "is_owner": m.get("role") == "account-owner",
                            "expires_at": ei["expires_at"] if ei else None,
                            "first_seen_at": ei.get("first_seen_at") if ei else None,
                            "source": ei.get("source") if ei else None,
                            "created_time": m.get("created_time", m.get("created")),
                            "status": "active",
                        })

                    cached_pending = []
                    for inv in (pending_items or []):
                        c_email = inv.get("email_address", inv.get("email", ""))
                        ei = exp_map.get((c_email or "").lower())
                        cached_pending.append({
                            "id": inv.get("id", ""), "email": c_email,
                            "name": None,
                            "role": inv.get("role", "standard-user"),
                            "seat_type": inv.get("seat_type", "default"),
                            "is_owner": False,
                            "expires_at": ei["expires_at"] if ei else None,
                            "first_seen_at": ei.get("first_seen_at") if ei else None,
                            "source": ei.get("source") if ei else None,
                            "created_time": inv.get("created_time", inv.get("created")),
                            "status": "pending",
                        })

                    # 库里已有一份开始得更晚的快照（例如管理端刚刷新过）时不覆盖它，
                    # 也不拿这份旧名单去改 teams 上的人数。
                    snapshot_written = store_member_snapshot_sync(
                        conn, team_id, cached_members, cached_pending, fetch_started_at,
                        updated_at=now,
                    )

                    member_usage = (
                        member_seat_usage_from_members(cached_members) if snapshot_written else None
                    )
                    if member_usage is not None:
                        member_updates = []
                        member_params = []
                        if "error" in subscription or subscription.get("seats_in_use") is None:
                            member_updates.append("seats_in_use = ?")
                            member_params.append(member_usage.seats_in_use_total)
                        if official_codex is None:
                            member_updates.append("codex_count = ?")
                            member_params.append(member_usage.codex_count)
                        if official_chatgpt is None:
                            member_updates.append("chatgpt_count = ?")
                            member_params.append(member_usage.active_chatgpt)
                        if member_updates:
                            member_updates.append("updated_at = ?")
                            member_params.extend([now, team_id])
                            conn.execute(
                                f"UPDATE teams SET {', '.join(member_updates)} WHERE id = ?",
                                member_params,
                            )
                    conn.commit()

                    report_team_recovery_sync(
                        team_id,
                        "team_sync",
                        source="scheduled_data_sync",
                    )
                    report_team_recovery_sync(
                        team_id,
                        "chatgpt_auth",
                        source="scheduled_data_sync",
                    )
                    # 成员快照拉到了，且执行链路的输入（成员/邀请名单、订阅、
                    # 席位数）一个没漏，才算这一轮成功。展示接口失败照常播报，
                    # 但不参与挂起计时——见 enforcement_failures 处的说明。
                    round_ok = not enforcement_failures
                    snapshot_ok = not (set(enforcement_failures) - {"subscription"})
                    if display_failures and not enforcement_failures:
                        logger.warning(
                            "data_sync: display-only sub-interface failures on team=%s: %s "
                            "(enforcement inputs all green; not counted toward suspension)",
                            team_id,
                            ", ".join(display_failures),
                        )
                        _log_operation_sync(
                            team_id,
                            "data_sync_display_degraded",
                            None,
                            "display-only failures: " + ", ".join(display_failures),
                            "warning",
                        )
                else:
                    failed_team_ids.add(team_id)
                    sync_error = "; ".join(
                        part
                        for part in (
                            f"members: {m_err}" if m_err else "",
                            f"pending invites: {p_err}" if p_err else "",
                        )
                        if part
                    )
                    _log_operation_sync(
                        team_id,
                        "data_sync",
                        None,
                        "member snapshot refresh failed",
                        "failed",
                        sync_error,
                    )
                    report_team_failure_sync(
                        team_id,
                        "chatgpt_auth" if is_auth_error(sync_error) else "team_sync",
                        sync_error,
                        source="scheduled_data_sync",
                    )

                event = _record_team_sync_outcome(
                    conn,
                    team_id,
                    ok=round_ok,
                    now=datetime.now(timezone.utc),
                    failing_since=failing_since,
                    suspended_at=suspended_at,
                    last_full_sync_at=last_full_sync_at,
                )
                if event == "suspended":
                    newly_suspended.append((team_id, team_name))
                elif event == "resumed":
                    resumed_from_suspension.append((team_id, team_name))
                if suspended_at and event != "resumed":
                    # 探活失败只是"还没好"，不算本轮同步失败：否则每 6 小时
                    # 一次的探针会把日报摘要一并掐掉。
                    failed_team_ids.discard(team_id)
                    suspended_team_ids.add(team_id)

                # ── 巡逻白名单 ──
                # 挂起中（包括本轮刚挂起）的 team 不进：它的快照不再按时刷新。
                suspended_now = event == "suspended" or bool(suspended_at and event != "resumed")
                if snapshot_ok and not suspended_now:
                    if entitlement_fresh:
                        refreshed_team_ids.add(team_id)
                    else:
                        _admit_team_with_unconfirmed_entitlement(
                            conn,
                            team_id,
                            entitlement_problem,
                            refreshed_team_ids,
                            unconfirmed_entitlement_team_ids,
                        )
                if entitlement_fresh:
                    report_team_recovery_sync(
                        team_id, ENTITLEMENT_ALERT_KEY, source="scheduled_data_sync"
                    )

            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                failed_team_ids.add(team_id)
                _log_operation_sync(team_id, "data_sync", None, None, "failed", str(e))
                report_team_failure_sync(
                    team_id,
                    "chatgpt_auth" if is_auth_error(e) else "team_sync",
                    e,
                    source="scheduled_data_sync",
                )
                if _record_team_sync_outcome(
                    conn,
                    team_id,
                    ok=False,
                    now=datetime.now(timezone.utc),
                    failing_since=failing_since,
                    suspended_at=suspended_at,
                    last_full_sync_at=last_full_sync_at,
                ) == "suspended":
                    newly_suspended.append((team_id, team_name))
                if suspended_at:
                    failed_team_ids.discard(team_id)
                    suspended_team_ids.add(team_id)

        conn.close()
        skipped = len(suspended_team_ids)
        succeeded = len(teams) - len(failed_team_ids) - skipped
        # 挂起的 Team 不进这个判定：它本来就是"已知坏了、已经停手"的状态，
        # 再让它一直把整轮标成失败，日报摘要会跟着永远发不出去。
        sync_completed = not failed_team_ids
        _log_operation_sync(
            None,
            "data_sync",
            None,
            f"Synced {succeeded}/{len(teams)} teams; failed={len(failed_team_ids)}; "
            f"suspended={skipped}",
            "success" if sync_completed else "failed",
            ",".join(sorted(failed_team_ids)) or None,
        )

        for team_id, _team_name in newly_suspended:
            _log_operation_sync(
                team_id,
                "data_sync_suspended",
                None,
                f"sync suspended after {SYNC_FAILURE_SUSPEND_HOURS}h of continuous failure; "
                f"probing every {SYNC_SUSPENDED_PROBE_HOURS}h",
                "success",
            )
        for team_id, _team_name in resumed_from_suspension:
            _log_operation_sync(
                team_id, "data_sync_resumed", None, "sync recovered; suspension lifted", "success",
            )

        # 挂起中的 Team 不再重复播报同一条失败：它的失败已经用「同步已暂停」
        # 播报过一次，恢复时也会播报一次，中间的每一轮都是同一句废话。
        suspended_ids = suspended_team_ids | {tid for tid, _ in newly_suspended}
        pending_failure_alerts = [
            item for item in teams_with_overview_failures if item[0] not in suspended_ids
        ]

        # Send consolidated Telegram notification for overview sync failures
        if pending_failure_alerts:
            try:
                from .services.tg_notify import notify_admins_sync
                from .tg_format import detail_card

                rows = ["🔴 定时数据同步检测到部分数据源失败："]
                for team_id, team_name, failures in pending_failure_alerts:
                    failed_sources = ", ".join(failures)
                    rows.append(f"• {team_name} (id: {team_id}): {failed_sources}")

                text = detail_card("⚠️ 数据源同步失败", rows)
                notify_admins_sync(text)
            except Exception as e:
                _log_operation_sync(
                    None,
                    "overview_failures_notification",
                    None,
                    None,
                    "failed",
                    str(e),
                )

        if newly_suspended or resumed_from_suspension:
            try:
                from .services.tg_notify import notify_admins_sync
                from .tg_format import detail_card

                if newly_suspended:
                    rows = [
                        f"连续 {SYNC_FAILURE_SUSPEND_HOURS} 小时同步失败，已停止定时请求："
                    ]
                    for team_id, team_name in newly_suspended:
                        rows.append(f"• {team_name} (id: {team_id})")
                    rows.append(
                        f"每 {SYNC_SUSPENDED_PROBE_HOURS} 小时探活一次；"
                        "重新导入会话或手动同步成功即恢复。"
                    )
                    notify_admins_sync(detail_card("⏸️ 同步已暂停", rows))

                if resumed_from_suspension:
                    rows = ["以下 Team 同步已恢复，定时请求继续："]
                    for team_id, team_name in resumed_from_suspension:
                        rows.append(f"• {team_name} (id: {team_id})")
                    notify_admins_sync(detail_card("✅ 同步已恢复", rows))
            except Exception as e:
                _log_operation_sync(
                    None,
                    "sync_suspension_notification",
                    None,
                    None,
                    "failed",
                    str(e),
                )
    except Exception as e:
        _log_operation_sync(None, "data_sync_job_error", None, None, "failed", str(e))

    # ── 巡逻踢人：必须在成员缓存刷新完之后跑，保证"自动刷新后才踢" ──────────
    # 独立 try/except：巡逻出任何问题都绝不能拖垮 data_sync_job 本身。
    #
    # 每个 team 独立判断，不再一票否决。传的是**白名单**：只有本轮刚刚刷新成功的
    # team 才巡逻。同步失败的、已挂起的、以及这个函数在中途 return/抛异常时一个都
    # 没刷新到的，全都自动落在白名单外——黑名单在同样的情况下会交出一个空集合，
    # 巡逻就会拿着所有 team 的陈旧缓存全量开工。
    try:
        from .services.patrol import run_patrol

        patrol_conn = _get_sync_db()
        kick_row = patrol_conn.execute(
            "SELECT value FROM settings WHERE key = 'patrol_kick_enabled'"
        ).fetchone()
        patrol_conn.close()
        patrol_live = bool(kick_row and kick_row["value"] == "1")
        run_patrol(
            dry_run=not patrol_live,
            allow_team_ids=refreshed_team_ids,
            skip_over_quota_team_ids=unconfirmed_entitlement_team_ids,
        )
    except Exception as e:
        _log_operation_sync(None, "patrol_job_error", None, None, "failed", str(e))

    if failed_team_ids or suspended_team_ids:
        not_patrolled = sorted((failed_team_ids | suspended_team_ids) - refreshed_team_ids)
        _log_operation_sync(
            None,
            "patrol_partial_skip",
            None,
            f"patrolled {len(refreshed_team_ids)} freshly synced team(s); "
            f"{len(not_patrolled)} team(s) not refreshed this round were left alone",
            "success",
            ",".join(not_patrolled) or None,
        )

    # 摘要只在同步主流程完整走完后尝试发送；服务内部负责开关和间隔节流。
    if sync_completed:
        try:
            from .services.tg_summary import maybe_send_summary_sync

            maybe_send_summary_sync()
        except Exception as e:
            _log_operation_sync(None, "tg_summary_job_error", None, None, "failed", str(e))


def member_watch_job():
    """
    每 30s 执行：监视成员变动任务。
    - invite 类型：检测目标邮件是否出现在 members / pending_invites 中
    - kick 类型  ：检测目标是否从 members / pending_invites 中消失
    满足条件或超时后：刷新缓存并标记 done=1
    """
    try:
        conn = _get_sync_db()
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        cursor = conn.execute("""
            SELECT mw.*, t.access_token, t.device_id, t.proxy_id, t.name AS team_name
            FROM member_watch mw
            JOIN teams t ON mw.team_id = t.id
            WHERE mw.done = 0 AND t.status = 'active'
        """)
        watches = cursor.fetchall()

        for watch in watches:
            team_id = watch["team_id"]
            reason = watch["reason"]
            target_email = (watch["target_email"] or "").lower()
            target_user_id = watch["target_user_id"] or ""
            watch_id = watch["id"]
            expires_at_str = watch["expires_at"]
            access_token = watch["access_token"]
            device_id = watch["device_id"]

            # 超时判断
            timed_out = False
            if expires_at_str:
                try:
                    exp_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    timed_out = now_dt >= exp_dt
                except Exception:
                    timed_out = True

            try:
                proxy_url = _get_proxy_url_sync(conn, watch["proxy_id"])
                client = ChatGPTClient(access_token, team_id, device_id, proxy_url=proxy_url)

                # 拉取最新成员列表
                fetch_started_at = snapshot_fetch_started_now()
                members, members_error = _fetch_all_api_items_sync(
                    client.get_members, "users", require_items=True
                )
                pending, pending_error = _fetch_all_api_items_sync(client.get_pending_invites, "invites")

                if members_error or pending_error:
                    if timed_out:
                        conn.execute("UPDATE member_watch SET done = 1 WHERE id = ?", (watch_id,))
                        conn.commit()
                        tg_cid = watch["tg_chat_id"]
                        tg_mid = watch["tg_message_id"]
                        if tg_cid and tg_mid:
                            t_name = watch["team_name"] or team_id
                            edit_message_sync(str(tg_cid), int(tg_mid),
                                              f"⚠️ 监视超时且 API 查询失败，{target_email} 在「{t_name}」的状态未能确认")
                    continue

                # 检查条件是否满足
                condition_met = False
                tg_cid = watch["tg_chat_id"]
                tg_mid = watch["tg_message_id"]
                t_name = watch["team_name"] or team_id

                if reason == "invite":
                    in_members = any(
                        (m.get("email") or "").lower() == target_email
                        or (m.get("id") or m.get("user_id") or "") == target_user_id
                        for m in members if target_email or target_user_id
                    )
                    in_pending = any(
                        (inv.get("email_address") or inv.get("email") or "").lower() == target_email
                        for inv in pending if target_email
                    )
                    # 有 TG 追踪时只在目标真正接受(出现在 members)才算完成
                    if tg_cid:
                        condition_met = in_members
                    else:
                        condition_met = in_members or in_pending

                elif reason == "kick":
                    # 目标应从两个列表中消失
                    still_member = any(
                        (m.get("email") or "").lower() == target_email
                        or (m.get("id") or m.get("user_id") or "") == target_user_id
                        for m in members if target_email or target_user_id
                    )
                    still_pending = any(
                        (inv.get("email_address") or inv.get("email") or "").lower() == target_email
                        for inv in pending if target_email
                    )
                    condition_met = not still_member and not still_pending

                if condition_met or timed_out:
                    # 刷新缓存：读取 expiry 信息再写入
                    expiry_rows = conn.execute(
                        "SELECT * FROM member_expiry WHERE team_id = ? AND kicked = 0", (team_id,)
                    ).fetchall()
                    expiry_map = {}
                    for er in expiry_rows:
                        if er["user_id"]:
                            expiry_map[er["user_id"]] = dict(er)
                        if er["email"]:
                            expiry_map[er["email"].lower()] = dict(er)

                    cached_members = []
                    for m in members:
                        uid = m.get("id") or m.get("user_id") or ""
                        email = m.get("email") or ""
                        exp_info = expiry_map.get(uid) or expiry_map.get(email.lower())
                        cached_members.append({
                            "id": uid, "email": email,
                            "name": m.get("name"),
                            "role": m.get("role", "standard-user"),
                            "seat_type": m.get("seat_type", "default"),
                            "is_owner": m.get("role") == "account-owner",
                            "expires_at": exp_info["expires_at"] if exp_info else None,
                            "first_seen_at": exp_info.get("first_seen_at") if exp_info else None,
                            "source": exp_info.get("source") if exp_info else None,
                            "created_time": m.get("created_time", m.get("created")),
                            "status": "active",
                        })

                    cached_pending = []
                    for inv in pending:
                        email = inv.get("email_address", inv.get("email", ""))
                        exp_info = expiry_map.get(email.lower())
                        cached_pending.append({
                            "id": inv.get("id", ""), "email": email,
                            "name": None,
                            "role": inv.get("role", "standard-user"),
                            "seat_type": inv.get("seat_type", "default"),
                            "is_owner": False,
                            "expires_at": exp_info["expires_at"] if exp_info else None,
                            "first_seen_at": exp_info.get("first_seen_at") if exp_info else None,
                            "source": exp_info.get("source") if exp_info else None,
                            "created_time": inv.get("created_time", inv.get("created")),
                            "status": "pending",
                        })

                    snapshot_written = store_member_snapshot_sync(
                        conn, team_id, cached_members, cached_pending, fetch_started_at,
                        updated_at=now_iso,
                    )
                    member_usage = (
                        member_seat_usage_from_members(cached_members) if snapshot_written else None
                    )
                    if member_usage is not None:
                        conn.execute(
                            """UPDATE teams SET
                                 seats_in_use = ?,
                                 codex_count = ?,
                                 chatgpt_count = ?,
                                 updated_at = ?
                               WHERE id = ?""",
                            (
                                member_usage.seats_in_use_total,
                                member_usage.codex_count,
                                member_usage.active_chatgpt,
                                now_iso,
                                team_id,
                            ),
                        )
                    conn.execute("UPDATE member_watch SET done = 1 WHERE id = ?", (watch_id,))
                    conn.commit()

                    reason_label = "member_watch_invite" if reason == "invite" else "member_watch_kick"
                    _log_operation_sync(
                        team_id, reason_label, target_email or None,
                        f"timed_out={timed_out}", "success"
                    )

                    # 编辑 TG 消息通知结果
                    if tg_cid and tg_mid:
                        if condition_met:
                            if reason == "invite":
                                tg_text = f"✅ {target_email} 已接受邀请加入「{t_name}」"
                            else:
                                tg_text = f"✅ 已确认 {target_email} 已从「{t_name}」移出"
                        else:
                            if reason == "invite":
                                tg_text = f"⏰ {target_email} 未在 30 分钟内接受「{t_name}」的邀请"
                            else:
                                tg_text = f"⚠️ 未能确认 {target_email} 是否已从「{t_name}」移出"
                        edit_message_sync(str(tg_cid), int(tg_mid), tg_text)

            except Exception as e:
                _log_operation_sync(team_id, "member_watch_error", target_email or None,
                                    f"watch_id={watch_id}", "failed", str(e))

        conn.close()
    except Exception as e:
        _log_operation_sync(None, "member_watch_job_error", None, None, "failed", str(e))


def reschedule_sync_job(interval_minutes: int):
    try:
        scheduler.remove_job("data_sync_job")
    except Exception:
        pass
    scheduler.add_job(
        data_sync_job, "interval", minutes=interval_minutes, id="data_sync_job", replace_existing=True
    )


def member_expiry_reminder_job():
    try:
        result = run_member_expiry_reminders_sync()
        if result.get("sent", 0) > 0:
            _log_operation_sync(
                None,
                "tg_member_expiry_reminder",
                None,
                f"sent={result['sent']}, due={result.get('due', 0)}",
                "success",
            )
    except Exception as exc:
        _log_operation_sync(
            None,
            "tg_member_expiry_reminder",
            None,
            None,
            "failed",
            str(exc),
        )


def renewal_idle_seat_reminder_job():
    """续费前 3 天内还有没人用的计费席位就提醒管理员（只读本地已同步的数据，见 renewal_reminders）。
    每个 Team 自己的送达记录写在 renewal_idle_seat_reminder 日志里，这里只记整轮崩溃。"""
    try:
        run_renewal_idle_seat_reminders_sync()
    except Exception as exc:
        _log_operation_sync(
            None,
            "renewal_idle_seat_reminder",
            None,
            None,
            "failed",
            str(exc),
        )


def pending_redemption_reconciliation_job():
    try:
        # 延迟导入避免 scheduler -> routes -> main 的模块环；job 真正运行时
        # 应用路由已经全部加载完成。
        from .routes.access_tokens import reconcile_pending_redemptions

        result = asyncio.run(reconcile_pending_redemptions())
        if result.get("confirmed") or result.get("released"):
            _log_operation_sync(
                None,
                "pending_redemption_reconciliation",
                None,
                (
                    f"confirmed={result.get('confirmed', 0)}, "
                    f"released={result.get('released', 0)}, "
                    f"waiting={result.get('waiting', 0)}"
                ),
                "success",
            )
    except Exception as exc:
        _log_operation_sync(
            None,
            "pending_redemption_reconciliation",
            None,
            None,
            "failed",
            str(exc),
        )


def fx_refresh_job(only_if_stale: bool = False):
    """Refresh exchange rates; failures are logged and the old rates are kept."""
    from .services.fx import refresh_fx_rates_safely

    try:
        asyncio.run(refresh_fx_rates_safely(only_if_stale=only_if_stale))
    except Exception:
        logging.getLogger(__name__).warning("FX refresh job crashed", exc_info=True)


def get_sync_interval() -> int:
    try:
        conn = _get_sync_db()
        cursor = conn.execute("SELECT value FROM settings WHERE key = 'sync_interval_minutes'")
        row = cursor.fetchone()
        conn.close()
        if row:
            return int(row["value"])
    except Exception:
        pass
    return 15


def start_scheduler():
    sync_interval = get_sync_interval()

    scheduler.add_job(auto_kick_job, "interval", seconds=60, id="auto_kick_job", replace_existing=True)
    scheduler.add_job(data_sync_job, "interval", minutes=sync_interval, id="data_sync_job", replace_existing=True)
    scheduler.add_job(member_watch_job, "interval", seconds=30, id="member_watch_job", replace_existing=True)
    scheduler.add_job(
        pending_redemption_reconciliation_job,
        "interval",
        seconds=60,
        id="pending_redemption_reconciliation_job",
        replace_existing=True,
    )
    scheduler.add_job(
        member_expiry_reminder_job,
        "interval",
        minutes=5,
        id="member_expiry_reminder_job",
        replace_existing=True,
    )
    scheduler.add_job(
        renewal_idle_seat_reminder_job,
        "interval",
        minutes=30,
        id="renewal_idle_seat_reminder_job",
        replace_existing=True,
    )
    # 汇率：每天一次，并在启动后 1 分钟内补刷一次（仅当库里的汇率已超过 24h）。
    scheduler.add_job(
        fx_refresh_job,
        "cron",
        hour=3,
        minute=17,
        id="fx_refresh_job",
        replace_existing=True,
    )
    scheduler.add_job(
        fx_refresh_job,
        "date",
        run_date=datetime.now() + timedelta(seconds=60),
        kwargs={"only_if_stale": True},
        id="fx_refresh_startup_job",
        replace_existing=True,
    )

    scheduler.start()


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
