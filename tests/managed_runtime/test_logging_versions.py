"""Actual logger file generations across configuration failure and recovery."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Ready, Committed
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_business import ManagedBusiness
from companion_memory.runtime.managed_logging import ManagedLogging
from .test_business import setup_draft, synthetic_resources


class LoggingVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sink_preparation_failure_publication_recovery_and_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            active_id = None
            for fresh in (True, False):
                bootstrap = ManagedBootstrap(settings)
                logger = ManagedLogging()
                business = None
                try:
                    self.assertIs(type(await bootstrap.open()), Ready)
                    logger.open(bootstrap)
                    identity = bootstrap.assembly.identity
                    assert identity is not None
                    business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                    business.logging = logger
                    if fresh:
                        draft = setup_draft(root)
                        draft['configuration']['foundation']['logging.console_level'] = 'WARNING'
                        await identity.save_draft('draft', None, draft)
                        await business.initialize('initialize', 1)
                    else:
                        await business.recover()
                    manager = business.configuration
                    assert manager is not None and logger.window is not None
                    self.assertTrue(logger.current.ready)
                    self.assertEqual(logger.current.version_id, manager.work.versions.active.version_id)
                    self.assertEqual(logger.service.get_sink_health().sinks[0].threshold, 30 if fresh else 40)
                    if not fresh:
                        self.assertEqual(logger.current.version_id, active_id)
                        self.assertFalse(business.sends_enabled)
                        continue
                    old_service, old_window = logger.service, logger.window
                    change: dict[str, object] = {'foundation': {'logging.console_level': 'ERROR'}}
                    _, plan = await manager.preview(0, change)
                    self.assertEqual(plan['changes'][0]['boundary'], 'LOGGER_REBUILD')
                    saved = await manager.save('logger-failed', 0, change, plan['plan_digest'], actor='test-operator', reason='合成日志资源故障')
                    self.assertIs(type(saved), Committed)
                    failed_id, _ = manager.versions.ids('logger-failed')
                    initialize = logger._initialize
                    failed = False
                    def fail_once(generation):
                        nonlocal failed
                        if not failed:
                            failed = True
                            raise OSError('synthetic log resource failure')
                        initialize(generation)
                    with patch.object(logger, '_initialize', side_effect=fail_once):
                        result = await manager.activate(failed_id)
                    self.assertEqual(result['state'], 'PREPARATION_FAILED', result)
                    self.assertFalse(result['cleanup_pending'])
                    self.assertFalse((await manager.status())['admission_closed'])
                    self.assertEqual(logger.service.get_sink_health().sinks[0].threshold, 30)
                    self.assertEqual(old_service.get_sink_health().lifecycle, 'CLOSED')
                    self.assertIsNot(logger.window, old_window)
                    saved = await manager.save('logger-success', 0, change, plan['plan_digest'], actor='test-operator', reason='合成日志版本切换')
                    self.assertIs(type(saved), Committed)
                    aid, active_id = manager.versions.ids('logger-success')
                    with patch.object(logger, 'publish', side_effect=OSError('synthetic publication interruption')):
                        result = await manager.activate(aid)
                    self.assertEqual(result['state'], 'RECOVERING', result)
                    status = await manager.status()
                    self.assertEqual(status['authoritative_version'], active_id)
                    self.assertTrue(status['admission_closed'])
                    self.assertIsNotNone(logger.pending)
                    self.assertEqual((await manager.activate(aid))['state'], 'APPLIED')
                    self.assertEqual(logger.current.version_id, active_id)
                    self.assertEqual(logger.service.get_sink_health().sinks[0].threshold, 40)
                finally:
                    if business is not None:
                        self.assertTrue(await business.close())
                    self.assertTrue(await logger.close())
                    self.assertTrue(await bootstrap.close())
