"""Real administrator binding reads retain old formats and native permission gates.

A local synthetic persona establishes readiness. The listener, identity records,
ingress registrations and mode controls use their actual persistent owners.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.daily_cognition.test_initial_persona_host import persona
from tests.daily_cognition.test_reasoning import responses
from .communication_live_support import ready_application
from .http_support import request


class ManagementBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_binding_read_preserves_state_dream_and_revocation(self):
        with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
            app, http = await ready_application(self, Path(directory) / 'instance', provider_port,
                communication_format=False)
            try:
                self.assertFalse(app.identity.communication_format)
                session = await self.administrator(app)
                async def call(path, payload):
                    return await request(18180, path, payload, '', origin='http://127.0.0.1:18180', session=session)
                status, blocked_connection = await call('/api/connections/hosts/list', {'after': ''})
                self.assertNotEqual(status, 200)
                self.assertEqual(blocked_connection['error']['reason'], 'NOT_READY')
                status, bindings = await call('/api/administration/bindings', {'after': ''})
                self.assertEqual(status, 200, bindings)
                self.assertEqual(bindings['data'], {'items': [{'host_id': 'host', 'entry_id': 'entry'}], 'after': 'entry'})
                selected = {'host_id': 'host', 'entry_id': 'entry', 'input': {}}
                status, current = await call('/api/administration/state', selected)
                self.assertEqual(status, 200, current)
                status, dream = await call('/api/dream/status', {})
                self.assertEqual(status, 200, dream)
                self.assertIsNone(dream['data']['schedule']['active_run_id'])
                start = {'key': 'binding-focused-start', 'run_id': 'binding-focused-run',
                    'expected_revision': dream['data']['schedule']['revision'],
                    'mode_epoch': dream['data']['mode_epoch'], 'mode': 'FOCUSED'}
                status, started = await call('/api/dream/start', {**selected, 'input': start})
                self.assertEqual(status, 200, started)
                status, focused_bindings = await call('/api/administration/bindings', {'after': ''})
                self.assertEqual(status, 200, focused_bindings)
                self.assertEqual(focused_bindings['data'], bindings['data'])
                status, focused_state = await call('/api/administration/state', selected)
                self.assertEqual(status, 409, focused_state)
                status, inspected = await call('/api/dream/inspect', {**selected, 'input': {'run_id': 'binding-focused-run'}})
                self.assertEqual(status, 200, inspected)
                run = inspected['data']['run']
                status, aborted = await call('/api/dream/abort', {**selected, 'input': {
                    'key': 'binding-focused-abort', 'run_id': run['run_id'],
                    'expected_revision': run['revision'], 'mode_epoch': run['mode_epoch']}})
                self.assertEqual(status, 200, aborted)
                await self.assert_restricted(app, call)
                host = app.business.host
                assert host is not None
                ingress = host.assembly.ingress
                principal = await app.identity.authenticate(session['session'], host=False)
                original_read = ingress.verify_host_entry
                async def revoke_after_read(entry_id, host_id):
                    valid = await original_read(entry_id, host_id)
                    self.assertIs(type(await app.identity.revoke('binding-revoke-during-read',
                        principal.identity, principal.revision, host=False)), Committed)
                    return valid
                with patch.object(ingress, 'verify_host_entry', revoke_after_read):
                    status, denied = await call('/api/administration/bindings', {'after': ''})
                self.assertEqual(status, 401, denied)
                self.assertNotIn('data', denied)
                self.assertFalse(app.business.sends_enabled)
                self.assertEqual(len(requests), 1)
                self.assertEqual(failures, [])
            finally:
                self.assertTrue(await http.close())
                while not await app.business.close():
                    await asyncio.sleep(.05)
                self.assertTrue(await app.bootstrap.close())

    async def test_new_format_empty_single_and_multiple_registered_bindings(self):
        with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
            app, http = await ready_application(self, Path(directory) / 'instance', provider_port,
                initial_binding=False)
            try:
                session = await self.administrator(app)
                async def call(path, payload):
                    return await request(18180, path, payload, '', origin='http://127.0.0.1:18180', session=session)
                status, empty = await call('/api/administration/bindings', {'after': ''})
                self.assertEqual(status, 200, empty)
                self.assertEqual(empty['data'], {'items': [], 'after': None})
                for entry, owner in (('entry-a', 'host-a'), ('entry-b', 'host-b')):
                    status, registered = await call('/api/connections/hosts/register', {
                        'key': 'register-' + entry, 'host_id': owner, 'entry_id': entry,
                        'external_entry_id': 'conversation-' + entry})
                    self.assertEqual(status, 200, registered)
                    status, listing = await call('/api/administration/bindings', {'after': ''})
                    self.assertEqual(status, 200, listing)
                    self.assertEqual(len(listing['data']['items']), 1 if entry == 'entry-a' else 2)
                    self.assertEqual(listing['data']['after'], entry)
                    self.assertEqual(listing['data']['items'][-1], {'host_id': owner, 'entry_id': entry})
                    status, state = await call('/api/administration/state', {
                        'host_id': owner, 'entry_id': entry, 'input': {}})
                    self.assertEqual(status, 200, state)
                status, next_page = await call('/api/administration/bindings', {'after': 'entry-a'})
                self.assertEqual(status, 200, next_page)
                self.assertEqual(next_page['data'], {'items': [{'host_id': 'host-b', 'entry_id': 'entry-b'}], 'after': 'entry-b'})
                status, end = await call('/api/administration/bindings', {'after': 'entry-b'})
                self.assertEqual(status, 200, end)
                self.assertEqual(end['data'], empty['data'])
                status, ambiguous = await call('/api/administration/state', {})
                self.assertEqual(status, 409, ambiguous)
                self.assertEqual(ambiguous['error']['reason'], 'BINDING_REQUIRED')
                status, mismatched = await call('/api/administration/state', {
                    'host_id': 'host-a', 'entry_id': 'entry-b', 'input': {}})
                self.assertEqual(status, 403, mismatched)
                await self.assert_restricted(app, call, entry='entry-a', owner='host-a')
                self.assertEqual(len(requests), 1)
                self.assertEqual(failures, [])
            finally:
                self.assertTrue(await http.close())
                while not await app.business.close():
                    await asyncio.sleep(.05)
                self.assertTrue(await app.bootstrap.close())

    async def administrator(self, app):
        self.assertIs(type(await app.identity.establish('binding-admin',
            app.resources.read_secret('bootstrap').decode(), 'synthetic-binding-password')), Committed)
        _, session = await app.identity.login('binding-admin-login', 'synthetic-binding-password')
        assert session is not None
        return session

    async def assert_restricted(self, app, call, *, entry='entry', owner='host'):
        for payload in ({}, {'after': 1}, {'after': 'a' * 129}, {'after': '界' * 128}, {'after': '', 'entry_id': entry}):
            status, denied = await call('/api/administration/bindings', payload)
            self.assertEqual(status, 409, denied)
            self.assertEqual(denied['error']['code'], 'INVALID_INPUT')
            self.assertEqual(denied['error']['reason'], 'INVALID_SHAPE')
        status, anonymous = await request(18180, '/api/administration/bindings', {'after': ''}, '',
            origin='http://127.0.0.1:18180')
        self.assertEqual(status, 401, anonymous)
        created, token = await app.identity.create_token('binding-host-token', owner, (entry,),
            ('state_read',), time.time_ns() // 1000 + 60000000)
        self.assertIs(type(created), Committed)
        assert token is not None
        status, host_denied = await request(18180, '/api/administration/bindings', {'after': ''}, token,
            origin='http://127.0.0.1:18180')
        self.assertEqual(status, 401, host_denied)
        principal = await app.identity.authenticate(token, host=True)
        with self.assertRaises(OwnerFailure) as denied_host:
            await app.dispatch(principal, 'POST', '/api/administration/bindings', {'after': ''})
        self.assertEqual(denied_host.exception.code, 'ACCESS_DENIED')


if __name__ == '__main__':
    unittest.main()
