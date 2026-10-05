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
           atu.team_id AS team_id, t.name AS team_name, atu.created_at AS created_at
      FROM access_token_uses atu
      LEFT JOIN teams t ON t.id = atu.team_id
     WHERE lower(atu.email) = ?
       AND (atu.result = 'pending'
            OR (atu.result = 'uncertain'
                AND (atu.team_id = ? OR (? AND t.id IS NOT NULL))))
    UNION
    SELECT atu.id AS token_use_id, atu.result AS result, atu.action AS action,
           atu.team_id AS team_id, t.name AS team_name, atu.created_at AS created_at
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
    ``team_id`` / ``team_name``（兑换落到的 Team，lookup 阶段为 None）、``created_at``。
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
# * 退码（确认失败、兑换失败、本地阶段回滚）：码的 used_count 归零，客户还能拿同一张
#   码再兑换一次。管理员这时手动邀请 / 设置到期 / 续期去"补"，客户再兑换又拿一份，
#   一张码两次授予。所以退码后让客户自己重新兑换。
# pending 的收尾时长在调用时从上面两个常量现算，不写死数字。
_REDEEM_AGAIN = (
    "请让客户用同一兑换码重新兑换（码已过期或停用就换发一张同面额的新码），"
    "不要用手动邀请、设置到期或续期来补。"
)
_UNCERTAIN_DETAIL = (
    "该邮箱在 Team {team_label} 有一笔兑换的邀请结果待确认（兑换记录 #{token_use_id}），{refused}。"
    "该邮箱出现在这个 Team（含待接受邀请）后会自动确认，也可在「兑换码 → 待确认的兑换」里核实收尾。"
    "确认成功则兑换码时长已记过一次；确认失败会退码，" + _REDEEM_AGAIN
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


def _minutes(seconds: int) -> int:
    """秒数向上取整成分钟：说明告诉管理员要等到什么时候，宁可说长、不能说短。"""
    return max(1, -(-int(seconds) // 60))


def open_redemption_detail(open_redemption: dict[str, Any], *, operation: str) -> str:
    """管理员因 ``open_redemption`` 被拒时看到的说明（中文，直接展示在后台）。

    ``operation``：``invite`` / ``batch_invite`` / ``set_expiry`` / ``extend_expiry``。
    uncertain 点名兑换所在的 Team：邀请看所有 Team 的 uncertain，它可能不是管理员
    正在操作的那个 Team。
    """
    template = _UNCERTAIN_DETAIL if open_redemption["result"] == "uncertain" else _PENDING_DETAIL
    return template.format(
        token_use_id=open_redemption["token_use_id"],
        team_label=open_redemption.get("team_name") or open_redemption.get("team_id") or "",
        refused=_REFUSALS[operation],
        invite_minutes=_minutes(INTERRUPTED_INVITE_AFTER_SECONDS),
        stale_minutes=_minutes(STALE_LOCAL_REDEMPTION_AFTER_SECONDS),
    )
