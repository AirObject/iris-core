"""Bootstrap-only product upgrades preserve drafts and fence interrupted switches.

An already started business remains on its exact old reader. No Provider rows,
old immutable configuration or original command receipts are rewritten.
"""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Ready, Committed
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_product_upgrade import ProductSetupUpgrade
from companion_memory.runtime.managed_resources import read_record
from .test_business import setup_draft


class ProductUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def create(self, root: Path, *, initializing: bool = False):
        settings=resolve_deployment({'deployment.data_root':str(root)})
        bootstrap=ManagedBootstrap(settings,product_format=False)
        self.assertIs(type(await bootstrap.open()),Ready)
        identity=bootstrap.assembly.identity;assert identity is not None
        resources=bootstrap.resources;assert resources is not None
        self.assertIs(type(await identity.establish('administrator',resources.read_secret('bootstrap').decode(),'synthetic-test-password')),Committed)
        draft=setup_draft(root)
        self.assertIs(type(await identity.save_draft('saved-draft',None,draft)),Committed)
        if initializing:self.assertIs(type(await identity.advance_wizard('start-business',1,'INITIALIZING','start-business')),Committed)
        saved=await identity.read_draft()
        self.assertTrue(await bootstrap.close())
        return settings,saved

    async def test_upgrade_preserves_identity_admin_draft_and_original_save_confirmation(self):
        for cut in ('none','after_fence','after_database','after_identity'):
            with TemporaryDirectory() as directory:
                root=Path(directory)
                settings,saved=await self.create(root)
                identity_before=read_record(root/'bootstrap/identity.json')
                automatic=ManagedBootstrap(settings)
                self.assertFalse(automatic.product_format)
                upgrade=ProductSetupUpgrade(settings)
                try:
                    prepared=await asyncio.to_thread(upgrade.prepare,'original-product-upgrade')
                    self.assertEqual(prepared['state'],'PREPARED')
                    if cut!='none':
                        def checkpoint(point):
                            if point==cut:raise RuntimeError('injected interrupted switch')
                        with self.assertRaises(RuntimeError):
                            await asyncio.to_thread(upgrade.activate,'original-product-upgrade',checkpoint=checkpoint)
                    active=await asyncio.to_thread(upgrade.activate,'original-product-upgrade')
                    self.assertEqual(active['state'],'ACTIVE')
                finally:upgrade.close()
                identity_after=read_record(root/'bootstrap/identity.json')
                for key in ('database_id','instance_id','authority_id'):self.assertEqual(identity_after[key],identity_before[key])
                opened=ManagedBootstrap(settings)
                self.assertTrue(opened.product_format)
                try:
                    self.assertIs(type(await opened.open()),Ready)
                    identity=opened.assembly.identity;assert identity is not None
                    self.assertEqual(await identity.read_draft(),saved)
                    repeated=await identity.save_draft('saved-draft',None,setup_draft(root))
                    self.assertIs(type(repeated),Committed)
                    assert type(repeated) is Committed
                    self.assertEqual(repeated.source,'EXISTING')
                finally:self.assertTrue(await opened.close())

    async def test_initializing_wizard_refuses_upgrade_and_old_default_reader_still_opens(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);settings,_=await self.create(root,initializing=True)
            upgrade=ProductSetupUpgrade(settings)
            try:
                with self.assertRaisesRegex(ValueError,'unstarted wizard'):
                    await asyncio.to_thread(upgrade.prepare,'refused-upgrade')
            finally:upgrade.close()
            bootstrap=ManagedBootstrap(settings)
            self.assertFalse(bootstrap.product_format)
            try:self.assertIs(type(await bootstrap.open()),Ready)
            finally:self.assertTrue(await bootstrap.close())

    async def test_persisted_business_configuration_is_not_reinterpreted_as_product(self):
        from companion_memory.configuration.managed_registry import resolve_values
        from companion_memory.configuration.managed_resolution import ManagedConfigurationOk
        from companion_memory.configuration.managed_persistent_results import ConfigurationCommitted
        from companion_memory.configuration.managed_persistence import ManagedConfigurationAssembly
        with TemporaryDirectory() as directory:
            root=Path(directory);settings,_=await self.create(root)
            bootstrap=ManagedBootstrap(settings)
            self.assertFalse(bootstrap.product_format)
            self.assertIs(type(await bootstrap.open()),Ready)
            resources=bootstrap.resources;assert resources is not None
            draft=setup_draft(root)
            candidate=resolve_values(settings,resources.protected_directories(),'sample_platform',draft['configuration'])
            assert type(candidate) is ManagedConfigurationOk
            configuration=bootstrap.assembly.configuration
            assert type(configuration) is ManagedConfigurationAssembly
            owner=configuration.bind(bootstrap.assembly.storage,resources.instance_id,candidate.value)
            try:
                committed=await owner.persist_managed_configuration('original-business-configuration',candidate.value,
                    actor='bootstrap',protected_directories=resources.protected_directories())
                self.assertIs(type(committed),ConfigurationCommitted,committed)
            finally:
                owner.close()
                self.assertTrue(await bootstrap.close())
            upgrade=ProductSetupUpgrade(settings)
            try:
                with self.assertRaisesRegex(ValueError,'Existing business data'):
                    await asyncio.to_thread(upgrade.prepare,'refused-business-upgrade')
            finally:upgrade.close()
