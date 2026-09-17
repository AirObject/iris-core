"""Actual reverse pages and unavailable support settle through native owners."""
import asyncio
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.memory.formats import record,isolate_links,transition
from companion_memory.persistence.content_codec import decode_content
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_review_host import graph_output,score_output


def six_dependents(request):
    result=graph_output(request);leaf=result['actions'][0]
    result['actions']=[dict(leaf,local_ref=index,body='合成派生对象 '+str(index)+'：表达应区分观察与推测。') for index in range(6)]
    return result


def lost_support(request):
    body=json.loads(request['messages'][1]['content'])
    assert body['influence']['event_reason']=='FORGOTTEN'
    assert body['evidence'][0]['basis_availability'][0]['state']=='FORGOTTEN'
    assert body['evidence'][0]['independent_roots']==[]
    result=score_output(request);value=body['evidence'][0]['object'];action=result['actions'][0]
    action.update(belief=value['scores']['belief'],belief_reason=value['scores']['belief_reason'],retention_delta=-3,
        retention_reason='支持当前不可用，不因此断言命题为假。')
    return result


async def set_retention(host,current,retention,key):
    memory=host.assembly.memory;oid=current['object_id'];revision=current['revision']
    row=(await memory.rows.read('links_get',{'object_id':oid}))[0]
    links=isolate_links(decode_content(row['body'].encode(),2048),oid,revision)
    updated={name:tuple(dict(record(link))|{('object_revision' if name=='sources' else 'dependent_revision'):revision+1} for link in cast(tuple,items)) for name,items in links.items()}
    now=max(host.assembly.utc_now_us(),current['modified_at_us']);state,since=transition(current,retention,now,20,35)
    value=dict(current)|{'revision':revision+1,'modified_at_us':now,'lifecycle':state,'forgotten_since_us':since,
        'scores':dict(record(current['scores']))|{'retention':retention,'retention_reason':'受控验证支持遗忘或恢复的生命周期。'}}
    return await host.runtime.maintenance.bind((oid,)).set_scores(key,{'change_version':1,'action':'SET_SCORES','target_id':oid,
        'expected_revision':revision,'proposed_value':value,'links':updated})


async def forget(host,current):return await set_retention(host,current,19,'forget-root')

class InfluenceHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_edges_two_pages_and_original_effect_commit_once(self):
        with TemporaryDirectory() as directory,responses((six_dependents,lost_support)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[],with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                root=await establish_memory(host,with_self=True);c=host.combination.dream
                info=host.assembly.memory.information;long=host.assembly.memory.long_term
                if c is None or info is None or long is None:raise AssertionError('Native owner missing')
                self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                for _ in range(5):
                    result=await host.advance_dream();self.assertIs(type(result),Committed,result)
                objects=await info.current_page('',16)
                self.assertEqual(len(objects),7)
                root=next(item for item in objects if item['object_id']==root['object_id'])
                self.assertIs(type(await forget(host,root)),Committed)
                queue=long.influence;latest=None;pages=0
                while True:
                    run=await c.inspect('run')
                    if run is None:raise AssertionError('Missing run')
                    event=await queue.next_event(run['impact_cursor'])
                    if event is None:break
                    latest=event;walk=await queue.rows.read('influence_walks',queue.key('influence-walk',event['object_id']))
                    result=await c.execute('scan_dream_influence','scan-'+str(pages),{'run_id':'run','expected_revision':run['revision'],'mode_epoch':1,
                        'event_id':event['object_id'],'event_sequence':event['sequence']},actor='dream_coordinator')
                    self.assertIs(type(result),Committed,result);pages+=1
                    if pages>12:raise AssertionError('Unbounded dependency walk')
                if latest is None:raise AssertionError('Missing real event')
                self.assertEqual(latest['reason'],'FORGOTTEN');self.assertEqual(pages,6)
                derived=next(item for item in objects if item['object_id']!=root['object_id'])
                workid=queue.key('influence-work',latest['object_id'],derived['object_id']);run=await c.inspect('run')
                if run is None:raise AssertionError('Missing run')
                frozen=await c.execute('prepare_dream_influence','prepare-influence',{'run_id':'run','expected_revision':run['revision'],'mode_epoch':1,
                    'object_id':derived['object_id'],'object_revision':derived['revision'],'influence_id':workid},actor='dream_coordinator')
                self.assertIs(type(frozen),Committed,frozen)
                await asyncio.sleep(30)
                for expected in ('RESULT_STORED','PLANNED','APPLIED'):
                    result=await host.advance_dream();self.assertIs(type(result),Committed,result)
                    if type(result) is not Committed:raise AssertionError(result)
                    self.assertEqual(record(result.receipt.result)['state'],expected)
                after=next(item for item in await info.current_page('',16) if item['object_id']==derived['object_id'])
                self.assertEqual(record(after['scores'])['belief'],record(derived['scores'])['belief'])
                self.assertEqual(record(after['scores'])['retention'],cast(int,record(derived['scores'])['retention'])-3)
                self.assertEqual(after['revision'],cast(int,derived['revision'])+1)
                work=await queue.rows.read('influence_work',workid)
                if work is None:raise AssertionError('Missing original work')
                self.assertEqual(work['state'],'APPLIED')
                effects=await queue.rows.page('influence_effects');self.assertEqual(len(effects),1)
                self.assertEqual(effects[0]['cause_root'],latest['cause_root']);self.assertEqual(effects[0]['effect_kind'],'SUPPORT_LOST')
                confirmed=await c.confirm(result.receipt.identity.operation_kind,result.receipt.identity.operation_key)
                self.assertIs(type(confirmed),Committed);self.assertEqual(len(await queue.rows.page('influence_effects')),1)
                forgotten=next(item for item in await info.current_page('',16) if item['object_id']==root['object_id'])
                self.assertIs(type(await set_retention(host,forgotten,35,'restore-root')),Committed)
                for index in range(5):
                    skipped=await host.advance_dream();self.assertIs(type(skipped),Committed,skipped)
                    if type(skipped) is not Committed:raise AssertionError(skipped)
                    self.assertEqual(record(skipped.receipt.result)['state'],'UNCHANGED')
                pending=await queue.pending('run');self.assertIsNone(pending)
                all_work=[];after_key=''
                while page:=await queue.rows.page('influence_work',after_key):
                    all_work.extend(page);after_key=cast(str,page[-1]['object_id'])
                remaining=[item for item in all_work if item['event_id']==latest['object_id'] and item['object_id']!=workid]
                self.assertEqual(len(remaining),5);self.assertTrue(all(item['reason']=='SUPERSEDED' for item in remaining))
                self.assertEqual(len(await queue.rows.page('influence_effects')),1)
                self.assertEqual(len(requests),2);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())
