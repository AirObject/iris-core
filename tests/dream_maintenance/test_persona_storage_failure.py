"""Actual kernel commit failure cannot expose half a periodic publication."""
from pathlib import Path
from tempfile import TemporaryDirectory
import resource
import signal
import sqlite3
import time
import unittest
from companion_memory.persistence import Found,Committed,Unconfirmed
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .storage_support import FaultConnections
from .test_stage_host import quiet


class PersonaStorageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_commit_io_failure_retains_previous_pointer_and_original_review(self):
        outputs=({'schema_version':1,'text':'我是 Iris，明确表达不确定。','basis_refs':[],'change_reason':'整理稳定身份的表达。'},
            {'schema_version':1,'decision':'APPROVE','reason':'独立监管认可当前材料。'})
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);faults=FaultConnections();credentials=[];limits=resource.getrlimit(resource.RLIMIT_FSIZE);handler=signal.getsignal(signal.SIGXFSZ)
            host=make_dream_host(root,port,credentials,connection_factory=faults.connect)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                c=host.combination.dream;p=host.combination.periodic;current=host.current_persona
                if c is None or p is None or current is None:raise AssertionError('Missing native owners')
                previous=await current.port.read_current(time.monotonic()+5)
                self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                for _ in range(3):
                    await quiet(host);self.assertIs(type(await host.advance_dream()),Committed)
                reviewed=await c.inspect('run');work=await p.rows.read('periodic_persona_runs',p.work_id('run'))
                signal.signal(signal.SIGXFSZ,signal.SIG_IGN);faults.fault='KERNEL_WRITE_LIMIT'
                outcome=await host.advance_dream();self.assertIs(type(outcome),Unconfirmed,outcome)
                self.assertIn(sqlite3.SQLITE_IOERR,[code&255 for code in faults.errors])
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE,limits);signal.signal(signal.SIGXFSZ,handler)
                self.assertTrue(await host.close())
            count=len(credentials);reopened=make_dream_host(root,port,credentials)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')),Found)
                c=reopened.combination.dream;p=reopened.combination.periodic;current=reopened.current_persona
                if c is None or p is None or current is None:raise AssertionError('Missing native owners')
                self.assertEqual(await current.port.read_current(time.monotonic()+5),previous)
                self.assertEqual(await c.inspect('run'),reviewed)
                self.assertEqual(await p.rows.read('periodic_persona_runs',p.work_id('run')),work)
                self.assertEqual(await p.rows.page('periodic_persona_publications'),())
                self.assertEqual(len(await p.rows.page('periodic_persona_reviews')),1)
                self.assertIsNone(await c.confirm('publish_periodic_persona',p.key('publish_periodic_persona','run')))
                self.assertFalse(c.dispatch_enabled);self.assertEqual(len(credentials),count);self.assertEqual(len(requests),2);self.assertFalse(failures)
                print({'actual_sqlite_codes':faults.errors,'half_publication':False,'previous_pointer_retained':True,'reopen_added_sends':0})
            finally:self.assertTrue(await reopened.close())
