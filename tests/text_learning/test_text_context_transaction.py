"""Native context/work publication against real ingress and a SQLite transaction.

The prior reviewed persona is an explicitly installed synthetic fixture. These
tests verify learning's source and context boundary, not initial persona setup,
runtime dispatch permission or the complete text host's recovery publication.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import asyncio
from datetime import datetime,timezone
import json
import sqlite3
import threading
import time
import unittest
import uuid

from companion_memory.configuration.text_persistence import TextConfigurationAssembly
from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
from companion_memory.cognition.text_context import freeze_context
from companion_memory.cognition.text_resources import render_learning_instructions
from companion_memory.memory.formats import record,sequence
from companion_memory.memory.service import MemoryService
from companion_memory.memory.sources import decode_source
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,Committed,ResultBoundCommand,ResultBoundCommandDefinition,RecordSchema,Field,AuditResultBinding,AuditFieldBinding
from companion_memory.persistence.schema import Value
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.text_records import ID,stable_identity,digest
from companion_memory.persistence.text_results import audits,result_schema,result,INTENT
from companion_memory.provider.service import derived_id,OPTIONALS
from companion_memory.provider import LedgerAssembly,ProviderService,Ready as ProviderReady,WorkGrant,ResultGrant,Completed,CancellationSource
from companion_memory.provider.credentials import CredentialResolver,CredentialLease,Available
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.chat_protocol import ChatBinding
from companion_memory.provider.generation_resources import ChatGenerationAdapter,RealGenerationResources
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.values import freeze,as_record
from companion_memory.cognition.text_resources import output_schema
from companion_memory.cognition.context_storage import ReleasedContext
from companion_memory.ingress.events import plain
from companion_memory.logging_service import AuditRequirement
from companion_memory.information.records import FACT,TARGETS
from companion_memory.runtime.content_assembly import ContentAssembly
from companion_memory.self_model.formats import isolate_run,isolate_candidate,isolate_publication,candidate_digest,projection
from tests.cognition.test_text_context import inputs
from tests.persistence.support import Hooks,sqlite_fault
from tests.text_learning.configuration_support import candidate
from tests.text_learning.test_record_catalogs import records
from tests.text_learning.test_text_candidates import proposal
from tests.text_learning.test_provider import response
from tests.provider.test_chat_transport import server
from tests.provider.support import Gate


class TextContextTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_native_stage_rechecks_retained_source_and_live_read_scope(self):
        for count,invalid in ((0,False),(1,False),(8,False),(1,True)):
            with self.subTest(count=count,invalid=invalid):await self._exercise(count,invalid)

    async def _exercise(self,count: int,invalid: bool):
        expected_count=0 if invalid else count
        with TemporaryDirectory(prefix='text-context-command-') as directory:
            root=Path(directory).resolve();configuration,supplied=candidate(root);config=TextConfigurationAssembly()
            assembly=ContentAssembly(information_format=True,text_format=True,utc_now_us=lambda:1000000)
            hooks=Hooks();publisher=None;memory=None;stored=None;publication=None
            ledger=LedgerAssembly(text_generation=True);provider=ProviderService(ledger)
            def install(uow,values):
                nonlocal publication
                assert stored is not None
                owner=assembly.text_commands.persona;assert owner is not None
                fixture=records();base={'database_id':stored.database_id,'instance_id':'instance','config_snapshot_id':stored.snapshot_id}
                rid=stable_identity('persona-run',stored.database_id,'instance');cid=stable_identity('persona-candidate',stored.database_id,'instance',rid,1)
                pid=stable_identity('persona-publication',stored.database_id,'instance')
                key={'owner_namespace':'self_model','operation_kind':'install_reviewed_persona_fixture','scope_id':'instance','operation_key':'fixture-persona'}
                run=isolate_run({**fixture['self_model_initial_persona_runs'],**base,'object_id':rid,'state':'PUBLISHED',
                    'resolution_id':cid,'publication_id':pid,'provider_request_id':'fixture-request','last_operation':key})
                candidate_value=isolate_candidate({**fixture['self_model_initial_persona_candidates'],**base,'object_id':cid,'run_id':rid,
                    'provider_request_id':'fixture-request','handoff_id':'fixture-handoff','resolution':'SUCCEEDED','failure_reason':'NONE',
                    'text':'Explicit prior reviewed persona fixture.','text_digest':digest('Explicit prior reviewed persona fixture.'),
                    'review':'APPROVED','reviewed_by':'fixture-reviewer','reviewed_at_us':1,'review_operation':key})
                publication=isolate_publication({**fixture['self_model_persona_publications'],**base,'object_id':pid,'run_id':rid,'candidate_id':cid,
                    'candidate_revision':1,'candidate_digest':candidate_digest(candidate_value),'text':candidate_value['text'],
                    'provider_request_id':'fixture-request','handoff_id':'fixture-handoff','reviewed_by':'fixture-reviewer',
                    'review_operation':key,'publication_operation':key})
                owner._validate_published(run,candidate_value,publication)
                for kind,value in (('run',run),('candidate',candidate_value),('publication',publication)):owner._insert(uow,kind,value)
                refs=tuple({'kind':kind,'object_id':oid,'revision':1} for kind,oid in (('RUN',rid),('RESOLUTION',cid),('PUBLICATION',pid)))
                return result(str(values['operation_id']),'INSTALLED',{'self_model':{'rows_changed':3,'references':refs}},refs,
                    tuple({'object_id':oid,'previous_revision':None,'revision':1} for oid in (rid,cid,pid)))
            requirements,bindings=audits('install_reviewed_persona_fixture',('self_model',))
            fixture_definition=ResultBoundCommandDefinition('self_model','install_reviewed_persona_fixture',1,RecordSchema((Field('operation_id',ID),)),1,
                result_schema(('self_model',),('INSTALLED',)),(assembly.text_commands.catalog.definition,),requirements,install,INTENT,bindings)
            def initialize_memory(uow,values):
                assert assembly.memory.information is not None
                fact=assembly.memory.information.initialize(uow,1000000)
                return {'change':fact,'targets':({'object_id':fact['object_id'],'previous_revision':None,'revision':1},
                    {'object_id':'instance','previous_revision':None,'revision':1})}
            memory_audit=AuditRequirement('memory','fixture_memory_initialized','FIXTURE_MEMORY_INITIALIZED',1,('APPLY',),FACT)
            memory_definition=ResultBoundCommandDefinition('memory','fixture_initialize_memory',1,RecordSchema((Field('operation_id',ID),)),1,
                RecordSchema((Field('change',FACT),Field('targets',TARGETS))),tuple(c.definition for c in assembly.catalogs if c.definition.owner_module=='memory'),
                (memory_audit,),initialize_memory,INTENT,(AuditResultBinding(memory_audit.event_slot,1,(
                    AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
                    AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),AuditFieldBinding('target_refs','RESULT',('targets',)),
                    AuditFieldBinding('change','RESULT',('change',)))),))
            storage=PersistenceService(config.repositories+assembly.repositories+ledger.repositories,
                config.commands+assembly.commands+ledger.commands+(fixture_definition,memory_definition),assembly_format='MODEL_TEXT_LEARNING_V1')
            async def execute(kind,key,values):
                definition=assembly.command_definition(kind)
                return await assembly.operations[kind].execute(key,ResultBoundCommand(1,{'operation_id':key,**values},
                    {requirement.event_slot:{'actor':'fixture-scheduler'} for requirement in definition.required_audits}))
            try:
                opened=await storage.initialize(configuration.foundation,DatabaseResources('text-context-db',lambda identity,path:
                    identity=='text-context-db' and path==str(root/'database'/'runtime.sqlite3'),connect=hooks.connect),'CREATE_NEW')
                self.assertIs(type(opened),Ready,opened)
                publisher=config.bind(storage,'instance',configuration)
                published=await publisher.persist_text_learning_configuration('configuration',configuration,actor='fixture',protected_directories=supplied[6])
                assert type(published) is ConfigurationCommitted and published.configuration is not None,published
                stored=published.configuration;assembly.bind(storage,stored,'instance')
                assembly.memory.bind_information(stored)
                self.assertIs(type(await storage.bind_operation(memory_definition,'instance').execute('fixture-memory',
                    ResultBoundCommand(1,{'operation_id':'fixture-memory'},{'fixture_memory_initialized':{'actor':'fixture'}}))),Committed)
                installed=await storage.bind_operation(fixture_definition,'instance').execute('fixture-persona',
                    ResultBoundCommand(1,{'operation_id':'fixture-persona'},{'self_model_text_learning':{'actor':'fixture'}}))
                self.assertIs(type(installed),Committed,installed)
                for kind,key,values in (('initialize_content_runtime','initialize',{}),('register_content_entry','entry',
                        {'entry_id':'entry','host_id':'host','platform_id':'sample_platform','external_entry_id':'conversation'})):
                    self.assertIs(type(await execute(kind,key,values)),Committed)
                event=inputs()[0]['user']['members'][0]['event']
                for ordinal in range(3):
                    from companion_memory.ingress.media_events import isolate_media_event
                    accepted=isolate_media_event({**event,'client_event_key':'event-'+str(ordinal)},2048,occurrence_limit=2,text_limit=512)
                    self.assertIs(type(await execute('accept_media_event','accept-'+str(ordinal),{'entry_id':'entry','event':encode_content(accepted,8192).decode()})),Committed)
                for kind,key,values in (
                    ('select_content_preparation','select',{'entry_id':'entry','preparation_id':'preparation','batch_id':'batch','run_id':'run'}),
                    ('claim_content_preparation','claim',{'preparation_id':'preparation','expected_revision':1,'owner_generation':1}),
                    ('complete_content_preparation','complete',{'preparation_id':'preparation','expected_revision':2,'owner_generation':1}),
                    ('freeze_content_batch','freeze',{'preparation_id':'preparation','expected_revision':3,'owner_generation':1})):
                    self.assertIs(type(await execute(kind,key,values)),Committed,kind)
                source=decode_source(str((await assembly.rows.read('batches_get',{'batch_id':'batch'}))[0]['manifest']))
                work=(await assembly.rows.read('work_get',{'batch_id':'batch'}))[0]
                args=inputs();context=args[0];generation=stored.candidate.text.record('provider.generation')
                members=[]
                for raw in sequence(source['ordered_members']):
                    member=record(raw);payload=(await assembly.ingress.rows.read('payload',{'message_id':member['message_id']}))[0]
                    members.append({'member':{'role':{'H':'HISTORY','T':'TARGET','R':'RECENT'}[str(member['role'])],
                        'message_id':member['message_id'],'payload_digest':member['payload_digest']},
                        'event':decode_content(str(payload['body']).encode(),2048)})
                assert publication is not None
                context['system_text']=render_learning_instructions([],({'kind':'REAL','context_id':None},))
                context['user'].update(members=members,persona=projection(publication,False),identity={
                    'database_id':stored.database_id,'instance_id':'instance',**{name:source[name] for name in ('batch_id','run_id','source_id','config_snapshot_id')}})
                context['resources']={name:generation[name] for name in context['resources']}
                context['model_binding'].update(profile_id='fixture_generation',config_snapshot_id=stored.snapshot_id,
                    profile_revision=derived_id('profile',stored.snapshot_id,'fixture_generation'),price_revision='fixture_price',
                    capability_evidence_ref=generation['capability_evidence_ref'],billing_evidence_ref=generation['billing_evidence_ref'])
                key={'owner_namespace':'runtime','operation_kind':'stage_learning_context','scope_id':'instance','operation_key':'stage-context'}
                frozen=freeze_context(context,assembly.text_transactions.request_identity(source,work),[],key,1000000,128000,assembly.text_transactions.chat)
                values={'batch_id':'batch','expected_revision':1,'generation':1,'manifest':encode_content(frozen.manifest,8192).decode(),
                    'leaves':[encode_content(leaf,8192).decode() for leaf in frozen.leaves]}
                memory=MemoryService(assembly.memory,lambda:None)
                scope=memory.bind_read(('fixture-reader',),('get_current',))
                # Encoded material alone and an ordinary read capability never
                # install the separately retained native stage authority.
                self.assertIsNot(type(await execute('stage_learning_context','stage-context',values)),Committed)
                assembly.text_commands.retain(frozen,scope)
                entered=threading.Event();resume=threading.Event()
                def block_leaf(sql):
                    if sql.startswith('INSERT INTO cognition_learning_context_leaves'):
                        entered.set()
                        if not resume.wait(3):raise RuntimeError('Test release signal was not received.')
                hooks.before=block_leaf
                owned=asyncio.create_task(execute('stage_learning_context','stage-context',values))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,3))
                    memory.release_query_scope(scope)
                finally:resume.set()
                self.assertIsNot(type(await owned),Committed)
                hooks.before=lambda sql:None
                self.assertEqual(storage.get_health().writes_in_flight,0)
                self.assertIsNone((await assembly.rows.read('work_get',{'batch_id':'batch'}))[0]['model_binding'])
                assembly.text_commands.release_retained(frozen)
                scope=memory.bind_read(('fixture-reader',),('get_current',))
                assembly.text_commands.retain(frozen,scope)
                for failing in ('INSERT INTO cognition_learning_context_leaves','UPDATE runtime_content_work','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
                    seen=[]
                    def fail(sql):
                        if sql.startswith(failing):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                    hooks.before=fail
                    outcome=await execute('stage_learning_context','stage-context',values)
                    self.assertIsNot(type(outcome),Committed,outcome);self.assertTrue(seen,failing)
                    hooks.before=lambda sql:None
                    self.assertIsNone((await assembly.rows.read('work_get',{'batch_id':'batch'}))[0]['model_binding'])
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_contexts').fetchone()[0],0)
                committed=await execute('stage_learning_context','stage-context',values)
                self.assertIs(type(committed),Committed,committed)
                memory.release_query_scope(scope);assembly.text_commands.release_retained(frozen)
                replay=await execute('stage_learning_context','stage-context',values)
                self.assertIs(type(replay),Committed,replay);assert type(replay) is Committed and type(committed) is Committed
                self.assertEqual(replay.receipt,committed.receipt)
                self.assertEqual((await assembly.rows.read('work_get',{'batch_id':'batch'}))[0]['phase'],'FROZEN')
                association={'batch_id':'batch','expected_revision':2,'generation':1,'provider_operation_key':frozen.request['operation_key'],
                    'model_binding':encode_content(assembly.text_transactions.work_binding(frozen),8192).decode()}
                associated=await execute('associate_content_request','associate',association)
                self.assertIs(type(associated),Committed,associated)
                target=next(record(item) for item in sequence(source['ordered_members']) if record(item)['role']=='T')
                proposed=proposal();proposed.update(subject_ids=[],speaker_subject_id=None)
                proposed['target_anchors'][0]['message_id']='not-in-frozen-source' if invalid else str(target['message_id'])
                output={'schema_version':1,'memories':[proposed for _ in range(count)]}
                leases=[]
                def resolve(secret,revision,account):
                    self.assertEqual((secret,revision,account),('fixture_secret','fixture_secret_revision','fixture_account'))
                    lease=CredentialLease(b'independent-text-transaction-fixture');leases.append(lease);return Available(lease)
                resolver=CredentialResolver(resolve);gate=Gate();cancel=CancellationSource()
                with server(response(json.dumps(output))) as (port,requests,failures):
                    transport=ChatTransport.controlled_loopback(as_record(freeze(dict(stored.candidate.text.record('provider.transport')),2048)),resolver,time.monotonic,port)
                    persona_settings=stored.candidate.text.record('self_model.initial_persona')
                    persona_chat=ChatBinding('ark-code-latest',('ark-code-latest','fixture_backend'),None,'persona_schema',
                        str(persona_settings['schema_digest']),'initial_persona',output_schema('PERSONA'))
                    adapter=ChatGenerationAdapter(transport,assembly.text_transactions.chat,persona_chat)
                    resources=RealGenerationResources(gate.binding,adapter,resolver,time.monotonic,lambda:datetime.now(timezone.utc),lambda:str(uuid.uuid4()),lambda _:None)
                    self.assertIs(type(await provider.initialize(stored,ledger.bind(storage,stored),resources)),ProviderReady)
                    grant=WorkGrant('cognition','instance',None,'LEARNING',('fixture_generation',),('GENERATION',),'cognition','scheduler',
                        ('run',),entry_ids=('entry',),batch_ids=('batch',),prompt_revisions=('learning_prompt',))
                    model=provider.bind_work(grant)
                    completion=await model.generate({**cast(dict,plain(frozen.request)),'deadline':time.monotonic()+3,'cancellation':cancel.token})
                    self.assertIs(type(completion),Completed,completion);assert type(completion) is Completed
                    request_id=str(completion.record['object_id'])
                    original={name:frozen.request.get(name) for name in OPTIONALS}
                    original.update({name:frozen.request[name] for name in ('operation_key','run_id','profile_id','payload','entry_ids')})
                    result_owner=provider.bind_result_owner(ResultGrant('cognition',(request_id,)))
                    terminal=await result_owner.verify_terminal(request_id,as_record(freeze(original,131072,owned=True)))
                    self.assertIs(type(terminal),TerminalVerified,terminal);assert type(terminal) is TerminalVerified
                    candidate_value=assembly.text_transactions.candidates.build(source,1,terminal.value,frozen)
                    self.assertEqual(len(candidate_value.leaves),expected_count)
                    self.assertIs(type(await execute('confirm_content_request','confirm',{'batch_id':'batch','expected_revision':3,'generation':1,'request_id':request_id})),Committed)
                    stage_values={'batch_id':'batch','expected_revision':4,'generation':1,
                        'manifest':encode_content(candidate_value.manifest,4096).decode(),'leaves':[encode_content(leaf,8192).decode() for leaf in candidate_value.leaves]}
                    for failing in ('INSERT INTO cognition_candidates','UPDATE runtime_content_work','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
                        seen=[]
                        def fail_candidate(sql):
                            if sql.startswith(failing):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                        hooks.before=fail_candidate;assembly.retain_learning_terminal(terminal.value)
                        staged=await execute('store_content_candidate','candidate',stage_values)
                        self.assertIsNot(type(staged),Committed,staged);self.assertTrue(seen,failing)
                        hooks.before=lambda sql:None
                        self.assertEqual((await assembly.rows.read('work_get',{'batch_id':'batch'}))[0]['phase'],'REQUEST_ASSOCIATED')
                        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                            self.assertEqual(db.execute('SELECT count(*) FROM cognition_candidates').fetchone()[0],0)
                    assembly.retain_learning_terminal(terminal.value)
                    staged=await execute('store_content_candidate','candidate',stage_values)
                    self.assertIs(type(staged),Committed,staged)
                    terminal_kind='commit_content_published' if expected_count else 'commit_content_without_objects'
                    terminal_values={'batch_id':'batch','candidate_id':candidate_value.manifest['candidate_id'],'expected_revision':5,'generation':1,
                        'readable_objects':[],'readable_subjects':[]}
                    for failing in ('DELETE FROM cognition_learning_context_leaves','UPDATE runtime_content_work','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
                        seen=[]
                        def fail_finish(sql):
                            if sql.startswith(failing):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                        hooks.before=fail_finish
                        finished=await execute(terminal_kind,'finish',terminal_values)
                        self.assertIsNot(type(finished),Committed,finished);self.assertTrue(seen,failing)
                        hooks.before=lambda sql:None
                        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                            self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                            self.assertGreater(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    finished=await execute(terminal_kind,'finish',terminal_values)
                    self.assertIs(type(finished),Committed,finished);assert type(finished) is Committed
                    replay=await execute(terminal_kind,'finish',terminal_values);assert type(replay) is Committed
                    self.assertEqual(replay.receipt,finished.receipt)
                    self.assertEqual(len(sequence(record(finished.receipt.result)['object_refs'])),expected_count)
                    self.assertEqual(record(finished.receipt.result)['terminal'],'FAILED_DROPPED' if invalid else 'SUCCEEDED')
                    self.assertEqual(record(finished.receipt.result)['model_adapter'],'REMOTE_PROVIDER')
                    self.assertIs(type(await assembly.text_transactions.contexts.load(str(frozen.manifest['object_id']),time.monotonic()+3)),ReleasedContext)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],expected_count)
                        self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0],1 if expected_count else 0)
                    self.assertEqual(len(requests),1);self.assertEqual(failures,[])
                    self.assertTrue(all(lease.released for lease in leases))
                    provider.revoke(result_owner);provider.revoke(model)
            finally:
                await provider.close()
                if memory is not None:memory.close()
                if assembly._bound:assembly.close()
                if publisher is not None:publisher.close()
                await storage.close()
                if assembly._bound:assembly.close()
                if publisher is not None:publisher.close()
