"""Native holder competition preserves unchanged dream evidence and ownership."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.memory.formats import record,sequence
from companion_memory.persistence.content_codec import decode_content
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_review_host import score_output
from .test_independent_roots import links_for


async def attach(host,current,source_object):
    links=await links_for(host,source_object)
    sources=tuple(dict(record(link))|{'object_id':current['object_id'],'object_revision':current['revision']+1} for link in sequence(links['sources']))
    value=dict(current)|{'revision':current['revision']+1,'modified_at_us':max(host.assembly.utc_now_us(),current['modified_at_us'])}
    return await host.runtime.maintenance.bind((current['object_id'],),tuple(link['source_id'] for link in sources)).replace_current('attach',{
        'change_version':1,'action':'REPLACE_CURRENT','target_id':current['object_id'],'expected_revision':current['revision'],
        'proposed_value':value,'links':{'sources':sources,'bases':()}})


class SourceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_acquire_and_release_do_not_invalidate_unchanged_evidence(self):
        for acquire in (True,False):
            with self.subTest(acquire=acquire),TemporaryDirectory() as directory,responses((score_output,)) as (port,requests,failures):
                host=make_dream_host(Path(directory),port,[],with_self=True)
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    original=await establish_memory(host,with_self=True);fixed=host.fixed
                    if fixed is None or host.runtime is None:raise AssertionError('Missing native owners')
                    self.assertIs(type(await fixed.establish_one(fixed.envelope('fixed_establish','second',MappingProxyType({
                        'set_id':'fixed-set','expected_revision':15,'ordinal':1,'expected_member_revision':1}),1),time.monotonic()+5)),Committed)
                    info=host.assembly.memory.information;c=host.combination.dream
                    if info is None or c is None:raise AssertionError('Missing native owners')
                    other=next(v for v in await info.current_page('',16) if v['object_id']!=original['object_id'])
                    if not acquire:
                        attached=await attach(host,other,original);self.assertIs(type(attached),Committed,attached)
                        other=next(v for v in await info.current_page('',16) if v['object_id']==other['object_id'])
                    self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                    self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                    self.assertIs(type(await c.execute('prepare_dream_review','prepare',{'run_id':'run','expected_revision':2,'mode_epoch':1,
                        'object_id':original['object_id'],'object_revision':original['revision']},actor='dream_coordinator')),Committed)
                    for _ in range(2):self.assertIs(type(await host.advance_dream()),Committed)
                    step=(await c.rows.page('steps'))[0];memory=host.assembly.memory
                    old=(await memory.rows.read('release_plans_get',{'plan_id':step['plan_id']}))[0]
                    changed=await attach(host,other,original) if acquire else await host.runtime.maintenance.bind((cast(str,other['object_id']),)).delete_object('release',other['object_id'],other['revision'])
                    self.assertIs(type(changed),Committed,changed)
                    applied=await host.advance_dream();self.assertIs(type(applied),Committed,applied)
                    if type(applied) is not Committed:raise AssertionError(applied)
                    self.assertEqual(record(applied.receipt.result)['state'],'APPLIED')
                    self.assertIs(type(await c.operations[cast(str,old['command_kind'])].read_receipt(old['execution_key'])),Found)
                    new_step=(await c.rows.page('steps'))[0];new=(await memory.rows.read('release_plans_get',{'plan_id':new_step['plan_id']}))[0]
                    plan=decode_content(cast(str,new['body']).encode(),4096)
                    if type(plan) is not dict:raise AssertionError('Missing native plan')
                    self.assertIsNone(plan['previous_plan']);self.assertEqual(plan['ordinal'],1)
                    self.assertEqual(new['plan_id'],old['plan_id']);self.assertEqual(plan['leaf_digests'],[])
                    after=next(v for v in await info.current_page('',16) if v['object_id']==original['object_id'])
                    self.assertEqual(after['revision'],2);self.assertEqual(record(after['scores'])['belief'],73)
                    self.assertIs(type(await c.confirm(applied.receipt.identity.operation_kind,applied.receipt.identity.operation_key)),Committed)
                    self.assertEqual(len(requests),1);self.assertFalse(failures)
                finally:self.assertTrue(await host.close())
