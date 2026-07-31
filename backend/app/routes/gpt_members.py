from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..services.gpt_invites import (
    GptInviteFailed,
    NoGptSeatAvailable,
    cached_gpt_capacity_summary,
    invite_gpt_member_any_team,
    normalize_invite_emails,
)
from ..services.member_expiry import expires_in_to_datetime
from ..utils.durations import DurationError


router = APIRouter(prefix="/api/gpt-members", tags=["gpt-members"])


class InviteGptMembersRequest(BaseModel):
    email: Optional[str] = None
    emails: list[str] = Field(default_factory=list)
    expires_in: Optional[str] = None
    allow_overage: bool = False


def _overage_detail(
    *,
    message: str,
    capacity: dict,
    added: list[dict],
    remaining_emails: list[str],
    failed: list[dict] | None = None,
) -> dict:
    return {
        "code": "require_overage_confirmation",
        "message": message,
        "capacity": capacity,
        "added": added,
        "remaining_emails": remaining_emails,
        "failed": failed or [],
    }


@router.post("/invite")
async def invite_gpt_members(req: InviteGptMembersRequest):
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

    capacity = await cached_gpt_capacity_summary()
    if not req.allow_overage and len(emails) > int(capacity.get("available") or 0):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_overage_detail(
                message="空闲 GPT 席位不足，继续可能产生额外计费。",
                capacity=capacity,
                added=[],
                remaining_emails=emails,
            ),
        )

    added: list[dict] = []
    failed: list[dict] = []
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
                latest_capacity = await cached_gpt_capacity_summary()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_overage_detail(
                        message=f"已添加 {len(added)} 个，剩余 {len(remaining)} 个未添加。继续可能产生额外计费。",
                        capacity=latest_capacity,
                        added=added,
                        remaining_emails=remaining,
                        failed=failed,
                    ),
                ) from exc
            failed.append({"email": email, "error": exc.reason})
        except GptInviteFailed as exc:
            failed.append({"email": email, "error": exc.reason})
        except Exception as exc:
            failed.append({"email": email, "error": str(exc)})

    return {
        "status": "ok",
        "added": added,
        "failed": failed,
        "total": len(emails),
    }
