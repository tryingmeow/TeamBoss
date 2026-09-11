import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..client_ip import RateLimiter as _RateLimiter, get_client_ip
from ..database import log_operation
from ..security import (
    change_admin_password,
    get_admin_api_key,
    require_admin,
    rotate_admin_api_key,
    verify_admin_password,
)

logger = logging.getLogger(__name__)



class _FailureTracker:
    """跟踪每个 IP 的登录失败次数，超过限制后锁定一段时间。"""

    def __init__(self, max_failures: int, lockout_seconds: int):
        """
        max_failures: 触发锁定的连续失败次数
        lockout_seconds: 锁定持续时间（秒）
        """
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        # 登录是匿名接口，失败记录同样要有上界，否则大量来源 IP 会把 dict 撑爆。
        self.max_tracked_ips = 10000
        self.failures = {}  # {ip: {'count': n, 'first_failure_at': ts, 'locked_until': ts}}

    def is_locked(self, ip: str) -> bool:
        """检查该 IP 是否被锁定。"""
        if ip not in self.failures:
            return False
        now = time.time()
        locked_until = self.failures[ip].get("locked_until", 0)
        return locked_until > now

    def _sweep(self, now: float) -> None:
        """清掉既没在锁定中、失败窗口也已过期的条目，并对总量兜底。"""
        for key in list(self.failures):
            entry = self.failures[key]
            if entry.get("locked_until", 0) > now:
                continue
            if now - entry.get("first_failure_at", now) > 3600:
                del self.failures[key]
        overflow = len(self.failures) - self.max_tracked_ips
        if overflow > 0:
            # 和上面那段循环一样：仍在锁定期内的条目不参与淘汰，否则溢出清理会
            # 把刚触发锁定的 IP（first_failure_at 天然最老）反而清掉，等于免费解锁。
            evictable = sorted(
                (k for k, entry in self.failures.items() if entry.get("locked_until", 0) <= now),
                key=lambda k: self.failures[k].get("first_failure_at", 0),
            )[:overflow]
            for key in evictable:
                del self.failures[key]

    def record_failure(self, ip: str) -> tuple[bool, int]:
        """记录失败，返回 (is_now_locked, remaining_attempts)。"""
        now = time.time()
        self._sweep(now)

        if ip not in self.failures:
            self.failures[ip] = {
                "count": 1,
                "first_failure_at": now,
                "locked_until": 0,
            }
        else:
            # 如果上次的窗口已过期，重置计数
            first_failure = self.failures[ip].get("first_failure_at", now)
            if now - first_failure > 3600:  # 1小时窗口
                self.failures[ip] = {
                    "count": 1,
                    "first_failure_at": now,
                    "locked_until": 0,
                }
            else:
                self.failures[ip]["count"] += 1

            # 达到失败限制后锁定
            if self.failures[ip]["count"] >= self.max_failures:
                self.failures[ip]["locked_until"] = now + self.lockout_seconds

        remaining = max(0, self.max_failures - self.failures[ip]["count"])
        is_locked = self.is_locked(ip)
        return is_locked, remaining

    def record_success(self, ip: str) -> None:
        """成功登录后重置该 IP 的失败计数。"""
        if ip in self.failures:
            del self.failures[ip]



# 登录限流器：10 req/min
# 取值理由：正常用户手动登录频率不会超过 10/min（即 6 秒一次），
# 但爆破会以 100+ req/s 速率尝试，故 10/min 对正常用户无感、对爆破有效。
_limiter_login = _RateLimiter(max_requests=10, window_seconds=60)

# 失败次数锁定：5 次失败后锁定 15 分钟
# 取值理由：正常用户打错密码最多 2-3 次即会放弃重试（或检查大小写），
# 5 次失败是合理的容错；15 分钟锁定让爆破陷入困境（每 IP 只能每 15 min 尝试一轮），
# 同时对正常用户来说 15 分钟后自动解锁是可接受的。
_failure_tracker = _FailureTracker(max_failures=5, lockout_seconds=900)


router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminLoginRequest(BaseModel):
    password: str


class AdminLoginResponse(BaseModel):
    status: str
    api_key: str


class AdminAccountResponse(BaseModel):
    api_key: str
    api_key_prefix: str


class ChangeAdminPasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class RotateAdminApiKeyResponse(BaseModel):
    status: str
    api_key: str
    api_key_prefix: str


def _api_key_prefix(api_key: str) -> str:
    return f"{api_key[:8]}...{api_key[-4:]}"


@router.post("/login", response_model=AdminLoginResponse)
async def login(req: AdminLoginRequest, request: Request):
    client_ip = get_client_ip(request)

    # 速率限制检查
    if not _limiter_login.is_allowed(client_ip):
        await log_operation(None, "admin_login", None, f"ip={client_ip}", "failed", "Rate limit exceeded")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
        )

    # 失败锁定检查
    if _failure_tracker.is_locked(client_ip):
        await log_operation(None, "admin_login", None, f"ip={client_ip}", "failed", "Account locked due to multiple failures")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录失败次数过多，请 15 分钟后再试",
        )

    # 校验密码
    if not await verify_admin_password(req.password):
        is_locked, remaining = _failure_tracker.record_failure(client_ip)
        await log_operation(
            None,
            "admin_login",
            None,
            f"ip={client_ip}, failures_remaining={remaining}",
            "failed",
            "Invalid password",
        )
        if is_locked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="登录失败次数过多，请 15 分钟后再试",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码错误")

    # 密码正确，重置失败计数
    _failure_tracker.record_success(client_ip)

    api_key = await get_admin_api_key()
    if not api_key:
        await log_operation(None, "admin_login", None, f"ip={client_ip}", "failed", "API Key not initialized")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API Key 未初始化")

    await log_operation(None, "admin_login", None, f"ip={client_ip}", "success")
    return {"status": "ok", "api_key": api_key}


@router.get("/account", response_model=AdminAccountResponse, dependencies=[Depends(require_admin)])
async def get_admin_account():
    api_key = await get_admin_api_key()
    if not api_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API Key 未初始化")
    return {"api_key": api_key, "api_key_prefix": _api_key_prefix(api_key)}


@router.patch("/password", dependencies=[Depends(require_admin)])
async def update_admin_password(req: ChangeAdminPasswordRequest):
    try:
        await change_admin_password(req.current_password, req.new_password)
        await log_operation(None, "change_admin_password", None, None, "success")
        return {"status": "ok"}
    except Exception as e:
        await log_operation(None, "change_admin_password", None, None, "failed", str(e))
        raise


@router.post(
    "/api-key/rotate",
    response_model=RotateAdminApiKeyResponse,
    dependencies=[Depends(require_admin)],
)
async def rotate_api_key():
    try:
        api_key = await rotate_admin_api_key()
        try:
            await log_operation(
                None,
                "rotate_admin_api_key",
                None,
                f"prefix={_api_key_prefix(api_key)}",
                "success",
            )
        except Exception:
            # The new key is already committed.  An audit-log outage must not
            # turn the response into a 500 and hide the only copy from admin.
            logger.exception("failed to write audit log after API key rotation")
        return {"status": "ok", "api_key": api_key, "api_key_prefix": _api_key_prefix(api_key)}
    except Exception as e:
        try:
            await log_operation(None, "rotate_admin_api_key", None, None, "failed", str(e))
        except Exception:
            logger.exception("failed to write audit log for API key rotation failure")
        raise
