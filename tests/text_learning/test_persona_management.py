"""The local management port owns generation, original evidence and human review.

The actual runtime owns the ordinary-work cutoff and preparation drain. All
management calls use the native public port and focused gate; no caller supplies
Provider evidence or generated candidate text.
"""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from typing import cast
import unittest
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.persistence import PersistenceService,ResultBoundCommand,Committed,Found
from companion_memory.persistence.completion import CompletionScope
from companion_memory.persistence.text_records import stable_identity
from companion_memory.provider import Ready
from companion_memory.runtime.content_assembly import ContentAssembly
from companion_memory.runtime.content_gate import ContentGate
from companion_memory.runtime.content_service import ContentRuntimeService
from companion_memory.cognition.text_candidates import TextCandidateInput
from companion_memory.self_model.results import Rejected,Failed
from companion_memory.self_model.transactions import PersonaTransactions
from companion_memory.self_model.management import PersonaManagement,PersonaInitializationPort,SavedResolution
from companion_memory.self_model.formats import candidate_digest
from tests.text_learning.provider_support import Fixture
from tests.text_learning.test_provider import response


class PersonaManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_operator_generates_rejects_retries_and_publishes_without_supplied_proofs(self):
        database='text-provider-database';instance='instance'
        run_id=stable_identity('persona-run',database,instance);input_id=stable_identity('self-input',database,instance)
        body=response(json.dumps({'schema_version':1,'text':'A supplied external preset remains external.',
            'initial_input_ids':[input_id]})).split(b'\r\n\r\n',1)[1];requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size=int(self.headers['Content-Length'])
                if not 0<size<=131072:self.send_error(400);return
                requests.append(self.rfile.read(size))
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):pass
        listener=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=lambda:listener.serve_forever(poll_interval=.01));worker.start()
        try:
            with TemporaryDirectory() as directory:
                fixture=Fixture(Path(directory),listener.server_port);gate=ContentGate(1)
                fixture.resources=replace(fixture.resources,gate=gate.binding)
                content=ContentAssembly(information_format=True,text_format=True);transactions=PersonaTransactions(content,fixture.service)
                fixture.storage=PersistenceService(fixture.configuration.repositories+content.repositories+fixture.assembly.repositories,
                    fixture.configuration.commands+content.commands+fixture.assembly.commands,assembly_format='MODEL_TEXT_LEARNING_V1')
                manager=None;native_runtime=None
                try:
                    self.assertIs(type(await fixture.initialize()),Ready);stored=fixture.stored;assert stored is not None
                    fixture.service.revoke(fixture.work)
                    content.bind(fixture.storage,stored,instance)
                    native_runtime=ContentRuntimeService(content,fixture.service,TextCandidateInput(stored),'fixture_generation',None,gate=gate)
                    transactions.bind(InitialSelfBinding('local-operator','self','Local self','ACTUAL_INPUT'),gate)
                    manager=PersonaManagement(transactions,native_runtime.focus);port=manager.port
                    async def join():
                        if manager._task is not None:await manager._task
                    async def run():
                        assert transactions.persona is not None
                        value=await transactions.persona.read_original('run',run_id,time.monotonic()+3)
                        assert value is not None
                        return value.value
                    async def runtime(kind,key,values):
                        definition=content.command_definition(kind)
                        return await content.operations[kind].execute(key,ResultBoundCommand(definition.command_version,{'operation_id':key,**values},
                            {a.event_slot:{'actor':'test-coordinator'} for a in definition.required_audits}))
                    self.assertIs(type(await runtime('initialize_content_runtime','runtime',{})),Committed)
                    self.assertIs(type(await native_runtime.initialize()),Found)
                    self.assertIs(type(await port.register_initial_self('input','PRESET','An explicitly supplied external preset.','ACTUAL_INPUT',time.monotonic()+3)),Committed)
                    await join()
                    with self.assertRaises(TypeError):PersonaInitializationPort()
                    forged=object.__new__(PersonaInitializationPort);object.__setattr__(forged,'_owner',manager)
                    self.assertIs(type(await forged.read_pending(run_id,time.monotonic()+3)),Failed)
                    prepared=await port.prepare_initial_persona('prepare',input_id,1,1,time.monotonic()+5)
                    self.assertIs(type(prepared),Committed)
                    await join();self.assertEqual(gate.state,'DREAM_FOCUSED');self.assertEqual(gate.epoch,3)
                    for generation in (1,2):
                        before=await run();key=str(before['provider_operation_key'])
                        saved=await port.generate(run_id,generation,key,time.monotonic()+5)
                        self.assertIs(type(saved),SavedResolution,saved);await join()
                        self.assertEqual(len(requests),generation)
                        self.assertIs(type(await port.generate(run_id,generation,key,time.monotonic()+3)),Rejected);await join()
                        self.assertEqual(len(requests),generation)
                        pending=await port.read_pending(run_id,time.monotonic()+3);await join()
                        self.assertIs(type(pending),Found,pending);assert type(pending) is Found
                        from companion_memory.memory.formats import record
                        candidate=record(pending.value);before=await run();digest=candidate_digest(candidate)
                        reviewed=await port.review_initial_persona('review-'+str(generation),run_id,cast(int,before['revision']),str(candidate['object_id']),
                            cast(int,candidate['revision']),digest,'REJECT' if generation==1 else 'APPROVE',time.monotonic()+3)
                        self.assertIs(type(reviewed),Committed,reviewed);await join();before=await run()
                        expected_epoch=gate.epoch
                        if generation==1:
                            changed=await port.retry_initial_persona('retry',run_id,cast(int,before['revision']),generation,str(candidate['object_id']),gate.epoch,time.monotonic()+3)
                        else:
                            changed=await port.publish_initial_persona('publish',run_id,cast(int,before['revision']),str(candidate['object_id']),2,digest,gate.epoch,time.monotonic()+3)
                        self.assertIs(type(changed),Committed,changed);await join()
                        if generation==1:
                            replay=await port.retry_initial_persona('retry',run_id,cast(int,before['revision']),generation,str(candidate['object_id']),expected_epoch,time.monotonic()+3)
                        else:
                            replay=await port.publish_initial_persona('publish',run_id,cast(int,before['revision']),str(candidate['object_id']),2,digest,expected_epoch,time.monotonic()+3)
                        self.assertIs(type(replay),Committed,replay);await join()
                        assert type(changed) is Committed and type(replay) is Committed
                        self.assertEqual(changed.receipt,replay.receipt)
                    self.assertEqual((await run())['state'],'PUBLISHED');self.assertEqual(gate.state,'NORMAL')
                    self.assertEqual(len(requests),2);self.assertIsNone(manager.work._permit)
                    self.assertTrue(await manager.close(time.monotonic()+3))
                    self.assertIs(type(await port.read_pending(run_id,time.monotonic()+3)),Failed)
                finally:
                    if manager is not None:await manager.close(time.monotonic()+3)
                    if native_runtime is not None:await native_runtime.close()
                    transactions.close()
                    if content._bound:content.close()
                    await fixture.close()
        finally:
            listener.shutdown();listener.server_close();worker.join(3)
            self.assertFalse(worker.is_alive())
