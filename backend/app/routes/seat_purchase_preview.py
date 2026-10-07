from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.overage_policy import CONFIRMATION_MAX_SEATS
from ..services.seat_purchase_preview import QuoteUnavailable, fetch_seat_purchase_preview
from ..services.team_clients import get_team_client


router = APIRouter(prefix="/api/teams", tags=["teams"])


class SeatPurchasePreviewRequest(BaseModel):
    seat_type: Literal["default", "prolite"]
    additional_seats: int = Field(strict=True, ge=1, le=CONFIRMATION_MAX_SEATS)


@router.post("/{team_id}/seat-purchase-preview")
async def seat_purchase_preview(team_id: str, req: SeatPurchasePreviewRequest):
    try:
        client = await get_team_client(team_id)
        return await fetch_seat_purchase_preview(client, req.seat_type, req.additional_seats)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
    except QuoteUnavailable:
        pass
    raise HTTPException(
        status_code=502,
        detail={"code": "seat_purchase_quote_unavailable", "message": "暂时无法获取官方加购报价，请稍后重试"},
    )
