"""Public feedback and original dream expiry arbitrate against actual revisions."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.management import HostIdentity
from companion_memory.information.errors import InformationRejected,InformationNotCommitted
from companion_memory.memory.formats import record
from tests.information.test_queries import query
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_influence_host import set_retention


class UsageCompetitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_feedback_and_expiry_both_orders_keep_original_anchor_and_belief(self):
        for feedback_first in (True,False):
            with self.subTest(feedback_first=feedback_first),TemporaryDirectory() as directory:
                host=make_dream_host(Path(directory),9,[],with_self=True)
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    current=await establish_memory(host,with_self=True);memory=host.assembly.memory
                    info=memory.information;c=host.combination.dream;expiry=host.combination.dream_expiry
                    if info is None or memory.long_term is None or c is None or expiry is None:raise AssertionError('Missing native owners')
                    real_now=time.time_ns()//1000;host.assembly.utc_now_us=lambda:real_now-31*86400000000
                    self.assertIs(type(await set_retention(host,current,19,'forget')),Committed)
                    current=(await info.current_page('',1))[0]
                    self.assertIs(type(await set_retention(host,current,30,'still-forgotten')),Committed)
                    host.assembly.utc_now_us=lambda:time.time_ns()//1000
                    current=(await info.current_page('',1))[0];oid=cast(str,current['object_id'])
                    anchor=await memory.long_term.rows.read('maintenance_anchors',memory.long_term.anchor_id(oid))
                    if anchor is None:raise AssertionError('Missing anchor')
                    port=await host.bind_business(HostIdentity('use-race','principal','host','entry',frozenset(('deep_recall','record_usage')),(),time.monotonic()+120),include_forgotten=True)
                    recalled=await port.deep_recall(query('deep',query_text='',object_ids=(oid,)))
                    self.assertIs(type(recalled),Found,recalled)
                    if type(recalled) is not Found:raise AssertionError(recalled)
                    payload={'operation_key':'use','recall_id':record(recalled.value)['recall_id'],'used_at':None,
                        'used_members':({'object_id':oid,'returned_revision':current['revision']},)}
                    self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                    self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                    if feedback_first:
                        select=expiry.select
                        async def compete(current,run):
                            self.assertIs(type(await port.record_usage(payload)),Committed)
                            return await select(current,run)
                        expiry.select=compete
                        with self.assertRaises(OwnerFailure) as failed:await host.advance_dream()
                        self.assertIn(failed.exception.reason,('SOURCE_CHANGED','REVISION_CONFLICT'))
                        expiry.select=select
                        restored=(await info.current_page('',1))[0]
                        self.assertEqual(restored['lifecycle'],'ACTIVE');self.assertIsNone(restored['forgotten_since_us'])
                        self.assertEqual(record(restored['scores'])['belief'],record(current['scores'])['belief'])
                        after=await memory.long_term.rows.read('maintenance_anchors',memory.long_term.anchor_id(oid))
                        if after is None:raise AssertionError('Missing anchor')
                        self.assertEqual(after['accounted_until'],anchor['accounted_until']);self.assertIsNotNone(after['last_used_at'])
                        self.assertEqual(await c.rows.page('steps'),())
                        self.assertIs(type(await host.advance_dream()),Committed)
                        self.assertEqual((await info.current_page('',1))[0]['lifecycle'],'ACTIVE')
                    else:
                        self.assertIs(type(await host.advance_dream()),Committed)
                        late=await port.record_usage(payload)
                        self.assertIn(type(late),(InformationRejected,InformationNotCommitted),late)
                        self.assertEqual(await info.current_page('',1),())
                finally:self.assertTrue(await host.close())
