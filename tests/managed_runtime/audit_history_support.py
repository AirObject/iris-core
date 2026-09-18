"""Native historical body inspection never resurrects a deleted formal object."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from companion_memory.logging_service.object_history import result_history_ids
from companion_memory.management.audit_provisioning import issue_audit_grant
from companion_memory.management.developer_audit import DeveloperAudit
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_http import ManagedHTTP
from companion_memory.management.identity import digest
from companion_memory.persistence import Committed, Found
from .test_developer_audit import private_scope, wire


async def exercise_history_audit(test, business, deleted: Committed, object_id: str):
    bootstrap = business.bootstrap
    resources = bootstrap.resources; host = business.host
    assert resources is not None and host is not None
    app = object.__new__(ManagedApplication)
    from companion_memory.management.request_admission import RequestAdmission
    app.admission = RequestAdmission(16, 30)
    app.bootstrap, app.resources, app.business = bootstrap, resources, business
    identity = bootstrap.assembly.identity; assert identity is not None
    app.identity, app.logging = identity, None
    app.audit = DeveloperAudit(app)
    operation = deleted.receipt.identity
    reference = {name: getattr(operation, name) for name in ('owner_namespace', 'operation_kind', 'operation_key')}
    histories = result_history_ids(operation.owner_namespace, operation.operation_kind, deleted.receipt.result)
    test.assertTrue(histories)
    historical = reference | {'history_id': histories[0], 'object_id': object_id}
    with TemporaryDirectory() as directory:
        fixture_root = Path(directory)
        private_scope(fixture_root / 'history-scope.json', [reference], [historical])
        issue_audit_grant(resources, fixture_root / 'history-scope.json', fixture_root / 'history-grant', 600)
        credential = (fixture_root / 'history-grant/developer_audit_credential').read_text()
        original = resources.read_secret
        assets = fixture_root / 'audit-assets'; assets.mkdir()
        for name in ('index.html', 'app.js', 'style.css'): (assets / name).write_text('synthetic asset')
        http = ManagedHTTP(bootstrap.settings, identity, app.dispatch, app.health, assets, audit_source=lambda: app.audit)
        with patch.object(resources, 'read_secret', side_effect=lambda name:
                (fixture_root / 'history-grant/developer_audit').read_bytes() if name == 'developer_audit' else original(name)):
            await http.start()
            try:
                async def call(path, payload):
                    return await wire(http, path, payload, cookie=f'iris_audit={credential}', csrf=digest('audit-csrf:' + credential))
                code, body, _ = await call('/api/audit/history', historical)
                test.assertEqual(code, 200, body)
                test.assertIn('合成', str(body))
                test.assertEqual(body['data']['value']['previous_value']['object_id'], object_id)
                code, body, _ = await call('/api/audit/history', historical | {'object_id': 'absent'})
                test.assertEqual(code, 403, body); test.assertNotIn('data', body)
                code, body, _ = await call('/api/audit/history/restore', historical)
                test.assertEqual(code, 403, body)
                test.assertEqual(await host.assembly.memory.information.current_page('', 4), ())
                # The exact original write still resolves to its unchanged original receipt.
                confirmed = await host.assembly.storage.read_object_history_receipt(operation)
                assert type(confirmed) is Found
                test.assertEqual(confirmed.value, deleted.receipt)
                from .audit_mode_support import exercise_audit_modes
                await exercise_audit_modes(test, app, http, call,
                    fixture_root / 'history-grant/developer_audit', reference, historical, object_id)
            finally:
                test.assertTrue(await http.close())
    return {'operations': [reference], 'histories': [historical]}
