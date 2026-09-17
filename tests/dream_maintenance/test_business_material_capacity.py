"""Actual complete retained business sources qualify dream capacity disposition."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.schema import ValueTooLarge
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from companion_memory.memory.formats import record
from companion_memory.self_model.current import Available
from companion_memory.ingress.events import canonical_event
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.provider.dream_protocol import encode_dream_request
from tests.daily_cognition.test_reasoning import responses
from tests.runtime.configuration_support import event
from .host_support import make_dream_host
from .test_stage_host import quiet


def complete_event(index):
    raw=event('complete-source-'+str(index),'以下各项是合成表达训练材料，不记录真实经历。');raw['event_version']=2
    for item in range(100):
        paragraph=('\n示例'+str(index)+'-'+str(item)+'：在第'+str(item+1)+'号合成展台，观察记录只说明左侧图形的颜色与位置；'
            '转述记录需注明合成讲述者；推测应保留不确定性。编号用于核对完整来源，不能把这些示例变成 Iris 的现实经历。')
        candidate=raw|{'body':cast(str,raw['body'])+paragraph}
        try:frozen=isolate_media_event(candidate,8192,occurrence_limit=2,text_limit=512)
        except ValueTooLarge:break
        if len(canonical_event(frozen))>8192:break
        raw=candidate
    return raw


def eight_self_principles(request):
    source=json.loads(request['messages'][1]['content'])['source']
    anchors=[{'message_id':m['message_id'],'part':'EVENT','item_index':None,'start_utf8':None,'end_utf8':None,
        'occurrence_id':None,'interpretation_id':None} for m in source['ordered_members'] if m['role']=='T']
    distinctions=('观察范围','转述主体','推测强度','时间未知','世界边界','外部设定','来源重复','表达修订')
    return {'schema_version':1,'kind':'FINAL','actions':[{'action':'CREATE_MEMORY','local_ref':n,
        'target_anchors':anchors,'auxiliary_refs':[],'basis_refs':[],'category':'FACT',
        'body':'合成表达原则：'+distinction+'需要明确标注，不能把训练示例当成自己的现实经历。',
        'subject_ids':[{'existing_id':'self'}],'speaker_subject_id':None,'stance':'ASSERTED',
        'world_scope':{'kind':'REAL','context_id':None},'occurred_range':None,'applicable_range':None,
        'belief':80,'belief_reason':'来自当前合成训练设定，限于表达要求。'} for n,distinction in enumerate(distinctions)]}


class BusinessMaterialCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_large_sources_fit_review_but_defer_whole_sixteen_fact_persona(self):
        with TemporaryDirectory() as directory,responses((eight_self_principles,eight_self_principles)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[]);bodies=[];sizes=[]
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                entry=host.bind_entry('entry')
                for batch,indices in enumerate(((0,1,2),(3,4))):
                    if batch:await quiet(host)
                    for index in indices:
                        raw=complete_event(index);bodies.append(raw['body'])
                        sizes.append(len(canonical_event(isolate_media_event(raw,8192,occurrence_limit=2,text_limit=512))))
                        self.assertIs(type(await entry.accept_event('accept-'+str(index),raw)),Committed)
                    self.assertIs(type(await host.resume_learning('resume-'+str(batch))),Committed)
                    self.assertIs(type(await entry.run_learning('learn-'+str(batch))),Committed)
                    self.assertIs(type(await host.pause_learning('pause-'+str(batch))),Committed)
                info=host.assembly.memory.information;c=host.combination.dream;review=host.combination.dream_review;persona=host.combination.periodic
                if info is None or c is None or review is None or persona is None or host.current_persona is None:raise AssertionError('Missing native owners')
                objects=await info.current_page('',16);self.assertEqual(len(objects),16)
                previous=await host.current_persona.port.read_current(time.monotonic()+5)
                self.assertIs(type(previous),Available)
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+180))
                self.assertIs(type(await admin.start_dream('start','review-run',1,1,mode='BACKGROUND')),Committed)
                self.assertIs(type(await admin.resume_dream('resume','review-run',1,1)),Committed)
                old=objects[0]
                self.assertIs(type(await c.execute('prepare_dream_review','freeze-review',{'run_id':'review-run','expected_revision':2,'mode_epoch':1,
                    'object_id':old['object_id'],'object_revision':old['revision']},actor='dream_coordinator')),Committed)
                work=(await review.rows.page('dream_work'))[0]
                lease=await review.materials.borrow(cast(str,work['material_id']),cast(str,work['material_digest']),'review-run',time.monotonic()+5)
                try:
                    raw=lease.material.body;material=json.loads(raw);source=material['evidence'][0]['sources'][0]
                    self.assertGreaterEqual(len(source['members']),3)
                    for member in source['members']:self.assertIn(member['event']['body'],bodies)
                    wire=encode_dream_request(review.provider.dream_bindings['DREAM_REVIEW'],raw.decode())
                    review_bytes=len(raw);wire_bytes=len(wire)
                finally:review.materials.release_reader(lease)
                run=await c.inspect('review-run')
                if run is None:raise AssertionError('Missing run')
                self.assertIs(type(await admin.abort_dream('abort','review-run',cast(int,run['revision']),1)),Committed)
                schedule=await c.schedule()
                if schedule is None:raise AssertionError('Missing schedule')
                self.assertIs(type(await admin.start_dream('persona-start','persona-run',cast(int,schedule['revision']),1,mode='BACKGROUND')),Committed)
                self.assertIs(type(await admin.resume_dream('persona-resume','persona-run',1,1)),Committed)
                values={'run_id':'persona-run','expected_revision':2,'mode_epoch':1}
                with self.assertRaises((ValueTooLarge,OwnerFailure)):
                    await c.execute('prepare_periodic_persona','over-capacity',values,actor='dream_coordinator')
                result=await c.execute('defer_periodic_persona','defer-complete-view',values,actor='dream_coordinator')
                self.assertIs(type(result),Committed,result)
                deferred=(await persona.rows.page('periodic_persona_deferrals'))[0]
                self.assertEqual(len(deferred['basis_refs']),16);self.assertEqual(deferred['reason'],'CAPACITY_REACHED')
                self.assertIs(type(await host.advance_dream()),Committed)
                self.assertEqual(await host.current_persona.port.read_current(time.monotonic()+5),previous)
                self.assertEqual(len(requests),2);self.assertFalse(failures)
                self.assertTrue(all(7800<size<=8192 for size in sizes));self.assertGreater(review_bytes,24000)
                print(json.dumps({'qualification':'COMPLETE_NATIVE_BUSINESS_SOURCES','event_bytes':sizes,'self_objects':len(objects),
                    'review_material_bytes':review_bytes,'review_wire_bytes':wire_bytes,'persona_disposition':'DEFERRED_CAPACITY',
                    'new_dream_requests':0,'new_persona_requests':0,'source_body_omissions':0}))
            finally:self.assertTrue(await host.close())
