"""Stand-in requests for the client-IP and admin-login tests.

Not a test module (the name does not match test*.py), so neither pytest nor
unittest discovery collects it. Imports no app code.
"""


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class FakeRequest:
    """The two things app.client_ip reads: request.client.host and request.headers."""

    def __init__(self, peer: str, headers: dict | None = None):
        self.client = _FakeClient(peer)
        self.headers = headers or {}


def nginx_request(client_ip: str) -> FakeRequest:
    """What production Nginx forwards to 127.0.0.1:18087 for a visitor."""
    return FakeRequest(
        "127.0.0.1",
        {
            "X-Real-IP": client_ip,
            "X-Forwarded-For": client_ip,
            "X-Forwarded-Proto": "https",
        },
    )
