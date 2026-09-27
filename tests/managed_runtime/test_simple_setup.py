"""Protected local setup commits references, preserving original receipts and keys.

Provider accounts in this fixture are synthetic complete configuration; the
exercise never initializes a model host or sends a supplier request.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_http import ManagedHTTP, public_value
from companion_memory.persistence import Committed, Ready
from companion_memory.persistence.managed_backup import ConsistentBackup
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from .http_support import request
from .test_business import setup_draft


class SimpleSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'data'
        self.secret = Path(self.temporary.name) / 'provider-credentials'
        self.root.mkdir(mode=0o700)
        self.secret.mkdir(mode=0o700)
        self.settings = resolve_deployment({'deployment.data_root': str(self.root),
            'deployment.provider_secret_root': str(self.secret)})
        self.bootstrap = ManagedBootstrap(self.settings)
        self.assertIs(type(await self.bootstrap.open()), Ready)
        self.application = ManagedApplication(self.bootstrap)
        self.identity = self.application.identity
        self.assertIs(type(await self.identity.establish('administrator',
            self.application.resources.read_secret('bootstrap').decode(), 'synthetic-setup-password')), Committed)
        _, session = await self.identity.login('login', 'synthetic-setup-password')
        assert session is not None
        self.session = session
        self.principal = await self.identity.authenticate(session['session'], host=False)
        self.original_draft = setup_draft(self.root)

    async def asyncTearDown(self):
        self.assertTrue(await self.application.business.close())
        self.assertTrue(await self.bootstrap.close())
        self.temporary.cleanup()

    async def api(self, path, payload=None):
        result = await self.application.dispatch(self.principal, 'GET' if payload is None else 'POST', path, payload or {})
        await asyncio.sleep(0)
        assert type(result) is dict
        return result

    def payload(self, **extra):
        return {'key': 'setup-original', 'expected_revision': None,
            'role_name': 'Iris', 'initial_material': 'Synthetic administrator-provided background.',
            'timezone': 'Asia/Shanghai', 'generation_api_key': 'synthetic-generation-secret',
            'embedding_api_key': 'synthetic-embedding-secret', **extra}

    async def test_saved_keys_are_masked_and_original_receipt_is_independent_of_current_draft(self):
        initial = await self.api('/api/setup')
        self.assertEqual(initial['state'], 'DRAFT')
        self.assertFalse(initial['providers']['generation']['configured'])
        payload = self.payload()
        result = await self.api('/api/setup/save', payload)
        self.assertEqual((result['state'], result['revision']), ('CONFIRMED', 1))
        stored = await self.identity.read_draft()
        stored_draft = stored['draft']
        assert type(stored_draft) is dict
        self.assertEqual(stored_draft['setup_mode'], 'SIMPLE')
        repeated = await self.api('/api/setup/save', payload)
        self.assertEqual(repeated['receipt'].commit_id, result['receipt'].commit_id)
        self.assertEqual((await self.identity.read_draft())['revision'], 1)
        status = await self.api('/api/setup')
        self.assertTrue(status['providers']['generation']['configured'])
        self.assertTrue(status['providers']['embedding']['configured'])
        self.assertFalse(status['replace_required'])
        data = json.dumps(public_value([stored, result, status]))
        for secret in ('synthetic-generation-secret', 'synthetic-embedding-secret'):
            self.assertNotIn(secret, data)
            for file in (self.root / 'db').iterdir():
                self.assertNotIn(secret.encode(), file.read_bytes())
        # A later save changes the current draft. The original operation still
        # identifies revision 1, not the latest observed wizard revision.
        next_payload = self.payload(key='setup-next', expected_revision=1, role_name='Renamed')
        next_payload.pop('generation_api_key')
        next_payload.pop('embedding_api_key')
        self.assertEqual((await self.api('/api/setup/save', next_payload))['revision'], 2)
        confirmed = await self.api('/api/setup/operation', {'key': 'setup-original'})
        self.assertEqual((confirmed['state'], confirmed['revision']), ('CONFIRMED', 1))
        with self.assertRaises(OwnerFailure) as changed:
            await self.api('/api/setup/save', self.payload(generation_api_key='other-secret'))
        self.assertEqual(changed.exception.reason, 'CONTENT_MISMATCH')
        self.assertEqual((await self.api('/api/setup/operation', {'key': 'absent'}))['state'], 'ABSENT')

    async def test_custom_and_validated_material_require_explicit_preserving_conversion(self):
        self.assertIs(type(await self.identity.save_draft('custom', None, self.original_draft)), Committed)
        with self.assertRaises(OwnerFailure) as unconfirmed:
            await self.api('/api/setup/save', self.payload(expected_revision=1))
        self.assertEqual(unconfirmed.exception.reason, 'REPLACE_CONFIRMATION_REQUIRED')
        self.assertEqual(list(self.secret.iterdir()), [])
        self.assertIs(type(await self.identity.advance_wizard('validate', 1, 'VALIDATED', 'validate')), Committed)
        payload = self.payload(expected_revision=2, replace_existing=True,
            role_name=self.original_draft['role_name'], initial_material=self.original_draft['initial_material'])
        result = await self.api('/api/setup/save', payload)
        self.assertEqual(result['revision'], 3)
        saved = await self.identity.read_draft()
        saved_draft = saved['draft']
        assert type(saved_draft) is dict
        self.assertEqual(saved['state'], 'VALIDATED')
        self.assertEqual(saved_draft['role_name'], self.original_draft['role_name'])
        self.assertEqual(saved_draft['setup_mode'], 'SIMPLE')
        with self.assertRaises(OwnerFailure):
            await self.api('/api/setup/save', {**payload, 'key': 'changed', 'expected_revision': 3, 'role_name': 'Changed'})

    async def test_prepared_but_uncommitted_original_retries_without_key_echo(self):
        original = self.identity.save_draft
        async def interrupted(*args, **kwargs):
            raise OSError('synthetic interruption before commit')
        with patch.object(self.identity, 'save_draft', interrupted):
            with self.assertRaises(OSError):
                await self.api('/api/setup/save', self.payload())
        self.assertIsNone((await self.identity.read_draft())['revision'])
        self.assertEqual((await self.api('/api/setup/operation', {'key': 'setup-original'}))['state'], 'ABSENT')
        retry = self.payload()
        retry.pop('generation_api_key')
        retry.pop('embedding_api_key')
        result = await self.api('/api/setup/save', retry)
        self.assertEqual(result['state'], 'CONFIRMED')
        self.assertEqual(len(list(self.secret.iterdir())), 1)

    async def test_http_authentication_origin_csrf_and_revocation_reject_before_key_storage(self):
        static = Path(self.temporary.name) / 'static'
        static.mkdir()
        for name in ('index.html', 'app.js', 'style.css'):
            (static / name).write_text('synthetic static')
        http = ManagedHTTP(self.settings, self.identity, self.application.dispatch, self.application.health, static)
        try:
            await http.start()
            port = self.settings.integer('deployment.port')
            for origin, session in ((None, self.session), ('https://invalid.example', self.session),
                    (f'http://127.0.0.1:{port}', None), (f'http://127.0.0.1:{port}', {**self.session, 'csrf': 'wrong'})):
                status, result = await request(port, '/api/setup/save', self.payload(), '', origin=origin, session=session)
                self.assertIn(status, (401, 403), result)
                self.assertEqual(list(self.secret.iterdir()), [])
            self.assertIs(type(await self.identity.revoke('logout', self.principal.identity, self.principal.revision, host=False)), Committed)
            status, result = await request(port, '/api/setup/save', self.payload(), '',
                origin=f'http://127.0.0.1:{port}', session=self.session)
            self.assertIn(status, (401, 403), result)
            self.assertEqual(list(self.secret.iterdir()), [])
        finally:
            self.assertTrue(await http.close())

    async def test_backup_contains_only_key_references_and_missing_keys_do_not_break_bootstrap(self):
        await self.api('/api/setup/save', self.payload())
        resources = self.application.resources
        self.assertTrue(await self.bootstrap.close())
        # Reacquire the same physical resource identity after SQLite has closed.
        from companion_memory.runtime.managed_resources import ManagedResources
        retained = ManagedResources(self.settings, self.bootstrap.assembly_digest)
        try:
            backup = ConsistentBackup(retained, lambda: True)
            destination = backup.create('without-provider-secrets')
            manifest = backup.verify(destination)
            self.assertIs(manifest['secrets_included'], False)
            for path in destination.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'synthetic-generation-secret', path.read_bytes())
                    self.assertNotIn(b'synthetic-embedding-secret', path.read_bytes())
        finally:
            retained.close()
        self.secret.rename(self.secret.with_name('retained-private-keys'))
        self.bootstrap = ManagedBootstrap(self.settings)
        self.assertIs(type(await self.bootstrap.open()), Ready)
        self.application = ManagedApplication(self.bootstrap)
        self.assertEqual(self.bootstrap.state, 'BOOTSTRAP')


if __name__ == '__main__':
    unittest.main()
