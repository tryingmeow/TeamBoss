import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db, log_operation
from ..member_cache_service import fetch_and_cache_members
from ..services.patrol import (
    PatrolActivationError,
    activate_patrol_sync,
    parse_exempt_team_ids,
    run_patrol,
    team_risk_statuses_sync,
)
from ..services.seat_capacity import (
    fetch_live_chatgpt_seat_capacity,
    update_capacity_cache,
)
from ..services.team_clients import get_team_client

router = APIRouter(prefix="/api/patrol", tags=["patrol"])


# ── 本地 Pydantic 请求模型（按契约要求，不往 models.py 加，避免和 Agent-TGBot 撞车） ──

class PatrolSettingsUpdate(BaseModel):
    kick_enabled: Optional[bool] = None
    exempt_team_ids: Optional[list[str]] = None
    strict_mode_enabled: Optional[bool] = None


class PatrolRunRequest(BaseModel):
    dry_run: bool = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _write_settings(db, values: dict[str, str]) -> None:
    now = _now_iso()
    for key, value in values.items():
        await db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, now),
        )


async def _read_patrol_settings(db) -> dict:
    cursor = await db.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('patrol_kick_enabled', 'patrol_baseline_at', 'patrol_exempt_team_ids', "
        " 'sync_interval_minutes', 'patrol_strict_mode_enabled')"
    )
    rows = await cursor.fetchall()
    return {row["key"]: row["value"] for row in rows}


# ── GET /api/patrol/status ──────────────────────────────────────────────

async def _load_status_members(team_id: str, refresh: bool, errors: list[dict]) -> list:
    """读取某个 team 的成员列表：refresh=True 时先实时刷新缓存，失败则回退读缓存。"""
    if refresh:
        try:
            client = await get_team_client(team_id)
            snapshot = await fetch_and_cache_members(team_id, client)
            members = snapshot.get("members")
            if isinstance(members, list):
                return members
        except Exception as exc:
            errors.append({"team_id": team_id, "error": str(getattr(exc, "detail", exc))})

    async with get_db() as db:
        cache_cursor = await db.execute(
            "SELECT members_json FROM member_cache WHERE team_id = ?", (team_id,)
        )
        cache_row = await cache_cursor.fetchone()
    if cache_row and cache_row["members_json"]:
        try:
            parsed = json.loads(cache_row["members_json"])
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


@router.get("/status")
async def get_patrol_status(refresh: bool = Query(False)):
    async with get_db() as db:
        settings = await _read_patrol_settings(db)
        cursor = await db.execute(
            "SELECT id, name, is_codex_enabled, seats_entitled FROM teams WHERE status = 'active'"
        )
        teams = await cursor.fetchall()
        team_rows = [dict(team) for team in teams]

    errors: list[dict] = []
    status_inputs = []
    for team in team_rows:
        team_id = team["id"]
        members = await _load_status_members(team_id, refresh, errors)
        status_inputs.append({
            "team_id": team_id,
            "name": team["name"] or team_id,
            "codex_enabled": bool(team["is_codex_enabled"]),
            # 原样交给 classify_team：席位数未知时它不出任何踢人预览
            # （entitlement_valid=False），不能在这里先 `or 0` 把未知变成 0。
            "seats_entitled": team["seats_entitled"],
            "members": members,
        })
    # "待处理"必须和真踢同一份选人（TeamBoss 记录保护的人不算），要读库，放到线程里跑。
    team_statuses = await asyncio.to_thread(team_risk_statuses_sync, status_inputs)

    result = {
        "kick_enabled": settings.get("patrol_kick_enabled") == "1",
        "baseline_at": (settings.get("patrol_baseline_at") or "").strip() or None,
        "sync_interval_minutes": int(settings.get("sync_interval_minutes") or 15),
        "exempt_team_ids": parse_exempt_team_ids(settings.get("patrol_exempt_team_ids")),
        # 严格模式：独立危险开关，默认关闭。开启后忽略超员判定 + Codex 豁免，
        # 把所有非系统拉入的人一律列为候选（仍受基线/豁免/延迟/批量护栏约束）。
        "strict_mode_enabled": settings.get("patrol_strict_mode_enabled") == "1",
        "teams": team_statuses,
    }
    if errors:
        result["errors"] = errors
    return result


# ── PATCH /api/patrol/settings ──────────────────────────────────────────
@router.patch("/settings")
async def update_patrol_settings(req: PatrolSettingsUpdate):
    updates: dict[str, str] = {}
    if req.kick_enabled is not None:
        if req.kick_enabled:
            await log_operation(None, "update_patrol_settings", None, "kick_enabled=true", "failed", "Use /activate endpoint to enable")
            raise HTTPException(
                status_code=409,
                detail="开启时必须通过“豁免现有成员并开启自动踢人”安全入口",
            )
        updates["patrol_kick_enabled"] = "0"
    if req.exempt_team_ids is not None:
        updates["patrol_exempt_team_ids"] = json.dumps(
            [str(x) for x in req.exempt_team_ids], ensure_ascii=False
        )
    if req.strict_mode_enabled is not None:
        # 严格模式没有 kick_enabled 那样的"必须走安全入口"限制：开启它本身不会
        # 立即动手（仍然要求该 team 已有基线、过延迟窗口、批量护栏放行等），
        # 所以直接允许通过这个通用设置接口切换，true/false 都可以。
        updates["patrol_strict_mode_enabled"] = "1" if req.strict_mode_enabled else "0"

    try:
        async with get_db() as db:
            if updates:
                await _write_settings(db, updates)
                await db.commit()
            settings = await _read_patrol_settings(db)

        detail_parts = []
        if req.kick_enabled is not None:
            detail_parts.append(f"kick_enabled={req.kick_enabled}")
        if req.exempt_team_ids is not None:
            detail_parts.append(f"exempt_team_ids_count={len(req.exempt_team_ids)}")
        if req.strict_mode_enabled is not None:
            detail_parts.append(f"strict_mode_enabled={req.strict_mode_enabled}")
        detail = ", ".join(detail_parts) if detail_parts else "no_changes"
        await log_operation(None, "update_patrol_settings", None, detail, "success")

        return {
            "status": "ok",
            "kick_enabled": settings.get("patrol_kick_enabled") == "1",
            "exempt_team_ids": parse_exempt_team_ids(settings.get("patrol_exempt_team_ids")),
            "strict_mode_enabled": settings.get("patrol_strict_mode_enabled") == "1",
        }
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "update_patrol_settings", None, None, "failed", str(e))
        raise


# ── POST /api/patrol/run ────────────────────────────────────────────────

@router.post("/run")
async def trigger_patrol_run(req: PatrolRunRequest):
    """手动触发一轮巡逻。

    ``run_patrol`` 只处理白名单里的 team，而这个入口没有"刚刚跑完的同步轮次"可以
    继承，所以白名单必须由它自己挣来：逐个 active team 现场刷新执行链路的三项输入
    （成员/邀请名单、订阅、席位数），只有全部刷新成功的 team 才进白名单。刷新失败
    的 team 这一轮不碰——手动按钮同样不允许拿着一份从没刷新过的冻结缓存去判超员、
    挑人。和 ``/activate`` 的"全部刷新成功才动手"是同一条原则，只是这里退化成按
    team 生效，不整体拒绝。
    """
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM teams WHERE status = 'active'")
        team_rows = await cursor.fetchall()

    allow_team_ids: list[str] = []
    refresh_failures: list[str] = []
    for row in team_rows:
        team_id = row["id"]
        try:
            client = await get_team_client(team_id)
            await fetch_and_cache_members(team_id, client)
            capacity, subscription, seat_counts, _pending = (
                await fetch_live_chatgpt_seat_capacity(client)
            )
            await update_capacity_cache(team_id, subscription, seat_counts)
        except Exception as exc:
            refresh_failures.append(f"{team_id}: {exc}")
            continue
        allow_team_ids.append(team_id)

    if refresh_failures:
        await log_operation(
            None,
            "patrol_manual_run",
            None,
            f"patrolled={len(allow_team_ids)}, skipped_unrefreshed={len(refresh_failures)}",
            "success",
            "; ".join(refresh_failures),
        )

    # run_patrol 是同步阻塞函数（内部走同步 sqlite3 + 同步 HTTP 调用），
    # 丢进线程池跑，不阻塞事件循环。与 tg_notify.notify_admins 的做法一致。
    result = await asyncio.to_thread(run_patrol, req.dry_run, allow_team_ids)
    result["skipped_teams"] = refresh_failures
    return result


# ── POST /api/patrol/activate ───────────────────────────────────────────

@router.post("/activate")
async def activate_patrol():
    """豁免当前成员并开启自动踢人；这是唯一允许打开真踢的 API。

    先要求所有 active team 实时刷新成功，再原子地保护当前快照并打开开关。
    任何一个 team 刷新失败都会拒绝开启，避免部分成员未被保护。
    """
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT id FROM teams WHERE status = 'active'")
            team_rows = await cursor.fetchall()
        team_ids = [row["id"] for row in team_rows]

        refresh_failures: list[str] = []
        for team_id in team_ids:
            try:
                client = await get_team_client(team_id)
                await fetch_and_cache_members(team_id, client)
            except Exception as exc:
                refresh_failures.append(f"{team_id}: {exc}")

        if refresh_failures:
            await log_operation(None, "activate_patrol", None, f"refresh_failures={len(refresh_failures)}", "failed", "; ".join(refresh_failures))
            raise HTTPException(
                status_code=409,
                detail=f"成员刷新未全部成功，未开启自动踢人：{'；'.join(refresh_failures)}",
            )

        try:
            result = await asyncio.to_thread(activate_patrol_sync, team_ids)
            await log_operation(None, "activate_patrol", None, f"teams_protected={len(team_ids)}", "success")
            return result
        except PatrolActivationError as exc:
            await log_operation(None, "activate_patrol", None, f"teams_protected={len(team_ids)}", "failed", str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "activate_patrol", None, None, "failed", str(e))
        raise
