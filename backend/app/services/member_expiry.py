import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from ..database import get_db, log_operation
from .tg_member_bindings import deactivate_member_binding_if_inactive
from .tg_commands import sync_email_chat_commands_sync
from ..utils.durations import (
    duration_to_timedelta,
    expiry_from_duration,
    normalize_duration,
    parse_optional_datetime,
    utc_now,
)


logger = logging.getLogger(__name__)

APP_LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 邀请成功后本地落库的重试次数/退避。这里的写入是在 OpenAI 已经把人加进 Team 之后
# 发生的——那个动作不可回滚，所以宁可多等一点也要让本地记录落地。
_CONFIRM_WRITE_ATTEMPTS = 3
_CONFIRM_WRITE_BACKOFF_SECONDS = 0.3

# 续期写入的乐观并发重试次数（见 extend_member_expiry）。
_EXTEND_WRITE_ATTEMPTS = 3

# 定位"当前这个人在这个 Team 里还没被踢掉的那条记录"。与 upsert_member_expiry
# 里的子查询、scheduler 的检测逻辑逐字一致，避免两处选到不同的行。
_ACTIVE_EXPIRY_ROW_SQL = """
    SELECT id, expires_at, source
    FROM member_expiry
    WHERE team_id = ?
      AND kicked = 0
      AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
    ORDER BY COALESCE(created_at, first_seen_at) DESC, id DESC
    LIMIT 1
"""


class PermanentMembershipError(Exception):
    """当前成员是永久成员（member_expiry.expires_at 为 NULL）。

    给永久成员"续"一段有限时长只可能是降级：原本永不到期的人会变成 N 天后被
    自动踢出。这属于必须让人看见的冲突，而不是可以默默处理的边界情况，所以这里
    直接抛出来，由调用方决定怎么向用户交代（自助兑换是拒绝并保留兑换码）。
    """


class ExtensionReceiptMismatchError(Exception):
    """A request id was reused for a different member extension."""


async def get_active_expiry_state(team_id: str, user_id: str, email: str) -> str:
    """这个人在这个 Team 里当前的到期管理状态。

    返回值三选一，与 ``extend_member_expiry`` 内部走的分支一一对应：

    * ``"dated"``      —— 有未踢出的记录且带到期时间：续期就是往后加。
    * ``"permanent"``  —— 有未踢出的记录但 expires_at 为 NULL：续期会抛
      ``PermanentMembershipError``。
    * ``"unmanaged"``  —— 本地没有未踢出的记录，或者只有一条 ``source='detected'``
      且没有到期时间的记录：续期会给它装上到期时间和 ``auto_kick=1``。

    展示层必须能区分后两者：两者的 ``expires_at`` 都是 NULL，但一个不能续、
    另一个一续就会给人装上踢人倒计时。

    ``detected + NULL`` 不是永久授权，只是"巡逻发现了一个不是本系统邀请进来的人，
    还没有任何授权"。把它当永久会拒收一张本该正常使用的兑换码，而巡逻那边同时仍
    把这个人当未授权对象——两个模块对同一行的理解必须一致。
    """
    normalized_email = (email or "").strip().lower()
    normalized_user_id = user_id or ""
    if not normalized_user_id and not normalized_email:
        return "unmanaged"

    async with get_db() as db:
        cursor = await db.execute(
            _ACTIVE_EXPIRY_ROW_SQL,
            (
                team_id,
                normalized_user_id,
                normalized_user_id,
                normalized_email,
                normalized_email,
            ),
        )
        row = await cursor.fetchone()

    if row is None:
        return "unmanaged"
    if row["expires_at"] is not None:
        return "dated"
    return "unmanaged" if (row["source"] or "") == "detected" else "permanent"


def expires_in_to_datetime(expires_in: str) -> Optional[datetime]:
    duration = normalize_duration(expires_in, allow_never=True)
    return expiry_from_duration(duration)


async def upsert_member_expiry(
    team_id: str,
    user_id: str,
    email: str,
    expires_at: Optional[datetime],
    *,
    source: str = "system",
) -> Optional[str]:
    normalized_email = (email or "").strip().lower()
    normalized_user_id = user_id or ""
    now = utc_now().isoformat()
    expires_iso = expires_at.isoformat() if expires_at is not None else None
    auto_kick = 1 if expires_at is not None else 0

    if not normalized_user_id and not normalized_email:
        return expires_iso

    async with get_db() as db:
        cursor = await db.execute(
            """UPDATE member_expiry
               SET user_id = ?, email = ?, expires_at = ?, auto_kick = ?, source = ?,
                   kicked = 0, kicked_at = NULL, kick_source = NULL
               WHERE id = (
                   SELECT id FROM member_expiry
                   WHERE team_id = ?
                     AND kicked = 0
                     AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
                   ORDER BY COALESCE(created_at, first_seen_at) DESC, id DESC
                   LIMIT 1
               )""",
            (
                normalized_user_id,
                normalized_email,
                expires_iso,
                auto_kick,
                source,
                team_id,
                normalized_user_id,
                normalized_user_id,
                normalized_email,
                normalized_email,
            ),
        )
        if cursor.rowcount == 0:
            await db.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked,
                    first_seen_at, source, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (team_id, normalized_user_id, normalized_email, expires_iso, auto_kick, now, source, now),
            )
        await db.commit()

    return expires_iso


async def extend_member_expiry(
    team_id: str,
    user_id: str,
    email: str,
    duration: str,
    *,
    source: str = "system",
    keep_permanent: bool = False,
    token_use_id: Optional[int] = None,
    token_action: Optional[str] = None,
    admin_request_id: Optional[str] = None,
    admin_audit_detail: Optional[str] = None,
) -> Optional[str]:
    """在成员**现有**到期时间之上追加 ``duration``，而不是覆盖它。

    语义（唯一一处定义，续期分支和邀请分支都走这里）：

        新到期 = max(现有到期, 现在) + duration

    * 现有到期还在未来 → 从它往后加，客户已经买过的时长一分不少（覆盖式写入
      会让"剩 300 天的人兑换一张 30 天码"直接掉到 30 天）。
    * 现有到期已经过去 / 无法解析 → 从现在起算，不会因为一段陈年过期时间
      而把新买的时长吃掉。
    * 本地没有记录（成员是刚被外部加进来、检测任务还没跑到，或上一段成员身份
      已经 kicked=1 归档）→ 没有任何已购时长需要保护，按现在起算并建档。
    * 现有到期为 NULL（永久成员）→ 默认抛 ``PermanentMembershipError``，绝不把
      永久改成有期限；``keep_permanent=True`` 时改为保持永久并返回 None，供
      "外部动作已经发生、此刻不能再失败"的收尾路径使用。
    * ``duration == "never"`` → 直接升级为永久，没有时长损失。

    整个"读当前值 → 算新值 → 写回"在一个 ``BEGIN IMMEDIATE`` 事务里完成，并且
    写回时带上"现有值没被人改过"的条件（CAS）：同一个人几乎同时兑换两张码时，
    两次都会各自加满，不会一方把另一方的结果覆盖掉。
    """
    normalized_email = (email or "").strip().lower()
    normalized_user_id = user_id or ""
    if not normalized_user_id and not normalized_email:
        return None

    delta = None if duration == "never" else duration_to_timedelta(duration)
    match_params = (
        team_id,
        normalized_user_id,
        normalized_user_id,
        normalized_email,
        normalized_email,
    )

    for attempt in range(1, _EXTEND_WRITE_ATTEMPTS + 1):
        async with get_db() as db:
            # 立刻拿写锁，让并发的另一次续期排队等待，而不是读到同一个旧值。
            await db.execute("BEGIN IMMEDIATE")
            if admin_request_id:
                cursor = await db.execute(
                    """SELECT email, duration, expires_at
                       FROM admin_expiry_extension_receipts
                       WHERE team_id = ? AND user_id = ? AND request_id = ?""",
                    (team_id, normalized_user_id, admin_request_id),
                )
                receipt = await cursor.fetchone()
                if receipt is not None:
                    if receipt["email"] != normalized_email or receipt["duration"] != duration:
                        await db.rollback()
                        raise ExtensionReceiptMismatchError(
                            "request_id 已用于另一笔续期，不能复用"
                        )
                    await db.rollback()
                    return receipt["expires_at"]
            if token_use_id is not None:
                cursor = await db.execute(
                    "SELECT result, expires_at FROM access_token_uses WHERE id = ?",
                    (token_use_id,),
                )
                token_use = await cursor.fetchone()
                if not token_use:
                    await db.rollback()
                    raise RuntimeError(f"redemption attempt not found: {token_use_id}")
                if token_use["result"] == "success":
                    await db.rollback()
                    return token_use["expires_at"]
                if token_use["result"] not in {"pending", "uncertain"}:
                    await db.rollback()
                    raise RuntimeError(
                        f"redemption attempt is not active: {token_use_id} "
                        f"({token_use['result']})"
                    )

            cursor = await db.execute(_ACTIVE_EXPIRY_ROW_SQL, match_params)
            row = await cursor.fetchone()
            now = utc_now()

            if row is None:
                new_expires = None if delta is None else now + delta
                now_iso = now.isoformat()
                await db.execute(
                    """INSERT INTO member_expiry
                       (team_id, user_id, email, expires_at, auto_kick, kicked,
                        first_seen_at, source, created_at)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                    (
                        team_id,
                        normalized_user_id,
                        normalized_email,
                        new_expires.isoformat() if new_expires is not None else None,
                        1 if new_expires is not None else 0,
                        now_iso,
                        source,
                        now_iso,
                    ),
                )
                expires_iso = new_expires.isoformat() if new_expires is not None else None
                await _finalize_token_use(
                    db,
                    token_use_id,
                    token_action,
                    team_id,
                    normalized_user_id,
                    expires_iso,
                )
                await _finalize_admin_extension(
                    db,
                    team_id,
                    normalized_user_id,
                    normalized_email,
                    duration,
                    expires_iso,
                    admin_request_id,
                    admin_audit_detail,
                    now_iso,
                )
                await db.commit()
                return expires_iso

            current_raw = row["expires_at"]
            if delta is None:
                await db.execute(
                    """UPDATE member_expiry
                       SET user_id = ?, email = ?, expires_at = NULL, auto_kick = 0,
                           source = ?, kicked = 0, kicked_at = NULL, kick_source = NULL
                       WHERE id = ?""",
                    (
                        normalized_user_id,
                        normalized_email,
                        source,
                        row["id"],
                    ),
                )
                await _finalize_token_use(
                    db,
                    token_use_id,
                    token_action,
                    team_id,
                    normalized_user_id,
                    None,
                )
                await _finalize_admin_extension(
                    db,
                    team_id,
                    normalized_user_id,
                    normalized_email,
                    duration,
                    None,
                    admin_request_id,
                    admin_audit_detail,
                    now.isoformat(),
                )
                await db.commit()
                return None

            # detected + NULL 不是永久授权（见 get_active_expiry_state）：这里必须
            # 落到下面的常规分支，给它写上到期时间和 auto_kick=1，而不是拒收兑换码
            # （keep_permanent=False）或核销后原样保留永久（keep_permanent=True）。
            if current_raw is None and (row["source"] or "") != "detected":
                if not keep_permanent:
                    await db.rollback()
                    raise PermanentMembershipError(
                        f"member is permanent (expires_at is NULL): "
                        f"team_id={team_id!r} user_id={normalized_user_id!r} email={normalized_email!r}"
                    )
                # 这里不能再失败（调用方的外部动作已经生效），保持永久是唯一
                # 不会伤害成员的选择；记一条日志让管理员能发现这条脏记录。
                logger.warning(
                    "extend_member_expiry: keeping permanent membership as-is "
                    "(stale NULL expires_at) team=%s email=%s user_id=%s duration=%s",
                    team_id, normalized_email, normalized_user_id, duration,
                )
                await _finalize_token_use(
                    db,
                    token_use_id,
                    token_action,
                    team_id,
                    normalized_user_id,
                    None,
                )
                await _finalize_admin_extension(
                    db,
                    team_id,
                    normalized_user_id,
                    normalized_email,
                    duration,
                    None,
                    admin_request_id,
                    admin_audit_detail,
                    now.isoformat(),
                )
                await db.commit()
                return None

            current = parse_optional_datetime(current_raw)
            base = current if (current is not None and current > now) else now
            new_expires = base + delta

            cursor = await db.execute(
                """UPDATE member_expiry
                   SET user_id = ?, email = ?, expires_at = ?, auto_kick = 1, source = ?,
                       kicked = 0, kicked_at = NULL, kick_source = NULL
                   WHERE id = ? AND expires_at IS ?""",
                (
                    normalized_user_id,
                    normalized_email,
                    new_expires.isoformat(),
                    source,
                    row["id"],
                    current_raw,
                ),
            )
            if cursor.rowcount == 1:
                await _finalize_token_use(
                    db,
                    token_use_id,
                    token_action,
                    team_id,
                    normalized_user_id,
                    new_expires.isoformat(),
                )
                await _finalize_admin_extension(
                    db,
                    team_id,
                    normalized_user_id,
                    normalized_email,
                    duration,
                    new_expires.isoformat(),
                    admin_request_id,
                    admin_audit_detail,
                    now.isoformat(),
                )
                await db.commit()
                return new_expires.isoformat()
            await db.rollback()

        # rowcount == 0：这一行在事务之外被别人改了（例如管理员同时改到期时间）。
        # 重新读一次再算，绝不拿着过期的基准值硬写。
        logger.warning(
            "extend_member_expiry: expiry changed under us, retrying (%s/%s) "
            "team=%s email=%s",
            attempt, _EXTEND_WRITE_ATTEMPTS, team_id, normalized_email,
        )

    raise RuntimeError(
        f"extend_member_expiry: gave up after {_EXTEND_WRITE_ATTEMPTS} attempts "
        f"(concurrent writers) team_id={team_id!r} email={normalized_email!r}"
    )


async def _finalize_admin_extension(
    db,
    team_id: str,
    user_id: str,
    email: str,
    duration: str,
    expires_at: Optional[str],
    request_id: Optional[str],
    audit_detail: Optional[str],
    now_iso: str,
) -> None:
    """Persist an admin extension receipt and audit row in the expiry transaction."""
    if request_id:
        await db.execute(
            """INSERT INTO admin_expiry_extension_receipts
               (team_id, user_id, request_id, email, duration, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (team_id, user_id, request_id, email, duration, expires_at, now_iso),
        )
    if audit_detail is not None:
        await db.execute(
            """INSERT INTO operation_logs
               (team_id, action, target_email, detail, result, error_message, trigger_type, created_at)
               VALUES (?, 'extend_expiry', ?, ?, 'success', NULL, 'manual', ?)""",
            (team_id, email, audit_detail, now_iso),
        )


async def _finalize_token_use(
    db,
    token_use_id: Optional[int],
    action: Optional[str],
    team_id: str,
    user_id: str,
    expires_at: Optional[str],
) -> None:
    """把兑换收据与成员到期时间放在同一个 SQLite 事务中提交。"""
    if token_use_id is None:
        return
    cursor = await db.execute(
        """UPDATE access_token_uses
           SET action = ?, team_id = ?, user_id = ?, expires_at = ?,
               result = 'success', error_message = NULL
           WHERE id = ? AND result IN ('pending', 'uncertain')""",
        (action or "redeemed", team_id, user_id, expires_at, token_use_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"failed to finalize redemption attempt: {token_use_id}")
    await db.execute(
        "DELETE FROM redemption_email_claims WHERE token_use_id = ?",
        (token_use_id,),
    )


async def _insert_pending_invite_reconciliation(
    team_id: str,
    user_id: str,
    email: str,
    expires_iso: Optional[str],
    source: str,
    reason: str,
    *,
    token_use_id: Optional[int] = None,
    kind: str = "backfill",
) -> None:
    """写一条巡逻屏障行。

    ``kind='backfill'``：远端邀请已确认成功、本地 member_expiry 落库失败，调度器
    看到人出现后按行内 ``expires_at`` 回填，并顺带结清 ``token_use_id`` 这次兑换。

    ``kind='extend'``：``backfill`` 的累加版。远端邀请/续期已确认成功、本地累加
    写入失败，行内 ``expires_at`` 是"落盘时刻 + 购买时长"，调度器据此还原时长并
    在成员现有到期时间之上追加（绝不能只取 max，否则更远的现有到期会把这次购买
    的时长吃掉）。

    ``kind='barrier'``：远端结果未定的自助邀请，只借这张表挡住巡逻撤销。调度器
    绝不能据此写到期时间——结算必须走 ``reconcile_pending_redemptions`` 的累加
    语义，否则同一张码会被两条恢复路径各加一次时长。
    """
    now = utc_now().isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO pending_invite_reconciliations
               (team_id, user_id, email, expires_at, source, reason, resolved, created_at,
                token_use_id, kind)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                team_id,
                user_id or "",
                (email or "").strip().lower(),
                expires_iso,
                source,
                reason,
                now,
                token_use_id,
                kind,
            ),
        )
        await db.commit()


async def record_uncertain_invite(
    team_id: str,
    user_id: str,
    email: str,
    expires_at: Optional[datetime],
    *,
    source: str = "system",
    reason: str = "remote invite result uncertain",
    token_use_id: Optional[int] = None,
    kind: str = "backfill",
) -> None:
    """为结果不确定的邀请建立巡逻安全屏障，等待实时快照确认。

    自助兑换必须传 ``kind='barrier'`` 且 ``expires_at=None``：这条行只用来挡住
    巡逻，时长结算走 ``reconcile_pending_redemptions`` 的累加语义。若按
    ``backfill`` 写入，调度器会把 NULL 到期时间当成"永久"直接落库。
    """
    await _insert_pending_invite_reconciliation(
        team_id,
        user_id,
        email,
        expires_at.isoformat() if expires_at is not None else None,
        source,
        reason,
        token_use_id=token_use_id,
        kind=kind,
    )


async def resolve_invite_barrier(token_use_id: int) -> None:
    """结清某次兑换立下的巡逻屏障行。

    只在这次兑换已经有终态（远端确认成功、或管理员判定失败退码）之后调用：屏障
    在这之前是唯一挡住巡逻撤销我们自己那个邀请的东西。
    """
    async with get_db() as db:
        await db.execute(
            """UPDATE pending_invite_reconciliations
               SET resolved = 1, resolved_at = ?
               WHERE token_use_id = ? AND resolved = 0
                 AND COALESCE(kind, 'backfill') = 'barrier'""",
            (utc_now().isoformat(), token_use_id),
        )
        await db.commit()


async def record_confirmed_invite(
    team_id: str,
    user_id: str,
    email: str,
    expires_at: Optional[datetime],
    *,
    source: str = "system",
) -> Optional[str]:
    """Persist the local membership record for an invite that has *already*
    succeeded on OpenAI's side (member added / pending invite created).

    That external action cannot be rolled back, so silently losing the local
    ``member_expiry`` row here would make the next sync misclassify a member
    the system itself just added as an externally-added ("detected") one —
    and a future auto-kick pass would then remove someone we invited
    ourselves. To avoid that:

      1. retry the primary write a few times to absorb transient errors
         (SQLite lock contention, a brief disk hiccup, ...);
      2. if every retry fails, durably record the gap in
         ``pending_invite_reconciliations`` so it can be found and
         backfilled later;
      3. always leave a clear ``operation_logs`` entry with everything
         needed to backfill by hand;
      4. as an absolute last resort (even those fallback writes failing),
         log to the process logger so the event is never silently dropped.

    Never raises: the OpenAI-side action already happened and cannot be
    undone, so callers must be able to treat the invite as successful
    regardless of whether local bookkeeping fully succeeded.

    邀请绝不缩短、替换已有到期（语义见 ``_merge_confirmed_invite_expiry``）：
    管理员可以对待接受的邀请重发，那条邀请的本地记录里是已付时长或永久授权；
    即使调用方已确认邮箱不在 Team，本地也可能留着一条 kicked=0 的旧记录（邀请在
    OpenAI 侧过期/被撤、成员自己退出而同步还没跟上），同样可能是已付时长或
    永久授权。兜底行 ``kind='backfill'`` 由调度器按同一套规则结清（max、保持永久），
    两条路径结果一致。返回值是落库后的实际到期时间；本地写入全部失败时是兜底行
    里的申请值，需要区分这两种情况的调用方用 ``record_confirmed_invite_expiry``。
    """
    outcome = await record_confirmed_invite_expiry(
        team_id, user_id, email, expires_at, source=source
    )
    return outcome.expires_at


@dataclass(frozen=True)
class ConfirmedInviteExpiry:
    """一次已确认邀请落库之后的到期。

    合并规则会保留更晚的已有到期和已授权的永久记录，所以 ``expires_at`` 可能
    不是这次邀请申请的值；告诉管理员"有效期是多少"必须用它，不能照抄申请值。

    * ``recorded=True``：``expires_at`` 是 member_expiry 里此刻生效的值，None = 永久。
    * ``recorded=False``：本地写入全部失败，只留下 ``kind='backfill'`` 兜底行；
      ``expires_at`` 是兜底行里的申请值，最终到期等调度器对账后才确定。
    """

    expires_at: Optional[str]
    recorded: bool


async def record_confirmed_invite_expiry(
    team_id: str,
    user_id: str,
    email: str,
    expires_at: Optional[datetime],
    *,
    source: str = "system",
) -> ConfirmedInviteExpiry:
    """``record_confirmed_invite`` 的同一套写入，额外报告结果是否已经落库。"""
    recorded = False

    async def _write() -> Optional[str]:
        nonlocal recorded
        stored = await _merge_confirmed_invite_expiry(
            team_id, user_id, email, expires_at, source=source
        )
        recorded = True
        return stored

    stored = await _persist_confirmed_membership(
        _write,
        team_id,
        user_id,
        email,
        expires_at.isoformat() if expires_at is not None else None,
        source,
    )
    return ConfirmedInviteExpiry(expires_at=stored, recorded=recorded)


async def _merge_confirmed_invite_expiry(
    team_id: str,
    user_id: str,
    email: str,
    expires_at: Optional[datetime],
    *,
    source: str = "system",
) -> Optional[str]:
    """把一次已确认邀请的到期时间并入现有记录，只延长、不缩短。

    与调度器结清 ``kind='backfill'`` 兜底行的规则逐条一致：

    * 没有未踢出的记录 → 按邀请的到期建档。
    * 邀请本身是永久（``expires_at is None``）→ 升级为永久。
    * 现有记录 NULL 且 source != 'detected'（已授权的永久）→ 原样保留，不降级。
      这个人此刻不在 Team、记录只是同步还没标成 kicked 时也一样（kicked=1 的
      记录不参与合并，会新建一行）；返回 None，调用方据此报告"永久"。
    * 现有记录 NULL 且 source == 'detected'（外部发现、尚未授权）→ 取邀请的到期，
      source 改成邀请来源。
    * 两边都有到期 → 取较晚者；现有值无法解析时保留现有值，不拿它冒险覆盖。

    与 ``upsert_member_expiry`` 的区别：后者是管理员"设置到期"的覆盖语义（可以
    合法地缩短），这里只用于邀请落库。读、算、写在一个 ``BEGIN IMMEDIATE`` 事务里。
    """
    normalized_email = (email or "").strip().lower()
    normalized_user_id = user_id or ""
    new_iso = expires_at.isoformat() if expires_at is not None else None
    if not normalized_user_id and not normalized_email:
        return new_iso

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            _ACTIVE_EXPIRY_ROW_SQL,
            (
                team_id,
                normalized_user_id,
                normalized_user_id,
                normalized_email,
                normalized_email,
            ),
        )
        row = await cursor.fetchone()
        now_iso = utc_now().isoformat()

        if row is None:
            await db.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked,
                    first_seen_at, source, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    team_id,
                    normalized_user_id,
                    normalized_email,
                    new_iso,
                    1 if new_iso is not None else 0,
                    now_iso,
                    source,
                    now_iso,
                ),
            )
            await db.commit()
            return new_iso

        current_raw = row["expires_at"]
        current_source = row["source"] or ""
        resolved_source = source
        if new_iso is None:
            resolved = None
        elif current_raw is None and current_source != "detected":
            # 已授权的永久成员：保持原样（连 source 一起），不能被一次有限期邀请降级。
            await db.rollback()
            logger.warning(
                "record_confirmed_invite: keeping permanent membership "
                "team=%s email=%s user_id=%s",
                team_id, normalized_email, normalized_user_id,
            )
            return None
        elif current_raw is None:
            resolved = new_iso
        else:
            current = parse_optional_datetime(current_raw)
            # 两边都转成 UTC 感知时间再比较，传入 naive datetime 也不会抛 TypeError。
            incoming = parse_optional_datetime(new_iso)
            if current is None or incoming is None or current >= incoming:
                resolved = current_raw
                # 现有到期胜出：保留它原来的来源，只有 detected 要改成邀请来源。
                if current_source != "detected":
                    resolved_source = current_source or source
            else:
                resolved = new_iso

        await db.execute(
            """UPDATE member_expiry
               SET user_id = COALESCE(NULLIF(?, ''), user_id), email = ?,
                   expires_at = ?, auto_kick = ?, source = ?,
                   kicked = 0, kicked_at = NULL, kick_source = NULL
               WHERE id = ?""",
            (
                normalized_user_id,
                normalized_email,
                resolved,
                1 if resolved is not None else 0,
                resolved_source,
                row["id"],
            ),
        )
        await db.commit()
        return resolved


async def record_confirmed_invite_extension(
    team_id: str,
    user_id: str,
    email: str,
    duration: str,
    *,
    source: str = "system",
    token_use_id: Optional[int] = None,
    token_action: Optional[str] = None,
) -> Optional[str]:
    """``record_confirmed_invite`` 的累加版：邀请已在 OpenAI 侧成功后，用
    ``extend_member_expiry`` 的"max(现有到期, now) + duration"语义落库。

    为什么邀请分支也要累加：被邀请人此刻确实不在任何 Team 里，但本地仍可能留着
    一条 kicked=0 的旧记录（例如上一次的 pending invite 在 OpenAI 侧过期/被撤销，
    本地记录没跟着清理）。直接覆盖就会把那条记录里还没用掉的时长抹平——和续期
    分支是同一个 bug。

    ``keep_permanent=True``：邀请已经生效、这里不允许再失败，所以遇到 NULL
    （永久）记录时保持永久而不是抛错。同样绝不缩短任何人的到期时间。
    """
    return await _persist_confirmed_membership(
        lambda: extend_member_expiry(
            team_id,
            user_id,
            email,
            duration,
            source=source,
            keep_permanent=True,
            token_use_id=token_use_id,
            token_action=token_action,
        ),
        team_id,
        user_id,
        email,
        # 兜底行的到期时间由 grant_duration 在落盘那一刻算出（见下）。
        None,
        source,
        token_use_id,
        grant_duration=duration,
    )


def _nominal_expiry_iso(duration: str) -> Optional[str]:
    try:
        expires_at = expiry_from_duration(duration)
    except Exception:  # pragma: no cover - defensive: duration 已在上游校验过
        return None
    return expires_at.isoformat() if expires_at is not None else None


async def _persist_confirmed_membership(
    writer: Callable[[], Awaitable[Optional[str]]],
    team_id: str,
    user_id: str,
    email: str,
    expires_iso: Optional[str],
    source: str,
    token_use_id: Optional[int] = None,
    *,
    grant_duration: Optional[str] = None,
) -> Optional[str]:
    """执行 ``writer``（本地成员记录写入），带重试 + 持久化兜底，且永不抛异常。

    契约与注意事项见 ``record_confirmed_invite`` 的文档字符串——这里只是把那套
    重试/兜底机制抽出来，让"覆盖式写入"和"累加式写入"共用同一份实现。
    """
    last_exc: Optional[BaseException] = None

    for attempt in range(1, _CONFIRM_WRITE_ATTEMPTS + 1):
        try:
            return await writer()
        except Exception as exc:  # defensive: SQLite lock, disk, anything at all
            last_exc = exc
            logger.warning(
                "record_confirmed_invite: local write failed (attempt %s/%s) "
                "team=%s email=%s user_id=%s: %s",
                attempt, _CONFIRM_WRITE_ATTEMPTS, team_id, email, user_id, exc,
            )
            if attempt < _CONFIRM_WRITE_ATTEMPTS:
                await asyncio.sleep(_CONFIRM_WRITE_BACKOFF_SECONDS * attempt)

    # All retries exhausted. OpenAI already has this member — the gap must
    # not disappear silently.
    error_text = str(last_exc) if last_exc else "unknown error"

    # 累加式写入的兜底行必须保住"买到的时长"，不是一个名义到期时间：调度器回填
    # 时若只取 max(行内到期, 现有到期)，一个已经有更远到期时间的成员会让这次购买
    # 的时长凭空蒸发，而兑换却被置成 success，用户和管理员都看不到任何异常。
    # 这里把行标成 kind='extend'，到期时间在**落盘这一刻**按 now + duration 算，
    # 调度器就能用 (行内到期 - 行的 created_at) 还原出购买时长并按
    # max(现有到期, 现在) + 时长 追加——和 extend_member_expiry 同一套语义。
    reconciliation_kind = "backfill"
    if grant_duration is not None:
        expires_iso = _nominal_expiry_iso(grant_duration)
        reconciliation_kind = "extend"
    backfill_detail = (
        f"member_expiry write failed after {_CONFIRM_WRITE_ATTEMPTS} attempts; "
        f"OpenAI invite already succeeded and MUST be backfilled manually: "
        f"team_id={team_id!r}, user_id={user_id!r}, email={email!r}, "
        f"expires_at={expires_iso!r}, source={source!r}"
    )

    try:
        await _insert_pending_invite_reconciliation(
            team_id, user_id, email, expires_iso, source, error_text,
            token_use_id=token_use_id,
            kind=reconciliation_kind,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "record_confirmed_invite: FAILED to persist pending_invite_reconciliations "
            "row too (%s). %s", exc, backfill_detail,
        )

    try:
        await log_operation(
            team_id,
            "member_expiry_write_failed",
            email,
            backfill_detail,
            "failed",
            error_text,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "record_confirmed_invite: FAILED to write operation_logs too (%s). %s",
            exc, backfill_detail,
        )

    logger.error("record_confirmed_invite: %s (error=%s)", backfill_detail, error_text)
    return expires_iso


async def delete_member_expiry(team_id: str, user_id: str = "", email: str = "") -> None:
    # Keep the audit row, but make the current membership permanent.
    await upsert_member_expiry(team_id, user_id, email, None)


async def mark_member_kicked(
    team_id: str,
    kick_source: str = "admin",
    user_id: str = "",
    email: str = "",
) -> None:
    """Mark a member_expiry record as kicked instead of deleting it.

    This preserves the record for audit trail purposes.  If no record exists
    (e.g. member was never tracked), a new kicked record is created so the
    event still shows up in the "已踢出" list.
    """
    normalized_email = (email or "").strip().lower()
    normalized_user_id = user_id or ""
    now = utc_now().isoformat()

    async with get_db() as db:
        cursor = await db.execute(
            """UPDATE member_expiry
               SET kicked = 1, kicked_at = ?, kick_source = ?
               WHERE team_id = ?
                 AND kicked = 0
                 AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))""",
            (now, kick_source, team_id,
             normalized_user_id, normalized_user_id,
             normalized_email, normalized_email),
        )
        if cursor.rowcount == 0:
            # No existing record — create a kicked record for audit trail
            await db.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at,
                    kick_source, first_seen_at, source, created_at)
                   VALUES (?, ?, ?, NULL, 0, 1, ?, ?, ?, 'system', ?)""",
                (team_id, normalized_user_id, normalized_email,
                 now, kick_source, now, now),
            )
        await deactivate_member_binding_if_inactive(db, normalized_email, now_iso=now)
        await db.commit()
    await asyncio.to_thread(sync_email_chat_commands_sync, normalized_email)


def normalize_kick_mode(mode: Optional[str]) -> str:
    if mode == "day_start":
        return "day_end"
    if mode in {"delay_hours", "day_end"}:
        return mode
    return "delay_hours"


async def get_kick_policy() -> dict:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT key, value FROM settings WHERE key IN ('expiry_kick_mode', 'expiry_kick_delay_hours')"
        )
        rows = await cursor.fetchall()

    raw = {row["key"]: row["value"] for row in rows}
    mode = normalize_kick_mode(raw.get("expiry_kick_mode"))
    try:
        delay_hours = int(raw.get("expiry_kick_delay_hours") or 0)
    except (TypeError, ValueError):
        delay_hours = 0
    delay_hours = min(max(delay_hours, 0), 720)

    return {
        "mode": mode,
        "delay_hours": delay_hours,
        "timezone": "Asia/Shanghai",
        "label": "日末" if mode == "day_end" else (f"+{delay_hours}h" if delay_hours else "到期"),
    }


def compute_effective_kick_at(expires_at: datetime, policy: dict) -> datetime:
    mode = normalize_kick_mode(policy.get("mode"))
    delay_hours = int(policy.get("delay_hours") or 0)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expires_at = expires_at.astimezone(timezone.utc)

    if mode == "day_end":
        local = expires_at.astimezone(APP_LOCAL_TZ)
        return local.replace(hour=23, minute=59, second=0, microsecond=0).astimezone(timezone.utc)
    return expires_at + timedelta(hours=delay_hours)


def _format_local_minute(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(APP_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def build_expiry_view(expires_at_raw: Optional[str], policy: dict) -> dict:
    expires_at = parse_optional_datetime(expires_at_raw)
    if expires_at is None:
        return {
            "expires_at": None,
            "expires_at_local": None,
            "effective_kick_at": None,
            "effective_kick_at_local": None,
            "kick_label": "永不",
            "kick_display": "永不",
        }

    effective_kick_at = compute_effective_kick_at(expires_at, policy)
    label = policy.get("label") or "到期"
    local = _format_local_minute(effective_kick_at)
    return {
        "expires_at": expires_at.isoformat(),
        "expires_at_local": _format_local_minute(expires_at),
        "effective_kick_at": effective_kick_at.isoformat(),
        "effective_kick_at_local": local,
        "kick_label": label,
        "kick_display": f"{local} ({label})" if local else None,
    }
