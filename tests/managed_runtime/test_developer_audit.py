"""Finite developer authority over real SQLite audits and first HTTP delivery."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.management.audit_provisioning import issue_audit_grant
from companion_memory.management.developer_audit import decode_grant
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_http import ManagedHTTP
from companion_memory.persistence import Committed, Ready
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap


async def wire(http: ManagedHTTP, path: str, payload: dict | None = None, *, cookie: str = '', csrf: str = '', bearer: str = ''):
    """Actual HTTP, retaining headers for separate credential-cookie assertions."""
    port = http.settings.integer('deployment.port')
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    body = json.dumps(payload).encode() if payload is not None else b''
    head = [f'{"POST" if payload is not None else "GET"} {path} HTTP/1.1', f'Host: {http.authority}',
        f'Origin: {http.origin}', 'Content-Type: application/json', f'Content-Length: {len(body)}',
        f'Cookie: {cookie}', f'X-CSRF-Token: {csrf}', f'Authorization: Bearer {bearer}']
    writer.write(('\r\n'.join(head) + '\r\n\r\n').encode() + body)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 20)
    writer.close(); await writer.wait_closed()
    headers, encoded = response.split(b'\r\n\r\n', 1)
    return int(headers.split(b' ')[1]), json.loads(encoded), headers.decode()


def private_scope(path: Path, operations: list[dict], histories: list[dict]) -> None:
    path.write_text(json.dumps({'operations': operations, 'histories': histories}))
    path.chmod(0o600)


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.operator_directory = TemporaryDirectory()
        self.operator_root = Path(self.operator_directory.name)
        self.bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(self.root)}))
        self.assertIs(type(await self.bootstrap.open()), Ready)
        self.app = ManagedApplication(self.bootstrap)
        self.resources = self.app.resources
        saved = await self.app.identity.save_draft('audited-draft', None, {'name': 'synthetic'})
        assert type(saved) is Committed
        identity = saved.receipt.identity
        self.reference = {name: getattr(identity, name) for name in ('owner_namespace', 'operation_kind', 'operation_key')}
        private_scope(self.root / 'scope.json', [self.reference], [])
        issue_audit_grant(self.resources, self.root / 'scope.json', self.operator_root / 'issued', 600)
        self.grant_path = self.operator_root / 'issued/developer_audit'
        self.grant = self.grant_path.read_bytes()
        self.credential = (self.operator_root / 'issued/developer_audit_credential').read_text()
        original = self.resources.read_secret
        self.secret_patch = patch.object(self.resources, 'read_secret', side_effect=lambda name:
            self.grant_path.read_bytes() if name == 'developer_audit' else original(name))
        self.secret_patch.start()
        self.principal = self.app.audit.authenticate(self.credential)
        assets = self.root / 'assets'; assets.mkdir()
        for name in ('index.html', 'app.js', 'style.css'): (assets / name).write_text('synthetic asset')
        self.http = ManagedHTTP(self.bootstrap.settings, self.app.identity, self.app.dispatch, self.app.health,
            assets, audit_source=lambda: self.app.audit)
        await self.http.start()

    async def asyncTearDown(self):
        self.assertTrue(await self.http.close())
        self.secret_patch.stop()
        self.assertTrue(await self.app.business.close())
        self.assertTrue(await self.bootstrap.close())
        self.temporary.cleanup()
        self.operator_directory.cleanup()

    async def test_native_audit_cookie_scope_and_no_inherited_authority(self):
        identity = self.app.identity
        self.assertIs(type(await identity.establish('admin', self.resources.read_secret('bootstrap').decode(), 'synthetic-only-password')), Committed)
        _, session = await identity.login('login', 'synthetic-only-password'); assert session is not None
        _, token = await identity.create_token('host', 'host', ('entry',), ('accept',), time.time_ns() // 1000 + 60000000)
        self.assertIsNotNone(token)
        for cookie, bearer in ((f'iris_session={session["session"]}', ''), ('', token or ''), ('', 'agent')):
            code, body, _ = await wire(self.http, '/api/audit/status', cookie=cookie, bearer=bearer)
            self.assertEqual(code, 403, body)
            self.assertNotIn('data', body)
        code, body, headers = await wire(self.http, '/api/audit/login', {'credential': self.credential})
        self.assertEqual(code, 200, body)
        self.assertIn('iris_audit=', headers); self.assertNotIn('iris_session=', headers)
        self.assertNotIn(self.credential, json.dumps(body))
        cookie = f'iris_audit={self.credential}'
        from companion_memory.management.identity import digest
        csrf = digest('audit-csrf:' + self.credential)
        code, body, _ = await wire(self.http, '/api/audit/operation', self.reference, cookie=cookie, csrf=csrf)
        self.assertEqual(code, 200, body)
        self.assertEqual(body['data']['status'], 'FOUND')
        self.assertTrue(body['data']['records'])
        rejected = []
        for reference in (self.reference | {'operation_key': 'absent'}, self.reference | {'operation_key': 'admin'},
                self.reference | {'sql': 'SELECT * FROM audit_records'}):
            code, body, _ = await wire(self.http, '/api/audit/operation', reference, cookie=cookie, csrf=csrf)
            self.assertEqual(code, 403, body); rejected.append(body)
        self.assertEqual(rejected[0], rejected[1]); self.assertEqual(rejected[1], rejected[2])
        code, body, _ = await wire(self.http, '/api/audit/history/restore', self.reference, cookie=cookie, csrf=csrf)
        self.assertEqual(code, 403, body)
        code, body, _ = await wire(self.http, '/api/wizard/save', {}, cookie=cookie, csrf=csrf)
        self.assertEqual(code, 401, body)
        self.grant_path.unlink()
        code, body, _ = await wire(self.http, '/api/audit/operation', self.reference, cookie=cookie, csrf=csrf)
        self.assertEqual(code, 403, body); self.assertNotIn('data', body)

    async def test_revocation_during_native_read_and_before_first_delivery(self):
        # First read creates the native audit reader, with real receipt verification.
        await self.app.audit.dispatch(self.principal, 'POST', '/api/audit/operation', self.reference)
        reader = self.app.audit.reader; assert reader is not None
        original = reader.read_audit
        async def revoke_during_read(_reader, identity):
            result = await original(identity)
            self.grant_path.unlink()
            return result
        with patch.object(type(reader), 'read_audit', new=revoke_during_read):
            with self.assertRaises(OwnerFailure):
                await self.app.audit.dispatch(self.principal, 'POST', '/api/audit/operation', self.reference)
        self.grant_path.write_bytes(self.grant)
        original_send = self.http.send
        from companion_memory.management.identity import digest
        for failure in ('REMOVED', 'EXPIRED'):
            self.grant_path.write_bytes(self.grant)
            async def invalidate_before_delivery(writer, status, body, **kwargs):
                if kwargs.get('authorize') is not None:
                    if failure == 'REMOVED': self.grant_path.unlink()
                    else:
                        value = json.loads(self.grant); value['expires_at_us'] = 1
                        self.grant_path.write_text(json.dumps(value))
                await original_send(writer, status, body, **kwargs)
            with patch.object(self.http, 'send', side_effect=invalidate_before_delivery):
                code, body, _ = await wire(self.http, '/api/audit/operation', self.reference,
                    cookie=f'iris_audit={self.credential}', csrf=digest('audit-csrf:' + self.credential))
            self.assertEqual(code, 403, body)
            self.assertNotIn('records', json.dumps(body))
        expired = json.loads(self.grant); expired['expires_at_us'] = 1
        self.grant_path.write_text(json.dumps(expired))
        with self.assertRaises(OwnerFailure): self.app.audit.authenticate(self.credential)
        changed = json.loads(self.grant); changed['revision'] += 1
        self.grant_path.write_text(json.dumps(changed))
        with self.assertRaises(OwnerFailure): self.app.audit.recheck(self.principal)
        self.assertNotEqual(self.app.audit.authenticate(self.credential), self.principal)

    async def test_private_scope_and_complete_grant_validation(self):
        self.assertEqual(decode_grant(self.grant).database_id, self.resources.database_id)
        with self.assertRaises(FileExistsError):
            issue_audit_grant(self.resources, self.root / 'scope.json', self.operator_root / 'issued', 600)
        with self.assertRaisesRegex(ValueError, 'outside'):
            issue_audit_grant(self.resources, self.root / 'scope.json', self.root / 'forbidden-secret', 600)
        (self.root / 'scope.json').chmod(0o644)
        with self.assertRaises(ValueError):
            issue_audit_grant(self.resources, self.root / 'scope.json', self.operator_root / 'unsafe', 600)
        changed = json.loads(self.grant); changed['database_id'] = 'other'
        self.grant_path.write_text(json.dumps(changed))
        with self.assertRaises(OwnerFailure): self.app.audit.authenticate(self.credential)
        with self.assertRaises(ValueError): decode_grant(b'{"format":1,"format":2}')

    async def test_unavailable_database_is_a_safe_audit_failure(self):
        from companion_memory.management.identity import digest
        # Prime the native reader, then make the actual SQLite parent unavailable.
        await self.app.audit.dispatch(self.principal, 'POST', '/api/audit/operation', self.reference)
        database = self.root / 'db'
        moved = self.root / 'unavailable-database'
        database.rename(moved)
        try:
            code, body, _ = await wire(self.http, '/api/audit/operation', self.reference,
                cookie=f'iris_audit={self.credential}', csrf=digest('audit-csrf:' + self.credential))
            self.assertGreaterEqual(code, 400, body)
            self.assertNotIn('records', json.dumps(body))
            self.assertNotIn(str(self.root), json.dumps(body))
            self.assertFalse(database.exists(), 'A failed audit must not recreate the missing authority directory.')
        finally:
            moved.rename(database)

    async def test_http_session_capacity_is_a_definite_rejection(self):
        identity = self.app.identity
        await identity.establish('capacity-admin', self.resources.read_secret('bootstrap').decode(), 'synthetic-only-password')
        for number in range(self.bootstrap.settings.integer('management.session_limit')):
            result, session = await identity.login('capacity-' + str(number), 'synthetic-only-password')
            self.assertIs(type(result), Committed)
            self.assertIsNotNone(session)
        code, body, headers = await wire(self.http, '/api/login', {'key': 'over-capacity', 'password': 'synthetic-only-password'})
        self.assertEqual(code, 409, body)
        self.assertEqual(body['outcome'], 'NOT_COMMITTED', body)
        self.assertEqual(body['error']['reason'], 'PARTICIPANT_REJECTED')
        self.assertNotIn('Set-Cookie:', headers)

    async def test_trusted_environment_timezone_does_not_guess_abbreviations(self):
        from companion_memory.configuration.daily_preparation import environment_timezone
        for zone, expected in (('UTC', 'UTC'), ('America/New_York', 'America/New_York'), ('EST', None), ('Invalid/Zone', None)):
            with patch.dict('os.environ', {'TZ': zone}):
                self.assertEqual(environment_timezone(), expected)
