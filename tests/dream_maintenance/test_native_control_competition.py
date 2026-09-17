"""Native host stop generations fence old completion and never register a model."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.owned_statements import OwnerFailure
from .host_support import make_dream_host


class NativeCompetitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_stop_orders_and_failed_stop_keep_native_admission_closed(self):
        for order in (('pause_dream','abort_background_dream'),('abort_background_dream','pause_dream')):
            with self.subTest(order=order),TemporaryDirectory() as directory:
                credentials=[];host=make_dream_host(Path(directory),9,credentials)
                try:
                    opened=await host.initialize('CREATE_NEW');self.assertIs(type(opened),Found,opened)
                    control=host.combination.dream
                    if control is None:raise AssertionError('Missing dream owner')
                    self.assertIs(type(await control.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                    reached,release=asyncio.Event(),asyncio.Event();original=control.inspect
                    async def blocked(run_id):
                        reached.set();await release.wait();return await original(run_id)
                    with patch.object(control,'inspect',blocked):
                        resumed=asyncio.create_task(control.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin'))
                        await asyncio.wait_for(reached.wait(),2)
                        first=asyncio.create_task(control.execute(order[0],'first-stop',{'run_id':'run','expected_revision':2,'mode_epoch':1},actor='admin'))
                        await asyncio.sleep(0)
                        second=asyncio.create_task(control.execute(order[1],'second-stop',{'run_id':'run','expected_revision':3,'mode_epoch':1},actor='admin'))
                        await asyncio.sleep(0);self.assertFalse(control.dispatch_enabled);release.set()
                        self.assertIs(type(await resumed),Committed)
                        self.assertIs(type(await first),Committed)
                        if order[0]=='abort_background_dream':
                            with self.assertRaises(OwnerFailure):await second
                        else:self.assertIs(type(await second),Committed)
                    current=await control.inspect('run')
                    if current is None:raise AssertionError('Missing original run')
                    self.assertEqual(current['state'],'ABORTED');self.assertFalse(control.dispatch_enabled)
                    self.assertIs(type(await control.confirm('resume_dream','resume')),Committed)
                    self.assertFalse(control.dispatch_enabled)
                    with sqlite3.connect(Path(directory)/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0],0)
                    self.assertFalse(credentials)
                finally:self.assertTrue(await host.close())

    async def test_public_older_resume_cannot_cross_new_pause_during_preparation(self):
        from companion_memory.information.management import HostIdentity
        from companion_memory.dream.port import OPERATIONS
        import time
        with TemporaryDirectory() as directory:
            credentials=[];host=make_dream_host(Path(directory),9,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','external')),Committed)
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+60))
                self.assertIs(type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')),Committed)
                control=host.combination.dream
                if control is None:raise AssertionError('Missing dream owner')
                reached,release=asyncio.Event(),asyncio.Event();confirm=control.confirm
                async def blocked(kind,key):
                    if key=='old-resume':reached.set();await release.wait()
                    return await confirm(kind,key)
                with patch.object(control,'confirm',blocked):
                    # Revision 2 would become valid after pause, but this resume
                    # was invoked earlier and cannot erase that later intent.
                    resumed=asyncio.create_task(admin.resume_dream('old-resume','run',2,1))
                    await asyncio.wait_for(reached.wait(),2)
                    self.assertIs(type(await admin.pause_dream('new-pause','run',1,1)),Committed)
                    release.set()
                    with self.assertRaises(OwnerFailure):await resumed
                current=await admin.inspect_dream('run')
                if current is None:raise AssertionError('Missing original run')
                self.assertEqual(current['state'],'PAUSED');self.assertEqual(current['revision'],2)
                self.assertFalse(control.dispatch_enabled);self.assertFalse(credentials)
                with sqlite3.connect(Path(directory)/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0],0)
            finally:self.assertTrue(await host.close())
