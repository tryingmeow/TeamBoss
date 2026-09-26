import hashlib
import re
from datetime import datetime, timezone

import jwt
import requests

# 上游异常的字符串形式有可能把请求头原样带出来（例如 requests 在 header 值非法时
# 会把 `authorization: Bearer <token>` 整段拼进 InvalidHeader 的消息里）。这些文本
# 会流向操作日志、Telegram 告警，甚至匿名接口的错误响应，所以在源头就抹掉。
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]{8,}=*"),
    re.compile(r"(?i)(__Secure-next-auth\.session-token\s*=\s*)[^\s;,'\"]{8,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,'\"]{8,}"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\s,'\"]{8,}"),
    # 裸 JWT（三段 base64url）。session token 与 access token 都是这个形状。
    re.compile(r"\beyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}"),
    # URL 里带的凭据，例如代理地址 http://user:pass@host:port。urllib3 的
    # LocationParseError 会把整个 URL（含 userinfo）拼进异常文本，代理地址可能
    # 就此原样落进 operation_logs / Telegram 告警。scheme 和 host 保留可读，
    # 只抹 "@" 前的 userinfo（用零宽断言让 "@" 本身不被吃掉，留在掩码后面）。
    re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^\s/@'\"]+(?=@)"),
)


def mask_secrets(text: str) -> str:
    """把文本里的 token / cookie / Authorization 值替换成 ``***``。"""
    if not text:
        return text
    masked = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            masked = pattern.sub(lambda m: f"{m.group(1)}***", masked)
        else:
            masked = pattern.sub("***", masked)
    return masked


# ── /api/auth/session 刷新诊断 ───────────────────────────────────────────────
# 刷新时只读 JSON body，不看 Set-Cookie。下面这些只产出哈希、长度、布尔值和键名/
# cookie 名，用来比对"JSON 里的 sessionToken"和"Set-Cookie 里真正轮换出来的会话
# cookie"是否一致；任何 token、cookie 值或身份信息（邮箱、用户 id）都不进结果。
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
# 新网页登录流程签发的 access token 带这个 claim；只记有没有，不记值。
LOGIN_FINALIZER_CLAIM = "chatgpt_login_finalizer_auth_session_id"
# refresh_token() 在原有返回之外追加的两个字段。
REFRESH_DIAGNOSTICS_KEY = "refresh_diagnostics"
SET_COOKIE_SESSION_KEY = "set_cookie_session_token"

_MAX_AGE_RE = re.compile(r"(?i);\s*max-age\s*=\s*(-?\d+)")
_MAX_LOGGED_COOKIES = 30


def _fingerprint(value) -> tuple[int | None, str | None]:
    """(长度, sha256 前 8 位)。高熵 token 的 8 位哈希足够区分，且不可逆。"""
    if not isinstance(value, str) or not value:
        return None, None
    return len(value), hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _epoch_iso(value) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _has_claim(value, name: str, depth: int = 0) -> bool:
    # claim 可能在顶层，也可能嵌在 "https://api.openai.com/auth" 这类命名空间里。
    if depth > 4:
        return False
    if isinstance(value, dict):
        return name in value or any(_has_claim(v, name, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_has_claim(v, name, depth + 1) for v in value)
    return False


def access_token_summary(access_token) -> dict:
    """access token 的 iat/exp（ISO）和是否带新登录流程 claim。解不开时全为 None。"""
    if not isinstance(access_token, str) or not access_token:
        return {"iat": None, "exp": None, "finalizer_claim": None}
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
    except Exception:
        return {"iat": None, "exp": None, "finalizer_claim": None}
    if not isinstance(claims, dict):
        return {"iat": None, "exp": None, "finalizer_claim": None}
    return {
        "iat": _epoch_iso(claims.get("iat")),
        "exp": _epoch_iso(claims.get("exp")),
        "finalizer_claim": _has_claim(claims, LOGIN_FINALIZER_CLAIM),
    }


def parse_set_cookie(header) -> tuple[str, str, bool] | None:
    """一条 Set-Cookie 头 -> (名字, 值, 是否为删除指令)。空值或 Max-Age<=0 视为删除。"""
    first, _, attrs = str(header).partition(";")
    name, sep, value = first.partition("=")
    name = name.strip()
    if not sep or not name:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    max_age = _MAX_AGE_RE.search(";" + attrs)
    cleared = not value or (max_age is not None and int(max_age.group(1)) <= 0)
    return name, value, cleared


def response_set_cookies(resp) -> list[tuple[str, str, bool]]:
    """按到达顺序取响应里的每条 Set-Cookie。

    ``resp.headers`` 会把多条 Set-Cookie 用逗号并成一条，而 Expires 里本身就有
    逗号，没法可靠拆开；所以读 urllib3 原始头的 getlist。拿不到原始头时退回
    cookie jar（名字和值都有，只是看不到 Max-Age）。
    """
    headers = getattr(getattr(resp, "raw", None), "headers", None)
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        try:
            parsed = (parse_set_cookie(h) for h in getlist("Set-Cookie"))
            return [item for item in parsed if item]
        except Exception:
            pass
    try:
        jar = getattr(resp, "cookies", None) or []
        return [(c.name, c.value or "", not c.value) for c in jar]
    except Exception:
        return []


def reassemble_session_cookie(
    cookies: list[tuple[str, str, bool]],
) -> tuple[str | None, dict]:
    """从 Set-Cookie 里拼出会话 cookie。

    NextAuth 在 cookie 超过单条上限时拆成 ``<name>.0``、``<name>.1`` …，按序号拼回；
    同时会下发删除旧分片 / 旧整条的指令（空值或 Max-Age=0），这些不参与拼接。
    """
    single = None
    chunks: dict[int, str] = {}
    cleared = False
    prefix = SESSION_COOKIE_NAME + "."
    for name, value, is_cleared in cookies:
        if name == SESSION_COOKIE_NAME:
            index = None
        elif name.startswith(prefix) and name[len(prefix):].isdigit():
            index = int(name[len(prefix):])
        else:
            continue
        if is_cleared:
            cleared = True
        elif index is None:
            single = value
        else:
            chunks[index] = value

    if chunks:
        order = sorted(chunks)
        token = "".join(chunks[i] for i in order)
        source = "chunks"
        contiguous = order == list(range(len(order)))
    elif single:
        token, source, contiguous = single, "single", None
    else:
        token, source, contiguous = None, "none", None
    return token, {
        "source": source,
        "chunks": len(chunks),
        "chunks_contiguous": contiguous,
        "cleared": cleared,
    }


def summarize_session_refresh(
    *,
    http_status: int | None,
    body,
    cookies: list[tuple[str, str, bool]],
    sent_session_token: str | None,
    exc: Exception | None = None,
) -> tuple[dict, str | None]:
    """一次 /api/auth/session 调用的诊断摘要，以及拼好的 Set-Cookie 会话 token。

    摘要里只有状态码、键名、cookie 名、长度、sha256 前 8 位和布尔值。第二个返回值
    是原始 token，只能交给调用方，绝不能写进日志。
    """
    is_dict = isinstance(body, dict)
    json_session = body.get("sessionToken") if is_dict else None
    json_session = json_session if isinstance(json_session, str) and json_session else None
    access = body.get("accessToken") if is_dict else None
    access = access if isinstance(access, str) and access else None
    error_value = body.get("error") if is_dict else None
    sent = sent_session_token if isinstance(sent_session_token, str) and sent_session_token else None

    sc_session, sc_meta = reassemble_session_cookie(cookies)
    access_info = access_token_summary(access)
    sc_len, sc_sha = _fingerprint(sc_session)
    json_len, json_sha = _fingerprint(json_session)
    sent_len, sent_sha = _fingerprint(sent)

    diag = {
        "http_status": http_status,
        "exc_type": type(exc).__name__ if exc is not None else None,
        "json_keys": sorted(str(key) for key in body) if is_dict else None,
        "error": mask_secrets(str(error_value))[:120] if error_value is not None else None,
        "access_present": access is not None,
        "access_iat": access_info["iat"],
        "access_exp": access_info["exp"],
        "access_finalizer_claim": access_info["finalizer_claim"],
        "set_cookie_names": [name[:64] for name, _, _ in cookies[:_MAX_LOGGED_COOKIES]],
        "sc_session_source": sc_meta["source"],
        "sc_session_chunks": sc_meta["chunks"],
        "sc_session_chunks_contiguous": sc_meta["chunks_contiguous"],
        "sc_session_cleared": sc_meta["cleared"],
        "sc_session_len": sc_len,
        "sc_session_sha8": sc_sha,
        "json_session_len": json_len,
        "json_session_sha8": json_sha,
        "sent_session_len": sent_len,
        "sent_session_sha8": sent_sha,
        "sc_eq_json": (sc_session == json_session) if sc_session and json_session else None,
        "sc_eq_sent": (sc_session == sent) if sc_session and sent else None,
        "json_eq_sent": (json_session == sent) if json_session and sent else None,
    }
    return diag, sc_session


def _json_body_or_none(resp):
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return None


class ChatGPTClient:
    def __init__(self, access_token: str, team_id: str, device_id: str, proxy_url: str | None = None):
        self.access_token = access_token
        self.team_id = team_id
        self.device_id = device_id
        self.base_url = "https://chatgpt.com"
        self.session = requests.Session()
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.session.headers.update({
            "authorization": f"Bearer {access_token}",
            "chatgpt-account-id": team_id,
            "content-type": "application/json",
            "oai-device-id": device_id,
            "oai-language": "en-US",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/admin/members",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        })

    @staticmethod
    def _error(exc: Exception) -> dict:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        result = {"error": mask_secrets(str(exc))}
        if status_code is not None:
            result["status_code"] = status_code
        return result

    def update_access_token(self, access_token: str) -> None:
        self.access_token = access_token
        self.session.headers.update({"authorization": f"Bearer {access_token}"})

    @staticmethod
    def _attach_refresh_evidence(
        result: dict,
        resp,
        body,
        session_token: str,
        exc: Exception | None = None,
    ) -> None:
        """给刷新结果追加诊断摘要和 Set-Cookie 会话 token，原有字段一个不动。

        诊断只是旁路观测，自身出任何错都不能让一次刷新变成失败。
        """
        try:
            diag, sc_session = summarize_session_refresh(
                http_status=getattr(resp, "status_code", None),
                body=body,
                cookies=response_set_cookies(resp) if resp is not None else [],
                sent_session_token=session_token,
                exc=exc,
            )
        except Exception as diag_exc:
            diag, sc_session = {"diag_error": type(diag_exc).__name__}, None
        result[REFRESH_DIAGNOSTICS_KEY] = diag
        result[SET_COOKIE_SESSION_KEY] = sc_session

    @staticmethod
    def refresh_token(session_token: str, proxy_url: str | None = None) -> dict:
        """GET /api/auth/session，返回上游 JSON body（出错时为 ``_error`` 的形状）。

        在此之外追加两个字段：``refresh_diagnostics``（只含哈希/长度/布尔/键名的诊断
        摘要）和 ``set_cookie_session_token``（从 Set-Cookie 拼出的会话 token，可能为
        None）。后者目前只用于观测，调用方持久化的仍是 JSON 里的 ``sessionToken``。
        """
        resp = None
        try:
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            resp = requests.get(
                "https://chatgpt.com/api/auth/session",
                headers={
                    "cookie": f"__Secure-next-auth.session-token={session_token}",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                },
                proxies=proxies,
                timeout=60
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            result = ChatGPTClient._error(e)
            ChatGPTClient._attach_refresh_evidence(
                result, resp, _json_body_or_none(resp), session_token, exc=e
            )
            return result

        if not isinstance(body, dict):
            return body
        # 诊断要看上游原样的键，所以先于下面补 status_code 计算。
        evidence: dict = {}
        ChatGPTClient._attach_refresh_evidence(evidence, resp, body, session_token)
        # NextAuth 刷新失败时照样回 200，只在 body 里带 error（例如
        # RefreshAccessTokenError）。超时、断网的错误结果里没有 status_code，
        # 不带上这个 200 调用方就分不清"上游明确答复拿不到 token"和"网络失败"。
        if "error" in body:
            body["status_code"] = resp.status_code
        body.update(evidence)
        return body

    def get_account_info(self) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/accounts/check/v4-2023-04-27",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def get_subscription(self) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/subscriptions?account_id={self.team_id}",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def get_remaining_balance(self) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/remaining_balance",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def get_members(self, offset: int = 0, limit: int = 25) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/users?offset={offset}&limit={limit}",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def invite_member(self, email: str, seat_type: str = "default", role: str = "standard-user") -> dict:
        try:
            resp = self.session.post(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/invites",
                json={
                    "email_addresses": [email],
                    "role": role,
                    "seat_type": seat_type,
                    "resend_emails": True
                },
                timeout=60
            )
            resp.raise_for_status()
            # 任何 2xx 都代表服务端已经接受了变更。即使响应体为空或 JSON
            # 损坏，也不能把它误判成失败后换另一个 Team 重试。
            try:
                result = resp.json()
            except ValueError:
                result = {}
            if not isinstance(result, dict):
                result = {"response": result}
            result["_mutation_status"] = "confirmed"
            return result
        except Exception as e:
            result = self._error(e)
            status_code = result.get("status_code")
            # 明确的客户端拒绝才允许释放兑换码。超时、断线、5xx，以及可能
            # 在服务端已执行的 408/409/425/429，都属于结果不确定。
            is_clear_rejection = (
                isinstance(status_code, int)
                and 400 <= status_code < 500
                and status_code not in {408, 409, 425, 429}
            )
            result["_mutation_status"] = "rejected" if is_clear_rejection else "uncertain"
            return result

    def get_pending_invites(self, offset: int = 0, limit: int = 25) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/invites?offset={offset}&limit={limit}",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def revoke_invite(self, email: str) -> dict:
        try:
            resp = self.session.delete(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/invites",
                json={"email_address": email},
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def change_seat_type(self, user_id: str, seat_type: str) -> dict:
        try:
            resp = self.session.patch(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/users/{user_id}",
                json={"seat_type": seat_type},
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def remove_member(self, user_id: str) -> dict:
        try:
            resp = self.session.delete(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/users/{user_id}",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def get_seat_type_counts(self) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/users/seat_type_counts",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def get_invoices(self, limit: int = 6) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/invoices?limit={limit}&account_id={self.team_id}",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def get_payment_methods(self) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/payments/payment_methods?account_id={self.team_id}",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def get_pricing_config(self, country_code: str) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/checkout_pricing_config/configs/{country_code}",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def get_billing_pricing_config(self, billing_currency: str) -> dict:
        return self.get_pricing_config(billing_currency)

    def get_workspace_settings(self) -> dict:
        try:
            resp = self.session.get(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/settings",
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)

    def set_default_seat_type(self, seat_type: str) -> dict:
        try:
            resp = self.session.post(
                f"{self.base_url}/backend-api/accounts/{self.team_id}/settings/default_seat_type",
                json={"value": seat_type},
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return self._error(e)
