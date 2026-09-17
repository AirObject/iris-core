"""Paid Provider facts survive an ended HTTP grant; new business writes do not."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Ready, Committed, Found
from companion_memory.memory.formats import record
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_business import ManagedBusiness
from companion_memory.provider.values import as_record
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .test_business import setup_draft, controlled_resources


class LateProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_paid_result_settles_after_request_permission_ends(self):
        allowed = True
        def response(body):
            nonlocal allowed
            allowed = False
            return persona(body)
        with TemporaryDirectory() as directory, responses((response,)) as (port, requests, failures):
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                business = ManagedBusiness(bootstrap, identity, resource_factory=controlled_resources(port))
                await identity.save_draft('draft', None, setup_draft(root))
                await business.initialize('initialize', 1)
                host = business.host
                assert host is not None and host.runtime is not None and host.provider is not None
                await business.set_dispatch('enable', None, True, str(business.dispatch_disclosure()['digest']))
                self.assertIs(type(await business.persona('prepare', {'key':'prepare', 'self_revision':1, 'epoch':host.runtime.gate.epoch})), Committed)
                pending = await business.persona('pending', {})
                assert type(pending) is Found
                run = record(pending.value['run'])
                assert identity.lease is not None
                with bootstrap.assembly.storage.managed_request_permission(identity.lease, identity.catalog.definition,
                        lambda uow: allowed):
                    await business.persona('generate', {'key':run['provider_operation_key'], 'generation':run['generation']})
                    await host.combination.initial_persona.control.wait_actual()
                    # No new scheduling write receives the ended request's authority.
                    from tests.runtime.configuration_support import event
                    supplied = event('after-expiry', '合成过期身份的新输入')
                    supplied['event_version'] = 2
                    refused = await host.bind_entry('entry').accept_event('after-expiry', supplied)
                    self.assertIsNot(type(refused), Committed, refused)
                provider = host.provider
                stored = await provider.ledger.read('requests_page', {'after':'','limit':8})
                self.assertEqual(len(stored), 1, stored)
                self.assertEqual(stored[0]['phase'], 'TERMINAL')
                self.assertEqual(stored[0]['outcome'], 'SUCCEEDED')
                self.assertEqual(len(requests), 1)
                attempts = await provider.ledger.read('attempts_for_request', {'request_id':stored[0]['object_id']})
                self.assertEqual(attempts[0]['state'], 'COMPLETED')
                self.assertEqual(as_record(attempts[0]['usage'])['quota_held'], 0)
                self.assertFalse(failures)
                assert provider.ledger.lease is not None
                with self.assertRaises(ValueError):
                    await provider.storage.reconcile_managed_provider(provider.ledger.lease,
                        provider.chat_operations['register_daily_request'], 'forbidden-registration', object())
                # A new process uses its own local recovery authority to receive
                # the original paid candidate; it never retries its transport.
                self.assertTrue(await business.close())
                self.assertTrue(await bootstrap.close())
                bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root':str(root)}))
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                business = ManagedBusiness(bootstrap,identity,resource_factory=controlled_resources(port))
                await business.recover()
                pending = await business.persona('pending', {})
                assert type(pending) is Found
                recovered = record(pending.value['run'])
                self.assertEqual(recovered['state'],'WAITING_REVIEW')
                self.assertIsNotNone(pending.value['candidate'])
                self.assertEqual(len(requests),1)
                self.assertFalse(business.sends_enabled)
            finally:
                if business is not None:
                    self.assertTrue(await business.close())
                self.assertTrue(await bootstrap.close())
