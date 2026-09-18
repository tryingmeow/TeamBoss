from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from ..database import get_db, log_operation
from ..models import SettingsUpdate


router = APIRouter(prefix="/api/settings", tags=["settings"])

# 只有这四个 key 会经 GET /api/settings 返回给前端；settings 表里还存着
# admin_api_key / admin_password_hash / tg_bot_token 等敏感项，绝不能随
# 普通页面加载泄露出去。PATCH 也只写这四个 key，二者保持一致。
PUBLIC_SETTINGS_KEYS = (
    "sync_interval_minutes",
    "api_concurrency",
    "expiry_kick_mode",
    "expiry_kick_delay_hours",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _write_settings(values: dict[str, Any]) -> None:
    now = _now_iso()
    async with get_db() as db:
        for key, value in values.items():
            await db.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, str(value), now),
            )
        await db.commit()


@router.get("")
async def get_settings():
    placeholders = ",".join("?" * len(PUBLIC_SETTINGS_KEYS))
    async with get_db() as db:
        cursor = await db.execute(
            f"SELECT key, value, updated_at FROM settings WHERE key IN ({placeholders})",
            PUBLIC_SETTINGS_KEYS,
        )
        rows = await cursor.fetchall()
    return {row["key"]: {"value": row["value"], "updated_at": row["updated_at"]} for row in rows}


@router.patch("")
async def update_settings(req: SettingsUpdate):
    updates: dict[str, Any] = {}

    if req.sync_interval_minutes is not None:
        if not 5 <= req.sync_interval_minutes <= 60:
            await log_operation(None, "update_settings", None, f"sync_interval_minutes={req.sync_interval_minutes}", "failed", "Value out of range")
            raise HTTPException(status_code=400, detail="sync_interval_minutes must be between 5 and 60")
        updates["sync_interval_minutes"] = req.sync_interval_minutes

    if req.api_concurrency is not None:
        if not 1 <= req.api_concurrency <= 10:
            await log_operation(None, "update_settings", None, f"api_concurrency={req.api_concurrency}", "failed", "Value out of range")
            raise HTTPException(status_code=400, detail="api_concurrency must be between 1 and 10")
        updates["api_concurrency"] = req.api_concurrency

    if req.expiry_kick_mode is not None:
        updates["expiry_kick_mode"] = "day_end" if req.expiry_kick_mode == "day_start" else req.expiry_kick_mode

    if req.expiry_kick_delay_hours is not None:
        if not 0 <= req.expiry_kick_delay_hours <= 720:
            await log_operation(None, "update_settings", None, f"expiry_kick_delay_hours={req.expiry_kick_delay_hours}", "failed", "Value out of range")
            raise HTTPException(status_code=400, detail="expiry_kick_delay_hours must be between 0 and 720")
        updates["expiry_kick_delay_hours"] = req.expiry_kick_delay_hours

    if updates:
        try:
            await _write_settings(updates)
            if "sync_interval_minutes" in updates:
                from ..scheduler import reschedule_sync_job
                reschedule_sync_job(int(updates["sync_interval_minutes"]))
            detail = ", ".join(f"{k}={v}" for k, v in updates.items())
            await log_operation(None, "update_settings", None, detail, "success")
        except Exception as e:
            await log_operation(None, "update_settings", None, None, "failed", str(e))
            raise

    return {"status": "ok", "updated": updates}
