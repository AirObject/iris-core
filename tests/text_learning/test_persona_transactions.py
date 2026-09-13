"""Real first-input, three-generation owner transactions and atomic publication.

Native transaction invocations are driven by trusted test coordination. Provider
HTTP, SQLite, audits and the actual focused gate are exercised together. Public
management orchestration and host lifecycle require their own tests.
"""
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import gc
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from typing import cast
import unittest
from dataclasses import replace
from companion_memory.ingress.events import plain
from companion_memory.persistence import PersistenceService,ResultBoundCommand,Committed
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.text_records import stable_identity,digest as semantic_digest
from companion_memory.persistence.content_codec import encode_content
from companion_memory.provider import WorkGrant,ResultGrant,Ready,Completed
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.service import OPTIONALS
from companion_memory.provider.values import as_record,freeze
from companion_memory.runtime.content_assembly import ContentAssembly
from companion_memory.runtime.content_gate import ContentGate
from companion_memory.self_model.transactions import PersonaTransactions
from companion_memory.self_model.focused_work import InitialPersonaWork,PersonaWorkPermit
from companion_memory.self_model.current import CurrentPersona,CurrentPersonaPort,Available,Unavailable
from companion_memory.self_model.preparation import retained_material
from companion_memory.self_model.formats import candidate_digest,isolate_candidate
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from tests.text_learning.provider_support import Fixture
from tests.text_learning.test_provider import response
from tests.persistence.support import sqlite_fault


class PersonaTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_original_generations_review_and_publication_have_atomic_owner_effects(self):
        database='text-provider-database';instance='instance'
        run_id=stable_identity('persona-run',database,instance);input_id=stable_identity('self-input',database,instance)
        payload=json.dumps({'schema_version':1,'text':'No preset background was supplied.','initial_input_ids':[input_id]})
        body=response(payload).split(b'\r\n\r\n',1)[1];requests=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def do_POST(self):
                size=int(self.headers['Content-Length'])
                if not 0<size<=131072:self.send_error(400);return
                requests.append(self.rfile.read(size))
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers()
                self.wfile.write(body);self.close_connection=True
            def log_message(self,format,*args):pass
        listener=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=lambda:listener.serve_forever(poll_interval=.01));worker.start()
        with TemporaryDirectory() as directory:
            root=Path(directory)
            grant=WorkGrant('self_model',instance,None,'PERSONA',('fixture_generation',),('GENERATION',),'self_model','operator',(run_id,),
                prompt_revisions=('persona_prompt',),internal_dream=True)
            fixture=Fixture(root,listener.server_port,grant)
            gate=ContentGate(1)
            fixture.resources=replace(fixture.resources,gate=gate.binding)
            content=ContentAssembly(information_format=True,text_format=True);persona=PersonaTransactions(content,fixture.service)
            fixture.storage=PersistenceService(fixture.configuration.repositories+content.repositories+fixture.assembly.repositories,
                fixture.configuration.commands+content.commands+fixture.assembly.commands,assembly_format='MODEL_TEXT_LEARNING_V1')
            collection_enabled=gc.isenabled();gc.disable()
            try:
                self.assertIs(type(await fixture.initialize()),Ready)
                stored=fixture.stored;assert stored is not None
                content.bind(fixture.storage,stored,instance);gate.publish_mode('NORMAL',1)
                registration=persona.bind(InitialSelfBinding('operator','self','Local identity','ACTUAL_INPUT'),gate)
                focused=InitialPersonaWork(persona)
                current_persona=CurrentPersona(persona)
                self.assertIs(type(await current_persona.port.read_current(time.monotonic()+3)),Unavailable)
                with self.assertRaises(TypeError):CurrentPersonaPort()
                fixture.service.revoke(fixture.work)
                owner=persona.persona;initial=persona.initial;assert owner is not None and initial is not None
                async def runtime(kind,key,values):
                    definition=content.command_definition(kind)
                    return await content.operations[kind].execute(key,ResultBoundCommand(definition.command_version,{'operation_id':key,**values},
                        {r.event_slot:{'actor':'operator'} for r in definition.required_audits}))
                self.assertIs(type(await runtime('initialize_content_runtime','runtime',{})),Committed)
                self.assertIs(type(await registration.register_initial_self('input','NO_PRESET','I explicitly choose no preset identity.','ACTUAL_INPUT')),Committed)
                async def execute(kind,key,values,*,work=None,result_owner=None,completion=None):
                    definition=persona.definitions[kind]
                    complete={'operation_id':key,**values}
                    scope=persona.retain(kind,complete,work=work,result_owner=result_owner,completion=completion)
                    try:
                        saved=await fixture.storage.bind_operation(definition,instance).execute(key,ResultBoundCommand(1,complete,
                            {r.event_slot:{'actor':'operator'} for r in definition.required_audits}))
                        self.assertEqual(fixture.storage.get_health().writes_in_flight,0)
                        return saved
                    finally:persona.release(scope)
                async def read_run():
                    found=await owner.read_original('run',run_id,time.monotonic()+3)
                    self.assertIsNotNone(found);assert found is not None
                    return found.value
                async def mode():return (await content.rows.read('mode_get',{'mode_id':'instance_mode'}))[0]
                # No invocation and ordinary FINISH/ENTER cannot create the run.
                self.assertIsNot(type(await runtime('change_content_mode','outside-enter',{'action':'ENTER','expected_epoch':1,'run_id':run_id,'publication_id':None})),Committed)
                gate.close_ordinary()
                params={'input_id':input_id,'expected_self_revision':1,'expected_epoch':1}
                for prefix in ('INSERT INTO self_model_initial_persona_runs','UPDATE runtime_content_mode','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
                    seen=[]
                    def fail(sql):
                        if sql.startswith(prefix):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                    fixture.hooks.before=fail
                    self.assertIsNot(type(await execute('prepare_initial_persona','prepare',params)),Committed)
                    self.assertTrue(seen);fixture.hooks.before=lambda sql:None
                    self.assertIsNone(await owner.read_original('run',run_id,time.monotonic()+3))
                    self.assertEqual((await mode())['state'],'NORMAL')
                prepared=await execute('prepare_initial_persona','prepare',params)
                self.assertIs(type(prepared),Committed,prepared)
                replay=await execute('prepare_initial_persona','prepare',params)
                assert type(prepared) is Committed and type(replay) is Committed
                self.assertEqual(prepared.receipt,replay.receipt)
                gate.publish_mode('DREAM_PREPARING',2)
                ready=await runtime('change_content_mode','ready',{'action':'READY','expected_epoch':2,'run_id':run_id,'publication_id':None})
                self.assertIs(type(ready),Committed,ready);gate.resolve_cutoff('DREAM_FOCUSED',3)
                self.assertFalse(gate.admit(grant,3))
                self.assertFalse(gate.check(grant))
                self.assertFalse(gate.dispatch(grant,lambda:self.fail('Forged internal role dispatched.')))
                with self.assertRaises(TypeError):PersonaWorkPermit()
                keys=[];prior_candidate_ids=[]
                for generation in (1,2,3):
                    current=await read_run();epoch=(await mode())['epoch']
                    keys.append(current['provider_operation_key'])
                    associated=await execute('associate_initial_persona_request','associate-'+str(generation),{'run_id':run_id,'expected_revision':current['revision'],
                        'generation':generation,'expected_epoch':epoch})
                    self.assertIs(type(associated),Committed,associated)
                    current=await read_run();found=await initial.read_original(input_id,time.monotonic()+3);assert found is not None
                    material=retained_material(stored,found.input,current)
                    permit=await focused.issue(run_id,generation,str(current['provider_operation_key']),time.monotonic()+3)
                    self.assertTrue(gate.check(permit.grant));self.assertIsNot(permit.grant,grant)
                    self.assertEqual(permit.material,material)
                    fixture.work=permit.work
                    generated=await fixture.work.generate({**cast(dict,plain(material.request)),'deadline':time.monotonic()+3,'cancellation':fixture.cancellation.token})
                    self.assertIs(type(generated),Completed,generated);assert type(generated) is Completed
                    self.assertEqual(generated.record['outcome'],'SUCCEEDED');self.assertTrue(fixture.work.consumers_ended())
                    rid=str(generated.record['object_id'])
                    self.assertIs(type(await execute('confirm_initial_persona_request','confirm-'+str(generation),{'run_id':run_id,'expected_revision':current['revision'],
                        'generation':generation,'request_id':rid},work=fixture.work)),Committed)
                    current=await read_run();result_owner=fixture.service.bind_result_owner(ResultGrant('self_model',(rid,)))
                    original=as_record(freeze({**{name:material.request.get(name) for name in OPTIONALS},**material.request},131072,owned=True))
                    terminal=await result_owner.verify_terminal(rid,original)
                    self.assertIs(type(terminal),TerminalVerified,terminal);assert type(terminal) is TerminalVerified
                    completion=await result_owner.confirm_completion(terminal.value)
                    self.assertIs(type(completion),ConfirmedCompletion,completion);assert type(completion) is ConfirmedCompletion
                    resolved=await execute('record_initial_persona_resolution','resolve-'+str(generation),{'run_id':run_id,'expected_revision':current['revision'],
                        'generation':generation,'provider_reference':rid,'evidence_revision':generated.record['revision']},work=fixture.work,result_owner=result_owner,completion=completion)
                    self.assertIs(type(resolved),Committed,resolved)
                    current=await read_run();proposal=await owner.read_original('candidate',str(current['resolution_id']),time.monotonic()+3);assert proposal is not None
                    prior_candidate_ids.append(proposal.value['object_id'])
                    digest=candidate_digest(proposal.value)
                    review=await execute('review_initial_persona','review-'+str(generation),{'run_id':run_id,'expected_revision':current['revision'],
                        'candidate_id':proposal.value['object_id'],'candidate_revision':1,'candidate_digest':digest,'decision':'REJECT' if generation<3 else 'APPROVE'})
                    self.assertIs(type(review),Committed,review);current=await read_run()
                    if generation<3:
                        params={'run_id':run_id,'expected_revision':current['revision'],'expected_generation':generation,'prior_resolution_id':proposal.value['object_id'],'expected_epoch':epoch}
                        reviewed=await owner.read_original('candidate',str(proposal.value['object_id']),time.monotonic()+3);assert reviewed is not None
                        tampered={**reviewed.value,'text':'Altered retained result.','text_digest':semantic_digest('Altered retained result.')}
                        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                            db.execute('UPDATE self_model_initial_persona_candidates SET body=? WHERE object_id=?',
                                (encode_content(isolate_candidate(tampered),4096).decode(),proposal.value['object_id']))
                        self.assertIsNot(type(await execute('retry_initial_persona','damaged-retry-'+str(generation),params,
                            work=fixture.work,result_owner=result_owner,completion=completion)),Committed)
                        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                            db.execute('UPDATE self_model_initial_persona_candidates SET body=? WHERE object_id=?',
                                (encode_content(reviewed.value,4096).decode(),proposal.value['object_id']))
                        retried=await execute('retry_initial_persona','retry-'+str(generation),params,work=fixture.work,result_owner=result_owner,completion=completion)
                        self.assertIs(type(retried),Committed,retried)
                        new_mode=await mode();gate.publish_mode('DREAM_FOCUSED',cast(int,new_mode['epoch']))
                        self.assertEqual((await read_run())['generation'],generation+1)
                    else:
                        params={'run_id':run_id,'expected_revision':current['revision'],'candidate_id':proposal.value['object_id'],'candidate_revision':2,'candidate_digest':digest,'expected_epoch':epoch}
                        for prefix in ('INSERT INTO self_model_persona_publications','UPDATE self_model_initial_persona_runs','UPDATE runtime_content_mode','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
                            seen=[]
                            def fail_publication(sql):
                                if sql.startswith(prefix):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                            fixture.hooks.before=fail_publication
                            self.assertIsNot(type(await execute('publish_initial_persona','publish',params,result_owner=result_owner,completion=completion)),Committed)
                            self.assertTrue(seen);fixture.hooks.before=lambda sql:None
                            self.assertIsNone(await owner.current_original(time.monotonic()+3))
                            self.assertEqual((await read_run())['state'],'APPROVED');self.assertEqual((await mode())['state'],'DREAM_FOCUSED')
                        published=await execute('publish_initial_persona','publish',params,result_owner=result_owner,completion=completion)
                        self.assertIs(type(published),Committed,published)
                        repeated=await execute('publish_initial_persona','publish',params,result_owner=result_owner,completion=completion)
                        assert type(repeated) is Committed and type(published) is Committed
                        self.assertEqual(repeated.receipt,published.receipt)
                        self.assertEqual((await read_run())['state'],'PUBLISHED');self.assertEqual((await mode())['state'],'DRAINING')
                        new_mode=await mode();gate.publish_mode('DRAINING',cast(int,new_mode['epoch']))
                    fixture.service.revoke(result_owner)
                    self.assertTrue(focused.release(permit))
                    self.assertFalse(gate.check(permit.grant))
                    del permit
                    del completion,terminal,result_owner
                    self.assertEqual(len(fixture.service._terminal_evidence),0)
                    self.assertEqual(len(fixture.service._completion_evidence),0)
                self.assertEqual(len(set(keys)),3);self.assertEqual(len(requests),3)
                self.assertEqual(len(set(prior_candidate_ids)),3)
                published=await owner.current_original(time.monotonic()+3);assert published is not None
                self.assertEqual(published.value['generation'],3);self.assertEqual(published.value['text'],'No preset background was supplied.')
                projected=await current_persona.port.read_current(time.monotonic()+3)
                self.assertIs(type(projected),Available,projected);assert type(projected) is Available
                self.assertEqual(projected.value['text'],published.value['text']);self.assertFalse(projected.value['stale'])
                self.assertEqual(len(requests),3)
                self.assertTrue(await current_persona.close(time.monotonic()+3))
                self.assertIsNot(type(await current_persona.port.read_current(time.monotonic()+3)),Available)
                with self.assertRaises(InvalidValue):persona.retain('retry_initial_persona',{'operation_id':'fourth','run_id':run_id,'expected_revision':(await read_run())['revision'],
                    'expected_generation':3,'prior_resolution_id':prior_candidate_ids[-1],'expected_epoch':(await mode())['epoch']})
                for cid in prior_candidate_ids:
                    self.assertIsNotNone(await owner.read_original('candidate',str(cid),time.monotonic()+3))
            finally:
                if persona.gate is not None and persona.gate._initial_persona is not None:persona.gate._initial_persona.close()
                persona.close()
                if content._bound:content.close()
                await fixture.close()
                listener.shutdown();listener.server_close();worker.join(3)
                self.assertFalse(worker.is_alive())
                if collection_enabled:gc.enable()
