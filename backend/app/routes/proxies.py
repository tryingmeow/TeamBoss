import asyncio

from curl_cffi import requests as curl_requests
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..chatgpt_client import IMPERSONATE
from ..database import get_db, log_operation
from ..models import ProxyCreate, ProxyUpdate


router = APIRouter(prefix="/api/proxies", tags=["proxies"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("")
async def list_proxies():
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM proxies ORDER BY id")
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@router.post("")
async def create_proxy(req: ProxyCreate):
    if not req.name.strip():
        await log_operation(None, "create_proxy", None, None, "failed", "Name required")
        raise HTTPException(status_code=400, detail="Name required")
    if not req.url.strip():
        await log_operation(None, "create_proxy", None, None, "failed", "URL required")
        raise HTTPException(status_code=400, detail="URL required")

    try:
        now = _now_iso()
        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO proxies (name, url, status, created_at, updated_at) VALUES (?, ?, 'unknown', ?, ?)",
                (req.name.strip(), req.url.strip(), now, now),
            )
            proxy_id = cursor.lastrowid
            await db.commit()

        await log_operation(None, "create_proxy", None, f"name={req.name.strip()}", "success")
        return {"id": proxy_id, "name": req.name.strip(), "url": req.url.strip(), "status": "unknown"}
    except Exception as e:
        await log_operation(None, "create_proxy", None, f"name={req.name.strip()}", "failed", str(e))
        raise


@router.patch("/{proxy_id}")
async def update_proxy(proxy_id: int, req: ProxyUpdate):
    try:
        async with get_db() as db:
            row = await (await db.execute("SELECT id FROM proxies WHERE id = ?", (proxy_id,))).fetchone()
            if not row:
                await log_operation(None, "update_proxy", None, f"proxy_id={proxy_id}", "failed", "Proxy not found")
                raise HTTPException(status_code=404, detail="Proxy not found")

            updates, params = [], []
            detail_parts = []
            if req.name is not None:
                updates.append("name = ?")
                params.append(req.name.strip())
                detail_parts.append(f"name={req.name.strip()}")
            if req.url is not None:
                updates.append("url = ?")
                params.append(req.url.strip())
                detail_parts.append("url=***")
            if updates:
                updates.append("updated_at = ?")
                params.append(_now_iso())
                params.append(proxy_id)
                await db.execute(f"UPDATE proxies SET {', '.join(updates)} WHERE id = ?", params)
                await db.commit()

        detail = ", ".join(detail_parts) if detail_parts else f"proxy_id={proxy_id}"
        await log_operation(None, "update_proxy", None, detail, "success")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "update_proxy", None, f"proxy_id={proxy_id}", "failed", str(e))
        raise


@router.delete("/{proxy_id}")
async def delete_proxy(proxy_id: int):
    try:
        async with get_db() as db:
            row = await (await db.execute("SELECT id FROM proxies WHERE id = ?", (proxy_id,))).fetchone()
            if not row:
                await log_operation(None, "delete_proxy", None, f"proxy_id={proxy_id}", "failed", "Proxy not found")
                raise HTTPException(status_code=404, detail="Proxy not found")

            await db.execute("UPDATE teams SET proxy_id = NULL WHERE proxy_id = ?", (proxy_id,))
            await db.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
            await db.commit()

        await log_operation(None, "delete_proxy", None, f"proxy_id={proxy_id}", "success")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "delete_proxy", None, f"proxy_id={proxy_id}", "failed", str(e))
        raise


@router.post("/{proxy_id}/check")
async def check_proxy(proxy_id: int):
    try:
        async with get_db() as db:
            row = await (await db.execute("SELECT url FROM proxies WHERE id = ?", (proxy_id,))).fetchone()
        if not row:
            await log_operation(None, "check_proxy", None, f"proxy_id={proxy_id}", "failed", "Proxy not found")
            raise HTTPException(status_code=404, detail="Proxy not found")

        proxy_url = row["url"]
        now = _now_iso()
        try:
            def _do_check():
                # 与 ChatGPTClient 同一套 TLS 指纹，测的才是业务请求真实的可达性。
                return curl_requests.get(
                    "https://chatgpt.com",
                    impersonate=IMPERSONATE,
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=10,
                )

            resp = await asyncio.to_thread(_do_check)
            check_status = "ok" if resp.status_code < 500 else "error"
            check_error = None
        except Exception as check_exc:
            check_status = "error"
            check_error = str(check_exc)

        async with get_db() as db:
            await db.execute(
                "UPDATE proxies SET status = ?, last_check_at = ?, updated_at = ? WHERE id = ?",
                (check_status, now, now, proxy_id),
            )
            await db.commit()

        if check_status == "ok":
            await log_operation(None, "check_proxy", None, f"proxy_id={proxy_id}", "success")
        else:
            await log_operation(None, "check_proxy", None, f"proxy_id={proxy_id}", "failed", check_error or "Connection check failed")
        return {"status": check_status, "last_check_at": now}
    except HTTPException:
        raise
    except Exception as e:
        await log_operation(None, "check_proxy", None, f"proxy_id={proxy_id}", "failed", str(e))
        raise
