"""Focused expiry preserves original deletion identity and real source ownership."""
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import identity
from companion_memory.dream.port import OPERATIONS
from companion_memory.memory.formats import record,TOMBSTONE_SCHEMA,isolate
from companion_memory.persistence.content_codec import decode_content
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_influence_host import forget


class ExpiryHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_focused_expiry_tombstone_index_step_and_original_retry(self):
        with TemporaryDirectory() as directory:
            credentials=[];host=make_dream_host(Path(directory),9,credentials,with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                current=await establish_memory(host,with_self=True)
                self.assertIs(type(await forget(host,current)),Committed)
                c=host.combination.dream;memory=host.assembly.memory;info=memory.information;long=memory.long_term
                if c is None or info is None or long is None or host.runtime is None:raise AssertionError('Native owners missing')
                current=(await info.current_page('',1))[0]
                now=cast(int,current['forgotten_since_us'])+30*86400*1000000
                host.assembly.utc_now_us=lambda:now;c.now=lambda:now
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+120))
                self.assertIs(type(await admin.start_dream('start','run',1,1)),Committed)
                run=await c.inspect('run')
                if run is None:raise AssertionError('Missing run')
                self.assertIs(type(await admin.resume_dream('resume','run',cast(int,run['revision']),cast(int,run['mode_epoch']))),Committed)
                result=await host.advance_dream();self.assertIs(type(result),Committed,result)
                self.assertEqual(await info.current_page('',1),())
                tombstones=await memory.rows.read('tombstones_get',{'object_id':current['object_id']})
                self.assertEqual(len(tombstones),1)
                tombstone=isolate(TOMBSTONE_SCHEMA,decode_content(cast(str,tombstones[0]['body']).encode(),1024),1024)
                self.assertEqual(tombstone['deleted_at_us'],now)
                events=await long.rows.page('influence_events')
                event=next(e for e in events if e['reason']=='DELETED')
                self.assertEqual(event['created_at_us'],now)
                steps=await c.rows.page('steps');self.assertEqual(len(steps),1);self.assertEqual(steps[0]['kind'],'EXPIRY')
                self.assertEqual(steps[0]['state'],'APPLIED');self.assertTrue(c.dispatch_enabled)
                coverage=memory.semantic
                if coverage is None:raise AssertionError('Missing native semantic coverage')
                _,gap=await coverage.semantic_current(cast(str,current['object_id']))
                if gap is None:raise AssertionError('Missing deletion index gap')
                self.assertEqual(gap['action'],'DELETE')
                replay=await host.runtime.maintenance.bind((cast(str,current['object_id']),)).delete_object(
                    identity('expired_memory',current['object_id'],current['revision']),current['object_id'],current['revision'])
                self.assertIs(type(replay),Committed,replay)
                if type(replay) is Committed and type(result) is Committed:self.assertEqual(replay.receipt,result.receipt)
                self.assertEqual(len(await c.rows.page('steps')),1);self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())
