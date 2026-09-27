"""A published local role retains its original operation until setup is complete."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Committed
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from . import test_local_persona as support


class LocalPersonaRecoveryTests(unittest.IsolatedAsyncioTestCase):
    close = support.LocalPersonaTests.close
    initialized = support.LocalPersonaTests.initialized
    assert_no_requests = support.LocalPersonaTests.assert_no_requests

    async def test_failed_wizard_tail_keeps_original_local_publication_resumable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                business, _, initialized = await self.initialized(bootstrap, root)
                self.assertEqual(initialized['state'], 'AWAITING_REVIEW')
                with patch.object(business, 'complete_wizard', new=AsyncMock(return_value=False)) as finish:
                    pending = await business.use_local_persona('original-local-confirmation')
                finish.assert_awaited_once()
                self.assertEqual(pending, {'state': 'RECOVERING', 'cleanup_pending': True,
                    'startup_sends': 0, 'business_ready': False})
                assert type(pending) is dict
                # The browser's common original-operation helper regards any
                # top-level receipt as completed. Keep this action pending.
                self.assertNotIn('receipt', pending)
                self.assertNotIn('result', pending)
                self.assertEqual(bootstrap.state, 'RECOVERING')
                self.assertEqual((await business.identity.read_draft())['state'], 'AWAITING_REVIEW')
                self.assertTrue(await business.business_ready())
                self.assertIsNot(type(await business.use_local_persona('replacement-local-key')), Committed)
                resumed = await business.use_local_persona('original-local-confirmation')
                self.assertIs(type(resumed), Committed, resumed)
                assert type(resumed) is Committed
                self.assertEqual(resumed.source, 'EXISTING')
                self.assertEqual((await business.identity.read_draft())['state'], 'COMPLETE')
                self.assertEqual(bootstrap.state, 'READY')
                confirmed = await business.use_local_persona('original-local-confirmation')
                assert type(confirmed) is Committed
                self.assertEqual(confirmed.receipt, resumed.receipt)
                self.assertFalse(business.sends_enabled)
                self.assert_no_requests(root)
            finally:
                await self.close(bootstrap, business)
