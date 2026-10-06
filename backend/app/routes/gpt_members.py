from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..services.gpt_invites import (
    GptInviteFailed,
    NoGptSeatAvailable,
    batch_overage_plan,
    confirmed_overage_team_ids,
    has_overfill_target,
    invite_gpt_member_any_team,
    load_gpt_invite_candidates,
    normalize_invite_emails,
    summarize_gpt_candidates,
)
from ..services.member_expiry import expires_in_to_datetime
from ..services.overage_policy import NO_PLACE_ERROR, batch_confirmation_detail
from ..utils.durations import DurationError


router = APIRouter(prefix="/api/gpt-members", tags=["gpt-members"])


class InviteGptMembersRequest(BaseModel):
    email: Optional[str] = None
    emails: list[str] = Field(default_factory=list)
    expires_in: Optional[str] = None
    # 管理员已确认超员计划（会加购扣费）。确认只对 overage_team_ids 里列出的「超员需确认」
    # Team 生效，即 409 的 overage_plan 里他看到的那些 team_id；没列出的不会被超员。
    allow_overage: bool = False
    overage_team_ids: list[str] = Field(default_factory=list)


@router.post("/invite")
async def invite_gpt_members(req: InviteGptMembersRequest):
    """不指定 Team 批量拉 ChatGPT 成员：先填空位，再按各 Team 的超员策略超员。

    * 空位按缓存挑 Team、发邀请前在锁内现拉确认（逻辑同以前）。
    * 没空位的邮箱：有「超员自动」的 Team 就直接加在那里，不问；
      只剩「超员需确认」的 Team 时问一次（409，带 ``overage_plan``：加购几个、加在哪）；
      「禁止超员」的 Team 永远不超员。哪里都去不了的邮箱记为「没位置，未邀请」。
    * 确认绑定在计划上：带 ``allow_overage`` 重发时，只超员 ``overage_team_ids`` 里的
      confirm Team。计划不成立了（那个 Team 改成禁止超员、不在了、接不住）就带新计划
      再问一次，绝不把加购挪到管理员没看到的 Team 上。
    """
    raw_emails = list(req.emails)
    if req.email:
        raw_emails.insert(0, req.email)
    try:
        emails = normalize_invite_emails(raw_emails)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not emails:
        raise HTTPException(status_code=400, detail="邮箱不能为空")

    expires_in = req.expires_in or "never"
    try:
        expires_at = expires_in_to_datetime(expires_in)
    except DurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    confirmed_ids = confirmed_overage_team_ids(req.allow_overage, req.overage_team_ids)
    # 带了确认标记、但确认过的计划已经不成立时，重新问的文案先说清楚这一点。
    replan_note = "你确认过的超员计划已经不成立，需要重新确认。" if req.allow_overage else ""
    candidates = await load_gpt_invite_candidates(include_full=True)
    capacity = summarize_gpt_candidates(candidates)
    free = int(capacity.get("available") or 0)
    if len(emails) > free and not has_overfill_target(candidates, confirmed_ids):
        plan = batch_overage_plan(candidates, len(emails) - free, exclude_team_ids=confirmed_ids)
        if plan:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=batch_confirmation_detail(
                    lead=f"{replan_note}空闲 ChatGPT 席位只有 {free} 个，要邀请 {len(emails)} 个。",
                    plan=plan,
                    capacity=capacity,
                    added=[],
                    remaining_emails=emails,
                ),
            )

    added: list[dict] = []
    failed: list[dict] = []
    no_place: list[str] = []
    for index, email in enumerate(emails):
        try:
            item = await invite_gpt_member_any_team(
                email,
                expires_at,
                allow_overage=req.allow_overage,
                overage_team_ids=req.overage_team_ids,
                action="invite_gpt_member",
            )
            added.append(item)
        except NoGptSeatAvailable as exc:
            # 没空位、auto Team 和确认过的 Team 都接不住：还有别的 confirm Team 就带新计划
            # 再问（已确认过却没接住的 Team 不再列入），没有就记为没位置。
            remaining = emails[index:]
            latest = await load_gpt_invite_candidates(include_full=True)
            plan = batch_overage_plan(latest, len(remaining), exclude_team_ids=confirmed_ids)
            if plan:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=batch_confirmation_detail(
                        lead=(
                            f"{replan_note}已添加 {len(added)} 个，"
                            f"剩余 {len(remaining)} 个没有空闲 ChatGPT 席位。"
                        ),
                        plan=plan,
                        capacity=summarize_gpt_candidates(latest),
                        added=added,
                        remaining_emails=remaining,
                        failed=failed,
                    ),
                ) from exc
            no_place.append(email)
            failed.append({"email": email, "error": NO_PLACE_ERROR})
        except GptInviteFailed as exc:
            failed.append({"email": email, "error": exc.reason})
        except Exception as exc:
            failed.append({"email": email, "error": str(exc)})

    return {
        "status": "ok",
        "added": added,
        "failed": failed,
        "no_place_emails": no_place,
        "total": len(emails),
    }
