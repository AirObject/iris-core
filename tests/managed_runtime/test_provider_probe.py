"""Explicit synthetic connection uses native Provider accounting at most once."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Ready, Committed
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.provider_probe import request, disclosure, SYNTHETIC_TEXT
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from tests.daily_cognition.test_reasoning import responses
from .test_business import setup_draft
from .semantic_support import semantic_resources
from .test_semantic_backup import EMBEDDING


class ProviderProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_native_attempt_original_confirmation_and_zero_startup_sends(self):
        with TemporaryDirectory() as directory, responses((EMBEDDING,)) as (port, requests, failures):
            root=Path(directory)
            settings=resolve_deployment({'deployment.data_root':str(root)})
            original=None
            for fresh in (True,False):
                bootstrap=ManagedBootstrap(settings)
                application=None
                try:
                    self.assertIs(type(await bootstrap.open()),Ready)
                    application=ManagedApplication(bootstrap)
                    application.business.resource_factory=semantic_resources(port)
                    if fresh:
                        await application.identity.save_draft('draft',None,setup_draft(root))
                        await application.business.initialize('initialize',1)
                    else:
                        await application.business.recover()
                    host=application.business.host
                    assert host is not None and host.provider is not None and host.network is not None
                    plan=disclosure(application)
                    supplied={'key':'one-connection','disclosure_digest':plan['digest']}
                    if fresh:
                        self.assertEqual(plan['material'],SYNTHETIC_TEXT)
                        self.assertFalse(requests)
                        self.assertIs(type(await application.business.set_dispatch('enable',None,True,str(application.business.dispatch_disclosure()['digest']))),Committed)
                        self.assertIs(type(await host.resume_learning('enable-learning')),Committed)
                        resumed=await request(application,'semantic-resume',{'key':'enable-semantic'})
                        self.assertIn('receipt',resumed)
                        await host.network.wait_quiet(time.monotonic()+60)
                        original=await request(application,'run',supplied)
                        self.assertEqual(original['state'],'APPLIED',original)
                        self.assertFalse(original['cleanup_pending'])
                    repeated=await request(application,'run',supplied)
                    self.assertEqual(repeated,original)
                    self.assertEqual(len(requests),1)
                    roots=await host.provider.ledger.read('requests_page',{'after':'','limit':8})
                    self.assertEqual(len(roots),1)
                    self.assertEqual(roots[0]['capability'],'EMBEDDING')
                    self.assertEqual(roots[0]['phase'],'TERMINAL')
                    self.assertFalse(failures)
                    if not fresh:self.assertFalse(application.business.sends_enabled)
                finally:
                    if application is not None:
                        self.assertTrue(await application.business.close())
                    self.assertTrue(await bootstrap.close())
