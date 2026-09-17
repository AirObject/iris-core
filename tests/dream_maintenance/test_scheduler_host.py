"""Native local-date scheduling, duplicate ticks, pause fences and zero-send open."""
import asyncio
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.dream_clock import utc_microseconds
from .host_support import make_dream_host


class DreamSchedulerHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_date_commits_once_and_pause_cannot_be_reopened_by_tick(self):
        with TemporaryDirectory() as directory:
            credentials=[];host=make_dream_host(Path(directory),9,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                c=host.combination.dream
                if c is None or host.runtime is None:raise AssertionError('Native owner missing')
                c.now=lambda:utc_microseconds(datetime(2026,9,16,12,tzinfo=timezone.utc))
                self.assertIsNone(host.dream_scheduler.task);self.assertFalse(c.dispatch_enabled)
                with self.assertRaises(OwnerFailure):
                    await c.execute('start_background_dream','future',{'run_id':'future-run','expected_revision':1,'mode_epoch':1,
                        'trigger':'SCHEDULED','local_date':'2026-09-17'},actor='admin')
                initial=await c.schedule()
                if initial is None:raise AssertionError('Missing schedule')
                self.assertIsNone(initial['active_run_id'])
                host.enable_dream_schedule()
                for _ in range(200):
                    if c.dispatch_enabled:break
                    await asyncio.sleep(.01)
                self.assertTrue(c.dispatch_enabled,host.dream_scheduler.failure)
                root=await c.schedule()
                if root is None:raise AssertionError('Missing schedule')
                run=await c.inspect(cast(str,root['active_run_id']))
                if run is None:raise AssertionError('Missing run')
                self.assertEqual(run['local_date'],'2026-09-16');self.assertEqual(run['trigger'],'SCHEDULED')
                self.assertEqual(host.runtime.gate.state,'DREAM_FOCUSED')
                paused=await c.execute('pause_dream','admin-pause',{'run_id':run['run_id'],
                    'expected_revision':run['revision'],'mode_epoch':run['mode_epoch']},actor='admin')
                self.assertIs(type(paused),Committed)
                for _ in range(3):await host.dream_scheduler.tick()
                after=await c.inspect(cast(str,run['run_id']));schedule=await c.schedule()
                if after is None or schedule is None:raise AssertionError('Missing durable run')
                self.assertFalse(c.dispatch_enabled);self.assertEqual(after['state'],'PAUSED')
                self.assertEqual(schedule['revision'],root['revision']);self.assertFalse(credentials)
                host.pause_dream_schedule()
                if host.dream_scheduler.task is None:raise AssertionError('Missing actual scheduler')
                await host.dream_scheduler.task
            finally:
                host.pause_dream_schedule()
                if host.dream_scheduler.task is not None:await host.dream_scheduler.task
                self.assertTrue(await host.close())
            second=make_dream_host(Path(directory),9,credentials)
            try:
                self.assertIs(type(await second.initialize('OPEN_EXISTING')),Found)
                self.assertIsNone(second.dream_scheduler.task);self.assertFalse(second.dream_scheduler.enabled)
                self.assertFalse(credentials)
            finally:self.assertTrue(await second.close())
