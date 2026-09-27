"""Real local role publication reaches readiness with zero Provider attempts.

Disposable product-format databases exercise original receipts, reopen,
operator provenance, native workspace attachment and outstanding-run refusal.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
import sqlite3
from unittest.mock import patch

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Committed, Found, Ready
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_business import ManagedBusiness
from companion_memory.runtime.managed_host_resources import host_resources
from companion_memory.self_model.current import Available
from companion_memory.self_model.local_persona import role_text
from tests.managed_runtime.test_business import setup_draft, synthetic_resources


class LocalPersonaTests(unittest.IsolatedAsyncioTestCase):
    def assert_no_requests(self, root):
        with sqlite3.connect((root / 'db/memory.sqlite3').as_uri() + '?mode=ro', uri=True) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM provider_requests').fetchone()[0], 0)
            self.assertEqual(database.execute('SELECT COUNT(*) FROM provider_attempts').fetchone()[0], 0)

    async def close(self, bootstrap, business):
        for _ in range(100):
            if business is None or await business.close():
                break
            await asyncio.sleep(.05)
        else:
            self.fail('Business cleanup did not finish.')
        if bootstrap.assembly.identity is not None:
            bootstrap.assembly.identity.close()
        self.assertTrue(await bootstrap.close())

    async def initialized(self, bootstrap, root, *, simple=False, synthetic=False):
        self.assertIs(type(await bootstrap.open()), Ready)
        identity = bootstrap.assembly.identity
        assert identity is not None
        draft = setup_draft(root)
        for name in ('entry_id', 'host_id', 'conversation_id'):
            del draft[name]
        if simple:
            draft['setup_mode'] = 'SIMPLE'
        self.assertIs(type(await identity.save_draft('save-role', None, draft)), Committed)
        business = ManagedBusiness(bootstrap, identity,
            resource_factory=synthetic_resources if synthetic else host_resources)
        result = await business.initialize('initialize-role', 1)
        assert type(result) is dict
        return business, draft, result

    async def test_simple_setup_is_ready_and_reopen_keeps_local_origin(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            bootstrap = ManagedBootstrap(settings)
            business = None
            try:
                business, draft, result = await self.initialized(bootstrap, root, simple=True)
                self.assertEqual(result, {'state': 'READY', 'startup_sends': 0, 'business_ready': True})
                host = business.host
                assert host is not None and host.current_persona is not None
                current = await host.current_persona.port.read_current(time.monotonic() + 5)
                self.assertIs(type(current), Available)
                assert type(current) is Available
                self.assertEqual(current.value['text'], role_text(draft['role_name'], draft['initial_material']))
                self.assertEqual(current.value['origin'], 'LOCAL_CONFIGURATION')
                self.assertEqual(current.value['publication_origin'], 'LOCAL_DEFAULT')
                self.assertEqual(current.value['review_status'], 'USER_CONFIRMED')
                self.assertFalse(business.sends_enabled)
                self.assert_no_requests(root)
                bindings = await host.assembly.ingress.registered_entries()
                self.assertEqual(len(bindings), 1)
                self.assertEqual(host.configured_entries(), (bindings[0]['entry_id'],))
                self.assertEqual((await business.identity.read_draft())['state'], 'COMPLETE')
                again = await business.initialize('initialize-role', 1)
                self.assertEqual(again, result)
                publication = current.value['publication_id']
            finally:
                await self.close(bootstrap, business)
            bootstrap = ManagedBootstrap(settings)
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                business = ManagedBusiness(bootstrap, identity)
                result = await business.recover()
                self.assertEqual(result['state'], 'READY')
                host = business.host
                assert host is not None and host.current_persona is not None
                current = await host.current_persona.port.read_current(time.monotonic() + 5)
                assert type(current) is Available
                self.assertEqual(current.value['publication_id'], publication)
                self.assertEqual(await host.assembly.ingress.registered_entries(), bindings)
                self.assertFalse(business.sends_enabled)
                self.assert_no_requests(root)
            finally:
                await self.close(bootstrap, business)

    async def test_waiting_instance_can_explicitly_use_local_role_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                business, _, result = await self.initialized(bootstrap, root)
                self.assertEqual(result['state'], 'AWAITING_REVIEW')
                first = await business.use_local_persona('confirm-local-role')
                self.assertIs(type(first), Committed)
                second = await business.use_local_persona('confirm-local-role')
                self.assertIs(type(second), Committed)
                assert type(first) is Committed and type(second) is Committed
                self.assertEqual(first.receipt, second.receipt)
                self.assertEqual(second.source, 'EXISTING')
                self.assertTrue(await business.business_ready())
                self.assertFalse(business.sends_enabled)
                host = business.host
                assert host is not None and host.current_persona is not None
                before = await host.current_persona.port.read_current(time.monotonic() + 5)
                self.assertIsNot(type(await business.use_local_persona('different-local-key')), Committed)
                self.assertEqual(await host.current_persona.port.read_current(time.monotonic() + 5), before)
            finally:
                await self.close(bootstrap, business)

    async def test_prepared_model_run_is_not_abandoned_by_local_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                business, _, _ = await self.initialized(bootstrap, root)
                host = business.host
                assert host is not None and host.runtime is not None
                prepared = await business.persona('prepare', {'key': 'prepare-model', 'self_revision': 1,
                    'epoch': host.runtime.gate.epoch})
                self.assertIs(type(prepared), Committed)
                pending = await business.persona('pending', {})
                self.assertIs(type(pending), Found)
                self.assertIsNot(type(await business.use_local_persona('local-after-prepare')), Committed)
                self.assertEqual(await business.persona('pending', {}), pending)
                self.assertFalse(await business.business_ready())
                self.assertEqual(await host.assembly.ingress.registered_entries(), ())
            finally:
                await self.close(bootstrap, business)

    async def test_synthetic_input_cannot_claim_operator_confirmed_local_origin(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                business, _, _ = await self.initialized(bootstrap, root, synthetic=True)
                self.assertIsNot(type(await business.use_local_persona('synthetic-local')), Committed)
                self.assertFalse(await business.business_ready())
            finally:
                await self.close(bootstrap, business)

    async def test_pointer_failure_rolls_back_local_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                business, _, _ = await self.initialized(bootstrap, root)
                with patch('companion_memory.self_model.unified_persona.initialize_pointer',
                        side_effect=OwnerFailure('STORAGE_FAILED', 'persona', 'INTEGRITY_FAILURE')):
                    failed = await business.use_local_persona('interrupted-local-role')
                self.assertIsNot(type(failed), Committed)
                self.assertFalse(await business.business_ready())
                host = business.host
                assert host is not None
                local = host.combination.initial_persona.local
                assert local is not None
                self.assertIsNone(await local.rows().read('local_persona_publications', local.publication_id()))
                self.assertEqual(await host.assembly.ingress.registered_entries(), ())
                self.assertIs(type(await business.use_local_persona('confirmed-local-role')), Committed)
                self.assert_no_requests(root)
            finally:
                await self.close(bootstrap, business)
