"""Full native text host, public persona management, learning and local querying.

The protocol server computes controlled outputs from the actual frozen request
IDs. SQLite, file owners and scheduler are real. No response claims supplier
quality or human acceptance of generated content.
"""
from dataclasses import replace
import asyncio
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import sqlite3
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import threading
import time
import sys
from typing import cast
import unittest
from unittest.mock import patch
from companion_memory.runtime.text_host import TextHost,TextHostResources
from companion_memory.runtime.content_gate import ContentGate
from companion_memory.media.service import MediaResources
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.persistence import Committed,Found
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from companion_memory.persistence.content_codec import decode_content
from companion_memory.provider import WorkPort
from companion_memory.provider.values import freeze,as_record
from companion_memory.persistence.text_records import stable_identity
from companion_memory.memory.formats import record,sequence
from companion_memory.self_model.formats import candidate_digest
from companion_memory.self_model.management import SavedResolution
from companion_memory.self_model.current import Available
from companion_memory.self_model.results import Failed as SelfFailed
from companion_memory.self_model.local_recovery import PersonaRecoveryGrant
from companion_memory.self_model.storage import PersonaStorage
from companion_memory.runtime.results import Rejected as RuntimeRejected
from companion_memory.information.management import HostIdentity
from companion_memory.information.errors import InformationRejected
from tests.text_learning.provider_support import Fixture
from tests.text_learning.host_driver import confirm_local,complete_learning
from tests.text_learning.test_provider import response
from tests.runtime.configuration_support import event
from tests.information.test_queries import query
from tests.cognition.test_text_output import proposal


def make_host(root: Path,port: int) -> TextHost:
    """Bind the same declared resources in either interpreter, without a send."""
    fixture=Fixture(root,port);gate=ContentGate(1)
    resources=TextHostResources(fixture.storage_resources,MediaResources('text-media',lambda rid,did,path:
        (rid,did,path)==('text-media','text-provider-database',str(root/'media'))),'instance','configuration',fixture.inputs[6],
        InitialSelfBinding('local-operator','self','Local self','ACTUAL_INPUT'))
    return TextHost(fixture.candidate,resources,replace(fixture.resources,gate=gate.binding),gate)


class TextHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_persona_then_zero_one_eight_memories_query_and_original_reopen(self):
        requests=[];counts=iter((0,1,8,1))
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size=int(self.headers['Content-Length'])
                if not 0<size<=131072:self.send_error(400);return
                raw=self.rfile.read(size);requests.append(raw);wire=json.loads(raw)
                user=json.loads(wire['messages'][1]['content'])
                if 'initial_input' in user:
                    output={'schema_version':1,'text':'No external preset was supplied.','initial_input_ids':[user['initial_input']['object_id']]}
                else:
                    target=next(item for item in user['members'] if item['member']['role']=='TARGET')
                    item=proposal();item.update(body='A controlled factual claim.',subject_ids=['self'],speaker_subject_id=None,
                        target_anchors=[{'message_id':target['member']['message_id'],'part':'BODY','item_index':None,'start_utf8':None,'end_utf8':None}])
                    item['basis_refs']=[{'object_id':value['object_id'],'expected_revision':value['revision'],'kind':'SUPPORTS'} for value in user['related']]
                    output={'schema_version':1,'memories':[item for _ in range(next(counts))]}
                body=response(json.dumps(output)).split(b'\r\n\r\n',1)[1]
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):pass
        listener=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=lambda:listener.serve_forever(poll_interval=.01));worker.start()
        try:
            with TemporaryDirectory() as directory:
                root=Path(directory).resolve()
                def host():
                    return make_host(root,listener.server_port)
                h=host();keys=[];receipts=[]
                try:
                    opened=await confirm_local(lambda:h.initialize('CREATE_NEW'));self.assertIs(type(opened),Found,opened)
                    assert type(opened) is Found
                    self.assertFalse(record(opened.value)['learning_ready']);self.assertEqual(len(requests),0)
                    self.assertEqual(len(h.combination.commands),103)
                    runtime=h.runtime;assert runtime is not None and h.persona is not None and h.stored is not None
                    port=h.initialization_port()
                    async def join():
                        assert h.persona is not None
                        if h.persona._task is not None:await h.persona._task
                    self.assertIs(type(await confirm_local(lambda:port.register_initial_self('input','NO_PRESET','I explicitly provide no external preset.','ACTUAL_INPUT',time.monotonic()+5))),Committed)
                    await join();input_id=stable_identity('self-input',h.stored.database_id,'instance');run_id=stable_identity('persona-run',h.stored.database_id,'instance')
                    prepared=await confirm_local(lambda:port.prepare_initial_persona('prepare',input_id,1,1,time.monotonic()+5))
                    self.assertIs(type(prepared),Committed,prepared);await join()
                    owner=h.combination.persona.persona;assert owner is not None
                    run=await owner.read_original('run',run_id,time.monotonic()+3);assert run is not None
                    persona_key=str(run.value['provider_operation_key'])
                    generated=await confirm_local(lambda:port.generate(run_id,1,persona_key,time.monotonic()+5))
                    self.assertIs(type(generated),SavedResolution,generated);await join()
                    pending=await port.read_pending(run_id,time.monotonic()+3);self.assertIs(type(pending),Found,pending);await join()
                    assert type(pending) is Found
                    candidate=record(pending.value);digest=candidate_digest(candidate)
                    run=await owner.read_original('run',run_id,time.monotonic()+3);assert run is not None
                    self.assertIs(type(await port.review_initial_persona('approve',run_id,cast(int,run.value['revision']),str(candidate['object_id']),1,digest,'APPROVE',time.monotonic()+3)),Committed)
                    await join();run=await owner.read_original('run',run_id,time.monotonic()+3);assert run is not None
                    published=await port.publish_initial_persona('publish',run_id,cast(int,run.value['revision']),str(candidate['object_id']),2,digest,h.gate.epoch,time.monotonic()+5)
                    self.assertIs(type(published),Committed,published);await join();self.assertEqual(h.gate.state,'NORMAL')
                    ready=await h.initialize('CREATE_NEW');assert type(ready) is Found
                    self.assertTrue(record(ready.value)['learning_ready'])
                    self.assertIs(type(await confirm_local(lambda:h.register_entry('entry','entry','host','sample_platform','conversation'))),Committed)
                    entry=runtime.bind_entry('entry');read=runtime.memory.bind_read(('self',),('read_subject','get_current'))
                    assert runtime.text_contexts is not None
                    runtime.text_contexts.bind_entry(entry,read,('self',),(),({'kind':'REAL','context_id':None},))
                    for batch,count in enumerate((0,1,8,1)):
                        if batch==3:
                            related=tuple(str(record(value)['object_id']) for value in sequence(record(receipts[-1].result)['object_refs'])[:2])
                            read=runtime.memory.bind_read(('self',*related),('read_subject','get_current'))
                            runtime.text_contexts.bind_entry(entry,read,('self',),related,({'kind':'REAL','context_id':None},))
                        for ordinal in range(3):
                            value=event(f'event-{batch}-{ordinal}','A controlled factual claim.');value['event_version']=2
                            accepted=await confirm_local(lambda:entry.accept_event(f'accept-{batch}-{ordinal}',value))
                            self.assertIs(type(accepted),Committed,accepted)
                        key='learn-'+str(batch);keys.append(key)
                        if batch==0:
                            original_generate=WorkPort.generate
                            async def blocked_once(port,request):
                                h.gate.close_ordinary()
                                try:return await original_generate(port,request)
                                finally:h.gate.resolve_cutoff('NORMAL',h.gate.epoch)
                            with patch.object(WorkPort,'generate',blocked_once):
                                paused=await confirm_local(lambda:entry.run_learning(key))
                            self.assertIs(type(paused),Found,paused);assert type(paused) is Found
                            self.assertEqual(record(paused.value)['state'],'WAITING_ADMISSION');self.assertEqual(len(requests),1)
                            repeated=await entry.run_learning(key)
                            self.assertIs(type(repeated),Found,repeated)
                            contexts=h.assembly.text_transactions.contexts
                            before=await contexts._rows.read('learning_contexts_recovery_page',{'after':'','limit':2})
                            self.assertEqual(len(before),1)
                            header=as_record(freeze(decode_content(str(before[0]['body']).encode(),8192),8192))
                            from companion_memory.cognition.context_storage import StoredContext
                            stored_before=await contexts.load(str(header['object_id']),time.monotonic()+3)
                            self.assertIs(type(stored_before),StoredContext);assert type(stored_before) is StoredContext
                            from companion_memory.cognition.text_context import restore_context
                            from companion_memory.memory.sources import decode_source
                            batch_row=(await h.assembly.rows.read('batches_get',{'batch_id':header['batch_id']}))[0]
                            work_row=(await h.assembly.rows.read('work_get',{'batch_id':header['batch_id']}))[0]
                            text=h.assembly.text_transactions
                            first=restore_context(header,stored_before.leaves,
                                text.request_identity(decode_source(str(batch_row['manifest'])),work_row),text.chat).request
                            async def compare_readmission(port,request):
                                self.assertEqual(await contexts._rows.read('learning_contexts_get',{'object_id':header['object_id']}),before)
                                self.assertEqual(await contexts.load(str(header['object_id']),time.monotonic()+3),stored_before)
                                self.assertEqual(as_record(freeze(request['payload'],131072)),first['payload']);self.assertNotEqual(request['operation_key'],first['operation_key'])
                                return await original_generate(port,request)
                            with patch.object(WorkPort,'generate',compare_readmission):
                                learned=await complete_learning(entry,'explicit-readmission')
                        else:learned=await complete_learning(entry,key)
                        self.assertIs(type(learned),Committed,learned);assert type(learned) is Committed
                        self.assertEqual(len(sequence(record(learned.receipt.result)['object_refs'])),count)
                        receipts.append(learned.receipt)
                    self.assertEqual(len(requests),5)
                    native=await h.bind_query(HostIdentity('query','operator','host','entry',frozenset(('search_memory','prepare_reply')),(),time.monotonic()+300))
                    # Let the real automatic index consume its actual pending
                    # coverage before checking the local read result. No worker
                    # is stopped and no clock or Provider call is substituted.
                    assert h.retrieval is not None and h.scheduler is not None
                    until=time.monotonic()+5
                    while time.monotonic()<until and ((await h.retrieval.memory.coverage_view())['pending_count'] or h.scheduler.index.jobs):await asyncio.sleep(.01)
                    recalled=None
                    for attempt in range(4):
                        recalled=await native.prepare_reply(query('query-text-'+str(attempt),query_text='controlled',participant_ids=(),situation=''))
                        if type(recalled) is Found:break
                        self.assertIs(type(recalled),InformationRejected,recalled);assert type(recalled) is InformationRejected
                        self.assertIn(recalled.error.reason,('REVISION_CONFLICT','ADMISSION_FULL'))
                        await asyncio.sleep(.02)
                    self.assertIs(type(recalled),Found,recalled);assert type(recalled) is Found
                    persona=record(record(record(recalled.value)['sections'])['persona'])
                    self.assertEqual(persona['origin'],'REMOTE_PROVIDER');self.assertEqual(persona['review_status'],'APPROVED')
                    self.assertEqual(record(record(record(recalled.value)['sections'])['runtime'])['model_adapter'],'REMOTE_PROVIDER')
                    self.assertEqual(len(requests),5)
                    # Close wins after the SQLite read but before delivery. The
                    # occupied owner remains retained until the blocked read
                    # actually returns; a timeout must not free that slot.
                    assert h.current_persona is not None
                    current=h.current_persona;arrived=asyncio.Event();release=asyncio.Event()
                    original_read=PersonaStorage.current_original
                    async def paused_read(storage,deadline):
                        value=await original_read(storage,deadline);arrived.set();await release.wait();return value
                    with patch.object(PersonaStorage,'current_original',paused_read):
                        pending_read=asyncio.create_task(current.port.read_current(time.monotonic()+3))
                        await asyncio.wait_for(arrived.wait(),3)
                        self.assertFalse(await current.close(time.monotonic()+.01))
                        self.assertIsNotNone(current._task)
                        release.set();withdrawn=await pending_read
                        self.assertIs(type(withdrawn),SelfFailed,withdrawn)
                        self.assertNotIsInstance(withdrawn,Available)
                        self.assertTrue(await current.close(time.monotonic()+3))
                    refused=await native.prepare_reply(query('closed-persona',query_text='controlled',participant_ids=(),situation=''))
                    self.assertIs(type(refused),InformationRejected,refused);assert type(refused) is InformationRejected
                    self.assertEqual(refused.error.reason,'NOT_READY');self.assertEqual(len(requests),5)
                finally:
                    closed=await h.close()
                    if not closed:
                        print('TEXT_HOST_CLOSE',h.phase,h.state,h.storage.get_health(),h.provider.get_health(),h.runtime.state if h.runtime is not None else None,
                            h.media.get_health())
                    self.assertTrue(closed)
                reopened=host()
                # Model a later legitimate SELF revision in a stopped test
                # database. Recovery must expose staleness without regenerating
                # or rewriting any original input, context or persona record.
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as connection:
                    body=json.loads(connection.execute('SELECT body FROM memory_subjects WHERE subject_id=?',('self',)).fetchone()[0])
                    body['revision']=2
                    from companion_memory.persistence.content_codec import encode_content
                    connection.execute('UPDATE memory_subjects SET revision=2,body=? WHERE subject_id=?',(encode_content(MappingProxyType(body),1024).decode(),'self'))
                try:
                    result=await confirm_local(lambda:reopened.initialize('OPEN_EXISTING'));self.assertIs(type(result),Found,result)
                    assert reopened.current_persona is not None
                    stale=await reopened.current_persona.port.read_current(time.monotonic()+3)
                    self.assertIs(type(stale),Available,stale);assert type(stale) is Available
                    self.assertTrue(stale.value['stale'])
                    assert reopened.runtime is not None
                    entry=reopened.runtime.bind_entry('entry')
                    for key,receipt in zip(keys,receipts,strict=True):
                        original=await entry.run_learning(key)
                        self.assertIs(type(original),Committed,original);assert type(original) is Committed
                        self.assertEqual(original.receipt,receipt)
                    self.assertEqual(len(requests),5)
                finally:self.assertTrue(await reopened.close())
                child=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.text_learning.text_host_process_worker',str(root),str(listener.server_port),*keys,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                try:
                    stdout,stderr=await asyncio.wait_for(child.communicate(),30)
                    self.assertEqual(child.returncode,0,stderr.decode())
                    observed=json.loads(stdout)
                    self.assertTrue(observed['learning_ready']);self.assertTrue(observed['closed'])
                    self.assertEqual(observed['receipts'],[hashlib.sha256(encode_value(receipt_value(value),65536)).hexdigest() for value in receipts])
                    self.assertEqual(len(requests),5)
                finally:
                    if child.returncode is None:child.kill();await child.wait()
        finally:
            listener.shutdown();listener.server_close();worker.join(3);self.assertFalse(worker.is_alive())

    async def test_close_wins_final_recovery_fence_without_publishing_scheduler(self):
        with TemporaryDirectory() as directory:
            host=make_host(Path(directory).resolve(),12345)
            arrived=asyncio.Event();release=asyncio.Event();original=TextHost._verify_persona
            async def pause(value):
                await original(value)
                assert value.persona_recovery is not None
                denied=await value.persona_recovery.recover_local(object.__new__(PersonaRecoveryGrant),time.monotonic()+3)
                self.assertIs(type(denied),SelfFailed,denied)
                arrived.set();await release.wait()
            with patch.object(TextHost,'_verify_persona',pause):
                initialize=asyncio.create_task(host.initialize('CREATE_NEW'))
                try:
                    await asyncio.wait_for(arrived.wait(),5)
                    closing=asyncio.create_task(host.close())
                    await asyncio.sleep(0)
                    self.assertEqual(host.state,'CLOSING');self.assertEqual(host.gate.state,'CLOSED')
                    release.set()
                    result=await initialize
                    self.assertIs(type(result),RuntimeRejected,result)
                    self.assertTrue(await closing)
                    self.assertIsNone(host.scheduler);self.assertIsNone(host.queries)
                    self.assertIs(type(await host.initialize('CREATE_NEW')),RuntimeRejected)
                    self.assertTrue(await host.close())
                finally:
                    release.set();await initialize
                    await host.close()
