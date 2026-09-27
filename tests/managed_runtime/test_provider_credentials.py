"""Both Provider keys rotate through native activation without sending a model.

The actual SQLite configuration owner, consumers and protected key resources
are exercised. A retained embedding request and older execution version keep
their original key after activation; recovery uses only the original operation.
"""
import asyncio
from hashlib import sha256
from types import MappingProxyType
import time
import unittest
from unittest.mock import patch

from companion_memory.management.managed_application import ManagedApplication
from companion_memory.configuration.deployment import DeploymentSettings
from companion_memory.persistence import Ready
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from . import test_simple_setup as support


class ProviderCredentialTests(unittest.IsolatedAsyncioTestCase):
    settings: DeploymentSettings
    session: dict[str, str]
    asyncSetUp = support.SimpleSetupTests.asyncSetUp
    asyncTearDown = support.SimpleSetupTests.asyncTearDown
    api = support.SimpleSetupTests.api
    payload = support.SimpleSetupTests.payload

    async def initialize(self):
        await self.api('/api/setup/save', self.payload())
        result = await self.application.business.initialize('initialize', 1)
        assert type(result) is dict
        if result['state'] == 'RECOVERING':
            assert self.application.business.task is not None
            result = await self.application.business.task
        assert type(result) is dict
        self.assertEqual(result['state'], 'READY', result)
        business = self.application.business
        if business.goal_scheduler is not None:
            self.assertTrue(await business.goal_scheduler.close(5))
        return business

    async def test_two_keys_activate_keep_frozen_versions_and_recover_without_resending(self):
        business = await self.initialize()
        host, manager = business.host, business.configuration
        assert host is not None and host.provider is not None and manager is not None
        provider = host.provider
        versions = provider.managed_versions
        assert versions is not None
        birth = manager.work.versions.birth
        original_embedding = versions.embedding_transport(birth)
        assert original_embedding is not None
        material = 'Synthetic retained query.'
        work = MappingProxyType({'kind': 'EMBED', 'space_id': provider.space,
            'config': MappingProxyType({'database_id': provider.configuration.database_id,
                'instance_id': provider.instance, 'snapshot_id': provider.configuration.snapshot_id}),
            'material_digest': sha256(material.encode()).hexdigest(), 'purpose': 'QUERY',
            'work_id': 'synthetic-work', 'original_request_key': 'synthetic-query'})
        frozen = provider.request(work, material, time.time_ns() // 1000 + 60000000)
        original_generation = versions.transports(birth)['LEARNING']
        changed = {'key': 'replace-keys', 'generation_api_key': 'synthetic-rotated-generation',
            'embedding_api_key': 'synthetic-rotated-embedding'}
        result = await self.api('/api/setup/providers', changed)
        self.assertEqual((result['state'], result['activation_state']), ('CONFIRMED', 'APPLIED'), result)
        self.assertEqual(result['revision'], 1)
        active = manager.work.versions.active
        self.assertNotEqual(active.version_id, birth.version_id)
        next_embedding = versions.embedding_transport(active)
        assert next_embedding is not None and frozen.transport is not None
        self.assertNotEqual(next_embedding._settings['secret_ref'], original_embedding._settings['secret_ref'])
        self.assertEqual(frozen.execution_version, birth)
        self.assertEqual(frozen.transport._settings['secret_ref'], original_embedding._settings['secret_ref'])
        retained_embedding = versions.embedding_transport(birth)
        assert retained_embedding is not None
        self.assertEqual(retained_embedding._settings, original_embedding._settings)
        self.assertNotEqual(versions.transports(active)['LEARNING']._settings['secret_ref'], original_generation._settings['secret_ref'])
        # The image role shares generation credentials and must also rotate.
        self.assertEqual(versions.transports(active)['MEDIA']._settings['secret_ref'],
            versions.transports(active)['LEARNING']._settings['secret_ref'])
        new_request = provider.request(work, material, time.time_ns() // 1000 + 60000000)
        assert new_request.transport is not None
        self.assertEqual(new_request.execution_version, active)
        self.assertEqual(new_request.transport._settings['secret_ref'], next_embedding._settings['secret_ref'])
        self.assertFalse(business.sends_enabled)
        self.assertEqual(await provider.ledger.read('requests_page', {'after': '', 'limit': 8}), ())
        repeated = await self.api('/api/setup/providers/operation', {'key': 'replace-keys'})
        self.assertEqual(repeated['receipt'].commit_id, result['receipt'].commit_id)
        with self.assertRaises(OwnerFailure) as conflict:
            await self.api('/api/setup/providers', {**changed, 'generation_api_key': 'different'})
        self.assertEqual(conflict.exception.reason, 'CONTENT_MISMATCH')
        self.assertTrue(await business.close())
        self.assertTrue(await self.bootstrap.close())
        self.bootstrap = ManagedBootstrap(self.settings)
        self.assertIs(type(await self.bootstrap.open()), Ready)
        self.application = ManagedApplication(self.bootstrap)
        recovered = await self.application.business.recover()
        if recovered['state'] == 'RECOVERING':
            assert self.application.business.task is not None
            recovered = await self.application.business.task
        self.assertEqual(recovered['state'], 'READY', recovered)
        self.identity = self.application.identity
        self.principal = await self.identity.authenticate(self.session['session'], host=False)
        status = await self.api('/api/setup')
        self.assertTrue(status['providers']['generation']['configured'])
        self.assertTrue(status['providers']['embedding']['configured'])
        confirmed = await self.api('/api/setup/providers/operation', {'key': 'replace-keys'})
        self.assertEqual(confirmed['state'], 'CONFIRMED')
        self.assertFalse(self.application.business.sends_enabled)

    async def test_interrupted_activation_is_not_confirmed_and_original_operation_finishes_it(self):
        business = await self.initialize()
        manager = business.configuration
        assert manager is not None
        consumer = manager.coordinator.consumers['retrieval']
        with patch.object(consumer, 'publish', side_effect=OSError('synthetic publication interruption')):
            result = await self.api('/api/setup/providers', {'key': 'interrupted', 'embedding_api_key': 'synthetic-updated'})
        self.assertEqual(result['state'], 'RECOVERING')
        self.assertTrue(result['cleanup_pending'])
        original = await self.api('/api/setup/providers/operation', {'key': 'interrupted'})
        self.assertEqual(original['state'], 'CONFIRMED', original)
        self.assertFalse(original['cleanup_pending'])
        self.assertEqual((await self.api('/api/setup/providers/operation', {'key': 'unknown'}))['state'], 'ABSENT')
        self.assertFalse(business.sends_enabled)


if __name__ == '__main__':
    unittest.main()
