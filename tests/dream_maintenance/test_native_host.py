"""Native host initialization and atomic memory settlement on actual SQLite."""
from pathlib import Path
from typing import cast
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
import json
import sqlite3
import unittest
from companion_memory.persistence import Found,Committed
from tests.semantic.test_semantic_host import material
from .host_support import make_dream_host


class NativeDreamHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_focused_control_uses_native_gate_and_safe_abort_without_publication(self):
        from companion_memory.information.management import HostIdentity
        from companion_memory.dream.port import OPERATIONS
        with TemporaryDirectory() as directory:
            credentials=[];host=make_dream_host(Path(directory),9,credentials)
            try:
                opened=await host.initialize('CREATE_NEW');self.assertIs(type(opened),Found,opened)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                port=await host.bind_dream(HostIdentity('dream-admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+60))
                started=await port.start_dream('focus','focus-run',1,1)
                self.assertIs(type(started),Committed,started)
                if host.runtime is None:raise AssertionError('Missing runtime')
                self.assertEqual(host.runtime.gate.state,'DREAM_FOCUSED')
                current=await port.inspect_dream('focus-run')
                if current is None:raise AssertionError('Missing run')
                resumed=await port.resume_dream('resume','focus-run',cast(int,current['revision']),cast(int,current['mode_epoch']))
                self.assertIs(type(resumed),Committed,resumed)
                current=await port.inspect_dream('focus-run')
                if current is None:raise AssertionError('Missing run')
                ended=await port.abort_dream('abort','focus-run',cast(int,current['revision']),cast(int,current['mode_epoch']))
                self.assertIs(type(ended),Committed,ended)
                self.assertEqual(host.runtime.gate.state,'NORMAL')
                current=await port.inspect_dream('focus-run')
                if current is None:raise AssertionError('Missing run')
                self.assertEqual(current['state'],'ABORTED');self.assertEqual(current['exit_result'],'ABORTED_SAFELY')
                self.assertIsNone(current['persona_publication_id']);self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())

    async def test_native_memory_settlement_keeps_belief_debt_and_original_receipt(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);credentials=[];host=make_dream_host(root,9,credentials)
            try:
                opened=await host.initialize('CREATE_NEW')
                self.assertIs(type(opened),Found,(host.phase,opened))
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                if host.fixed is None or host.stored is None or host.current_persona is None:raise AssertionError('Native owner missing')
                current=await host.current_persona.port.read_current(time.monotonic()+5)
                from companion_memory.self_model.current import Available
                self.assertIs(type(current),Available,current)
                if type(current) is Available:self.assertEqual(current.value['projection_version'],'PERIODIC_PERSONA_V1')
                assert host.resources.review is not None
                fixed=host.fixed;claims=host.resources.review.claims
                def envelope(kind,key,payload):return fixed.envelope(kind,key,MappingProxyType(payload),1)
                config={'database_id':host.stored.database_id,'instance_id':'instance','snapshot_id':host.stored.snapshot_id}
                self.assertIs(type(await fixed.begin(envelope('fixed_begin','begin',{'set_id':'fixed-set','config':config,
                    **{k:claims[k] for k in ('manifest_digest','review_ref','review_digest')}}),time.monotonic()+5)),Committed)
                for ordinal,member in enumerate(material()):
                    added=await fixed.add_member(envelope('fixed_add_member','add-'+str(ordinal),{'set_id':'fixed-set','expected_revision':ordinal+1,**member}),time.monotonic()+5)
                    self.assertIs(type(added),Committed,added)
                self.assertIs(type(await fixed.seal(envelope('fixed_seal','seal',{'set_id':'fixed-set','expected_revision':13}),time.monotonic()+5)),Committed)
                established=await fixed.establish_one(envelope('fixed_establish','establish',{'set_id':'fixed-set','expected_revision':14,'ordinal':0,'expected_member_revision':1}),time.monotonic()+5)
                self.assertIs(type(established),Committed,established)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as database:
                    before=json.loads(database.execute('SELECT body FROM memory_objects').fetchone()[0])
                now=before['created_at_us']+10*86400000000
                control=host.combination.dream
                if control is None:raise AssertionError('Dream owner missing')
                control.now=lambda:now
                started=await control.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')
                self.assertIs(type(started),Committed,started)
                resumed=await control.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')
                self.assertIs(type(resumed),Committed,resumed)
                values={'run_id':'run','expected_revision':2,'mode_epoch':1,'memory_id':before['object_id'],'memory_revision':1,'observed_at_us':now}
                settled=await control.execute('decay_dream_memory','decay',values,actor='admin')
                self.assertIs(type(settled),Committed,settled)
                original=await control.execute('decay_dream_memory','decay',values,actor='admin')
                self.assertIs(type(original),Committed,original)
                if type(original) is Committed and type(settled) is Committed:self.assertEqual(original.receipt,settled.receipt)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as database:
                    after=json.loads(database.execute('SELECT body FROM memory_objects').fetchone()[0])
                    anchor=json.loads(database.execute('SELECT body FROM memory_maintenance_anchors').fetchone()[0])
                    step=json.loads(database.execute('SELECT body FROM dream_steps').fetchone()[0])
                    self.assertEqual(database.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                self.assertEqual(after['scores']['belief'],before['scores']['belief'])
                self.assertEqual(after['scores']['retention'],before['scores']['retention']-7)
                self.assertEqual(anchor['accounted_until'],before['created_at_us']+7*86400000000)
                self.assertEqual(step['accounted_through'],anchor['accounted_until'])
                self.assertEqual(step['state'],'APPLIED');self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())
            reopened=make_dream_host(root,9,credentials)
            try:
                opened=await reopened.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,(reopened.phase,opened))
                control=reopened.combination.dream
                if control is None:raise AssertionError('Dream owner missing')
                self.assertFalse(control.dispatch_enabled)
                self.assertIs(type(await control.confirm('decay_dream_memory','decay')),Committed)
                self.assertFalse(credentials)
            finally:self.assertTrue(await reopened.close())
