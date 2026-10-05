import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..client_ip import RateLimiter as _RateLimiter, get_client_ip_info, normalize_ip, rate_limit_key
from ..database import get_db, log_operation
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



class _SharedIdentityCooldown:
    """共享身份（is_proxy_self_identity=True）专用的站点级递增冷却。

    这种身份下所有访客共享同一个计数 IP，不能用 _FailureTracker 的按 IP 锁定
    （会把管理员和所有访客一起锁死），但也不能完全不设防——只靠 10/min 的请求
    限流，匿名者一天仍能试 14400 次密码。折中方案：连续失败次数全站累计，前
    max_free_failures 次和现在一样只是普通密码错误，从下一次失败起，每次失败
    都把「下一次允许尝试」的时间往后顺延一段递增的冷却（下面 cooldown_seconds
    定义翻倍规律），冷却期内的请求在校验密码之前就直接拒绝。成功登录一次即清零。
    """

    def __init__(self, max_free_failures: int, initial_cooldown: int, max_cooldown: int):
        self.max_free_failures = max_free_failures
        self.initial_cooldown = initial_cooldown
        self.max_cooldown = max_cooldown
        self.count = 0
        self.last_failure_at = 0.0
        self.cooldown_until = 0.0

    def is_active(self, now: float) -> bool:
        return self.cooldown_until > now

    def record_failure(self, now: float) -> None:
        # 距上次失败超过 1 小时，视为新一轮，计数从头开始。按「上次失败」而不是
        # 「本轮第一次失败」计时：持续尝试的人每 15 分钟就会失败一次，计数永远不会
        # 被窗口清零，冷却也就一直停在封顶值上。
        if self.count and now - self.last_failure_at > 3600:
            self.count = 0
            self.cooldown_until = 0.0

        self.last_failure_at = now
        self.count += 1

        if self.count > self.max_free_failures:
            # 第 5 次失败对应第 1 级冷却（60s），第 6 次翻倍，以此类推，封顶 900s。
            level = self.count - self.max_free_failures - 1
            seconds = min(self.initial_cooldown * (2 ** level), self.max_cooldown)
            self.cooldown_until = now + seconds

    def record_success(self) -> None:
        self.count = 0
        self.last_failure_at = 0.0
        self.cooldown_until = 0.0


class _GlobalLoginBudget:
    """全站登录失败预算：限住「手里有大量地址」的攻击者能试的密码总数。

    按来源锁定（_FailureTracker）只挡得住单个来源，代理池/僵尸网络每换一个地址就多
    5 次。这里把所有来源的失败汇总成一份计数，超过 max_free_failures 后进入冷却：
    每次再失败都把「下一次允许校验密码」的时间往后推（initial_cooldown 起按超出量翻倍，
    封顶 max_cooldown），冷却期内的请求在校验密码之前就直接 429。于是能试的密码总数
    和攻击者有多少地址无关（每天几百次以内）。

    为了不让少数来源把管理员关在门外：
    * 每个来源在一轮里只有前 per_source_charge 次失败计入预算。单个来源（以及两个）
      永远凑不满预算，冷却不会因它们触发；之后它们的失败仍会在冷却期里重新计时，
      所以大量「已记满」的来源也换不来额外的尝试次数。
    * 一轮 = 上一次「计入预算的失败」之后 window_seconds 内。超过这段时间没有新的计入，
      计数和各来源的额度一起清零，冷却最多再持续 max_cooldown。
    * 冷却只拦「近期没用密码登录成功过」的来源；登录成功过的来源（见
      admin_login_trusted_sources）只受按来源锁定。调用方负责这个判断。

    登录成功不清零这份计数——否则管理员每登录一次就送攻击者一批免费尝试。
    """

    def __init__(
        self,
        max_free_failures: int,
        initial_cooldown: int,
        max_cooldown: int,
        *,
        per_source_charge: int = 5,
        window_seconds: int = 3600,
        max_tracked_sources: int = 10000,
    ):
        self.max_free_failures = max_free_failures
        self.initial_cooldown = initial_cooldown
        self.max_cooldown = max_cooldown
        self.per_source_charge = per_source_charge
        self.window_seconds = window_seconds
        self.max_tracked_sources = max_tracked_sources
        self.count = 0
        self.last_charged_at = 0.0
        self.cooldown_until = 0.0
        self.charged_by_source: dict[str, int] = {}

    def is_active(self, now: float) -> bool:
        return self.cooldown_until > now

    def record_failure(self, source: str, now: float) -> None:
        if self.count and now - self.last_charged_at > self.window_seconds:
            self.count = 0
            self.charged_by_source.clear()

        charged = self.charged_by_source.get(source, 0)
        if charged < self.per_source_charge:
            if source not in self.charged_by_source and len(self.charged_by_source) >= self.max_tracked_sources:
                # 内存兜底。被挤掉的来源下次还能再计入几次，只会让预算更快耗尽，不会多给尝试。
                self.charged_by_source.pop(next(iter(self.charged_by_source)))
            self.charged_by_source[source] = charged + 1
            self.count += 1
            self.last_charged_at = now

        excess = self.count - self.max_free_failures
        if excess > 0:
            seconds = min(self.initial_cooldown * (2 ** min(excess - 1, 16)), self.max_cooldown)
            self.cooldown_until = max(self.cooldown_until, now + seconds)


# 登录限流器：10 req/min
# 取值理由：正常用户手动登录频率不会超过 10/min（即 6 秒一次），
# 但爆破会以 100+ req/s 速率尝试，故 10/min 对正常用户无感、对爆破有效。
_limiter_login = _RateLimiter(max_requests=10, window_seconds=60)

# 失败次数锁定：5 次失败后锁定 15 分钟
# 取值理由：正常用户打错密码最多 2-3 次即会放弃重试（或检查大小写），
# 5 次失败是合理的容错；15 分钟锁定让爆破陷入困境（每 IP 只能每 15 min 尝试一轮），
# 同时对正常用户来说 15 分钟后自动解锁是可接受的。
_failure_tracker = _FailureTracker(max_failures=5, lockout_seconds=900)

# 共享身份（见下方 login 里的 is_proxy_self_identity 分支）专用的递增冷却：
# 前 4 次失败不受影响，第 5 次起触发 60s 冷却，此后每次再失败翻倍，封顶 900s，
# 成功登录一次清零。取值和上面的按 IP 锁定对齐（同样是 5 次容错、同样 15 分钟
# 封顶），只是把「锁定一个身份」换成了「让下一次尝试变慢」。
_shared_identity_cooldown = _SharedIdentityCooldown(
    max_free_failures=4, initial_cooldown=60, max_cooldown=900
)

# 全站登录失败预算（见 _GlobalLoginBudget）：每个来源一轮最多计 5 次，全站计满 10 次后，
# 每次再失败都触发冷却（60s 起按超出量翻倍，封顶 900s）。始终生效，叠加在按来源锁定之上，
# 但只拦没用密码登录成功过的来源。浏览器里已保存的 API Key，以及带 X-API-Key / Bearer
# 的请求走 require_admin，不经过这里，照常可用。
_global_login_cooldown = _GlobalLoginBudget(
    max_free_failures=10, initial_cooldown=60, max_cooldown=900
)

# 「登录成功过的来源」名单：最近 90 天内用密码登录成功过的来源（IPv4 地址 / IPv6 /64）
# 不受全站冷却影响。只有知道密码的人能往里加，最多保留最近的 50 个。
TRUSTED_LOGIN_SOURCE_TTL_SECONDS = 90 * 24 * 3600
TRUSTED_LOGIN_SOURCE_MAX = 50


def _can_be_trusted_source(client_ip: str, is_proxy_self_identity: bool) -> bool:
    # 共享身份下所有访客是同一个来源，信任它等于让所有人绕过全站冷却。
    return not is_proxy_self_identity and bool(normalize_ip(client_ip))


async def _is_trusted_login_source(source: str, now: float) -> bool:
    try:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT 1 FROM admin_login_trusted_sources WHERE source = ? AND last_success_at > ?",
                (source, now - TRUSTED_LOGIN_SOURCE_TTL_SECONDS),
            )
            return await cursor.fetchone() is not None
    except Exception:
        # 查不到就按陌生来源处理：最多是管理员在攻击期间要多等一会儿，不会放宽限制。
        logger.exception("failed to read trusted admin login sources")
        return False


async def _remember_trusted_login_source(source: str, now: float) -> None:
    try:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO admin_login_trusted_sources (source, first_success_at, last_success_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET last_success_at = excluded.last_success_at""",
                (source, now, now),
            )
            await db.execute(
                "DELETE FROM admin_login_trusted_sources WHERE last_success_at <= ?",
                (now - TRUSTED_LOGIN_SOURCE_TTL_SECONDS,),
            )
            await db.execute(
                """DELETE FROM admin_login_trusted_sources WHERE source NOT IN (
                       SELECT source FROM admin_login_trusted_sources
                       ORDER BY last_success_at DESC LIMIT ?
                   )""",
                (TRUSTED_LOGIN_SOURCE_MAX,),
            )
            await db.commit()
    except Exception:
        # 名单只是便利；写不进去不能让已经验证通过的登录失败。
        logger.exception("failed to record trusted admin login source")


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


def _login_attempt_lock() -> asyncio.Lock:
    """「检查冷却/锁定 → 校验密码 → 记账」这一段的串行锁（按事件循环惰性创建）。

    不串行的话，一批并发请求会全部先通过检查、再一起去校验密码——失败还没记上，
    冷却和锁定就已经被并发绕过了。锁里只有内存判断和密码校验，拒绝请求的日志在锁外
    写，冷却期间大量被拒的请求不会排队拖慢管理员。
    """
    global _login_lock, _login_lock_loop
    loop = asyncio.get_running_loop()
    if _login_lock is None or _login_lock_loop is not loop:
        _login_lock = asyncio.Lock()
        _login_lock_loop = loop
    return _login_lock


_login_lock: asyncio.Lock | None = None
_login_lock_loop: asyncio.AbstractEventLoop | None = None


@router.post("/login", response_model=AdminLoginResponse)
async def login(req: AdminLoginRequest, request: Request):
    client_ip, is_proxy_self_identity = get_client_ip_info(request)
    # 限流和失败锁定按这个键计数（IPv6 按 /64 归并），日志里仍记录原始 IP。
    identity = rate_limit_key(client_ip)

    # 速率限制检查（不受下面"共享身份不锁定"规则影响：请求频率限制始终生效）
    if not _limiter_login.is_allowed(identity):
        await log_operation(None, "admin_login", None, f"ip={client_ip}", "failed", "Rate limit exceeded")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
        )

    # 用密码登录成功过的来源不受全站冷却影响（只受下面的按来源锁定），陌生人再怎么
    # 刷失败，也关不住管理员常用的网络。名单在数据库里，所以在锁外查。
    can_be_trusted = _can_be_trusted_source(client_ip, is_proxy_self_identity)
    trusted_source = can_be_trusted and await _is_trusted_login_source(identity, time.time())

    rejection: tuple[str, str] | None = None
    password_ok = False
    is_locked = False
    remaining = 0
    async with _login_attempt_lock():
        now = time.time()
        # 全站失败预算：冷却期内，除了登录成功过的来源，谁来都不校验密码。
        if _global_login_cooldown.is_active(now) and not trusted_source:
            rejection = ("Global login cooldown active", "登录失败次数过多，请稍后再试")
        # 失败锁定检查。is_proxy_self_identity=True 说明这个 IP 其实是可信代理自己的
        # 地址（部署把每个访客都坍缩成了同一个身份，见 client_ip.get_client_ip_info），
        # 按 IP 锁定会把全站所有访客一起锁掉、管理员也不例外，所以这种情况不走
        # _failure_tracker，改用 _shared_identity_cooldown：全站共享一份计数，冷却期内
        # 直接拒绝、不查密码，冷却时长随连续失败次数递增（见该类注释）。真实的每访客
        # 身份（正确配置下的正常情况）走 _failure_tracker。
        elif is_proxy_self_identity:
            if _shared_identity_cooldown.is_active(now):
                rejection = ("Shared identity cooldown active", "登录失败次数过多，请稍后再试")
        elif _failure_tracker.is_locked(identity):
            rejection = ("Account locked due to multiple failures", "登录失败次数过多，请 15 分钟后再试")

        if rejection is None:
            password_ok = await verify_admin_password(req.password)
            if not password_ok:
                _global_login_cooldown.record_failure(identity, time.time())
                if is_proxy_self_identity:
                    _shared_identity_cooldown.record_failure(time.time())
                else:
                    is_locked, remaining = _failure_tracker.record_failure(identity)
            # 密码正确，重置失败计数（全站预算不在这里清零，见 _global_login_cooldown）
            elif is_proxy_self_identity:
                _shared_identity_cooldown.record_success()
            else:
                _failure_tracker.record_success(identity)

    if rejection is not None:
        reason, detail = rejection
        await log_operation(None, "admin_login", None, f"ip={client_ip}", "failed", reason)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)

    if not password_ok:
        if is_proxy_self_identity:
            await log_operation(
                None,
                "admin_login",
                None,
                f"ip={client_ip}, shared_identity_failures={_shared_identity_cooldown.count}",
                "failed",
                "Invalid password",
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码错误")

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

    api_key = await get_admin_api_key()
    if not api_key:
        await log_operation(None, "admin_login", None, f"ip={client_ip}", "failed", "API Key not initialized")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API Key 未初始化")

    if can_be_trusted:
        await _remember_trusted_login_source(identity, time.time())
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
