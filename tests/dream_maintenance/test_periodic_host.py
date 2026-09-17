"""Native periodic Provider calls, independent review and the public pointer."""
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import asyncio
import time
import json
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.self_model.current import Available
from companion_memory.dream.port import OPERATIONS
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host


class PeriodicHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_generation_review_atomic_current_pointer(self):
        outputs=({'schema_version':1,'decision':'KEEP','reason':'当前正式材料保持原状。','actions':[]},
            {'schema_version':1,'text':'我是 Iris，保持独立判断，也会明确表达不确定。','basis_refs':[],'change_reason':'整理现有稳定身份的表达。'},
            {'schema_version':1,'decision':'APPROVE','reason':'保留原身份，未增加未经支持的经历。'})
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            credentials=[];host=make_dream_host(Path(directory),port,credentials,with_self=True)
            try:
                opened=await host.initialize('CREATE_NEW');self.assertIs(type(opened),Found,opened)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                from .seed_support import establish_memory
                from companion_memory.memory.formats import record
                before=await establish_memory(host,with_self=True)
                control=host.combination.dream
                if control is None:raise AssertionError('Missing dream owner')
                control.now=lambda:max(time.time_ns()//1000,cast(int,before['created_at_us'])+10*86400000000)
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+180))
                self.assertIs(type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')),Committed)
                self.assertIs(type(await admin.resume_dream('resume','run',1,1)),Committed)
                maintained=await host.advance_dream();self.assertIs(type(maintained),Committed,maintained)
                information=host.assembly.memory.information
                if information is None:raise AssertionError('Missing memory owner')
                after=(await information.current_page('',1))[0]
                self.assertEqual(record(after['scores'])['retention'],cast(int,record(before['scores'])['retention'])-7)
                self.assertEqual(record(after['scores'])['belief'],record(before['scores'])['belief'])
                if type(maintained) is Committed:
                    self.assertTrue(record(record(maintained.receipt.result)['facts'])['memory'])
                prepared=await host.advance_dream();self.assertIs(type(prepared),Committed,prepared)
                paused=await admin.inspect_dream('run')
                if paused is None:raise AssertionError('Missing run')
                self.assertIs(type(await admin.pause_dream('pause-frozen','run',cast(int,paused['revision']),1)),Committed)
                paused=await admin.inspect_dream('run')
                if paused is None:raise AssertionError('Missing run')
                self.assertIs(type(await admin.resume_dream('resume-frozen','run',cast(int,paused['revision']),1)),Committed)
                for stage in ('review-result','review-disposition','persona-material'):
                    value=await host.advance_dream();self.assertIs(type(value),Committed,(stage,value))
                await asyncio.sleep(30)
                generated=await host.advance_dream();self.assertIs(type(generated),Committed,generated)
                await asyncio.sleep(30)
                reviewed=await host.advance_dream();self.assertIs(type(reviewed),Committed,reviewed)
                published=await host.advance_dream();self.assertIs(type(published),Committed,published)
                if host.current_persona is None:raise AssertionError('Missing public persona')
                current=await host.current_persona.port.read_current(time.monotonic()+5)
                self.assertIs(type(current),Available,current)
                if type(current) is Available:
                    self.assertEqual(current.value['text'],outputs[1]['text']);self.assertEqual(current.value['revision'],2)
                    self.assertEqual(current.value['review_status'],'MODEL_REVIEWED')
                self.assertEqual(len(requests),3);self.assertFalse(failures)
                sent=json.loads(requests[1])
                body=json.loads(sent['messages'][1]['content'])
                self.assertEqual(len(body['evidence']),1)
                self.assertEqual(body['evidence'][0]['object']['revision'],2)
                self.assertTrue(body['evidence'][0]['sources'][0]['members'][0]['event'])
                run=await admin.inspect_dream('run')
                if run is None:raise AssertionError('Missing run')
                self.assertIsNone(run['active_step_id'])
                self.assertEqual(run['model_calls_used'],3)
                finished=await host.advance_dream();self.assertIs(type(finished),Committed,finished)
                ended=await admin.inspect_dream('run')
                if ended is None:raise AssertionError('Missing ended run')
                self.assertEqual(ended['state'],'COMPLETED');self.assertEqual(ended['exit_result'],'PUBLISHED_NEW')
                await check_public_interfaces(self,host,outputs[1]['text'])
            finally:
                if host.combination.periodic is not None and host.combination.periodic.job is not None:
                    await asyncio.wait((host.combination.periodic.job,))
                self.assertTrue(await host.close())

    async def test_two_updates_then_rejection_focused_exit_and_new_interpreter(self):
        import hashlib
        import json
        import subprocess
        import sys
        texts=('我是 Iris，独立判断并清楚表达不确定。','我是 Iris，重视独立判断、坦诚沟通与明确的事实依据。','我是 Iris，使用另一种待监管的表达。')
        outputs=tuple(item for index,text in enumerate(texts) for item in (
            {'schema_version':1,'text':text,'basis_refs':[],'change_reason':'整理现有稳定身份的表达。'},
            {'schema_version':1,'decision':'APPROVE' if index<2 else 'REJECT','reason':'本次独立监管的明确决定。'}))
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_dream_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+360))
                current=None
                for index in range(3):
                    if index:await asyncio.sleep(30)
                    control=host.combination.dream
                    if control is None or host.runtime is None:raise AssertionError('Missing native owner')
                    schedule=await control.schedule()
                    if schedule is None:raise AssertionError('Missing schedule')
                    rid='run-'+str(index)
                    self.assertIs(type(await admin.start_dream('start-'+rid,rid,cast(int,schedule['revision']),host.runtime.gate.epoch,mode='FOCUSED')),Committed)
                    run=await admin.inspect_dream(rid)
                    if run is None:raise AssertionError('Missing run')
                    self.assertIs(type(await admin.resume_dream('resume-'+rid,rid,cast(int,run['revision']),cast(int,run['mode_epoch']))),Committed)
                    for _ in range(2):
                        value=await host.advance_dream();self.assertIs(type(value),Committed,value)
                    await asyncio.sleep(30)
                    for _ in range(3):
                        value=await host.advance_dream();self.assertIs(type(value),Committed,value)
                    self.assertEqual(host.runtime.gate.state,'NORMAL')
                    ended=await admin.inspect_dream(rid)
                    if ended is None:raise AssertionError('Missing run')
                    self.assertEqual(ended['state'],'COMPLETED')
                    self.assertEqual(ended['exit_result'],'PUBLISHED_NEW' if index<2 else 'KEPT_PREVIOUS')
                    if host.current_persona is None:raise AssertionError('Missing public reader')
                    current=await host.current_persona.port.read_current(time.monotonic()+5)
                    self.assertIs(type(current),Available,current)
                    if type(current) is not Available:raise AssertionError(current)
                    self.assertEqual(current.value['text'],texts[min(index,1)])
                    self.assertEqual(current.value['revision'],min(index+2,3))
                self.assertEqual(len(requests),6);self.assertFalse(failures)
                if type(current) is not Available:raise AssertionError(current)
                expected=current.value
            finally:self.assertTrue(await host.close())
            completed=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','tests.dream_maintenance.periodic_reopen_worker',str(root)],capture_output=True,text=True,timeout=30)
            self.assertEqual(completed.returncode,0,completed.stderr)
            restored=json.loads(completed.stdout)
            self.assertEqual(restored,{'revision':3,'publication_id':expected['publication_id'],
                'text_digest':hashlib.sha256(cast(str,expected['text']).encode()).hexdigest(),'new_credentials':0})
            self.assertEqual(len(requests),6)


async def check_public_interfaces(test,host,expected_text,*,tag=""):
    """The current pointer is identical in native replies, HTTP and observations."""
    from tests.information.test_queries import query
    from companion_memory.memory.formats import record
    from companion_memory.information.errors import InformationRejected
    identity=HostIdentity('business'+tag,'supervisor','host','entry',frozenset(('prepare_reply',)),(),time.monotonic()+120)
    port=await host.bind_business(identity)
    response=await port.prepare_reply(query('native-reply'+tag,participant_ids=(),situation='合成验证'))
    test.assertIs(type(response),Found,response)
    if type(response) is not Found:raise AssertionError(response)
    persona=record(record(response.value['sections'])['persona'])
    test.assertEqual(persona['text'],expected_text)
    if host.http is None or host.observations is None:raise AssertionError('Missing public interfaces')
    observed=host.observations.bind(frozenset(('dream','maintenance','persona')))
    for scope in ('dream','maintenance','persona'):
        result=await observed.read(scope,{})
        test.assertIs(type(result),Found,result)
        test.assertNotIn(expected_text,repr(result))
    restricted=host.observations.bind(frozenset(('dream',)))
    test.assertIs(type(await restricted.read('persona',{})),InformationRejected)
    address=await host.http.start() if host.http.server is None else host.http.server.sockets[0].getsockname()
    token=host.http.issue_test_session(port,time.monotonic()+30)
    payload=json.dumps(query('http-reply'+tag,participant_ids=(),situation='合成验证'),ensure_ascii=False).encode()
    reader,writer=await asyncio.open_connection(*address)
    writer.write(('POST /api/host/prepare HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+'\r\nContent-Length: '+str(len(payload))+'\r\n\r\n').encode()+payload)
    await writer.drain();raw=await asyncio.wait_for(reader.read(),5)
    writer.close();await writer.wait_closed()
    header,body=raw.split(b'\r\n\r\n',1)
    test.assertIn(b'200 OK',header,body)
    reply=json.loads(body)
    test.assertEqual(reply['value']['sections']['persona'],dict(persona))
