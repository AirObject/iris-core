"""Temporary network ownership is distinct from timeout and unknown outcomes."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from companion_memory.persistence import Found
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.daily_network import DailyNetwork
from .host_support import make_dream_host


class AdmissionContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_observes_physical_owner_confirmation_cooldown_and_account_fence(self):
        clock=[100.0];network=DailyNetwork(lambda key:True,('account',),monotonic=lambda:clock[0])
        release=threading.Event();entered=threading.Event()
        def exchange():
            entered.set()
            if not release.wait(3):raise AssertionError('Missing release')
        try:
            network.resume();deadline=time.monotonic()+10
            permit=network.reserve('request','key','account',200.0,consumer_required=True)
            self.assertEqual(network.admission_state(deadline),'ORIGINAL_REQUEST_UNCONFIRMED')
            future=network.start(permit,exchange)
            async with asyncio.timeout(2):
                while not entered.is_set():await asyncio.sleep(.005)
            self.assertEqual(network.admission_state(deadline),'NETWORK_OCCUPIED')
            self.assertTrue(network.observation().io_pending)
            with self.assertRaises(OwnerFailure) as failure:network.admission_state(time.monotonic()-1)
            self.assertEqual(failure.exception.code,'TIMEOUT');self.assertTrue(network.observation().occupied)
            release.set();await future;await network.wait_io(permit)
            self.assertEqual(network.admission_state(deadline),'ORIGINAL_REQUEST_UNCONFIRMED')
            network.confirm_terminal(permit)
            self.assertEqual(network.admission_state(deadline),'NETWORK_OCCUPIED')
            self.assertTrue(network.observation().consumer_cleanup_pending)
            network.confirm_consumed(permit)
            self.assertEqual(network.admission_state(deadline),'NETWORK_COOLDOWN')
            self.assertFalse(network.observation().occupied);self.assertEqual(network.observation().quiet_until,130)
            clock[0]=130;self.assertEqual(network.admission_state(deadline),'READY')
            network.block_account('account')
            with self.assertRaises(OwnerFailure) as failure:network.reserve('next','next','account',200)
            self.assertEqual(failure.exception.reason,'REMOTE_RESULT_UNKNOWN')
            network.pause()
            with self.assertRaises(OwnerFailure):network.admission_state(deadline)
        finally:release.set();network.close()

    async def test_scheduler_retries_only_pre_registration_admission_race(self):
        with TemporaryDirectory() as directory:
            host=make_dream_host(Path(directory),9,[])
            scheduler=host.dream_scheduler
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                for code,field,reason,retry in (
                    ('RESOURCE_BUSY','resource','ADMISSION_FULL',True),
                    ('TIMEOUT','request','DEADLINE_EXCEEDED',False),
                    ('RESULT_UNCONFIRMED','exit','COMMIT_UNCONFIRMED',False),
                    ('INVALID_STATE','account','REMOTE_RESULT_UNKNOWN',False)):
                    with self.subTest(reason=reason):
                        calls=[]
                        async def tick():
                            calls.append(1)
                            if len(calls)==1:raise OwnerFailure(code,field,reason)
                            scheduler.stop()
                        scheduler.tick=tick;scheduler.enabled=True;scheduler.failure=None
                        await asyncio.wait_for(scheduler.run(),3)
                        self.assertEqual(len(calls),2 if retry else 1)
                        self.assertEqual(scheduler.failure,None if retry else reason)
            finally:self.assertTrue(await host.close())
