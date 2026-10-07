import asyncio
import json
from datetime import datetime, timezone
from typing import Literal, Optional

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
    # live：先现场刷新每个 active Team，只巡逻刷新成功的（真跑只能走这条）。
    # cache：不调上游，拿库里已存的成员 / 席位缓存出一份只读的空跑预览。
    source: Literal["live", "cache"] = "live"
    # 只预览这些 Team（只给 cache 用）：面板先逐个现场刷新，再只拿刷新成功的出预览。
    team_ids: Optional[list[str]] = None


# 现场刷新时同时刷新的 Team 数。每个 Team 内部仍是成员 → 席位 → 写缓存的顺序；
# 上游请求总并发另有 chatgpt_limiter 的全局上限管着。
PATROL_REFRESH_CONCURRENCY = 3


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

def _parse_time(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _error_text(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail")
    return str(detail or exc)


async def _refresh_patrol_inputs(team_id: str) -> None:
    """现场刷新一个 Team 巡逻执行链路的三项输入（成员/邀请名单、订阅、席位数），任何一步失败都抛。"""
    client = await get_team_client(team_id)
    await fetch_and_cache_members(team_id, client)
    capacity, subscription, seat_counts, _pending = (
        await fetch_live_chatgpt_seat_capacity(client)
    )
    await update_capacity_cache(team_id, subscription, seat_counts)


async def _refresh_teams_for_patrol(
    team_ids: list[str], concurrency: int
) -> tuple[list[str], list[str]]:
    """逐个 Team 现场刷新，同时最多 ``concurrency`` 个。返回 (刷新成功的, 失败说明)，都按传入顺序。

    每个 Team 只由一个协程刷新一次，全部结束后才返回，所以并发不改变"谁进白名单"：
    仍然只有三项输入全部刷新成功的 Team。
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def refresh_one(team_id: str) -> Optional[str]:
        async with semaphore:
            try:
                await _refresh_patrol_inputs(team_id)
            except Exception as exc:
                return f"{team_id}: {exc}"
        return None

    outcomes = await asyncio.gather(*(refresh_one(team_id) for team_id in team_ids))
    refreshed = [team_id for team_id, error in zip(team_ids, outcomes) if error is None]
    failures = [error for error in outcomes if error is not None]
    return refreshed, failures


async def _usable_cache_times(team_ids: list[str]) -> dict[str, str]:
    """有可用成员缓存（非空名单）的 Team → 这份缓存的时间。和 run_patrol 的冷启动守卫同一个判断。"""
    if not team_ids:
        return {}
    placeholders = ",".join("?" for _ in team_ids)
    async with get_db() as db:
        cursor = await db.execute(
            f"SELECT team_id, members_json, updated_at FROM member_cache WHERE team_id IN ({placeholders})",
            team_ids,
        )
        rows = await cursor.fetchall()
    usable: dict[str, str] = {}
    for row in rows:
        try:
            members = json.loads(row["members_json"] or "[]")
        except Exception:
            continue
        if isinstance(members, list) and members:
            usable[row["team_id"]] = row["updated_at"] or ""
    return usable


async def _run_cached_preview(team_rows: list, team_ids: Optional[list[str]]) -> dict:
    """缓存空跑预览：不调上游，只读。没有可用缓存的 Team 单独列出来，不拿别的数据去猜。"""
    if team_ids is not None:
        wanted = {str(team_id) for team_id in team_ids}
        team_rows = [row for row in team_rows if row["id"] in wanted]
    ids = [row["id"] for row in team_rows]
    cache_times = await _usable_cache_times(ids)
    allow_team_ids = [team_id for team_id in ids if team_id in cache_times]
    no_cache_teams = [
        {"team_id": row["id"], "name": row["name"] or row["id"]}
        for row in team_rows
        if row["id"] not in cache_times
    ]

    try:
        result = await asyncio.to_thread(run_patrol, True, allow_team_ids, preview=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"预览失败：{exc}") from exc
    # 同一批里最旧的那份缓存，就是这份预览能保证的"数据截至"；有一份读不出时间就不报（未知）。
    times = [_parse_time(cache_times[team_id]) for team_id in allow_team_ids]
    result["as_of"] = min(times).isoformat() if times and None not in times else None
    result["no_cache_teams"] = no_cache_teams
    result["skipped_teams"] = []
    result["team_count"] = len(allow_team_ids)
    result["source"] = "cache"
    return result


@router.post("/run")
async def trigger_patrol_run(req: PatrolRunRequest):
    """手动触发一轮巡逻。

    ``run_patrol`` 只处理白名单里的 team，而这个入口没有"刚刚跑完的同步轮次"可以
    继承，所以白名单必须由它自己挣来：逐个 active team 现场刷新执行链路的三项输入
    （成员/邀请名单、订阅、席位数），只有全部刷新成功的 team 才进白名单。刷新失败
    的 team 这一轮不碰——手动按钮同样不允许拿着一份从没刷新过的冻结缓存去判超员、
    挑人。和 ``/activate`` 的"全部刷新成功才动手"是同一条原则，只是这里退化成按
    team 生效，不整体拒绝。

    ``source="cache"`` 是例外，而且只给空跑：直接拿已存缓存出预览，不刷新、不发通知、
    不写库。真跑带 cache 一律拒绝。
    """
    if req.source == "cache" and not req.dry_run:
        raise HTTPException(status_code=400, detail="缓存数据只能用来空跑预览，真踢必须实时刷新")
    if req.team_ids is not None and req.source != "cache":
        raise HTTPException(status_code=400, detail="只预览部分 Team 仅支持缓存预览")

    async with get_db() as db:
        cursor = await db.execute("SELECT id, name FROM teams WHERE status = 'active'")
        team_rows = await cursor.fetchall()

    if req.source == "cache":
        return await _run_cached_preview(team_rows, req.team_ids)

    # 空跑可以几个 Team 一起刷新；真跑保持一个一个来，和以前完全一样。
    concurrency = PATROL_REFRESH_CONCURRENCY if req.dry_run else 1
    allow_team_ids, refresh_failures = await _refresh_teams_for_patrol(
        [row["id"] for row in team_rows], concurrency
    )

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
    result["team_count"] = len(allow_team_ids)
    result["source"] = "live"
    return result


# ── POST /api/patrol/refresh/{team_id} ──────────────────────────────────

@router.post("/refresh/{team_id}")
async def refresh_team_for_patrol(team_id: str):
    """现场刷新一个 active Team 的巡逻输入，和 /run 实时模式对每个 Team 做的是同一件事。

    面板的实时演练用它逐个刷新（能显示进度），再只拿刷新成功的 Team 出缓存预览。
    """
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM teams WHERE id = ? AND status = 'active'", (team_id,)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Team 不存在或未启用")
    try:
        await _refresh_patrol_inputs(team_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"刷新失败：{_error_text(exc)}") from exc
    cache_times = await _usable_cache_times([team_id])
    return {"team_id": team_id, "status": "ok", "cached_at": cache_times.get(team_id)}


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
