"""Native two-root evidence survives one forgotten root and closes a real cycle."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.content_codec import decode_content
from companion_memory.memory.formats import record,isolate_links,sequence
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_influence_host import forget
from .test_review_host import graph_output
from .test_stage_host import quiet


async def links_for(host,value):
    row=(await host.assembly.memory.rows.read('links_get',{'object_id':value['object_id']}))[0]
    return isolate_links(decode_content(row['body'].encode(),2048),value['object_id'],value['revision'])


async def add_basis(host,current,basis,key):
    links=await links_for(host,current);target=await links_for(host,basis)
    roots=sorted({cast(str,record(a)['message_id']) for s in sequence(target['sources']) for a in sequence(record(s)['target_anchors'])})
    revision=current['revision']+1
    changed={name:tuple(dict(record(link))|{('object_revision' if name=='sources' else 'dependent_revision'):revision} for link in sequence(items)) for name,items in links.items()}
    changed['bases']+=({'dependent_id':current['object_id'],'dependent_revision':revision,'basis_id':basis['object_id'],
        'basis_revision':basis['revision'],'kind':'SUPPORTS','evidence_roots':tuple(roots)},)
    value=dict(current)|{'revision':revision,'modified_at_us':max(host.assembly.utc_now_us(),current['modified_at_us']),
        'scores':dict(record(current['scores']))|{'score_basis':tuple(link['basis_id'] for link in changed['bases'])}}
    return await host.runtime.maintenance.bind((current['object_id'],),readable_object_ids=(basis['object_id'],)).replace_current(key,
        {'change_version':1,'action':'REPLACE_CURRENT','target_id':current['object_id'],'expected_revision':current['revision'],'proposed_value':value,'links':changed})


class IndependentRootTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_roots_cycle_and_keep_after_one_root_forgotten(self):
        seen=[]
        def derive(request):
            body=json.loads(request['messages'][1]['content']);seen.append(body)
            self.assertEqual(len(body['evidence'][0]['independent_roots']),2)
            result=graph_output(request);leaf=result['actions'][0]
            leaf['basis_refs']=[{'object_id':v['object']['object_id'],'expected_revision':v['object']['revision'],'kind':'SUPPORTS'} for v in body['evidence']]
            result['actions']=[leaf];return result
        def keep(request):
            body=json.loads(request['messages'][1]['content']);seen.append(body)
            evidence=body['evidence'][0]
            self.assertEqual({v['state'] for v in evidence['basis_availability']},{'FORGOTTEN','REVISED'})
            self.assertEqual(len(evidence['independent_roots']),1)
            return {'schema_version':1,'decision':'KEEP','reason':'另一个独立合成来源仍可用；循环引用没有增加来源数量。','actions':[]}
        with TemporaryDirectory() as directory,responses((derive,keep)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[],with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                first=await establish_memory(host,with_self=True);fixed=host.fixed
                if fixed is None:raise AssertionError('Missing fixed owner')
                self.assertIs(type(await fixed.establish_one(fixed.envelope('fixed_establish','second-root',MappingProxyType({
                    'set_id':'fixed-set','expected_revision':15,'ordinal':1,'expected_member_revision':1}),1),time.monotonic()+5)),Committed)
                info=host.assembly.memory.information;c=host.combination.dream;long=host.assembly.memory.long_term
                if info is None or c is None or long is None:raise AssertionError('Missing native owners')
                second=next(v for v in await info.current_page('',16) if v['object_id']!=first['object_id'])
                added=await add_basis(host,first,second,'two-roots');self.assertIs(type(added),Committed,added)
                first=next(v for v in await info.current_page('',16) if v['object_id']==first['object_id'])
                self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                self.assertIs(type(await c.execute('prepare_dream_review','prepare',{'run_id':'run','expected_revision':2,'mode_epoch':1,
                    'object_id':first['object_id'],'object_revision':first['revision']},actor='dream_coordinator')),Committed)
                for _ in range(3):
                    outcome=await host.advance_dream();self.assertIs(type(outcome),Committed,outcome)
                derived=next(v for v in await info.current_page('',16) if v['object_id'] not in (first['object_id'],second['object_id']))
                self.assertIs(type(await forget(host,first)),Committed)
                cycle=await add_basis(host,second,derived,'close-cycle');self.assertIs(type(cycle),Committed,cycle)
                queue=long.influence;latest=None
                for page in range(24):
                    run=await c.inspect('run')
                    if run is None:raise AssertionError('Missing run')
                    event=await queue.next_event(run['impact_cursor'])
                    if event is None:break
                    if event['reason']=='FORGOTTEN':latest=event
                    self.assertIs(type(await c.execute('scan_dream_influence','scan-'+str(page),{'run_id':'run','expected_revision':run['revision'],
                        'mode_epoch':1,'event_id':event['object_id'],'event_sequence':event['sequence']},actor='dream_coordinator')),Committed)
                else:raise AssertionError('Unbounded cycle traversal')
                if latest is None or latest['reason']!='FORGOTTEN':raise AssertionError('Missing native forget event')
                run=await c.inspect('run')
                if run is None:raise AssertionError('Missing run')
                workid=queue.key('influence-work',latest['object_id'],derived['object_id'])
                self.assertIs(type(await c.execute('prepare_dream_influence','prepare-remaining-root',{'run_id':'run','expected_revision':run['revision'],'mode_epoch':1,
                    'object_id':derived['object_id'],'object_revision':derived['revision'],'influence_id':workid},actor='dream_coordinator')),Committed)
                await quiet(host)
                for expected in ('RESULT_STORED','UNCHANGED'):
                    outcome=await host.advance_dream();self.assertIs(type(outcome),Committed,outcome)
                    if type(outcome) is not Committed:raise AssertionError(outcome)
                    self.assertEqual(record(outcome.receipt.result)['state'],expected)
                self.assertEqual(next(v for v in await info.current_page('',16) if v['object_id']==derived['object_id']),derived)
                effects=await queue.rows.page('influence_effects');self.assertEqual(len(effects),1)
                self.assertIs(type(await c.confirm(outcome.receipt.identity.operation_kind,outcome.receipt.identity.operation_key)),Committed)
                self.assertEqual(await queue.rows.page('influence_effects'),effects)
                self.assertEqual(len(requests),2);self.assertFalse(failures);self.assertEqual(len(seen),2)
            finally:self.assertTrue(await host.close())
