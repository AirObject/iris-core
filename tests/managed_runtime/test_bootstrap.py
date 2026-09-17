"""Actual SQLite bootstrap without business settings or outbound model requests."""
import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import time

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
from companion_memory.runtime.daily_assembly import DailyAssembly
from companion_memory.persistence import DatabaseResources, Ready, Committed
from companion_memory.management.identity import digest
from companion_memory.persistence.owned_statements import OwnerFailure


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_timeout_retains_owned_task_until_original_completion(self):
        from companion_memory.management.managed_http import ManagedHTTP
        from .http_support import request
        authority = self.authority
        assert authority is not None
        _, token = await authority.create_token('slow-token', 'host', ('entry',),
            ('accept',), time.time_ns() // 1000 + 60000000)
        assert token is not None
        settings = resolve_deployment({'deployment.data_root': str(self.root),
            'management.http_timeout_seconds': 1, 'management.maintenance_timeout_seconds': 1})
        assets = self.root / 'static'
        assets.mkdir()
        for name in ('index.html', 'app.js', 'style.css'):
            (assets / name).write_text('synthetic response fixture')
        release = asyncio.Event()
        completed = asyncio.Event()
        async def dispatch(principal, method, path, payload):
            await release.wait()
            result = await authority.save_draft('slow-original', None, {'name': 'synthetic'})
            self.assertIs(type(result), Committed, result)
            completed.set()
            return result
        http = ManagedHTTP(settings, authority, dispatch, lambda: {'state': 'READY'}, assets)
        try:
            await http.start()
            status, pending = await request(settings.integer('deployment.port'), '/api/host/accept', {'key': 'slow-original'}, token)
            self.assertEqual((status, pending['outcome'], pending['cleanup_pending']), (202, 'UNCONFIRMED', True))
            self.assertEqual(pending['operation_key'], 'slow-original')
            self.assertFalse(await http.close())
            self.assertEqual(len(http.work), 1)
            self.assertFalse(next(iter(http.work)).cancelled())
            release.set()
            await asyncio.wait_for(completed.wait(), 5)
            self.assertTrue(await http.close())
            original = await authority.save_draft('slow-original', None, {'name': 'synthetic'})
            assert type(original) is Committed
            self.assertEqual(original.source, 'EXISTING')
        finally:
            release.set()
            self.assertTrue(await http.close())

    async def test_http_post_commit_permission_loss_retains_original_confirmation(self):
        from companion_memory.management.managed_http import ManagedHTTP
        from .http_support import request
        authority = self.authority
        assert authority is not None
        _, token = await authority.create_token('late-reply-token', 'host', ('entry',),
            ('accept',), time.time_ns() // 1000 + 60000000)
        assert token is not None
        assets = self.root / 'static'
        assets.mkdir()
        for name in ('index.html', 'app.js', 'style.css'):
            (assets / name).write_text('synthetic response fixture')
        async def dispatch(principal, method, path, payload):
            if payload['key'] == 'reject-before-write':
                raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
            if payload['key'] == 'confirm-original':
                return await authority.save_draft('original-write', None, {'name': 'synthetic'})
            result = await authority.save_draft('original-write', None, {'name': 'synthetic'})
            self.assertIs(type(result), Committed, result)
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
        http = ManagedHTTP(self.settings, authority, dispatch, lambda: {'state': 'READY'}, assets)
        try:
            await http.start()
            port = self.settings.integer('deployment.port')
            status, denied = await request(port, '/api/host/accept', {'key': 'reject-before-write'}, token)
            self.assertEqual((status, denied['outcome']), (401, 'REJECTED'))
            status, late = await request(port, '/api/host/accept', {'key': 'original-write'}, token)
            self.assertEqual((status, late['outcome']), (401, 'UNCONFIRMED'))
            self.assertNotIn('data', late)
            status, confirmed = await request(port, '/api/host/accept', {'key': 'confirm-original'}, token)
            self.assertEqual((status, confirmed['outcome']), (200, 'COMMITTED'))
            self.assertEqual(confirmed['data']['source'], 'EXISTING')
            self.assertEqual((await authority.read_draft())['revision'], 1)
        finally:
            self.assertTrue(await http.close())

    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ('db', 'logs/runtime', 'blobs', 'upload_staging', 'backups', 'backup_staging'):
            (self.root / name).mkdir(parents=True, mode=0o700)
        self.settings = resolve_deployment({'deployment.data_root': str(self.root)})
        self.directories = {'media': (str(self.root / 'blobs'), str(self.root / 'upload_staging')),
            'database': (str(self.root / 'db'),), 'audit': (str(self.root / 'db'),),
            'provider_usage': (str(self.root / 'db'),),
            'backup': (str(self.root / 'backups'), str(self.root / 'backup_staging'))}
        self.snapshot = bootstrap_snapshot(self.settings, self.directories)
        self.binding = {'identity': 'managed-test-database', 'path': str(self.root / 'db/memory.sqlite3')}
        (self.root / 'retained.json').write_text(json.dumps(self.binding))
        self.resources = DatabaseResources('managed-test-database', lambda identity, path:
            json.loads((self.root / 'retained.json').read_text()) == {'identity': identity, 'path': path})
        self.assembly = DailyAssembly(dream_format=True, managed_format=True)
        self.assertIs(type(await self.assembly.storage.initialize(self.snapshot, self.resources, 'CREATE_NEW')), Ready)
        self.authority = self.assembly.identity
        self.assertIsNotNone(self.authority)
        assert self.authority is not None
        self.authority.bind(self.assembly.storage, 'managed-test-database', 'managed-test-instance',
                            self.settings, digest('synthetic-bootstrap-credential-000000'))

    async def asyncTearDown(self):
        if self.authority is not None:
            self.assertTrue(self.authority.close())
        await self.assembly.storage.close()
        self.assertEqual(self.assembly.storage.get_health().lifecycle, 'CLOSED')
        self.temporary.cleanup()

    async def test_bootstrap_secret_and_original_administrator_key(self):
        assert self.authority is not None
        with self.assertRaises(OwnerFailure):
            await self.authority.establish('create-admin', 'incorrect-secret', 'synthetic-password-1234')
        created = await self.authority.establish('create-admin', 'synthetic-bootstrap-credential-000000', 'synthetic-password-1234')
        self.assertIs(type(created), Committed)
        repeated = await self.authority.establish('create-admin', 'synthetic-bootstrap-credential-000000', 'synthetic-password-1234')
        self.assertIs(type(repeated), Committed)
        assert type(created) is Committed and type(repeated) is Committed
        self.assertEqual(created.receipt.commit_id, repeated.receipt.commit_id)
        account = await self.authority.rows.read('account', 'administrator')
        assert account is not None
        self.assertNotEqual(account['password_verifier'], 'synthetic-password-1234')

    async def test_login_authentication_and_persistent_rate_limit(self):
        assert self.authority is not None
        await self.authority.establish('create-admin', 'synthetic-bootstrap-credential-000000', 'synthetic-password-1234')
        result, session = await self.authority.login('login-correct', 'synthetic-password-1234')
        self.assertIs(type(result), Committed)
        self.assertIsNotNone(session)
        assert session is not None
        principal = await self.authority.authenticate(session['session'], host=False)
        self.assertEqual(principal.kind, 'administrator')
        with self.assertRaises(OwnerFailure):
            await self.authority.authenticate(session['session'], host=True)
        for index in range(self.settings.integer('management.login_attempts')):
            with self.assertRaises(OwnerFailure):
                await self.authority.login('failed-' + str(index), 'incorrect-password-1234')
        with self.assertRaises(OwnerFailure) as denied:
            await self.authority.login('limited', 'synthetic-password-1234')
        self.assertEqual(denied.exception.reason, 'LOGIN_RATE_LIMITED')

    async def test_revocation_blocks_previously_authenticated_write(self):
        authority = self.authority
        assert authority is not None
        await authority.establish('create-admin', 'synthetic-bootstrap-credential-000000', 'synthetic-password-1234')
        _, secret = await authority.login('login', 'synthetic-password-1234')
        assert secret is not None
        principal = await authority.authenticate(secret['session'], host=False)
        with authority.permission(principal, 'management'):
            saved = await authority.save_draft('draft', None, {'name': '合成测试角色', 'timezone': 'Asia/Shanghai'})
        self.assertIs(type(saved), Committed, saved)
        draft = await authority.read_draft()
        self.assertEqual(draft['revision'], 1)
        with authority.permission(principal, 'management'):
            revoked = await authority.revoke('logout', principal.identity, principal.revision, host=False)
        self.assertIs(type(revoked), Committed, revoked)
        with authority.permission(principal, 'management'):
            blocked = await authority.save_draft('draft-after-revoke', 1, {'name': '不得写入'})
        self.assertIsNot(type(blocked), Committed, blocked)
        self.assertEqual((await authority.read_draft())['revision'], 1)

    async def test_host_scope_and_independent_revocation(self):
        authority = self.authority
        assert authority is not None
        await authority.establish('create-admin', 'synthetic-bootstrap-credential-000000', 'synthetic-password-1234')
        result, token = await authority.create_token('token', 'host-one', ('entry-one',), ('query',), time.time_ns() // 1000 + 100000000)
        self.assertIs(type(result), Committed, result)
        assert token is not None
        principal = await authority.authenticate(token, host=True)
        self.assertEqual(principal.operations, ('query',))
        with self.assertRaises(OwnerFailure):
            await authority.authenticate(token, host=False)
        with authority.permission(principal, 'accept', 'entry-one'):
            blocked = await authority.save_draft('forbidden', None, {})
        self.assertIsNot(type(blocked), Committed)
        self.assertIs(type(await authority.revoke('revoke-host', principal.identity, 1, host=True)), Committed)
        with self.assertRaises(OwnerFailure):
            await authority.authenticate(token, host=True)

    async def test_draft_original_key_and_revision_conflict(self):
        authority = self.authority
        assert authority is not None
        body = {'initial_material': '合成内容' * 10000, 'timezone_confirmed': True}
        first = await authority.save_draft('save', None, body)
        again = await authority.save_draft('save', None, body)
        assert type(first) is Committed and type(again) is Committed
        self.assertEqual(first.receipt.commit_id, again.receipt.commit_id)
        self.assertEqual((await authority.read_draft())['draft'], body)
        conflict = await authority.save_draft('stale', None, {'initial_material': 'stale'})
        self.assertIsNot(type(conflict), Committed)

    async def test_password_rotation_original_receipt_and_old_session_revocation(self):
        authority = self.authority
        assert authority is not None
        await authority.establish('administrator', 'synthetic-bootstrap-credential-000000', 'synthetic-password-before')
        _, secret = await authority.login('login-before', 'synthetic-password-before')
        assert secret is not None
        principal = await authority.authenticate(secret['session'], host=False)
        account = await authority.rows.read('account', 'administrator')
        assert account is not None
        expected = account['revision']
        assert type(expected) is int
        with authority.permission(principal, 'management'):
            changed = await authority.update_password('change-password', expected, 'synthetic-password-before', 'synthetic-password-after')
        self.assertIs(type(changed), Committed, changed)
        with self.assertRaises(OwnerFailure):
            await authority.authenticate(secret['session'], host=False)
        _, replacement = await authority.login('login-after', 'synthetic-password-after')
        assert replacement is not None
        new_principal = await authority.authenticate(replacement['session'], host=False)
        with authority.permission(new_principal, 'management'):
            confirmed = await authority.update_password('change-password', expected, 'synthetic-password-before', 'synthetic-password-after')
        assert type(changed) is Committed and type(confirmed) is Committed
        self.assertEqual(changed.receipt.commit_id, confirmed.receipt.commit_id)
        with self.assertRaises(OwnerFailure):
            await authority.update_password('change-password', expected, 'synthetic-password-before', 'different-password-value')
        revoked = await authority.revoke('original-revoke', new_principal.identity, new_principal.revision, host=False)
        repeated = await authority.revoke('original-revoke', new_principal.identity, new_principal.revision, host=False)
        assert type(revoked) is Committed and type(repeated) is Committed
        self.assertEqual(revoked.receipt.commit_id, repeated.receipt.commit_id)

    async def test_revoked_credentials_are_bounded_and_original_receipts_survive_retirement(self):
        from companion_memory.memory.formats import record, sequence
        authority = self.authority
        assert authority is not None
        await authority.establish('administrator', 'synthetic-bootstrap-credential-000000', 'synthetic-password-before')
        for host in (False, True):
            previous = None
            for index in range(5):
                key = ('token-' if host else 'session-') + str(index)
                if host:
                    result, raw = await authority.create_token(key, 'host', ('entry',), ('query',), time.time_ns() // 1000 + 100000000)
                else:
                    result, session = await authority.login(key, 'synthetic-password-before')
                    assert session is not None
                    raw = session['session']
                assert raw is not None and type(result) is Committed, result
                principal = await authority.authenticate(raw, host=host)
                table = 'host_tokens' if host else 'sessions'
                count = await authority.rows.rows.read(table + '_total_count', {})
                self.assertEqual(count[0]['count'], 1)
                retired = sequence(record(record(result.receipt.result)['fact'])['retired'])
                self.assertEqual(len(retired), 0 if previous is None else 1)
                if previous is not None:
                    old_key, old_principal, old_raw, old_receipt = previous
                    with self.assertRaises(OwnerFailure):
                        await authority.authenticate(old_raw, host=host)
                    original = await authority.revoke(old_key, old_principal.identity, old_principal.revision, host=host)
                    assert type(original) is Committed
                    self.assertEqual(original.receipt.commit_id, old_receipt.commit_id)
                revoked = await authority.revoke('revoke-' + key, principal.identity, principal.revision, host=host)
                assert type(revoked) is Committed
                previous = ('revoke-' + key, principal, raw, revoked.receipt)


if __name__ == '__main__':
    unittest.main()
