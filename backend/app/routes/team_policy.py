"""每个 Team 的超员策略设置：``PATCH /api/teams/{team_id}/overage-policy``。

只改本地 ``teams.overage_policy``，不碰上游。执行规则见 services/overage_policy.py。
"""
from fastapi import APIRouter, HTTPException

from ..database import get_db, log_operation
from ..models import OveragePolicyRequest
from ..seat_types import normalize_overage_policy
from ..services.team_locks import team_invite_lock
from .teams import _team_row_to_response


router = APIRouter(prefix="/api/teams", tags=["teams"])


@router.patch("/{team_id}/overage-policy")
async def set_overage_policy(team_id: str, req: OveragePolicyRequest):
    new_policy = req.overage_policy
    # 与邀请 / 切换席位共用这把锁：接口返回之后，不会还有正在进行的加人沿用旧策略。
    async with team_invite_lock(team_id):
        async with get_db() as db:
            cursor = await db.execute("SELECT overage_policy FROM teams WHERE id = ?", (team_id,))
            row = await cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Team not found")
            previous = normalize_overage_policy(row["overage_policy"])
            await db.execute(
                "UPDATE teams SET overage_policy = ? WHERE id = ?", (new_policy, team_id)
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM teams WHERE id = ?", (team_id,))
            updated = await cursor.fetchone()

    await log_operation(
        team_id,
        "set_overage_policy",
        None,
        f"overage_policy={new_policy}, previous={previous}",
        "success",
    )
    return _team_row_to_response(updated)
