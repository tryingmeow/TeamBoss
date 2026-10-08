"""兑换什么时候算"未结"、未结的兑换多久会被对账收尾，以及管理员因此被拒时看到的说明。

管理员的手动授予（单个邀请 / 重发、批量拉人、设置到期、续期）写的是同一条到期
记录；对账任务日后确认一笔未结兑换时，会在那条记录上再累加一次兑换码时长。所以
这些入口在写之前都要问这里：这个邮箱有没有对账日后还会记账的兑换。有就拒绝、
什么都不写，由兑换自己走完（见 ``routes/members.py`` 的 ``_refuse_if_open_redemption``
和 ``services/gpt_invites.py`` 的 ``_invite_to_team``）。

对账任务（``routes/access_tokens.py`` 的 ``reconcile_pending_redemptions``）按本文件
的两个时长收尾被中断的兑换；给管理员的说明也从这里取数，不另写数字。
"""

from typing import Any, Optional

from ..database import get_db


# invite_pending 的兑换超过这个时长、且本进程里已没有请求在处理它，就判定为被中断：
# 远端邀请可能已发出，也可能没有，原 Team 里仍看不到人就转成 uncertain，交给
# 「待确认的兑换」。必须明显长于一次邀请请求（上游超时 60 秒，外加 shield 等它
# 收尾），而 created_at 是占用时刻、早于真正发请求，所以留足余量。
INTERRUPTED_INVITE_AFTER_SECONDS = 10 * 60

# lookup / 续期阶段的兑换只碰本地（续期和收据在同一事务里），超过这个时长仍是
# pending 就由对账任务回滚、退码。
STALE_LOCAL_REDEMPTION_AFTER_SECONDS = 30 * 60


# 两条腿：
# * 兑换本身（access_token_uses）：pending 不论在哪个 Team（还没落 Team 的 lookup、
#   以及被上游拒绝后会换到下一个 Team 重试的邀请，都可能落到这个 Team）。uncertain
#   的邀请钉死在原 Team、不会再换，对账确认时只给原 Team 记账；``uncertain_in_any_team``
#   为假时只看这个 Team，为真时看所有 Team，但不算已从系统删除的 Team：对账要拿那个
#   Team 的凭据现拉名单才能确认，删掉之后它再也确认不了，管理员也无法在那里核实退码，
#   算上只会让这个邮箱永远拉不进别的 Team。
# * 兜底行（pending_invite_reconciliations 里未结清、挂着兑换凭据的 barrier / extend /
#   backfill 行）：调度器回填时按凭据认领兑换，凭据仍是 pending/uncertain 就会记账。
#   这些行和凭据在正常流程里 Team、邮箱一致；单独查一遍，是为了不依赖这份一致性。
_OPEN_REDEMPTION_SQL = """
    SELECT atu.id AS token_use_id, atu.result AS result, atu.action AS action,
           atu.team_id AS team_id, t.name AS team_name, atu.created_at AS created_at,
           t.status AS team_status, t.auth_state AS team_auth_state,
           t.sync_suspended_at AS team_sync_suspended_at
      FROM access_token_uses atu
      LEFT JOIN teams t ON t.id = atu.team_id
     WHERE lower(atu.email) = ?
       AND (atu.result = 'pending'
            OR (atu.result = 'uncertain'
                AND (atu.team_id = ? OR (? AND t.id IS NOT NULL))))
    UNION
    SELECT atu.id AS token_use_id, atu.result AS result, atu.action AS action,
           atu.team_id AS team_id, t.name AS team_name, atu.created_at AS created_at,
           t.status AS team_status, t.auth_state AS team_auth_state,
           t.sync_suspended_at AS team_sync_suspended_at
      FROM pending_invite_reconciliations r
      JOIN access_token_uses atu ON atu.id = r.token_use_id
      LEFT JOIN teams t ON t.id = atu.team_id
     WHERE r.team_id = ? AND r.resolved = 0 AND lower(r.email) = ?
       AND atu.result IN ('pending', 'uncertain')
    ORDER BY token_use_id
    LIMIT 1
"""


async def find_open_redemption(
    team_id: str, email: str, *, uncertain_in_any_team: bool = False
) -> Optional[dict[str, Any]]:
    """对账日后还会给 ``team_id`` 上的这个邮箱记账的未结兑换，没有则 None。只读。

    返回 ``token_use_id``、``result``（'pending' / 'uncertain'）、``action``、
    ``team_id`` / ``team_name``（兑换落到的 Team，lookup 阶段为 None）、``created_at``，
    以及那个 Team 的 ``team_status`` / ``team_auth_state``（见 ``team_login_lost``）。
    邮箱按去空白、小写比较，与兑换码入口一致。
    """
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return None
    async with get_db() as db:
        cursor = await db.execute(
            _OPEN_REDEMPTION_SQL,
            (
                normalized_email,
                team_id,
                1 if uncertain_in_any_team else 0,
                team_id,
                normalized_email,
            ),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


# 未结兑换只有两种终态，说明要让管理员按终态走，而不是建议他另行授予：
# * 确认成功：兑换码时长已经记过一次。
# * 退码（确认失败、兑换失败、本地阶段回滚）：码的 used_count 归零，成员还能拿同一张
#   码再兑换一次。管理员这时手动邀请 / 设置到期 / 续期去"补"，成员再兑换又拿一份，
#   一张码两次授予。所以退码后让成员自己重新兑换。
# pending 的收尾时长在调用时从上面两个常量现算，不写死数字。
_REDEEM_AGAIN = (
    "请让成员用同一兑换码重新兑换（码已过期或停用就换发一张同规格的新码），"
    "不要用手动邀请、设置到期或续期来补。"
)
_UNCERTAIN_DETAIL = (
    "该邮箱在 Team {team_label} 有一笔兑换的邀请结果待确认（兑换记录 #{token_use_id}），{refused}。"
    "该邮箱出现在 Team {team_label}（含待接受邀请）后会自动确认，也可在「兑换码 → 待确认的兑换」里核实收尾。"
    "{login_lost}"
    "确认成功则兑换码时长已记过一次；确认失败会退码，" + _REDEEM_AGAIN
)
# 原 Team 登录失效、或名单同步被暂停（工作区没了 / 一直读不到）时，自动确认和「确认失败」
# 都要现拉那个 Team 的名单，拉不到就一直等着（确认失败被拒，见
# access_tokens.resolve_pending_confirmation），删除 Team 也被这笔未结兑换拒绝。
# 「确认成功」不需要名单，所以出口是恢复 Team 或直接确认成功；Team 恢复不了时同样用
# 确认成功收尾，再给成员换发一张同规格的新码，不让管理员手动邀请 / 设置到期 / 续期。
_LOGIN_LOST = (
    "Team {team_label} 登录已失效：请重新导入恢复登录（自动确认和确认失败都要等它恢复），"
    "或确定人已在里面时直接确认成功。"
)
_SYNC_SUSPENDED = (
    "Team {team_label} 的成员名单已读不到（同步已暂停）：自动确认和确认失败都要等它恢复，"
    "或确定人已在里面时直接确认成功。"
)
_CANNOT_RESTORE = (
    "Team 恢复不了时，用确认成功把这条记录收尾，再给成员换发一张同规格的新码。"
)
_PENDING_DETAIL = (
    "该邮箱有一笔兑换正在处理（兑换记录 #{token_use_id}），{refused}。"
    "请等它结束，处理中断的会自动收尾：进入邀请阶段的，Team 里出现该邮箱即确认，"
    "发起满 {invite_minutes} 分钟仍未出现则转入「兑换码 → 待确认的兑换」；"
    "未进入邀请阶段的，发起满 {stale_minutes} 分钟回滚退码。"
    "成功则兑换码时长已记过一次；失败或回滚会退码，" + _REDEEM_AGAIN
)
# 拒绝说明，按管理员操作区分。正文只提兑换自己落到的 Team、不提管理员正在操作的
# Team，所以批量拉人（没有选定 Team）也适用。
_REFUSALS = {
    "invite": "未发送邀请",
    "batch_invite": "未邀请",
    "set_expiry": "未修改到期时间",
    "extend_expiry": "未续期",
}


# 批量拉人被一条未结对账行挡住、行上挂着兑换凭据、兑换却已结束时的说明。兑换结清后
# 不会再给它写新行（见 ``member_expiry.insert_pending_invite_reconciliation_row``），
# 这里给库里的旧行。和上面一样，退码的让成员重新兑换，不让管理员到那个 Team 手动补发。
_SETTLED_ROW_DETAIL = (
    "该邮箱在 Team {team_label} 留有一笔已结束兑换（兑换记录 #{token_use_id}）的对账记录，"
    "未邀请、未换 Team 重新邀请。{outcome}"
    "该邮箱出现在 Team {team_label}（含待接受邀请）后，下一轮同步会清除这条记录。"
)
_SETTLED_OUTCOMES = {
    "success": "这笔兑换已确认成功，兑换码时长已记过一次。",
    "refunded": "这笔兑换已退码，" + _REDEEM_AGAIN,
}
# 兑换的终态。success 之外的两种（failed：失败 / 退码；notice：多 Team 选择提示中断）
# 都已把码退回。
SETTLED_REDEMPTION_RESULTS = ("success", "failed", "notice")


def _minutes(seconds: int) -> int:
    """秒数向上取整成分钟：说明告诉管理员要等到什么时候，宁可说长、不能说短。"""
    return max(1, -(-int(seconds) // 60))


def team_login_lost(team_status: Optional[str], team_auth_state: Optional[str]) -> bool:
    """Team 的登录已失效：session 已死（status='token_expired'），或刷新被上游明确
    拒绝（auth_state='rejected'）。两种情况下现拉名单都会失败，管理员能做的是重新导入。"""
    return team_status == "token_expired" or team_auth_state == "rejected"


def team_sync_suspended(team_sync_suspended_at: Optional[str]) -> bool:
    """Team 的名单同步已被暂停（``teams.sync_suspended_at`` 非空）：名单读不到，
    自动确认和确认失败同样要等它恢复。"""
    return bool(team_sync_suspended_at)


def unsettleable_team_note(
    team_label: str,
    team_status: Optional[str],
    team_auth_state: Optional[str],
    team_sync_suspended_at: Optional[str],
) -> str:
    """Team 读不到名单（登录失效或同步暂停）时补给管理员的出口，正常时为空串。"""
    if team_login_lost(team_status, team_auth_state):
        return _LOGIN_LOST.format(team_label=team_label) + _CANNOT_RESTORE
    if team_sync_suspended(team_sync_suspended_at):
        return _SYNC_SUSPENDED.format(team_label=team_label) + _CANNOT_RESTORE
    return ""


def open_redemption_detail(open_redemption: dict[str, Any], *, operation: str) -> str:
    """管理员因 ``open_redemption`` 被拒时看到的说明（中文，直接展示在后台）。

    ``operation``：``invite`` / ``batch_invite`` / ``set_expiry`` / ``extend_expiry``。
    uncertain 点名兑换所在的 Team：邀请看所有 Team 的 uncertain，它可能不是管理员
    正在操作的那个 Team。那个 Team 登录失效时补一句出口（见 ``_LOGIN_LOST``）。
    """
    template = _UNCERTAIN_DETAIL if open_redemption["result"] == "uncertain" else _PENDING_DETAIL
    team_label = open_redemption.get("team_name") or open_redemption.get("team_id") or ""
    login_lost = unsettleable_team_note(
        team_label,
        open_redemption.get("team_status"),
        open_redemption.get("team_auth_state"),
        open_redemption.get("team_sync_suspended_at"),
    )
    return template.format(
        token_use_id=open_redemption["token_use_id"],
        team_label=team_label,
        refused=_REFUSALS[operation],
        login_lost=login_lost,
        invite_minutes=_minutes(INTERRUPTED_INVITE_AFTER_SECONDS),
        stale_minutes=_minutes(STALE_LOCAL_REDEMPTION_AFTER_SECONDS),
    )


def settled_redemption_row_detail(
    token_use_id: int, token_use_result: Optional[str], team_label: str
) -> str:
    """批量拉人因一条挂着已结束兑换的未结对账行被拒时，管理员看到的说明。

    ``token_use_result`` 是那次兑换的终态（``SETTLED_REDEMPTION_RESULTS`` 之一）。
    """
    outcome = "success" if token_use_result == "success" else "refunded"
    return _SETTLED_ROW_DETAIL.format(
        team_label=team_label,
        token_use_id=token_use_id,
        outcome=_SETTLED_OUTCOMES[outcome],
    )


# 删除 Team 前要结清的兑换：兑换本身落在这个 Team，或这个 Team 上有它未结清的屏障 /
# 兜底行。删 Team 不动兑换、屏障和邮箱占用，但删掉之后对账拿不到这个 Team 的凭据，
# 管理员邀请又不再把已删 Team 里的 uncertain 当阻拦（见上面 ``_OPEN_REDEMPTION_SQL``）。
# 管理员于是能把这个邮箱拉进别的 Team；之后重新添加同一个 Team（Team id 就是上游账号
# id，加回来还是同一个），对账在原 Team 里看到人又确认一次，一笔兑换占两个席位。
_OPEN_REDEMPTIONS_IN_TEAM_SQL = """
    SELECT atu.id AS token_use_id, atu.result AS result
      FROM access_token_uses atu
     WHERE atu.team_id = ? AND atu.result IN ('pending', 'uncertain')
    UNION
    SELECT atu.id AS token_use_id, atu.result AS result
      FROM pending_invite_reconciliations r
      JOIN access_token_uses atu ON atu.id = r.token_use_id
     WHERE r.team_id = ? AND r.resolved = 0
       AND atu.result IN ('pending', 'uncertain')
    ORDER BY token_use_id
"""
_DELETE_TEAM_LISTED_IDS = 5
_DELETE_TEAM_DETAIL = (
    "Team {team_label} 还有未结的兑换（兑换记录 {ids}），未删除：删除后就无法再在"
    "这个 Team 里确认。"
)
_DELETE_TEAM_UNCERTAIN = "结果待确认的，请先在「兑换码 → 待确认的兑换」里确认成功或确认失败。"
_DELETE_TEAM_PENDING = (
    "正在处理的会自动收尾，进入邀请阶段、发起满 {invite_minutes} 分钟仍未在 Team 里"
    "出现的会转入「兑换码 → 待确认的兑换」。"
)
_DELETE_TEAM_THEN = "都收尾后再删除。"


async def find_open_redemptions_in_team(db, team_id: str) -> list[dict[str, Any]]:
    """落在 ``team_id`` 上、还没结的兑换（``token_use_id`` / ``result``），按记录号排序。

    在调用方的连接里读：删除 Team 在同一个写事务里先查后删，查完之后不会再有兑换
    抢在删除前落到这个 Team 上而不被看到。
    """
    cursor = await db.execute(_OPEN_REDEMPTIONS_IN_TEAM_SQL, (team_id, team_id))
    return [dict(row) for row in await cursor.fetchall()]


def delete_team_refusal_detail(
    team_label: str, open_redemptions: list[dict[str, Any]], *, unsettleable_note: str = ""
) -> str:
    """因 ``open_redemptions``（``find_open_redemptions_in_team`` 的结果）拒绝删除
    Team 时，管理员看到的说明。``unsettleable_note``：这个 Team 读不到名单时的出口
    （``unsettleable_team_note``，正常为空串）。"""
    ids = "、".join(
        f"#{item['token_use_id']}" for item in open_redemptions[:_DELETE_TEAM_LISTED_IDS]
    )
    if len(open_redemptions) > _DELETE_TEAM_LISTED_IDS:
        ids += f" 等 {len(open_redemptions)} 笔"
    results = {item["result"] for item in open_redemptions}
    parts = [_DELETE_TEAM_DETAIL.format(team_label=team_label, ids=ids)]
    if "uncertain" in results:
        parts.append(_DELETE_TEAM_UNCERTAIN)
        parts.append(unsettleable_note)
    if "pending" in results:
        parts.append(
            _DELETE_TEAM_PENDING.format(invite_minutes=_minutes(INTERRUPTED_INVITE_AFTER_SECONDS))
        )
    parts.append(_DELETE_TEAM_THEN)
    return "".join(parts)
