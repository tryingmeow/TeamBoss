"""Stand-ins shared by the invite tests: test_admin_invite, test_invite_classification,
test_gpt_batch_invite and test_open_redemption_guards.

Not a test module (the name does not match test*.py), so neither pytest nor
unittest discovery collects it. The temp-database plumbing lives in _fixtures.py.
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

from _fixtures import direct_call

from app.chatgpt_client import ChatGPTClient
from app.routes import members


class InviteClient:
    """Upstream client stand-in: records every invite as (email, seat_type), so a test
    can assert whether an invite went out, and answers each one as confirmed."""

    def __init__(self):
        self.invites = []

    def invite_member(self, email, seat_type="default"):
        self.invites.append((email, seat_type))
        return {"account_invites": [{"email_address": email}], "errored_emails": [],
                "_mutation_status": "confirmed"}


def ok_response(body):
    """A 2xx response stand-in. The body shape follows the fields the ChatGPT admin page
    reads: ``account_invites`` / ``errored_emails[{email_address, error}]`` /
    ``already_member_emails``."""
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def client_returning(body):
    """A real ChatGPTClient whose invite POST answers 2xx with ``body``."""
    client = ChatGPTClient("access", "team-1", "device-1")
    client.session.post = Mock(return_value=ok_response(body))
    return client


def run_admin_invite(team_id, request, *, client, fetch, seat_check=None, notify=None):
    """Run the real POST /api/teams/{team_id}/members/invite route once
    (members.invite_member; the web page and Telegram /invite share it).

    Stand-ins: the upstream client, the live member list ``fetch``, the seat check
    ``seat_check``, the Telegram notice ``notify``, the member watch and the seat
    reservation. The locks, member-operation claims, open-redemption check and the
    expiry merge are real. Returns the route's result or raises its HTTPException.
    """
    patches = [
        patch.object(members, "get_team_client", new=AsyncMock(return_value=client)),
        patch.object(members, "fetch_and_cache_members", new=fetch),
        patch.object(members, "_ensure_default_seat_available",
                     new=seat_check if seat_check is not None else AsyncMock()),
        patch.object(members, "run_chatgpt_call", new=direct_call),
        patch.object(members, "add_member_watch", new=AsyncMock()),
        patch.object(members, "reserve_default_seat", new=AsyncMock()),
        patch.object(members, "notify_member_event", new=notify if notify is not None else AsyncMock()),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return asyncio.run(members.invite_member(team_id, request))
