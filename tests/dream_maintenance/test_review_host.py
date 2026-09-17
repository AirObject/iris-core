"""Formal dream proposals commit through real owners and original Provider proof."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest
from typing import cast
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from companion_memory.memory.formats import record
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory


def score_output(request):
    body=json.loads(request['messages'][1]['content']);value=body['evidence'][0]['object']
    return {'schema_version':1,'decision':'CHANGE','reason':'依据当前正式材料保留明确的分数理由。','actions':[{
        'action':'SET_SCORES','local_ref':0,'basis_refs':[{'object_id':value['object_id'],'expected_revision':value['revision'],'kind':'SUPPORTS'}],
        'object_id':value['object_id'],'expected_revision':value['revision'],'belief':73,'belief_reason':'当前来源仍支持此表述。',
        'retention_delta':2,'retention_reason':'本次整理确认仍有保留价值。'}]}


class ReviewHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_formal_score_history_step_same_receipt_and_original_replay(self):
        with TemporaryDirectory() as directory,responses((score_output,)) as (port,requests,failures):
            credentials=[];host=make_dream_host(Path(directory),port,credentials,with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                before=await establish_memory(host,with_self=True)
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+120))
                self.assertIs(type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')),Committed)
                self.assertIs(type(await admin.resume_dream('resume','run',1,1)),Committed)
                for stage in ('time','freeze','receive','plan','apply'):
                    result=await host.advance_dream();self.assertIs(type(result),Committed,(stage,result))
                information=host.assembly.memory.information
                if information is None:raise AssertionError('Missing information owner')
                after=(await information.current_page('',1))[0]
                self.assertEqual(record(after['scores'])['belief'],73)
                self.assertEqual(after['revision'],cast(int,before['revision'])+2)
                self.assertEqual(record(after['scores'])['retention'],cast(int,record(before['scores'])['retention'])-7+2)
                self.assertIs(type(result),Committed)
                if type(result) is not Committed:raise AssertionError(result)
                facts=record(record(result.receipt.result)['facts'])
                self.assertIn('memory',facts);self.assertIn('dream',facts)
                self.assertTrue(record(result.receipt.result)['history_fact'])
                owner=host.combination.dream_review;control=host.combination.dream
                if owner is None or control is None:raise AssertionError('Missing native dream owners')
                confirmed=await control.operations[result.receipt.identity.operation_kind].read_receipt(result.receipt.identity.operation_key)
                self.assertIs(type(confirmed),Found)
                self.assertEqual(len(requests),1);self.assertEqual(len(credentials),1);self.assertFalse(failures)
                work=(await owner.rows.page('dream_work'))[0]
                self.assertEqual(work['origin'],'DREAM');self.assertEqual(work['state'],'RESULT_STORED')
                run=await admin.inspect_dream('run')
                if run is None:raise AssertionError('Missing run')
                self.assertIsNone(run['active_step_id']);self.assertEqual(run['model_calls_used'],1)
                self.assertFalse(host.provider.cleanup_pending if host.provider is not None else True)
            finally:
                if host.combination.periodic is not None and host.combination.periodic.job is not None:
                    import asyncio
                    await asyncio.wait((host.combination.periodic.job,))
                self.assertTrue(await host.close())

    async def test_identical_replacement_has_no_business_writer_or_history(self):
        def identical(request):
            result=replace_output(request)
            old=json.loads(request['messages'][1]['content'])['evidence'][0]['object']
            result['actions'][0]['content']['body']=old['content']['body']
            return result
        with TemporaryDirectory() as directory,responses((identical,)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[],with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                await establish_memory(host,with_self=True)
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+120))
                self.assertIs(type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')),Committed)
                self.assertIs(type(await admin.resume_dream('resume','run',1,1)),Committed)
                self.assertIs(type(await host.advance_dream()),Committed)
                info=host.assembly.memory.information
                if info is None:raise AssertionError('Missing memory')
                before=(await info.current_page('',1))[0]
                for expected in ('FROZEN','RESULT_STORED','UNCHANGED'):
                    outcome=await host.advance_dream();self.assertIs(type(outcome),Committed,outcome)
                    if type(outcome) is not Committed:raise AssertionError(outcome)
                    self.assertEqual(record(outcome.receipt.result)['state'],expected)
                self.assertEqual((await info.current_page('',1))[0],before)
                self.assertEqual(set(record(record(outcome.receipt.result)['facts'])),{'dream'})
                self.assertNotIn('history',record(outcome.receipt.result))
                self.assertEqual(len(requests),1);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())

    async def test_native_creation_relation_goal_and_complete_replacement(self):
        for output,expected_count,expected_body in ((graph_output,3,None),(replace_output,1,'合成试验中的表达应明确标出观察、转述与推测的区别。')):
            with self.subTest(output=output.__name__),TemporaryDirectory() as directory,responses((output,)) as (port,requests,failures):
                host=make_dream_host(Path(directory),port,[],with_self=True)
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    before=await establish_memory(host,with_self=True)
                    admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+120))
                    self.assertIs(type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')),Committed)
                    self.assertIs(type(await admin.resume_dream('resume','run',1,1)),Committed)
                    for stage in ('time','freeze','receive','plan','apply'):
                        outcome=await host.advance_dream();self.assertIs(type(outcome),Committed,(stage,outcome))
                        if type(outcome) is Committed:self.assertEqual(record(outcome.receipt.result)['state'],{'time':'APPLIED','freeze':'FROZEN','receive':'RESULT_STORED','plan':'PLANNED','apply':'APPLIED'}[stage])
                    information=host.assembly.memory.information
                    if information is None:raise AssertionError('Missing memory owner')
                    page=await information.current_page('',16)
                    self.assertEqual(len(page),expected_count)
                    if expected_body is not None:
                        self.assertEqual(record(page[0]['content'])['body'],expected_body)
                        self.assertEqual(page[0]['origin'],before['origin'])
                    else:
                        created=[item for item in page if item['object_id']!=before['object_id']]
                        self.assertEqual({cast(str,item['kind']) for item in created},{'MEMORY','RELATION'})
                        for item in created:
                            self.assertEqual(record(item['origin'])['kind'],'DERIVED')
                            self.assertIsNone(record(item['origin'])['batch_id'])
                        if type(outcome) is not Committed:raise AssertionError(outcome)
                        self.assertTrue(record(record(outcome.receipt.result)['facts'])['goals'])
                        await settle_stale_dependencies(self,host,admin,before,page)
                    self.assertEqual(len(requests),1);self.assertFalse(failures)
                finally:self.assertTrue(await host.close())


def graph_output(request):
    body=json.loads(request['messages'][1]['content']);old=body['evidence'][0]['object']
    basis=[{'object_id':old['object_id'],'expected_revision':old['revision'],'kind':'SUPPORTS'}]
    content={'category':'INFERENCE','body':'这是关于合成表达原则的派生推测。','stance':'UNCERTAIN','world_scope':{'kind':'REAL','context_id':None},
        'subject_ids':[{'existing_id':'self'}],'speaker_subject_id':None,'occurred_range':None,'applicable_range':None}
    return {'schema_version':1,'decision':'CHANGE','reason':'明确保留派生关系与待执行目标。','actions':[
        {'action':'CREATE_MEMORY','local_ref':0,'basis_refs':basis,**content,'belief':55,'belief_reason':'由给定原则推得，保留不确定性。'},
        {'action':'CREATE_RELATION','local_ref':1,'basis_refs':basis,'relation_type':'RELATED','assertion':'UNCERTAIN','world_scope':{'kind':'REAL','context_id':None},
            'from_ref':{'type':'SUBJECT','id':{'existing_id':'self'},'expected_revision':1},'to_ref':{'type':'OBJECT','id':{'local_ref':0},'expected_revision':1},
            'belief':55,'belief_reason':'派生对象与已登记自我有关。'},
        {'action':'CREATE_GOAL','local_ref':2,'basis_refs':basis,'basis_action_refs':[0],'content':'在合成试验中标明表达依据。','world_scope':'REAL',
            'subject_refs':[{'existing_id':'self'}],'deadline':None,'reminder_lead_seconds':None,'route_id':None}]}


def replace_output(request):
    body=json.loads(request['messages'][1]['content']);old=body['evidence'][0]['object'];content=dict(old['content'])
    content.update(subject_ids=[{'existing_id':sid} for sid in content['subject_ids']],body='合成试验中的表达应明确标出观察、转述与推测的区别。')
    return {'schema_version':1,'decision':'CHANGE','reason':'保持原含义的完整表述。','actions':[{
        'action':'REPLACE_CURRENT','local_ref':0,'basis_refs':[{'object_id':old['object_id'],'expected_revision':old['revision'],'kind':'SUPPORTS'}],
        'object_id':old['object_id'],'expected_revision':old['revision'],'content':content,'belief':old['scores']['belief'],'belief_reason':old['scores']['belief_reason']}]}


async def settle_stale_dependencies(test,host,admin,initial,objects):
    """Time still advances when the original supporting revision has changed."""
    control=host.combination.dream
    if control is None:raise AssertionError('Missing dream owner')
    run=await admin.inspect_dream('run')
    if run is None:raise AssertionError('Missing run')
    test.assertIs(type(await admin.abort_dream('abort','run',cast(int,run['revision']),cast(int,run['mode_epoch']))),Committed)
    control.now=lambda:max(cast(int,item['created_at_us']) for item in objects)+10*86400000000
    schedule=await control.schedule()
    if schedule is None:raise AssertionError('Missing schedule')
    test.assertIs(type(await admin.start_dream('start-time','time-run',cast(int,schedule['revision']),1,mode='BACKGROUND')),Committed)
    test.assertIs(type(await admin.resume_dream('resume-time','time-run',1,1)),Committed)
    ordered=sorted(objects,key=lambda item:0 if item['object_id']==initial['object_id'] else 1 if item['kind']=='MEMORY' else 2)
    for old in ordered:
        run=await control.inspect('time-run')
        if run is None:raise AssertionError('Missing time run')
        outcome=await control.execute('decay_dream_memory','time-'+cast(str,old['object_id']),{'run_id':'time-run','expected_revision':run['revision'],
            'mode_epoch':1,'memory_id':old['object_id'],'memory_revision':old['revision'],'observed_at_us':control.now()},actor='dream_coordinator')
        test.assertIs(type(outcome),Committed,outcome)
    information=host.assembly.memory.information
    if information is None:raise AssertionError('Missing memory')
    after={cast(str,item['object_id']):item for item in await information.current_page('',16)}
    for old in ordered:
        current=after[cast(str,old['object_id'])]
        test.assertEqual(current['revision'],cast(int,old['revision'])+1)
        test.assertEqual(record(current['scores'])['belief'],record(old['scores'])['belief'])
        test.assertEqual(record(current['scores'])['retention'],cast(int,record(old['scores'])['retention'])-7)
