"""Real immutable configuration publication and reopening with original evidence."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from companion_memory.configuration import RuntimeConfigurationOk
from companion_memory.configuration.persistence import ConfigurationAssembly
from companion_memory.configuration.persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready
from tests.runtime.configuration_support import candidate


class PersistentConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_timeout_keeps_configuration_owner_after_upper_task_ended(self):
        import asyncio
        import threading
        from companion_memory.configuration import RuntimeConfigurationErr
        from tests.runtime.support import Fixture
        with TemporaryDirectory(prefix='iris-config-storage-deadline-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'runtime.operation_timeout_ms':5000})
            await fixture.initialize()
            binding=fixture.config_binding;assert binding is not None
            entered=threading.Event();release=threading.Event();armed=True
            def block(sql):
                nonlocal armed
                if armed and 'FROM configuration_entries' in sql:
                    armed=False;entered.set();release.wait(10)
            fixture.hooks.before=block
            try:
                task=asyncio.create_task(binding.load_configuration_snapshot('configuration:1',fixture.candidate.material_contracts,fixture.supplied[3],bootstrap=fixture.candidate))
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                result=await task;self.assertIs(type(result),RuntimeConfigurationErr,result)
                await asyncio.sleep(0)
                self.assertFalse(binding._tasks)
                self.assertTrue(fixture.storage.get_health().cleanup_pending)
                self.assertFalse(binding.close())
                self.assertTrue(binding._lease.is_active())
                self.assertIsNone(fixture.storage.claim_module_owner(fixture.config_assembly.repository.definition))
                release.set()
                async with asyncio.timeout(3):
                    while fixture.storage.get_health().reads_in_flight:await asyncio.sleep(0.01)
                # The timed-out storage is FAULTED and still owns its path.
                # Explicitly complete borrowed resource cleanup before release.
                self.assertFalse(binding.close())
                assert fixture.runtime is not None
                await fixture.runtime.close()
                await fixture.provider.close()
                await fixture.storage.close()
                self.assertTrue(binding.close())
            finally:release.set();fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_complete_configuration_reopens_under_same_persistent_identity(self):
        with TemporaryDirectory(prefix='iris-runtime-config-store-') as temporary:
            root=Path(temporary).resolve()
            selected,supplied=candidate(root)
            path=root/'database'/'runtime.sqlite3'
            resources=DatabaseResources('configuration-database',lambda identity,target:identity=='configuration-database' and target==str(path))
            snapshot_id=None
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                assembly=ConfigurationAssembly()
                storage=PersistenceService(assembly.repositories,assembly.commands)
                binding=None
                try:
                    ready=await storage.initialize(selected.foundation,resources,mode)
                    self.assertIs(type(ready),Ready,ready)
                    binding=assembly.bind(storage,'instance')
                    result=await binding.persist_initial_configuration('original-config',selected,actor='bootstrap',protected_directories=supplied[3])
                    self.assertIs(type(result),ConfigurationCommitted,result)
                    assert type(result) is ConfigurationCommitted and result.configuration is not None
                    if snapshot_id is None:snapshot_id=result.configuration.snapshot_id
                    self.assertEqual(result.configuration.snapshot_id,snapshot_id)
                    self.assertEqual(result.configuration.candidate.runtime.integer('ingress.event_max_bytes'),1024)
                    self.assertEqual(result.configuration.revisions[:2],(('foundation',1),('runtime',1)))
                    self.assertEqual(len(result.configuration.revisions),3)
                    self.assertEqual(result.configuration.revisions[2][1],1)
                    self.assertEqual(result.configuration.candidate.platforms[0].platform_id,'sample_platform')
                finally:
                    if binding:binding.close()
                    await storage.close()

    async def test_whole_configuration_load_timeout_keeps_its_owner_until_reads_end(self):
        import asyncio
        import threading
        from companion_memory.configuration import RuntimeConfigurationErr
        from tests.runtime.support import Fixture
        with TemporaryDirectory(prefix='iris-config-load-owner-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'runtime.operation_timeout_ms':500})
            await fixture.initialize()
            entered=threading.Event();release=threading.Event()
            binding=fixture.config_binding;assert binding is not None
            armed=True
            def wait(sql):
                nonlocal armed
                if armed and 'FROM configuration_entries' in sql:
                    armed=False;entered.set();release.wait(5)
            fixture.hooks.before=wait
            try:
                task=asyncio.create_task(binding.load_configuration_snapshot('configuration:1',fixture.candidate.material_contracts,fixture.supplied[3],bootstrap=fixture.candidate))
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                result=await task;self.assertIs(type(result),RuntimeConfigurationErr,result)
                self.assertFalse(binding.close())
                self.assertTrue(fixture.storage.get_health().reads_in_flight)
                release.set()
                async with asyncio.timeout(3):
                    while binding._tasks or fixture.storage.get_health().reads_in_flight:await asyncio.sleep(0.01)
                self.assertTrue(binding.close())
            finally:release.set();fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_committed_initialization_remains_committed_when_full_view_is_unavailable(self):
        import sqlite3
        from companion_memory.configuration import RuntimeConfigurationErr
        from companion_memory.configuration.persistent_results import ConfigurationRejected
        from tests.runtime.support import Fixture
        from tests.persistence.support import sqlite_fault
        with TemporaryDirectory(prefix='iris-config-commit-evidence-') as directory:
            fixture=Fixture(Path(directory));await fixture.initialize()
            binding=fixture.config_binding;assert binding is not None
            try:
                def fail(sql):
                    if 'FROM configuration_entries' in sql:raise sqlite_fault(sqlite3.SQLITE_BUSY)
                fixture.hooks.before=fail
                result=await binding.persist_initial_configuration('original-config',fixture.candidate,actor='bootstrap',protected_directories=fixture.supplied[3])
                self.assertIs(type(result),ConfigurationCommitted,result)
                assert type(result) is ConfigurationCommitted and result.error is not None
                self.assertIsNone(result.configuration)
                self.assertEqual(result.source,'EXISTING')
                self.assertEqual(result.error.reason,'READ_FAILED')
                fixture.hooks.before=lambda sql:None
                recovered=await binding.persist_initial_configuration('original-config',fixture.candidate,actor='bootstrap',protected_directories=fixture.supplied[3])
                assert type(recovered) is ConfigurationCommitted and recovered.configuration is not None,recovered
                self.assertEqual(recovered.receipt,result.receipt)
                conflict=await binding.persist_initial_configuration('original-config',fixture.candidate,actor='other_actor',protected_directories=fixture.supplied[3])
                assert type(conflict) is ConfigurationRejected,conflict
                self.assertEqual(conflict.error.reason,'CONTENT_MISMATCH')
                missing=await binding.load_configuration_snapshot('configuration:999',fixture.candidate.material_contracts,fixture.supplied[3],bootstrap=fixture.candidate)
                assert type(missing) is RuntimeConfigurationErr,missing
                self.assertEqual(missing.error.reason,'VERSION_MISSING')
            finally:fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_maximum_platform_identity_restores_complete_long_parameter_keys(self):
        with TemporaryDirectory(prefix='iris-config-long-platform-') as directory:
            root=Path(directory).resolve();identity='p'*128
            selected,supplied=candidate(root,platform_ids=(identity,))
            path=root/'database'/'runtime.sqlite3'
            resources=DatabaseResources('long-platform-database',lambda retained,target:retained=='long-platform-database' and target==str(path))
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                assembly=ConfigurationAssembly();storage=PersistenceService(assembly.repositories,assembly.commands);binding=None
                try:
                    ready=await storage.initialize(selected.foundation,resources,mode);assert type(ready) is Ready,ready
                    binding=assembly.bind(storage,'instance')
                    published=await binding.persist_initial_configuration('original',selected,actor='bootstrap',protected_directories=supplied[3])
                    assert type(published) is ConfigurationCommitted and published.configuration is not None,published
                    restored=published.configuration.candidate.platform(identity)
                    self.assertEqual(restored.platform_id,identity)
                    self.assertEqual(restored.count('target_count'),2)
                    self.assertTrue(all(len(key)<=128 for key,_ in published.configuration.revisions))
                finally:
                    if binding:binding.close()
                    await storage.close()
