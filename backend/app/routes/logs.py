from fastapi import APIRouter, Query
from typing import Optional

from ..database import get_db

router = APIRouter(prefix="/api/logs", tags=["logs"])

LOG_SEARCH_COLUMNS = (
    "l.team_id",
    "l.action",
    "l.target_email",
    "l.detail",
    "l.result",
    "l.error_message",
    "l.trigger_type",
    "l.created_at",
    "t.name",
    "t.remark",
    "t.owner_email",
    "t.status",
)


def add_log_search_condition(conditions: list[str], params: list[str], q: Optional[str]) -> None:
    query = (q or "").strip()
    if not query:
        return

    conditions.append(
        "("
        + " OR ".join(
            f"LOWER(COALESCE({column}, '')) LIKE ?" for column in LOG_SEARCH_COLUMNS
        )
        + ")"
    )
    params.extend([f"%{query.lower()}%"] * len(LOG_SEARCH_COLUMNS))


@router.get("")
async def get_logs(
    team_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200)
):
    conditions = []
    params = []

    if team_id:
        conditions.append("l.team_id = ?")
        params.append(team_id)
    if action:
        conditions.append("l.action = ?")
        params.append(action)
    add_log_search_condition(conditions, params, q)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    offset = (page - 1) * per_page
    from_clause = "FROM operation_logs l LEFT JOIN teams t ON l.team_id = t.id"

    async with get_db() as db:
        count_cursor = await db.execute(
            f"SELECT COUNT(*) as total {from_clause} {where_clause}", params
        )
        count_row = await count_cursor.fetchone()
        total = count_row["total"] if count_row else 0

        cursor = await db.execute(
            f"""
            SELECT
                l.*,
                t.name AS team_name,
                t.remark AS team_remark,
                t.owner_email AS team_owner_email,
                t.status AS team_status
            {from_clause}
            {where_clause}
            ORDER BY l.created_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset]
        )
        rows = await cursor.fetchall()

    logs = [dict(row) for row in rows]
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page
    }
