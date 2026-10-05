from fastapi import APIRouter, HTTPException, Query
from typing import Literal, Optional, Sequence, Union

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

# Keep this classification on the server so the admin UI and Telegram expose
# the same historical slice. Do not infer it from target_email: team imports
# also use that column for the owner, while older seat/expiry logs did not.
MEMBER_LOG_ACTIONS = (
    "change_seat",
    "create_tg_member_pairing_code",
    "patrol_kick",
    "patrol_revoke_invite",
    "remove_expiry",
    "remove_member",
    "revoke_invite",
    "set_expiry",
    "extend_expiry",
    "update_user_display_name",
)

MEMBER_LOG_ACTION_PREFIXES = (
    "auto_kick",
    "auto_revoke_invite",
    "invite_",
    "member_",
    "patrol_strict_",
    "patrol_would_",
    "self_service_",
)


# 单次请求最多接受的搜索词 / 动作码个数，避免拼出无界 SQL。
MAX_LOG_FILTER_VALUES = 50


def normalize_log_values(
    values: Union[str, Sequence[str], None],
    *,
    split_commas: bool = False,
    lowercase: bool = False,
) -> list[str]:
    """Flatten repeated params into stripped, de-duplicated, non-blank values (order kept)."""
    if values is None:
        return []
    raw = [values] if isinstance(values, str) else list(values)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in (item.split(",") if split_commas else [item]):
            value = part.strip()
            if lowercase:
                value = value.lower()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
    if len(out) > MAX_LOG_FILTER_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"too many filter values (max {MAX_LOG_FILTER_VALUES})",
        )
    return out


def add_log_search_condition(
    conditions: list[str],
    params: list[str],
    q: Union[str, Sequence[str], None],
    q_actions: Union[str, Sequence[str], None] = None,
) -> None:
    """Search group: any term is a substring of any column, OR action is one of q_actions."""
    terms = normalize_log_values(q, lowercase=True)
    actions = normalize_log_values(q_actions, split_commas=True)
    if not terms and not actions:
        return

    clauses: list[str] = []
    for term in terms:
        clauses.extend(f"LOWER(COALESCE({column}, '')) LIKE ?" for column in LOG_SEARCH_COLUMNS)
        params.extend([f"%{term}%"] * len(LOG_SEARCH_COLUMNS))
    if actions:
        clauses.append(f"l.action IN ({', '.join('?' for _ in actions)})")
        params.extend(actions)
    conditions.append("(" + " OR ".join(clauses) + ")")


def add_log_scope_condition(
    conditions: list[str],
    params: list[str],
    scope: Optional[str],
) -> None:
    if scope != "members":
        return

    exact_placeholders = ", ".join("?" for _ in MEMBER_LOG_ACTIONS)
    clauses = [f"l.action IN ({exact_placeholders})"]
    clauses.extend("l.action LIKE ?" for _ in MEMBER_LOG_ACTION_PREFIXES)
    conditions.append("(" + " OR ".join(clauses) + ")")
    params.extend(MEMBER_LOG_ACTIONS)
    params.extend(f"{prefix}%" for prefix in MEMBER_LOG_ACTION_PREFIXES)


@router.get("")
async def get_logs(
    team_id: Optional[str] = Query(None),
    action: Optional[list[str]] = Query(None),
    scope: Optional[Literal["members"]] = Query(None),
    q: Optional[list[str]] = Query(None),
    q_action: Optional[list[str]] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=1000)
):
    conditions = []
    params = []

    if team_id:
        conditions.append("l.team_id = ?")
        params.append(team_id)
    actions = normalize_log_values(action, split_commas=True)
    if len(actions) == 1:
        conditions.append("l.action = ?")
        params.append(actions[0])
    elif actions:
        conditions.append(f"l.action IN ({', '.join('?' for _ in actions)})")
        params.extend(actions)
    add_log_scope_condition(conditions, params, scope)
    add_log_search_condition(conditions, params, q, q_action)

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
