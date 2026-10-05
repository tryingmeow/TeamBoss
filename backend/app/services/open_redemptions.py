"""兑换什么时候算"未结"，以及管理员因此被拒时看到的说明。

管理员的手动授予（单个邀请 / 重发、批量拉人、设置到期、续期）写的是同一条到期
记录；对账任务日后确认一笔未结兑换时，会在那条记录上再累加一次兑换码时长。所以
这些入口在写之前都要问这里：这个邮箱有没有对账日后还会记账的兑换。有就拒绝、
什么都不写，由兑换自己走完（见 ``routes/members.py`` 的 ``_refuse_if_open_redemption``
和 ``services/gpt_invites.py`` 的 ``_invite_to_team``）。
"""

from typing import Any, Optional

from ..database import get_db


# 两条腿：
# * 兑换本身（access_token_uses）：pending 不论在哪个 Team（还没落 Team 的 lookup、
#   以及被上游拒绝后会换到下一个 Team 重试的邀请，都可能落到这个 Team），uncertain
#   只看这个 Team（结果不明的邀请钉死在原 Team，不会再换）。
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
            OR (atu.result = 'uncertain' AND atu.team_id = ?))
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


async def find_open_redemption(team_id: str, email: str) -> Optional[dict[str, Any]]:
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
            (normalized_email, team_id, team_id, normalized_email),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


_UNCERTAIN_DETAIL = (
    "该邮箱在 Team {team_label} 有一笔兑换的邀请结果仍在确认中（兑换记录 #{token_use_id}），{refused}。"
    "请先在「兑换码 → 待确认的兑换」里核实收尾：确认成功会按兑换码补齐时长，确认失败会退码。"
    "收尾后再{next_step}。"
)
_PENDING_DETAIL = (
    "该邮箱有一笔兑换正在处理（兑换记录 #{token_use_id}），{refused}。"
    "请等它结束后刷新成员列表，再{next_step}；结果不明的兑换会转入「兑换码 → 待确认的兑换」。"
)
# (拒绝说明, 收尾后的下一步)，按管理员操作区分。
_REFUSALS = {
    "invite": ("未发送邀请", "决定是否需要另行邀请"),
    "batch_invite": ("未邀请", "决定是否需要另行邀请"),
    "set_expiry": ("未修改到期时间", "调整到期时间"),
    "extend_expiry": ("未续期", "决定是否需要续期"),
}


def open_redemption_detail(open_redemption: dict[str, Any], *, operation: str) -> str:
    """管理员因 ``open_redemption`` 被拒时看到的说明（中文，直接展示在后台）。

    ``operation``：``invite`` / ``batch_invite`` / ``set_expiry`` / ``extend_expiry``。
    """
    refused, next_step = _REFUSALS[operation]
    template = _UNCERTAIN_DETAIL if open_redemption["result"] == "uncertain" else _PENDING_DETAIL
    return template.format(
        token_use_id=open_redemption["token_use_id"],
        team_label=open_redemption.get("team_name") or open_redemption.get("team_id") or "",
        refused=refused,
        next_step=next_step,
    )
