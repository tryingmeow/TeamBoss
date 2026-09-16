import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from ..chatgpt_limiter import refresh_team_auth, run_chatgpt_call
from ..database import get_db, log_operation, get_sessions_dir
from ..models import DefaultSeatTypeRequest, TeamProxyUpdate, TeamRemarkUpdate, TeamSession, TeamResponse
from ..services.pricing import discounted_monthly_total
from ..services.subscription_status import subscription_status_display
from ..services.tg_member_bindings import deactivate_member_binding_if_inactive
from ..services.tg_commands import sync_email_chat_commands_sync
from ..services.team_clients import get_team_client
from ..services.team_health_alerts import report_team_failure, report_team_recovery
from ..services.user_display_names import attach_display_names
from ..team_sync_service import (
    TEAM_CACHE_TTL_SECONDS,
    cached_default_seat_type,
    fetch_and_cache_workspace_settings,
    get_cached_workspace_settings,
    member_emails_from_members_data,
    sync_team_cache,
    update_workspace_settings_cache,
)

router = APIRouter(prefix="/api/teams", tags=["teams"])
logger = logging.getLogger(__name__)


def _compute_days_remaining(active_until: str) -> int | None:
    if not active_until:
        return None
    try:
        until = datetime.fromisoformat(active_until.replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (until - now).days
        return max(delta, 0)
    except Exception:
        return None


def _team_row_to_response(row) -> dict:
    d = dict(row)
    # Subscription data is deliberately nullable when its upstream request
    # failed. Keep the public dashboard contract string-safe while showing an
    # empty currency until the next successful sync fills it in.
    d["billing_currency"] = d.get("billing_currency") or ""
    d["will_renew"] = bool(d.get("will_renew", 1))
    d["subscription_status"] = subscription_status_display(
        d.get("active_until"), d["will_renew"], d.get("last_full_sync_at")
    )
    d["is_codex_enabled"] = bool(d.get("is_codex_enabled", 0))
    d["default_seat_type"] = cached_default_seat_type(d.get("cached_data"))
    d["days_remaining"] = _compute_days_remaining(d.get("active_until"))
    if d.get("chatgpt_count") is None:
        d["chatgpt_count"] = max(
            0,
            int(d.get("seats_in_use") or 0) - int(d.get("codex_count") or 0),
        )

    # Extract billing_period
    billing_period = d.get("billing_period")
    d["billing_period"] = billing_period

    # Calculate monthly fees only if billing period is monthly and price is available
    price_per_seat = d.get("price_per_seat")
    seats_entitled = int(d.get("seats_entitled") or 0)

    if billing_period == "monthly" and price_per_seat is not None:
        price_per_seat_float = float(price_per_seat)
        d["price_per_seat"] = price_per_seat_float
        d["monthly_subtotal"] = price_per_seat_float * seats_entitled
        d["monthly_total"] = discounted_monthly_total(
            price_per_seat_float,
            seats_entitled,
            d.get("discount_amount") or 0,
        )
    else:
        d["price_per_seat"] = None
        d["monthly_subtotal"] = None
        d["monthly_total"] = None

    # 库里存的是 JSON 字符串，接口统一给数组，前端不必再解析一次。
    raw_failures = d.get("last_sync_partial_failures")
    parsed_failures: list[str] = []
    if raw_failures:
        try:
            loaded = json.loads(raw_failures)
            if isinstance(loaded, list):
                parsed_failures = [str(item) for item in loaded]
        except (ValueError, TypeError):
            parsed_failures = []
    d["last_sync_partial_failures"] = parsed_failures
    # 迁移前建的行是 NULL，接口统一给 'ok'。
    d["auth_state"] = "rejected" if d.get("auth_state") == "rejected" else "ok"

    fields = TeamResponse.model_fields.keys()
    team = {k: d.get(k) for k in fields}
    team["cached_member_emails"] = team.get("cached_member_emails") or []
    team["last_sync_partial_failures"] = team.get("last_sync_partial_failures") or []
    return team


def _chatgpt_error(result: dict) -> str | None:
    return result.get("error") if isinstance(result, dict) else None


@router.get("")
async def list_teams():
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM teams ORDER BY created_at DESC")
        rows = await cursor.fetchall()

    # 批量读取成员邮件缓存，附加到每个 team 供前端搜索
    async with get_db() as db:
        cache_cursor = await db.execute("SELECT team_id, members_json, pending_json FROM member_cache")
        cache_rows = await cache_cursor.fetchall()

    email_cache: dict[str, list[str]] = {}
    for c in cache_rows:
        emails: set[str] = set()
        try:
            for m in json.loads(c["members_json"] or "[]"):
                if m.get("email"):
                    emails.add(m["email"].lower())
        except Exception:
            pass
        try:
            for p in json.loads(c["pending_json"] or "[]"):
                if p.get("email"):
                    emails.add(p["email"].lower())
        except Exception:
            pass
        email_cache[c["team_id"]] = list(emails)

    result = []
    for r in rows:
        team = _team_row_to_response(r)
        team["cached_member_emails"] = email_cache.get(r["id"], [])
        result.append(team)
    return result


@router.get("/{team_id}")
async def get_team(team_id: str):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM teams WHERE id = ?", (team_id,))
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Team not found")
    return _team_row_to_response(row)


@router.post("/{team_id}/sync")
async def sync_team(team_id: str, force: bool = Query(False)):
    result = await sync_team_cache(team_id, force=force)
    team = _team_row_to_response(result["row"])
    team["cached_member_emails"] = member_emails_from_members_data(result["members"])
    return {
        "team": team,
        "members": await attach_display_names(result["members"]),
        "workspace_settings": result["workspace_settings"],
        "cached": result["cached"],
        "refreshed": result["refreshed"],
        "reason": result["reason"],
    }


@router.get("/{team_id}/workspace-settings")
async def get_team_workspace_settings(
    team_id: str,
    refresh: bool = Query(False),
    max_age_seconds: int = Query(TEAM_CACHE_TTL_SECONDS, ge=0),
):
    if not refresh:
        cached = await get_cached_workspace_settings(team_id, max_age_seconds=max_age_seconds)
        if cached is not None:
            return cached

    try:
        return await fetch_and_cache_workspace_settings(team_id)
    except HTTPException as exc:
        await log_operation(team_id, "get_workspace_settings", None, None, "failed", str(exc.detail))
        raise


@router.post("/{team_id}/workspace-settings/default-seat-type")
async def update_team_default_seat_type(team_id: str, req: DefaultSeatTypeRequest):
    client = await get_team_client(team_id)
    result = await run_chatgpt_call(client.set_default_seat_type, req.seat_type)

    error = _chatgpt_error(result)
    if error:
        await log_operation(
            team_id,
            "change_default_seat_type",
            None,
            f"seat_type={req.seat_type}",
            "failed",
            error,
        )
        raise HTTPException(status_code=502, detail=error)

    await log_operation(
        team_id,
        "change_default_seat_type",
        None,
        f"seat_type={req.seat_type}",
        "success",
    )

    return await update_workspace_settings_cache(team_id, result)


@router.patch("/{team_id}/proxy")
async def update_team_proxy(team_id: str, req: TeamProxyUpdate):
    try:
        async with get_db() as db:
            row = await (await db.execute("SELECT id FROM teams WHERE id = ?", (team_id,))).fetchone()
            if not row:
                await log_operation(team_id, "update_team_proxy", None, f"proxy_id={req.proxy_id}", "failed", "Team not found")
                raise HTTPException(status_code=404, detail="Team not found")
            if req.proxy_id is not None:
                proxy_row = await (await db.execute("SELECT id FROM proxies WHERE id = ?", (req.proxy_id,))).fetchone()
                if not proxy_row:
                    await log_operation(team_id, "update_team_proxy", None, f"proxy_id={req.proxy_id}", "failed", "Proxy not found")
                    raise HTTPException(status_code=400, detail="Proxy not found")
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "UPDATE teams SET proxy_id = ?, updated_at = ? WHERE id = ?",
                (req.proxy_id, now, team_id),
            )
            await db.commit()
        await log_operation(team_id, "update_team_proxy", None, f"proxy_id={req.proxy_id}", "success")
        return {"status": "ok", "proxy_id": req.proxy_id}
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(team_id, "update_team_proxy", None, f"proxy_id={req.proxy_id}", "failed", str(e))
        raise


@router.patch("/{team_id}/remark")
async def update_team_remark(team_id: str, req: TeamRemarkUpdate):
    try:
        remark = (req.remark or "").strip()
        if len(remark) > 80:
            await log_operation(team_id, "update_team_remark", None, f"remark_len={len(remark)}", "failed", "Remark exceeds 80 characters")
            raise HTTPException(status_code=400, detail="Remark must be 80 characters or fewer")

        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as db:
            row = await (await db.execute("SELECT id FROM teams WHERE id = ?", (team_id,))).fetchone()
            if not row:
                await log_operation(team_id, "update_team_remark", None, None, "failed", "Team not found")
                raise HTTPException(status_code=404, detail="Team not found")

            await db.execute(
                "UPDATE teams SET remark = ?, updated_at = ? WHERE id = ?",
                (remark or None, now, team_id),
            )
            await db.commit()

            cursor = await db.execute("SELECT * FROM teams WHERE id = ?", (team_id,))
            updated = await cursor.fetchone()

        await log_operation(team_id, "update_team_remark", None, f"remark_len={len(remark)}", "success")
        return _team_row_to_response(updated)
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(team_id, "update_team_remark", None, None, "failed", str(e))
        raise


@router.post("")
async def add_team(session_data: TeamSession, proxy_id: int | None = Query(None)):
    from ..team_service import upsert_team_from_session

    return await upsert_team_from_session(session_data, proxy_id=proxy_id)


@router.post("/{team_id}/reimport")
async def reimport_team(
    team_id: str,
    session_data: TeamSession,
    proxy_id: int | None = Query(None),
):
    from ..team_service import upsert_team_from_session

    async with get_db() as db:
        row = await (
            await db.execute("SELECT id FROM teams WHERE id = ?", (team_id,))
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Team not found")

    return await upsert_team_from_session(
        session_data,
        log_action="reimport_team",
        proxy_id=proxy_id,
        expected_team_id=team_id,
    )


@router.delete("/{team_id}")
async def delete_team(team_id: str):
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM teams WHERE id = ?", (team_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Team not found")

        expiry_cursor = await db.execute(
            "SELECT DISTINCT lower(trim(email)) AS email FROM member_expiry WHERE team_id = ? AND trim(email) != ''",
            (team_id,),
        )
        affected_emails = [row["email"] for row in await expiry_cursor.fetchall()]

        # Count member_expiry records to be retained
        expiry_count_cursor = await db.execute(
            "SELECT COUNT(*) as count FROM member_expiry WHERE team_id = ?",
            (team_id,),
        )
        expiry_count_row = await expiry_count_cursor.fetchone()
        retained_expiry_count = expiry_count_row["count"] if expiry_count_row else 0

        await db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        await db.execute("DELETE FROM patrol_team_baselines WHERE team_id = ?", (team_id,))
        # DO NOT DELETE member_expiry - retain payment history and audit trail
        await db.execute("DELETE FROM member_cache WHERE team_id = ?", (team_id,))
        await db.execute("UPDATE member_watch SET done = 1 WHERE team_id = ?", (team_id,))
        for email in affected_emails:
            await deactivate_member_binding_if_inactive(db, email)
        await db.commit()

    for email in affected_emails:
        try:
            await asyncio.to_thread(sync_email_chat_commands_sync, email)
        except Exception:
            logger.exception("failed to sync Telegram commands after deleting team=%s", team_id)

    session_file = os.path.join(get_sessions_dir(), f"{team_id}.json")
    try:
        os.remove(session_file)
    except FileNotFoundError:
        pass
    except OSError:
        # Team 主记录已经事务性删除；会话文件清理失败不能把一个已成功的
        # DELETE 翻成 500。保留明确日志供管理员修复文件权限。
        logger.exception("failed to delete session file after deleting team=%s", team_id)

    try:
        await log_operation(team_id, "delete_team", None, f"Team removed from management; retained {retained_expiry_count} member_expiry records for audit trail", "success")
    except Exception:
        logger.exception("failed to write audit log after deleting team=%s", team_id)
    return {"status": "ok"}


@router.post("/{team_id}/refresh")
async def refresh_team_token(team_id: str):
    outcome = await refresh_team_auth(
        team_id,
        trigger="manual_token_refresh",
        force=True,
    )
    if outcome.status == "not_found":
        raise HTTPException(status_code=404, detail="Team not found")
    if outcome.status == "failed":
        await report_team_failure(
            team_id,
            "chatgpt_auth",
            outcome.error or "Token refresh failed",
            source="manual_token_refresh",
        )
        raise HTTPException(
            status_code=502,
            detail=f"Token refresh failed: {outcome.error}",
        )

    if outcome.status == "refreshed":
        await report_team_recovery(
            team_id,
            "chatgpt_auth",
            source="manual_token_refresh",
        )
    return {
        "status": outcome.status,
        "access_changed": outcome.access_changed,
        "session_changed": outcome.session_changed,
        "token_expires": outcome.token_expires,
    }


@router.post("/refresh-all")
async def refresh_all_tokens():
    results = []

    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM teams")
        teams = await cursor.fetchall()

    for team in teams:
        team_id = team["id"]
        try:
            outcome = await refresh_team_auth(
                team_id,
                trigger="manual_token_refresh_all",
                force=True,
            )
            if outcome.status in {"failed", "not_found"}:
                await report_team_failure(
                    team_id,
                    "chatgpt_auth",
                    outcome.error or "Token refresh failed",
                    source="manual_token_refresh_all",
                )
                results.append(
                    {
                        "team_id": team_id,
                        "status": "failed",
                        "error": outcome.error or "Token refresh failed",
                    }
                )
            else:
                if outcome.status == "refreshed":
                    await report_team_recovery(
                        team_id,
                        "chatgpt_auth",
                        source="manual_token_refresh_all",
                    )
                results.append(
                    {
                        "team_id": team_id,
                        "status": "ok",
                        "refresh_result": outcome.status,
                        "access_changed": outcome.access_changed,
                        "session_changed": outcome.session_changed,
                    }
                )
        except Exception as e:
            await log_operation(team_id, "refresh_token", None, None, "failed", str(e), "manual")
            await report_team_failure(
                team_id,
                "chatgpt_auth",
                e,
                source="manual_token_refresh_all",
            )
            results.append({"team_id": team_id, "status": "failed", "error": str(e)})

    failed = sum(1 for item in results if item["status"] != "ok")
    return {
        "status": "partial" if failed else "ok",
        "results": results,
        "succeeded": len(results) - failed,
        "failed": failed,
    }


@router.post("/sync")
async def sync_all_teams():
    results = []

    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM teams WHERE status = 'active'")
        teams = await cursor.fetchall()

    for team in teams:
        team_id = team["id"]
        try:
            await sync_team_cache(team_id, force=True)
            results.append({"team_id": team_id, "status": "ok"})
        except Exception as e:
            results.append({"team_id": team_id, "status": "failed", "error": str(e)})

    failed = [item for item in results if item["status"] != "ok"]
    succeeded = len(results) - len(failed)
    await log_operation(
        None,
        "sync_all",
        None,
        f"Synced {succeeded}/{len(results)} teams; failed={len(failed)}",
        "failed" if failed else "success",
        "; ".join(item["team_id"] for item in failed) or None,
        "manual",
    )
    return {
        "status": "partial" if failed else "ok",
        "results": results,
        "succeeded": succeeded,
        "failed": len(failed),
    }
