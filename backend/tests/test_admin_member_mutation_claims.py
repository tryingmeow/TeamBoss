"""Admin changes serialize with paid renewal and expiry removal on the same Team."""
import _isolation  # noqa: F401
import asyncio
import sys
from pathlib import Path
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fixtures import start_temp_db

from app.models import SetExpiryRequest
from app.routes import members
from app.services.team_locks import member_operation_claim


class AdminMemberMutationClaimsTest(unittest.TestCase):
    def setUp(self):
        start_temp_db(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.resolve = self.stack.enter_context(patch.object(members, '_resolve_member_identity', new=AsyncMock(return_value=('user-1', 'member@example.com'))))
        self.open = self.stack.enter_context(patch.object(members, 'find_open_redemption', new=AsyncMock(return_value=None)))
        self.client = self.stack.enter_context(patch.object(members, 'get_team_client', new=AsyncMock(return_value=object())))
        self.snapshot = self.stack.enter_context(patch.object(members, 'fetch_and_cache_members', new=AsyncMock(return_value={'members': [], 'pending_invites': [{'email': 'member@example.com'}]})))
        self.remote = self.stack.enter_context(patch.object(members, 'run_chatgpt_call', new=AsyncMock(return_value={})))
        self.write = self.stack.enter_context(patch.object(members, 'upsert_member_expiry', new=AsyncMock(return_value='future')))
        self.kicked = self.stack.enter_context(patch.object(members, 'mark_member_kicked', new=AsyncMock()))
        for name in ('log_operation', 'notify_member_event', '_refresh_members_after_mutation', 'update_cached_member_expiry'):
            self.stack.enter_context(patch.object(members, name, new=AsyncMock()))

    def call(self, operation, team='team-1'):
        if operation == 'set':
            return members.set_expiry(team, 'user-1', SetExpiryRequest(expires_in='30d', email='member@example.com'))
        if operation == 'remove':
            return members.remove_member(team, 'user-1')
        return members.revoke_invite(team, 'Member%40example.com')

    def test_existing_renewal_or_auto_kick_claim_blocks_admin_changes(self):
        async def scenario(operation):
            # A renewal has verified upstream presence but not committed its receipt,
            # or expiry removal has read the old expiry but not issued its DELETE.
            async with member_operation_claim('team-1', email='member@example.com', user_id='user-1', operation='self_service_renew') as held:
                self.assertTrue(held)
                with self.assertRaises(HTTPException) as raised:
                    await self.call(operation)
                self.assertEqual(raised.exception.status_code, 409)
            self.remote.assert_not_awaited()
            self.write.assert_not_awaited()
        for operation in ('set', 'remove', 'revoke'):
            with self.subTest(operation=operation):
                asyncio.run(scenario(operation))

    def test_admin_claim_prevents_renewal_during_local_write_or_remote_delete(self):
        async def protected(*args, **kwargs):
            async with member_operation_claim('team-1', email='member@example.com', operation='self_service_renew') as acquired:
                self.assertFalse(acquired)
            return {}
        self.write.side_effect = protected
        self.remote.side_effect = protected
        # supply client methods without contacting any upstream
        from types import SimpleNamespace
        self.client.return_value = SimpleNamespace(remove_member=object(), revoke_invite=object())
        for operation in ('set', 'remove', 'revoke'):
            with self.subTest(operation=operation):
                asyncio.run(self.call(operation))

    def test_same_email_in_another_team_is_independent(self):
        async def scenario():
            async with member_operation_claim('team-other', email='member@example.com', operation='self_service_renew') as held:
                self.assertTrue(held)
                await self.call('set')
        asyncio.run(scenario())
        self.write.assert_awaited_once()

    def test_unresolved_redemption_is_rechecked_under_admin_claim(self):
        async def barrier(*args, **kwargs):
            async with member_operation_claim('team-1', email='member@example.com', operation='self_service_renew') as acquired:
                self.assertFalse(acquired)
            return {'token_use_id': 7, 'result': 'uncertain'}
        self.open.side_effect = barrier
        self.stack.enter_context(patch.object(members, 'open_redemption_detail', return_value='unresolved'))
        for operation in ('set', 'remove', 'revoke'):
            with self.subTest(operation=operation):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(self.call(operation))
                self.assertEqual(raised.exception.status_code, 409)
        self.remote.assert_not_awaited()
        self.write.assert_not_awaited()

    def test_member_disappearing_before_claim_recheck_cannot_be_modified(self):
        for operation in ('set', 'remove'):
            self.resolve.side_effect = [('user-1', 'member@example.com'), HTTPException(status_code=404)]
            with self.subTest(operation=operation), self.assertRaises(HTTPException):
                asyncio.run(self.call(operation))
        self.remote.assert_not_awaited()
        self.write.assert_not_awaited()

    def test_accepted_invite_cannot_be_revoked_as_a_stale_pending_invite(self):
        self.snapshot.return_value = {'members': [{'id': 'user-1', 'email': 'member@example.com'}], 'pending_invites': []}
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(self.call('revoke'))
        self.assertEqual(raised.exception.status_code, 409)
        self.remote.assert_not_awaited()

    def test_ambiguous_remote_failure_does_not_mark_kicked_and_releases_claim(self):
        from types import SimpleNamespace
        self.client.return_value = SimpleNamespace(remove_member=object(), revoke_invite=object())
        self.remote.return_value = {'error': 'timeout'}
        async def scenario(operation):
            with self.assertRaises(HTTPException) as raised:
                await self.call(operation)
            self.assertEqual(raised.exception.status_code, 502)
            async with member_operation_claim('team-1', email='member@example.com', operation='self_service_renew') as acquired:
                self.assertTrue(acquired)
        for operation in ('remove', 'revoke'):
            with self.subTest(operation=operation):
                asyncio.run(scenario(operation))
        self.kicked.assert_not_awaited()
