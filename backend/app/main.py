import asyncio
import logging
import os
import subprocess
from contextlib import asynccontextmanager

from typing import Optional

from fastapi import Depends, FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware

from .database import init_database
from .scheduler import start_scheduler, stop_scheduler
from .routes import teams, members, sessions, settings, logs, access_tokens, admin, users, resources, proxies, gpt_members, finance, patrol, tg
from .security import (
    _bearer_token,
    ensure_admin_credentials_initialized,
    get_admin_api_key,
    get_cors_origins,
    require_admin,
    safe_compare_digest,
)
from .tg_bot import start_bot_thread, stop_bot_thread


def _resolve_app_version() -> str:
    """Best-effort real identifier for the running code, never a fabricated number.

    "1.0.0" never changed with deploys and told operators nothing. A short git
    commit hash actually identifies what's running. Falls back to an explicit
    env var (for container builds without a .git dir), then "unknown" — never
    a made-up version string.
    """
    env_version = os.getenv("AUTO_TEAM_VERSION") or os.getenv("GIT_COMMIT")
    if env_version:
        return env_version
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=2,
        )
        commit = result.stdout.strip()
        if result.returncode == 0 and commit:
            return commit
    except Exception:
        pass
    return "unknown"


APP_VERSION = _resolve_app_version()

logger = logging.getLogger(__name__)


def _log_entrypoints() -> None:
    """把两个入口地址打到启动日志里。

    后台在 /admin，根路径是给成员用的兑换页——不写出来的话，第一次部署的人会打开根路径
    看到一个要填邮箱和兑换码的界面，完全找不到登录入口。
    """
    port = os.getenv("AUTO_TEAM_PORT", "8080")
    logger.info("管理后台:   http://<你的服务器地址>:%s/admin", port)
    logger.info("成员兑换页: http://<你的服务器地址>:%s/", port)
    logger.info("默认只监听 127.0.0.1，请在宿主机配好 TLS 反代后再对外访问。")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_database()
    await ensure_admin_credentials_initialized()
    _log_entrypoints()
    start_scheduler()
    start_bot_thread()
    yield
    stop_bot_thread()
    stop_scheduler()


app = FastAPI(title="TeamBoss", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

admin_dependencies = [Depends(require_admin)]

app.include_router(teams.router, dependencies=admin_dependencies)
app.include_router(members.router, dependencies=admin_dependencies)
app.include_router(sessions.router, dependencies=admin_dependencies)
app.include_router(settings.router, dependencies=admin_dependencies)
app.include_router(logs.router, dependencies=admin_dependencies)
app.include_router(users.router, dependencies=admin_dependencies)
app.include_router(resources.router, dependencies=admin_dependencies)
app.include_router(proxies.router, dependencies=admin_dependencies)
app.include_router(gpt_members.router, dependencies=admin_dependencies)
app.include_router(finance.router, dependencies=admin_dependencies)
app.include_router(patrol.router, dependencies=admin_dependencies)
app.include_router(tg.router, dependencies=admin_dependencies)
app.include_router(admin.router)
app.include_router(access_tokens.admin_router)
app.include_router(access_tokens.public_router)


async def _is_admin_request(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> bool:
    """健康检查专用的“可选鉴权”：带对管理员凭据返回 True，否则 False，不抛异常。"""
    try:
        expected = await get_admin_api_key()
    except Exception:
        return False
    if not expected:
        return False
    supplied = (x_api_key or "").strip() or (_bearer_token(authorization) or "").strip()
    if not supplied:
        return False
    return safe_compare_digest(supplied, expected)


@app.get("/api/health")
async def health_check(is_admin: bool = Depends(_is_admin_request)):
    """
    Comprehensive health check endpoint.

    公网可裸调，但只返回粗粒度状态（status/version/timestamp）——这个服务是公网可
    访问的，逐组件的错误信息可能带出数据库路径、Team 数量等内部细节。带管理员凭据
    调用才返回 components 明细。

    Returns a tiered status (healthy/degraded/unhealthy) with details on:
    - Database connectivity and query capability
    - Scheduler thread alive + job count
    - Last successful data sync across teams
    - Telegram bot configuration and thread status

    HTTP 200: Healthy or Degraded (partial failures but core functional)
    HTTP 503: Unhealthy (database or scheduler down)
    """
    from datetime import datetime, timezone, timedelta
    import sqlite3

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Components health tracking
    checks = {
        "database": {"status": "unknown"},
        "scheduler": {"status": "unknown"},
        "sync": {"status": "unknown"},
        "telegram": {"status": "unknown"},
    }

    http_code = 200
    overall_status = "healthy"

    # ──────────────────────────────────────────────────────────────────
    # 1. Database Check
    # ──────────────────────────────────────────────────────────────────
    try:
        from .database import get_db_path

        def _check_database():
            conn = sqlite3.connect(get_db_path(), timeout=2)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT COUNT(*) as count FROM teams")
                result = cursor.fetchone()
                return result["count"] if result else 0
            finally:
                conn.close()

        total_teams = await asyncio.to_thread(_check_database)

        checks["database"] = {
            "status": "ok",
            "checked_at": now_iso,
        }
    except Exception as e:
        checks["database"] = {
            "status": "error",
            "error": str(e),
            "checked_at": now_iso,
        }
        overall_status = "unhealthy"
        http_code = 503

    # ──────────────────────────────────────────────────────────────────
    # 2. Scheduler Check (APScheduler thread alive + job count)
    # ──────────────────────────────────────────────────────────────────
    try:
        from .scheduler import scheduler

        running = scheduler.running
        jobs = scheduler.get_jobs() if running else []
        job_count = len(jobs)

        if running:
            checks["scheduler"] = {
                "status": "ok",
                "running": True,
                "jobs_count": job_count,
                "checked_at": now_iso,
            }
        else:
            checks["scheduler"] = {
                "status": "error",
                "running": False,
                "jobs_count": job_count,
                "checked_at": now_iso,
            }
            overall_status = "unhealthy"
            http_code = 503
    except Exception as e:
        checks["scheduler"] = {
            "status": "error",
            "error": str(e),
            "checked_at": now_iso,
        }
        overall_status = "unhealthy"
        http_code = 503

    # ──────────────────────────────────────────────────────────────────
    # 3. Sync Status Check (last_full_sync_at across teams)
    # ──────────────────────────────────────────────────────────────────
    try:
        def _check_sync():
            conn = sqlite3.connect(get_db_path(), timeout=2)
            try:
                conn.row_factory = sqlite3.Row
                # Get total and synced count
                total_row = conn.execute("SELECT COUNT(*) as count FROM teams").fetchone()
                total_teams = total_row["count"] if total_row else 0

                # Get the most recent sync time
                recent = conn.execute(
                    "SELECT last_full_sync_at FROM teams WHERE last_full_sync_at IS NOT NULL "
                    "ORDER BY last_full_sync_at DESC LIMIT 1"
                ).fetchone()
                return total_teams, recent
            finally:
                conn.close()

        total_teams, recent = await asyncio.to_thread(_check_sync)

        if recent and recent["last_full_sync_at"]:
            last_sync_iso = recent["last_full_sync_at"]
            try:
                last_sync_dt = datetime.fromisoformat(last_sync_iso.replace("Z", "+00:00"))
                seconds_since = int((now - last_sync_dt).total_seconds())

                sync_status = "ok"
                # Alert if sync is stale (> 1 hour)
                if seconds_since > 3600:
                    sync_status = "stale"
                    if overall_status == "healthy":
                        overall_status = "degraded"

                checks["sync"] = {
                    "status": sync_status,
                    "last_full_sync_at": last_sync_iso,
                    "seconds_since_last_sync": seconds_since,
                    "total_teams": total_teams,
                    "checked_at": now_iso,
                }
            except Exception as e:
                checks["sync"] = {
                    "status": "error",
                    "last_full_sync_at": last_sync_iso,
                    "error": str(e),
                    "total_teams": total_teams,
                    "checked_at": now_iso,
                }
        else:
            # Never synced
            checks["sync"] = {
                "status": "never",
                "last_full_sync_at": None,
                "total_teams": total_teams,
                "checked_at": now_iso,
            }
            if total_teams > 0:
                if overall_status == "healthy":
                    overall_status = "degraded"

    except Exception as e:
        checks["sync"] = {
            "status": "error",
            "error": str(e),
            "checked_at": now_iso,
        }

    # ──────────────────────────────────────────────────────────────────
    # 4. Telegram Bot Check (configured + thread alive)
    # ──────────────────────────────────────────────────────────────────
    try:
        import sqlite3

        def _check_telegram_token():
            conn = sqlite3.connect(get_db_path(), timeout=2)
            try:
                cursor = conn.execute("SELECT value FROM settings WHERE key = 'tg_bot_token'")
                return cursor.fetchone()
            finally:
                conn.close()

        token_row = await asyncio.to_thread(_check_telegram_token)

        token = (token_row[0] if token_row else "").strip() if token_row else ""
        configured = bool(token)

        if configured:
            # Check if bot thread is alive
            from .tg_bot import is_bot_thread_alive

            thread_alive = is_bot_thread_alive()
            status_val = "ok" if thread_alive else "error"

            checks["telegram"] = {
                "status": status_val,
                "configured": True,
                "thread_alive": thread_alive,
                "checked_at": now_iso,
            }

            if not thread_alive and overall_status == "healthy":
                overall_status = "degraded"
        else:
            checks["telegram"] = {
                "status": "ok",
                "configured": False,
                "thread_alive": False,
                "checked_at": now_iso,
            }

    except Exception as e:
        checks["telegram"] = {
            "status": "error",
            "error": str(e),
            "checked_at": now_iso,
        }

    # ──────────────────────────────────────────────────────────────────
    # Assemble response
    # ──────────────────────────────────────────────────────────────────
    response = {
        "status": overall_status,
        "version": APP_VERSION,
        "timestamp": now_iso,
    }
    # 明细只给管理员：状态码对所有人一致，编排/监控靠 200/503 就够判断了。
    if is_admin:
        response["components"] = checks

    from fastapi.responses import JSONResponse
    return JSONResponse(content=response, status_code=http_code)


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("AUTO_TEAM_BACKEND_HOST", "127.0.0.1"),
        port=int(os.getenv("AUTO_TEAM_BACKEND_PORT", "18087")),
        reload=True,
    )
