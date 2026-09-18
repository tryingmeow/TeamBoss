import json
import os

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ..database import get_db, get_sessions_dir
from ..models import TeamSession
from ..team_service import upsert_team_from_session

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/export")
async def export_all_sessions():
    sessions = []
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM teams")
        teams = await cursor.fetchall()

    for team in teams:
        team_id = team["id"]
        session_file = os.path.join(get_sessions_dir(), f"{team_id}.json")
        if os.path.exists(session_file):
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Some session files on disk hold a JSON-encoded string rather
            # than an object (routes/users.py._parse_session_file handles
            # the same shape). Unwrap it the same way instead of failing
            # the whole export with a TypeError on dict-only assignment.
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (TypeError, ValueError):
                    data = None
            if not isinstance(data, dict):
                continue
            data["_team_id"] = team_id
            sessions.append(data)

    return sessions


@router.get("/{team_id}/export")
async def export_session(team_id: str):
    session_file = os.path.join(get_sessions_dir(), f"{team_id}.json")
    if not os.path.exists(session_file):
        raise HTTPException(status_code=404, detail="Session file not found")

    with open(session_file, "r", encoding="utf-8") as f:
        return json.load(f)


@router.post("/import")
async def import_sessions(body: dict | list):
    if isinstance(body, dict):
        items = [body]
    else:
        items = body

    imported = []
    errors = []
    for session_data in items:
        try:
            parsed = TeamSession.model_validate(session_data)
            result = await upsert_team_from_session(parsed, log_action="import_session")
            imported.append(result["team_id"])
        except ValidationError as e:
            # include_input=False：Pydantic 默认会把整个提交的 payload（含
            # accessToken / sessionToken）塞进 errors() 里再原样回显给客户端；
            # 这里只保留字段路径和报错原因，不回显提交的值本身。
            errors.append({
                "error": "invalid_session",
                "detail": e.errors(include_context=False, include_input=False, include_url=False),
            })
        except HTTPException as e:
            errors.append({"error": "import_failed", "detail": e.detail})
        except Exception as e:
            errors.append({"error": "import_failed", "detail": str(e)})

    return {"status": "ok", "imported": imported, "count": len(imported), "errors": errors}
