import re

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
    def refresh_token(session_token: str, proxy_url: str | None = None) -> dict:
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
            return resp.json()
        except Exception as e:
            return ChatGPTClient._error(e)

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
