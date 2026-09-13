"""Separate actual first timeout and final length evidence through public persona.

The real loopback adapter is held after decoding, until SQLite has committed
UNKNOWN. Only then may that same worker return its known final response.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found as StorageFound
from companion_memory.provider import ResultGrant
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.service import error,_EvidenceConflict
from companion_memory.provider.values import as_record
from companion_memory.self_model.management import RemoteUnknown,SavedResolution
from tests.provider.test_chat_transport import server
from tests.text_learning.persona_terminal_support import PersonaScenario,length_response
from tests.text_learning.late_terminal_support import LateTerminalBarrier


class LateTerminalErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_timeout_then_length_keeps_final_reason_and_independent_cost(self):
        for usage in ('complete','missing'):
            with self.subTest(usage=usage),TemporaryDirectory() as directory,server(length_response(usage)) as (port,requests,failures):
                scenario=PersonaScenario(Path(directory),port)
                with LateTerminalBarrier(scenario) as barrier:
                    try:
                        await scenario.start();run=await scenario.run();key=cast(str,run['provider_operation_key'])
                        task=asyncio.create_task(scenario.api.generate(scenario.run_id,1,key,time.monotonic()+10))
                        await barrier.reach_unknown()
                        unknown=scenario.bodies('provider_attempts')[0]
                        self.assertEqual(unknown['state'],'REMOTE_RESULT_UNKNOWN')
                        self.assertEqual(unknown['first_error']['code'],'TIMEOUT')
                        self.assertEqual(scenario.bodies('provider_requests')[0]['phase'],'REMOTE_RESULT_UNKNOWN')
                        self.assertIs(type(await task),RemoteUnknown)
                        barrier.release.set();await scenario.join()
                        pending_run=await scenario.run()
                        saved=await scenario.api.generate(scenario.run_id,1,key,time.monotonic()+5)
                        self.assertIs(type(saved),SavedResolution,saved);await scenario.join()
                        proposal=await scenario.candidate()
                        self.assertEqual((proposal['resolution'],proposal['failure_reason']),('KNOWN_FAILED','OUTPUT_LIMIT'))
                        attempt=scenario.bodies('provider_attempts')[0];request=scenario.bodies('provider_requests')[0]
                        self.assertEqual(attempt['first_error'],unknown['first_error'])
                        self.assertEqual(attempt['terminal_error'],{'code':'ADAPTER_FAILED','field':'adapter','reason':'OUTPUT_LIMIT'})
                        self.assertEqual((request['phase'],request['outcome'],attempt['state'],attempt['logical_outcome']),('TERMINAL','FAILED','COMPLETED','FAILED'))
                        self.assertEqual(attempt['usage']['cost_complete'],usage=='complete')
                        self.assertEqual(attempt['usage']['held_atoms']==0,usage=='complete')
                        self.assertEqual(scenario.rows('provider_handoffs'),[])
                        self.assertEqual(len(requests),1);self.assertEqual(failures,[])
                        assert type(saved) is SavedResolution
                        repeated=await scenario.api.record_initial_persona_resolution(saved.receipt.identity.operation_key,scenario.run_id,
                            cast(int,pending_run['revision']),1,request['object_id'],request['revision'],time.monotonic()+5)
                        self.assertEqual(repeated,Committed(saved.receipt,'EXISTING'));await scenario.join()
                        job=barrier.job;assert job is not None and job.stored is not None and job.attempt is not None
                        profile=as_record(as_record(job.stored['execution_evidence'])['profile'])
                        provider=scenario.host.provider;cost=as_record(job.attempt['usage'])
                        before=(scenario.rows('operation_receipts'),scenario.rows('audit_records'),scenario.rows('provider_budget_windows'))
                        cause=error('ADAPTER_FAILED','generate','adapter','OUTPUT_LIMIT')
                        await provider._settle(job,profile,'OUTPUT_LIMIT',cause,cost,None,False)
                        for outcome,conflict in (('INVALID_RESPONSE',error('ADAPTER_FAILED','generate','adapter','INVALID_RESPONSE')),
                                ('OUTPUT_LIMIT',error('ADAPTER_FAILED','generate','payload','OUTPUT_LIMIT')),
                                ('REMOTE_RESULT_UNKNOWN',error('ADAPTER_FAILED','generate','adapter','ADAPTER_EXCEPTION'))):
                            with self.assertRaises(_EvidenceConflict):
                                await provider._settle(job,profile,outcome,conflict,cost,None,False)
                        self.assertEqual((scenario.rows('operation_receipts'),scenario.rows('audit_records'),scenario.rows('provider_budget_windows')),before)
                        owner=provider.bind_result_owner(ResultGrant('self_model',(request['object_id'],)))
                        terminal=await owner.verify_terminal(request['object_id'])
                        self.assertIs(type(terminal),TerminalVerified);assert type(terminal) is TerminalVerified
                        self.assertEqual(terminal.value.terminal_reason,'OUTPUT_LIMIT')
                        completion=await owner.confirm_completion(terminal.value)
                        self.assertIs(type(completion),ConfirmedCompletion);assert type(completion) is ConfirmedCompletion
                        self.assertEqual(dict(completion.operation),dict(as_record(proposal['terminal_receipt'])))
                        self.assertEqual(completion.operation['operation_kind'],'evidence')
                        ledger=provider._ledger
                        receipt=await ledger.operations['evidence'].read_receipt(cast(str,completion.operation['operation_key']))
                        self.assertIs(type(receipt),StorageFound)
                        assert type(receipt) is StorageFound and barrier.evidence_command is not None
                        from companion_memory.persistence._codec import prepare_command
                        definition=next(value for value in ledger.assembly.commands if value.operation_kind=='evidence')
                        frozen=prepare_command(definition,receipt.value.identity,barrier.evidence_command,1048576)
                        self.assertEqual(frozen[0].fingerprint,receipt.value.fingerprint)
                        from companion_memory.persistence._codec import command_descriptor
                        from companion_memory.persistence.schema import encode_value
                        from types import MappingProxyType
                        carrier=encode_value(MappingProxyType({'definition':command_descriptor(definition),'values':frozen[1],'audits':frozen[2]}),1048576)
                        import hashlib
                        self.assertEqual(hashlib.sha256(carrier).hexdigest(),receipt.value.fingerprint)
                        from companion_memory.provider.values import dump
                        body_bytes=len(dump(job.attempt).encode())
                        self.assertLessEqual(body_bytes,8192)
                        print({'source':'ACTUAL_LATE_TERMINAL_CARRIER','usage':usage,'attempt_bytes':body_bytes,
                            'final_error_bytes':len(dump(as_record(job.attempt['terminal_error'])).encode()),'actual_command_bytes':len(carrier)})
                        provider.revoke(owner)
                    finally:
                        barrier.release.set();await scenario.join();self.assertTrue(await scenario.host.close())

    async def test_closed_terminal_fields_and_proof_reject_changed_attempt(self):
        from contextlib import closing
        import sqlite3
        from companion_memory.provider import Failed
        from companion_memory.provider.text_stored_schema import validate
        from companion_memory.provider.values import freeze,InvalidData,dump
        with TemporaryDirectory() as directory,server(length_response()) as (port,requests,failures):
            scenario=PersonaScenario(Path(directory),port)
            try:
                await scenario.start();run=await scenario.run()
                await scenario.api.generate(scenario.run_id,1,cast(str,run['provider_operation_key']),time.monotonic()+5);await scenario.join()
                row=scenario.bodies('provider_attempts')[0];request=scenario.bodies('provider_requests')[0]
                def check(value):validate('attempts',as_record(freeze(value,8192,owned=True)))
                check(row)
                missing={k:v for k,v in row.items() if k!='terminal_error'}
                for invalid in (missing,{**row,'terminal_error':None},{**row,'logical_outcome':'SUCCEEDED'},
                        {**row,'state':'REMOTE_RESULT_UNKNOWN','logical_outcome':None},
                        {**row,'terminal_error':{'code':'ADAPTER_FAILED','field':'adapter','reason':'NEW_ERROR'}}):
                    with self.assertRaises(InvalidData):check(invalid)
                for state in ('PREPARED','REMOTE_RESULT_UNKNOWN'):
                    check({**row,'state':state,'logical_outcome':None,'terminal_error':None})
                check({**row,'logical_outcome':'SUCCEEDED','terminal_error':None})
                check({**row,'terminal_error':{'code':'ADAPTER_FAILED','field':'adapter','reason':'INVALID_RESPONSE'}})
                # Construct the bounded closed outer fields, independently of
                # whether all maxima can occur in one genuine provider response.
                # Usage retains its separately validated 4096-byte hard bound.
                maximal=dict(row)
                for name in ('object_id','request_id','account_id','profile_id','execution_owner_id','handoff_id'):
                    maximal[name]='x'*128
                for name in ('revision','evidence_revision','adapter_duration_ms'):maximal[name]=2**63-1
                maximal['result_fingerprint']='f'*64
                # Choose an existing legal long error, never an added enum.
                maximal['first_error']={'code':'UNSUPPORTED_CAPABILITY','field':'configuration','reason':'CAPABILITY_NOT_SUPPORTED'}
                maximal['terminal_error']=maximal['first_error']
                check(maximal)
                encoded=dump(as_record(freeze(maximal,8192,owned=True)))
                inner=dump(as_record(freeze(maximal['usage'],4096,owned=True)),4096)
                # Mutually exclusive enum maxima form an encoding envelope,
                # not a legal terminal. Its fixed fields dominate every branch.
                envelope={**maximal,'state':'REMOTE_RESULT_UNKNOWN','logical_outcome':'UNSUPPORTED_CAPABILITY',
                    'ever_unknown':False,'confirmed_started':False}
                outer=len(dump(as_record(freeze(envelope,8192,owned=True))).encode())-len(inner.encode())
                self.assertLessEqual(outer+4096,8192)
                print({'source':'CLOSED_ATTEMPT_CARRIER_BOUND','constructed_schema_bytes':len(encoded.encode()),
                    'outer_bytes':outer,'usage_hard_bound':4096,'complete_upper_bound':outer+4096,'hard_limit':8192,
                    'semantic_maximality_proven':False})
                provider=scenario.host.provider
                profile=as_record(as_record(as_record(freeze(request,8192,owned=True))['execution_evidence'])['profile'])
                not_sent={**row,'state':'NOT_SENT','logical_outcome':'CANCELLED','confirmed_started':False,
                    'terminal_error':{'code':'CANCELLED','field':'request','reason':'CANCEL_REQUESTED'},
                    'usage':provider._usage({},profile,0,not_sent=True)}
                check(not_sent)
                with self.assertRaises(InvalidData):check({**not_sent,'terminal_error':None})
                owner=provider.bind_result_owner(ResultGrant('self_model',(request['object_id'],)))
                terminal=await owner.verify_terminal(request['object_id']);assert type(terminal) is TerminalVerified
                confirmed=await owner.confirm_completion(terminal.value);assert type(confirmed) is ConfirmedCompletion
                # Keep the same root revision and final reason while changing
                # another attempt fact. Confirmation binds the whole original row.
                corrupted={**row,'adapter_duration_ms':row['adapter_duration_ms']+1}
                with closing(sqlite3.connect(scenario.root/'database'/'runtime.sqlite3')) as db:
                    db.execute('UPDATE provider_attempts SET body=?',(dump(as_record(freeze(corrupted,8192,owned=True))),));db.commit()
                self.assertIs(type(await owner.confirm_completion(terminal.value)),Failed)
                self.assertEqual(len(requests),1);self.assertEqual(failures,[])
            finally:self.assertTrue(await scenario.host.close())
