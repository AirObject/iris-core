"""Three real controlled HTTP turns and four durable native current-read steps."""
import asyncio
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found,Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record,sequence
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.self_model.approved_import_evidence import verify_approved_persona
from companion_memory.cognition.daily_material import freeze_material
from companion_memory.cognition.daily_resources import TRANSFORM_VERSION
from companion_memory.provider.daily_network import DailyNetwork
from companion_memory.provider.daily_protocol import encode_daily_request
from companion_memory.persistence.content_codec import encode_content
from tests.runtime.configuration_support import event
from .test_tools import ToolFixture
from .test_protocol import response

@contextmanager
def responses(outputs):
    requests=[];failures=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                raw=self.rfile.read(int(self.headers['Content-Length']));requests.append(raw)
                output=outputs[len(requests)-1]
                if callable(output):output=output(json.loads(raw))
                value=output if type(output) is bytes else response(output);self.send_response(200);self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
            except (OSError,IndexError,ValueError) as failure:failures.append(type(failure).__name__)
        def log_message(self,format,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever);worker.start()
    try:yield server.server_port,requests,failures
    finally:server.shutdown();server.server_close();worker.join(5)

class ReasoningFixture(ToolFixture):
    offset=0.0
    def create_network(self):return DailyNetwork(lambda key:True,('daily_generation_account','fixture_embedding'),monotonic=lambda:time.monotonic()+self.offset)
    async def freeze_context(self):
        c=self.c
        directory=Path(__file__).parent/'fixtures'/'approved_persona'
        evidence=verify_approved_persona((directory/'review.md').read_bytes(),(directory/'publication.json').read_bytes(),(directory/'reconciliation.json').read_bytes())
        import_grant=c.persona.bind(self.storage,self.stored,c.content.memory,InitialSelfBinding('bootstrap','self','Iris','SYNTHETIC_FIXTURE'),evidence,new_synthetic_instance=True)
        if import_grant is None:raise AssertionError()
        imported=await c.persona.import_approved_persona(import_grant,'persona')
        if type(imported) is not Committed:raise AssertionError(imported)
        steps:list[tuple[str,str,dict[str,object]]]=[('register_content_entry','entry',{'entry_id':'entry','host_id':'host','platform_id':'sample_platform','external_entry_id':'conversation'})]
        for n in range(3):
            value=event('event-'+str(n),'杯子在展板旁。');value['event_version']=2
            steps.append(('accept_media_event','event-'+str(n),{'entry_id':'entry','event':json.dumps(value,ensure_ascii=False,separators=(',',':'))}))
        steps.extend((('select_content_preparation','select',{'entry_id':'entry','preparation_id':'preparation','batch_id':'batch','run_id':'run'}),
            ('claim_content_preparation','claim',{'preparation_id':'preparation','expected_revision':1,'owner_generation':1}),
            ('complete_content_preparation','prepare',{'preparation_id':'preparation','expected_revision':2,'owner_generation':1}),
            ('freeze_content_batch','freeze',{'preparation_id':'preparation','expected_revision':3,'owner_generation':1})))
        for kind,key,value in steps:
            outcome=await self.runtime.execute(kind,key,value)
            if type(outcome) is not Committed:raise AssertionError((kind,outcome))
        frozen=await c.content.read_daily_batch('batch');self.original=record(frozen['source']);self.work=record(frozen['work'])
        world=MappingProxyType({'kind':'REAL','context_id':None})
        self.grant=self.tools.bind(self.port,'entry','partition',('self',),(world,))
        publication=await c.persona.read_current()
        if type(publication) is not Found:raise AssertionError(publication)
        self.publication=publication.value
        bodies=[]
        # Each byte is obtained from the actual source-holding native ingress owner.
        for member in sequence(self.original['ordered_members']):
            member=record(member)
            bodies.append({'member':member,'payload':(await c.content.ingress.rows.read('payload',{'message_id':member['message_id']}))[0]['body'],'interpretations':()})
        from companion_memory.provider.values import freeze
        subject=await self.port.read_subject('self')
        if type(subject) is not Found:raise AssertionError(subject)
        body=encode_content(cast(Value,freeze({'source':self.original,'members':bodies,'persona':publication.value,'authority_digest':self.grant.digest,'related':(),'subjects':(subject.value,),'authority':{'subject_ids':('self',),'worlds':(world,),'writable_objects':(),'route_ids':(),'memory_ref':self.memory.read_grant_reference(self.port),'partition_id':'partition'}},262144,owned=True)),262144)
        binding=self.provider.bindings['LEARNING'];role=next(record(r) for r in sequence(self.value.text.record('provider.transport')['roles']) if record(r)['role']=='LEARNING')
        now=cast(int,self.original['frozen_at_us']);mid=c.reasoning.key('initial-context','run') if c.reasoning.bound else 'initial-context'
        metadata={'format_version':1,'object_id':mid,'revision':1,'database_id':self.stored.database_id,'instance_id':'instance','config_snapshot_id':self.stored.snapshot_id,
            'created_at_us':now,'updated_at_us':now,'context_version':2,'context_kind':'LEARNING','owner_ref':'run','batch_id':'batch','run_id':'run','source_id':self.original['source_id'],
            'state':'STORED','persona_publication_id':publication.value['publication_id'],'persona_revision':publication.value['revision'],'prompt_ref':role['prompt_ref'],
            'schema_ref':binding.schema_ref,'transform_ref':TRANSFORM_VERSION,'model_binding_digest':sha256(body).hexdigest(),
            'ordered_members':tuple({'role':{'T':'TARGET','H':'HISTORY','R':'RECENT'}[cast(str,record(m)['role'])],'message_id':record(m)['message_id'],'payload_digest':record(m)['payload_digest']} for m in sequence(self.original['ordered_members'])),
            'related_objects':(),'wire_digest':sha256(encode_daily_request(binding,body.decode())).hexdigest(),'input_token_estimate':None,'reservation_input_bound':262144,
            'original_operation':{'owner_namespace':'cognition','operation_kind':'freeze_reasoning_run','scope_id':'instance','operation_key':'temporary'},'terminal_operation':None}
        def verify(uow,material,digest,deadline):
            source=c.content.participate_daily_batch(uow,'batch',cast(int,self.work['generation']),cast(int,self.work['revision']))
            current=c.persona.participate_current(uow,cast(str,publication.value['publication_id']),cast(int,publication.value['revision']))
            return source==self.original and current is not None and digest==self.grant.digest and material.body==body and deadline==now+1200000000
        def allowed(run,uow,fresh):
            return run['authority_digest']==self.grant.digest and (not fresh or self.runtime.gate.information_checkpoint() is not None)
        c.reasoning.bind(self.stored,self.provider,self.tools,verify,allowed)
        c.application.bind(self.stored,self.storage,lambda run,authority,uow:run['authority_digest']==self.grant.digest and authority.subject_ids==self.grant.subject_ids)
        metadata['original_operation']['operation_key']=c.reasoning.key('reasoning-freeze','run')
        self.initial_material=freeze_material(metadata,body)
        self.provider.authorize=c.reasoning.authorize_request;self.provider.received=c.reasoning.verify_received
        result=await c.reasoning.freeze_run(self.initial_material,self.grant.digest,now+1200000000)
        if type(result) is not Committed:raise AssertionError(result)
        return result
    async def close(self):
        if not self.c.application.close():raise AssertionError('Application retains actual work')
        if not self.c.reasoning.close():raise AssertionError('Reasoning retains actual work')
        if not self.c.persona.close():raise AssertionError('Persona retains actual work')
        self.tools.release(self.grant)
        await super().close()

class DailyReasoningTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_turns_four_original_tools_and_complete_reception_before_later_input(self):
        outputs=(
            {'schema_version':1,'kind':'TOOL','tools':[{'name':'read_subjects','arguments':{'subject_ids':['self']}},{'name':'list_goals','arguments':{'world_scope':'REAL','limit':4}}]},
            {'schema_version':1,'kind':'TOOL','tools':[{'name':'read_memories','arguments':{'refs':[{'object_id':'absent','expected_revision':1}]}},{'name':'search_memories','arguments':{'query':'杯子','world_scope':{'kind':'REAL','context_id':None},'limit':4}}]},
            {'schema_version':1,'kind':'FINAL','actions':[]})
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            fixture=await ReasoningFixture(Path(directory),port).open()
            try:
                await fixture.freeze_context();fixture.network.resume()
                for attempt in range(3):
                    try:value=await fixture.c.reasoning.process('run',fixture.grant,allow_first_send=True)
                    except OwnerFailure as failure:
                        if failure.reason!='ADMISSION_FULL' or fixture.network.observation().occupied:raise
                    else:break
                    await fixture.network.wait_quiet(time.monotonic()+35)
                else:raise AssertionError('Third original turn did not finish')
                if type(value) is not Found:raise AssertionError(value)
                self.assertEqual(value.value['state'],'FINAL_READY');self.assertEqual(len(requests),3);self.assertFalse(failures)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_reasoning_turns').fetchone()[0],3)
                    self.assertEqual(db.execute("SELECT count(*) FROM cognition_reasoning_tools WHERE json_extract(body,'$.state')='RESULT_STORED'").fetchone()[0],4)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],3)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_embedding_handoff_leaf').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.turn_count'),json_extract(body,'$.tool_count') FROM cognition_reasoning_runs").fetchone(),(3,4))
                first,second,third=(json.loads(raw) for raw in requests)
                self.assertNotIn('transcript',json.loads(first['messages'][1]['content']))
                self.assertEqual(len(json.loads(second['messages'][1]['content'])['transcript']),3)
                self.assertEqual(len(json.loads(third['messages'][1]['content'])['transcript']),6)
                confirmed=await fixture.c.reasoning.process('run',fixture.grant,allow_first_send=False)
                self.assertIs(type(confirmed),Found);self.assertEqual(len(requests),3)
                applied=await fixture.c.application.apply('run')
                self.assertIs(type(applied),Committed,applied)
                if type(applied) is not Committed:raise AssertionError(applied)
                materials=fixture.c.materials;root=fixture.initial_material.manifest
                held=await materials.borrow(cast(str,root['object_id']),cast(str,root['payload_digest']),'run',time.monotonic()+5)
                with sqlite3.connect(fixture.path) as db:before=db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0]
                with self.assertRaises(OwnerFailure) as refused:await fixture.c.reasoning.retire_material('run')
                self.assertTrue(refused.exception.cleanup_pending)
                with sqlite3.connect(fixture.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],before)
                materials.release_reader(held)
                retired=await fixture.c.reasoning.retire_material('run');self.assertIs(type(retired),Found,retired)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT count(*) FROM cognition_learning_contexts WHERE json_extract(body,'$.state')!='RELEASED'").fetchone()[0],0)
                    audit_count=db.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                self.assertIs(type(await fixture.c.reasoning.retire_material('run')),Found)
                with sqlite3.connect(fixture.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],audit_count)
                repeated=await fixture.c.application.apply('run')
                self.assertIs(type(repeated),Committed,repeated)
                if type(repeated) is not Committed:raise AssertionError(repeated)
                self.assertEqual(applied.receipt,repeated.receipt);self.assertEqual(len(requests),3)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'SUCCEEDED')
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_candidate_applications').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.phase') FROM cognition_reasoning_runs").fetchone()[0],'TERMINAL')
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0],0)
            finally:await fixture.close()
