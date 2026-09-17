"""Actual temporary SQLite control, arbitration and original-key reopening."""
import tempfile
import unittest
from pathlib import Path
from typing import cast

from companion_memory.configuration.dream_resolution import DreamConfigurationOk, resolve_dream_configuration
from companion_memory.configuration.dream_persistence import DreamConfigurationAssembly
from companion_memory.configuration.dream_persistent_results import ConfigurationCommitted
from companion_memory.dream.control import DreamControl
from companion_memory.persistence import Committed, DatabaseResources, PersistenceService, Ready
from companion_memory.persistence.owned_statements import OwnerFailure
from .configuration_support import inputs


class DreamRunStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_controls_arbitrate_and_reopen_without_enabling_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            supplied = inputs(root)
            resolved = resolve_dream_configuration(*supplied)
            if type(resolved) is not DreamConfigurationOk:
                raise AssertionError(resolved)
            clock = [1789531200000000]
            original = None
            for mode in ('CREATE_NEW', 'OPEN_EXISTING'):
                configuration = DreamConfigurationAssembly()
                control = DreamControl()
                storage = PersistenceService(configuration.repositories + (control.catalog.definition,),
                    configuration.commands + control.commands, assembly_format='DREAM_MAINTENANCE_V1')
                resources = DatabaseResources('dream-database', lambda identity, path:
                    identity == 'dream-database' and path == str(root / 'database' / 'runtime.sqlite3'))
                self.assertIsInstance(await storage.initialize(resolved.value.foundation, resources, mode), Ready)
                publisher = configuration.bind(storage, 'instance', resolved.value)
                try:
                    stored = await publisher.persist_dream_configuration('configuration', resolved.value,
                        actor='bootstrap', protected_directories=supplied[6])
                    if type(stored) is not ConfigurationCommitted or stored.configuration is None:
                        raise AssertionError(stored)
                    control.bind(storage, stored.configuration, checkpoint=lambda: None, now=lambda: clock[0])
                    self.assertFalse(control.dispatch_enabled)
                    if mode == 'CREATE_NEW':
                        self.assertIsInstance(await control.execute('initialize_dream_control', 'initialize', {}, actor='admin'), Committed)
                        started = await control.execute('start_background_dream', 'start', {
                            'run_id': 'first-run', 'expected_revision': 1, 'mode_epoch': 1,
                            'trigger': 'SCHEDULED', 'local_date': '2026-09-16'}, actor='admin')
                        self.assertIsInstance(started, Committed)
                        self.assertFalse(control.dispatch_enabled)
                        with self.assertRaises(OwnerFailure) as collision:
                            await control.execute('start_background_dream', 'competing-start', {
                                'run_id': 'competing-run', 'expected_revision': 2, 'mode_epoch': 1,
                                'trigger': 'MANUAL', 'local_date': None}, actor='admin')
                        self.assertEqual(collision.exception.reason, 'RUN_ACTIVE')
                        original = await control.execute('resume_dream', 'resume', {
                            'run_id': 'first-run', 'expected_revision': 1, 'mode_epoch': 1}, actor='admin')
                        self.assertIsInstance(original, Committed)
                        self.assertTrue(control.dispatch_enabled)
                        paused = await control.execute('pause_dream', 'pause', {
                            'run_id': 'first-run', 'expected_revision': 2, 'mode_epoch': 1}, actor='admin')
                        self.assertIsInstance(paused, Committed)
                        self.assertFalse(control.dispatch_enabled)
                    else:
                        confirmed = await control.confirm('resume_dream', 'resume')
                        if type(confirmed) is not Committed or type(original) is not Committed:
                            raise AssertionError(confirmed)
                        self.assertEqual(confirmed.receipt, original.receipt)
                        self.assertFalse(control.dispatch_enabled)
                        current = await control.inspect('first-run')
                        if current is None:
                            raise AssertionError('Original run missing')
                        deadline = current['deadline_at_us']
                        clock[0] += 1000000
                        resumed = await control.execute('resume_dream', 'resume-after-open', {
                            'run_id': 'first-run', 'expected_revision': 3, 'mode_epoch': 1}, actor='admin')
                        self.assertIsInstance(resumed, Committed)
                        current = await control.inspect('first-run')
                        if current is None:
                            raise AssertionError('Original run missing')
                        self.assertEqual(current['deadline_at_us'], deadline)
                        self.assertEqual(current['model_calls_used'], 0)
                        aborted = await control.execute('abort_background_dream', 'abort', {
                            'run_id': 'first-run', 'expected_revision': 4, 'mode_epoch': 1}, actor='admin')
                        self.assertIsInstance(aborted, Committed)
                        current = await control.inspect('first-run')
                        if current is None:
                            raise AssertionError('Original run missing')
                        self.assertEqual(current['state'], 'ABORTED')
                        self.assertEqual(current['exit_result'], 'ABORTED_SAFELY')
                        self.assertFalse(control.dispatch_enabled)
                        schedule = await control.schedule()
                        if schedule is None:
                            raise AssertionError('Original schedule missing')
                        self.assertIsNone(schedule['active_run_id'])
                        self.assertEqual(schedule['last_local_date'], '2026-09-16')
                        with self.assertRaises(OwnerFailure):
                            await control.execute('start_background_dream', 'duplicate-date', {
                                'run_id': 'duplicate-date-run', 'expected_revision': cast(int, schedule['revision']),
                                'mode_epoch': 1, 'trigger': 'SCHEDULED', 'local_date': '2026-09-16'}, actor='admin')
                    print({'scope': 'DREAM_CONTROL_OWNER_ONLY', 'storage': 'ACTUAL_SQLITE',
                           'mode': mode, 'model_sends': 0, 'host_integration': False})
                finally:
                    self.assertTrue(control.close())
                    self.assertTrue(publisher.close())
                    await storage.close()
                    self.assertEqual(storage.get_health().lifecycle, 'CLOSED')
