"""Actual SQLite write failure cannot publish part of a native memory dream step."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import resource
import signal
from typing import cast
import unittest
from companion_memory.persistence import Found,Committed,Unconfirmed,NotCommitted
from .host_support import make_dream_host
from .seed_support import establish_memory
from .storage_support import FaultConnections


class NativeStorageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_competing_sqlite_writer_cannot_partially_settle_memory(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);faults=FaultConnections();credentials=[]
            host=make_dream_host(root,9,credentials,connection_factory=faults.connect)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                before=await establish_memory(host);c=host.combination.dream;memory=host.assembly.memory
                if c is None or memory.information is None:raise AssertionError('Missing native owners')
                self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                run=await c.inspect('run')
                with sqlite3.connect(root/'database/runtime.sqlite3') as competitor:
                    competitor.execute('BEGIN IMMEDIATE')
                    outcome=await host.advance_dream()
                    self.assertIn(type(outcome),(NotCommitted,Unconfirmed),outcome)
                    competitor.rollback()
                self.assertIn(sqlite3.SQLITE_BUSY,[code&255 for code in faults.errors])
            finally:self.assertTrue(await host.close())
            reopened=make_dream_host(root,9,credentials)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')),Found)
                c=reopened.combination.dream;memory=reopened.assembly.memory;p=reopened.combination.periodic
                if c is None or p is None or memory.information is None:raise AssertionError('Missing native owners')
                self.assertEqual(await c.inspect('run'),run);self.assertEqual(await c.rows.page('steps'),())
                self.assertEqual((await memory.information.current_page('',1))[0],before)
                self.assertIsNone(await c.confirm('decay_dream_memory',p.key('dream-decay','run',before['object_id'])))
                self.assertFalse(c.dispatch_enabled)
                self.assertIs(type(await c.execute('resume_dream','retry-resume',{'run_id':'run','expected_revision':2,'mode_epoch':1},actor='admin')),Committed)
                self.assertIs(type(await reopened.advance_dream()),Committed)
                self.assertEqual(len(await c.rows.page('steps')),1);self.assertFalse(credentials)
                print({'physical_fault':'COMPETING_SQLITE_WRITER','sqlite_codes':faults.errors,'retry_effect_count':1,'new_sends':0})
            finally:self.assertTrue(await reopened.close())

    async def test_actual_page_exhaustion_and_kernel_write_failure_are_atomic(self):
        for fault in ('FULL','KERNEL_WRITE_LIMIT'):
            with self.subTest(fault=fault),TemporaryDirectory() as directory:
                root=Path(directory);faults=FaultConnections();credentials=[]
                host=make_dream_host(root,9,credentials,connection_factory=faults.connect)
                limits=resource.getrlimit(resource.RLIMIT_FSIZE);handler=signal.getsignal(signal.SIGXFSZ)
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    before=await establish_memory(host);c=host.combination.dream;long=host.assembly.memory.long_term
                    if c is None or long is None:raise AssertionError('Missing native owners')
                    self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                    self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                    run=await c.inspect('run');anchor=await long.rows.read('maintenance_anchors',long.anchor_id(cast(str,before['object_id'])))
                    # Physical compaction/checkpoint changes no logical business fact.
                    with sqlite3.connect(root/'database/runtime.sqlite3') as physical:
                        physical.execute('VACUUM');physical.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    if fault=='FULL':faults.fault='FULL'
                    else:
                        signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
                        faults.fault='KERNEL_WRITE_LIMIT'
                    outcome=await host.advance_dream()
                    observed_limit=resource.getrlimit(resource.RLIMIT_FSIZE)
                    resource.setrlimit(resource.RLIMIT_FSIZE,limits)
                    print({'physical_fault':fault,'outcome':repr(outcome),'sqlite_codes':faults.errors,'write_limit_at_completion':observed_limit})
                    self.assertIn(type(outcome),(NotCommitted,Unconfirmed),outcome)
                    expected=sqlite3.SQLITE_FULL if fault=='FULL' else sqlite3.SQLITE_IOERR
                    self.assertIn(expected,[code&255 for code in faults.errors])
                finally:
                    resource.setrlimit(resource.RLIMIT_FSIZE,limits);signal.signal(signal.SIGXFSZ,handler)
                    self.assertTrue(await host.close())
                reopened=make_dream_host(root,9,credentials)
                try:
                    self.assertIs(type(await reopened.initialize('OPEN_EXISTING')),Found)
                    memory=reopened.assembly.memory;c=reopened.combination.dream
                    if memory.information is None or memory.long_term is None or c is None:raise AssertionError('Missing native owners')
                    self.assertEqual((await memory.information.current_page('',1))[0],before)
                    self.assertEqual(await memory.long_term.rows.read('maintenance_anchors',memory.long_term.anchor_id(cast(str,before['object_id']))),anchor)
                    self.assertEqual(await c.inspect('run'),run);self.assertEqual(await c.rows.page('steps'),())
                    self.assertFalse(c.dispatch_enabled);self.assertFalse(credentials)
                    print({'fault':fault,'actual_sqlite_codes':faults.errors,'memory_anchor_step_atomic':True,'new_sends':0})
                finally:self.assertTrue(await reopened.close())

    async def test_real_readonly_during_memory_settlement_reopens_without_partial_effect(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);faults=FaultConnections();credentials=[]
            host=make_dream_host(root,9,credentials,connection_factory=faults.connect)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                before=await establish_memory(host);c=host.combination.dream;long=host.assembly.memory.long_term
                if c is None or long is None:raise AssertionError('Missing native owners')
                self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                run=await c.inspect('run');anchor=await long.rows.read('maintenance_anchors',long.anchor_id(cast(str,before['object_id'])))
                faults.fault='READONLY'
                outcome=await host.advance_dream()
                self.assertIs(type(outcome),Unconfirmed,outcome)
                self.assertIn(sqlite3.SQLITE_READONLY,[code&255 for code in faults.errors])
            finally:self.assertTrue(await host.close())
            reopened=make_dream_host(root,9,credentials)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')),Found)
                memory=reopened.assembly.memory;c=reopened.combination.dream
                if memory.information is None or memory.long_term is None or c is None:raise AssertionError('Missing native owners')
                self.assertEqual((await memory.information.current_page('',1))[0],before)
                self.assertEqual(await memory.long_term.rows.read('maintenance_anchors',memory.long_term.anchor_id(cast(str,before['object_id']))),anchor)
                self.assertEqual(await c.inspect('run'),run);self.assertEqual(await c.rows.page('steps'),())
                self.assertFalse(c.dispatch_enabled);self.assertFalse(credentials)
            finally:self.assertTrue(await reopened.close())
