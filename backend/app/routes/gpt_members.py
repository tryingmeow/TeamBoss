from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..services.gpt_invites import (
    GptInviteFailed,
    NoGptSeatAvailable,
    batch_overage_plan,
    has_auto_overage_team,
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
    # 管理员已确认：没有空位的邮箱可以超员加在「超员需确认」的 Team 上（会加购扣费）。
    allow_overage: bool = False


@router.post("/invite")
async def invite_gpt_members(req: InviteGptMembersRequest):
    """不指定 Team 批量拉 ChatGPT 成员：先填空位，再按各 Team 的超员策略超员。

    * 空位按缓存挑 Team、发邀请前在锁内现拉确认（逻辑同以前）。
    * 没空位的邮箱：有「超员自动」的 Team 就直接加在那里，不问；
      只剩「超员需确认」的 Team 时问一次（409，带 ``overage_plan``：加购几个、加在哪）；
      「禁止超员」的 Team 永远不超员。哪里都去不了的邮箱记为「没位置，未邀请」。
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

    candidates = await load_gpt_invite_candidates(include_full=True)
    capacity = summarize_gpt_candidates(candidates)
    free = int(capacity.get("available") or 0)
    if not req.allow_overage and len(emails) > free and not has_auto_overage_team(candidates):
        plan = batch_overage_plan(candidates, len(emails) - free)
        if plan:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=batch_confirmation_detail(
                    lead=f"空闲 ChatGPT 席位只有 {free} 个，要邀请 {len(emails)} 个。",
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
                action="invite_gpt_member",
            )
            added.append(item)
        except NoGptSeatAvailable as exc:
            if not req.allow_overage:
                remaining = emails[index:]
                latest = await load_gpt_invite_candidates(include_full=True)
                plan = batch_overage_plan(latest, len(remaining))
                if plan:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=batch_confirmation_detail(
                            lead=f"已添加 {len(added)} 个，剩余 {len(remaining)} 个没有空闲 ChatGPT 席位。",
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
