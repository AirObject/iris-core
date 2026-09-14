"""MiniMax fixed JSON protocol and actual native persona ledger qualification.

Only synthetic credentials and a local HTTP server are used. Review remains
pending; malformed bodies, incomplete usage and unapproved extras fail closed.
"""
import asyncio
import copy
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from typing import cast
from companion_memory.provider.chat_protocol import ChatBinding,decode_response,encode_request
from companion_memory.provider.credentials import CredentialLease,CredentialResolver,Available,CredentialUnavailable
from companion_memory.cognition.text_resources import output_schema,resource_digest
from companion_memory.persistence import Found,Committed
from companion_memory.self_model.management import SavedResolution
from tests.text_learning.host_driver import confirm_local
from .minimax_configuration import configuration
from .host import assemble
from .persona_execution import prepare,pending,join
from .authorization import AuthorizationJournal
from .test_controls import allowance,GOOD
from unittest.mock import patch
from companion_memory.provider.file_credentials import InheritedFileCredential


def envelope(content='{"schema_version":1,"memories":[]}'):
    return {'id':'minimax-response','object':'chat.completion','created':1,'model':'MiniMax-M3',
        'choices':[{'index':0,'message':{'role':'assistant','content':content,'name':'MiniMax AI','audio_content':''},'finish_reason':'stop'}],
        'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150,'total_characters':0},
        'input_sensitive':False,'output_sensitive':False,'input_sensitive_type':0,'output_sensitive_type':0,
        'output_sensitive_int':0,'base_resp':{'status_code':0,'status_msg':''}}


class ProtocolTests(unittest.TestCase):
    def test_specific_extras_and_qualified_repairs_never_reset_the_shared_allowance(self):
        with TemporaryDirectory() as directory:
            journal=AuthorizationJournal(Path(directory)/'authorization');approval=allowance();approval['extra_operations']={}
            journal.create(approval)
            journal.approve_extra('macos:persona-retry','Synthetic explicit retry','synthetic-user-decision')
            journal.approve_extra('linux:persona-retry','Synthetic explicit retry','synthetic-user-decision')
            with self.assertRaises(ValueError):journal.approve_extra('macos:another','No third extra','synthetic')
            token=journal.reserve('macos:persona-retry','a'*64,'b'*64,GOOD)
            with self.assertRaises(ValueError):journal.qualify_implementation('d'*64,'e'*64)
            self.assertTrue(journal.settle(token,evidence_digest='c'*64,attempts=1,money=10,quota=2,remote_known=True,
                local_committed=True,cleanup_ended=True,cost_complete=True,integrity_ok=True))
            journal.qualify_implementation('d'*64,'e'*64)
            self.assertEqual(sum(e['kind']=='RESERVED' for e in journal.inspect()['events']),1)
            with self.assertRaises(ValueError):journal.reserve('linux:persona-retry','a'*64,'b'*64,GOOD)
    def test_inherited_owner_projection_is_bound_to_the_same_inode_and_fixed_recipient(self):
        from types import SimpleNamespace
        with TemporaryDirectory() as directory:
            path=Path(directory)/'secret';path.write_bytes(b'synthetic-only');path.chmod(0o600)
            binding=InheritedFileCredential(path,secret_ref='s',secret_revision='r',account_ref='a',recipient_uid=65534)
            original=path.stat()
            def projected(**changes):
                return SimpleNamespace(**({'st_uid':65534,'st_dev':original.st_dev,'st_ino':original.st_ino,
                    'st_nlink':original.st_nlink,'st_mode':original.st_mode,'st_size':original.st_size}|changes))
            try:
                with patch('os.fstat',return_value=projected()):
                    value=binding.resolver.resolve('s','r','a');self.assertIs(type(value),Available)
                    assert type(value) is Available;value.lease.release()
                for change in ({'st_uid':12345},{'st_ino':original.st_ino+1},{'st_mode':0o100644},{'st_nlink':2}):
                    with patch('os.fstat',return_value=projected(**change)):
                        self.assertEqual(binding.resolver.resolve('s','r','a'),CredentialUnavailable('FAILED'))
            finally:binding.close()

    def test_inherited_binding_reads_only_on_native_resolve_and_revokes(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'secret';path.write_bytes(b'synthetic-inherited');path.chmod(0o600)
            with patch('os.pread',side_effect=AssertionError('Read before native resolution')):
                binding=InheritedFileCredential(path,secret_ref='s',secret_revision='r',account_ref='a')
                self.assertEqual(binding.resolver.resolve('wrong','r','a'),CredentialUnavailable('UNAVAILABLE'))
            value=binding.resolver.resolve('s','r','a');self.assertIs(type(value),Available)
            assert type(value) is Available;value.lease.release();binding.close()
            self.assertEqual(binding.resolver.resolve('s','r','a'),CredentialUnavailable('FAILED'))
            self.assertNotIn('synthetic-inherited',repr(binding))

    def test_whole_json_fixed_fields_identity_and_usage(self):
        schema=output_schema('LEARNING');binding=ChatBinding('MiniMax-M3',('MiniMax-M3',),None,'schema',resource_digest(schema),'text_learning',schema)
        original=envelope();result=decode_response(json.dumps(original).encode(),binding)
        self.assertEqual(result.outcome,'SUCCEEDED');self.assertTrue(result.usage.billing_covered)
        self.assertIsNone(result.usage.fields['cache_read_tokens'])
        for content in ('```json\n{}\n```','text {}','{} {}','{"a":1,"a":2}','{"a":','<think>x</think>{}'):
            self.assertEqual(decode_response(json.dumps(envelope(content)).encode(),binding).outcome,'INVALID_RESPONSE')
        for change in ({'total_tokens':151},{'prompt_tokens':True},{'extra':1},{'prompt_tokens_details':{'audio_tokens':1}}):
            value=copy.deepcopy(original);value['usage'].update(change)
            self.assertFalse(decode_response(json.dumps(value).encode(),binding).usage.billing_covered)
        value=copy.deepcopy(original);del value['usage']['completion_tokens']
        self.assertFalse(decode_response(json.dumps(value).encode(),binding).usage.billing_covered)
        value=copy.deepcopy(original);value['model']='other'
        self.assertEqual(decode_response(json.dumps(value).encode(),binding).outcome,'MODEL_BINDING_MISMATCH')
        value=copy.deepcopy(original);value['choices'][0]['finish_reason']='length'
        self.assertEqual(decode_response(json.dumps(value).encode(),binding).outcome,'OUTPUT_LIMIT')
        wire=json.loads(encode_request({'format_version':2,'messages':[{'role':'SYSTEM','text':'Do the task.'},{'role':'USER','text':'Frozen material.'}],
            'schema_ref':'schema','schema_digest':resource_digest(schema),'output_tokens':2048,'reservation_input_bound':1046528,'context_digest':'a'*64},binding))
        self.assertEqual(set(wire),{'model','messages','max_completion_tokens','stream','thinking'})
        self.assertEqual(wire['thinking'],{'type':'disabled'})
        self.assertIn(schema.decode(),wire['messages'][0]['content'])

    def test_usage_only_shared_policy_preserves_unknown_money(self):
        with TemporaryDirectory() as directory:
            journal=AuthorizationJournal(Path(directory)/'authorization');approval=allowance()
            approval.update(billing_policy='USAGE_ONLY_TRIAL',cost_limit_atoms=0,quota_limit=0,per_attempt_money_bound=0,per_attempt_quota_bound=0,extra_operations={})
            journal.create(approval);token=journal.reserve('macos:persona','a'*64,'b'*64,GOOD)
            self.assertTrue(journal.settle(token,evidence_digest='c'*64,attempts=1,money=0,quota=0,remote_known=True,
                local_committed=True,cleanup_ended=True,cost_complete=False,integrity_ok=True,usage_complete=True))
            self.assertFalse(journal.inspect()['events'][-1]['cost_complete'])
            with self.assertRaises(ValueError):journal.reserve('linux:unapproved','a'*64,'b'*64,GOOD)


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_failed_usage_only_persona_retries_in_original_run_and_preserves_first_candidate(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']));requests.append(raw)
                user=json.loads(json.loads(raw)['messages'][1]['content'])
                content='{"schema_version":1,"schema_version":1}' if len(requests)==1 else json.dumps({
                    'schema_version':1,'text':'Synthetic retry persona.','initial_input_ids':[user['initial_input']['object_id']]})
                body=json.dumps(envelope(content)).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=lambda:server.serve_forever(.01));thread.start()
        try:
            with TemporaryDirectory() as directory:
                root=Path(directory).resolve();config,inputs=configuration(root)
                host=assemble(config,root,'macos',inputs[6],CredentialResolver(lambda *args:Available(CredentialLease(b'synthetic-retry'))),loopback_port=server.server_port)
                try:
                    self.assertIs(type(await confirm_local(lambda:host.initialize('CREATE_NEW'))),Found)
                    run_id,key=await prepare(host)
                    self.assertIs(type(await host.initialization_port().generate(run_id,1,key,time.monotonic()+60)),SavedResolution)
                    await join(host);first=await pending(host,run_id)
                    self.assertEqual(first['candidate']['resolution'],'KNOWN_FAILED')
                    owner=host.combination.persona.persona;assert owner is not None
                    run=await owner.read_original('run',run_id,time.monotonic()+10);assert run is not None
                    original=run.value
                    retried=await confirm_local(lambda:host.initialization_port().retry_initial_persona('explicit-retry',run_id,
                        cast(int,original['revision']),1,cast(str,original['resolution_id']),host.gate.epoch,time.monotonic()+10))
                    await join(host);self.assertIs(type(retried),Committed,retried)
                    run=await owner.read_original('run',run_id,time.monotonic()+10);assert run is not None
                    self.assertEqual(run.value['generation'],2);self.assertNotEqual(run.value['provider_operation_key'],key)
                    self.assertIs(type(await host.initialization_port().generate(run_id,2,str(run.value['provider_operation_key']),time.monotonic()+60)),SavedResolution)
                    await join(host);second=await pending(host,run_id)
                    self.assertEqual(second['candidate']['resolution'],'SUCCEEDED');self.assertEqual(second['candidate']['review'],'PENDING')
                    self.assertEqual(len(requests),2)
                finally:self.assertTrue(await host.close())
                with sqlite3.connect(root/'database/runtime.sqlite3') as db:
                    candidates=[json.loads(row[0]) for row in db.execute('SELECT body FROM self_model_initial_persona_candidates')]
                    self.assertIn(first['candidate'],candidates);self.assertEqual(len(candidates),2)
                    self.assertEqual(json.loads(db.execute('SELECT body FROM provider_budget_windows').fetchone()[0])['attempt_count'],2)
        finally:server.shutdown();thread.join();server.server_close()

    async def test_persona_usage_ledger_and_pending_recovery_without_credentials(self):
        requests=[];leases=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']));requests.append(body)
                user=json.loads(json.loads(body)['messages'][1]['content'])
                output={'schema_version':1,'text':'外部设定的合成助手，表达简洁温和，不补造经历。','initial_input_ids':[user['initial_input']['object_id']]}
                raw=json.dumps(envelope(json.dumps(output,ensure_ascii=False))).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def log_message(self,format,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=lambda:server.serve_forever(.01));thread.start()
        try:
            with TemporaryDirectory() as directory:
                root=Path(directory).resolve();config,inputs=configuration(root)
                def resolve(*args):
                    lease=CredentialLease(b'synthetic-minimax');leases.append(lease);return Available(lease)
                host=assemble(config,root,'macos',inputs[6],CredentialResolver(resolve),loopback_port=server.server_port)
                try:
                    opened=await confirm_local(lambda:host.initialize('CREATE_NEW'));self.assertIs(type(opened),Found,opened)
                    run,key=await prepare(host)
                    result=await host.initialization_port().generate(run,1,key,time.monotonic()+60)
                    await join(host);self.assertIs(type(result),SavedResolution,result)
                    before=await pending(host,run);self.assertEqual(before['candidate']['review'],'PENDING')
                    self.assertEqual(before['candidate']['resolution'],'SUCCEEDED')
                    self.assertEqual(len(requests),1);self.assertTrue(all(lease.released for lease in leases))
                finally:self.assertTrue(await host.close())
                with sqlite3.connect(root/'database/runtime.sqlite3') as db:
                    usage=json.loads(db.execute('SELECT body FROM provider_attempts').fetchone()[0])['usage']
                    self.assertEqual(usage['format_version'],3);self.assertEqual(usage['coverage'],'COMPLETE')
                    self.assertFalse(usage['cost_complete']);self.assertIsNone(usage['reported_cost_atoms'])
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_cost_items').fetchone()[0],4)
                host=assemble(config,root,'macos',inputs[6],CredentialResolver(lambda *args:CredentialUnavailable('UNAVAILABLE')))
                try:
                    opened=await confirm_local(lambda:host.initialize('OPEN_EXISTING'));self.assertIs(type(opened),Found,opened)
                    self.assertEqual(await pending(host,run),before);self.assertEqual(len(requests),1)
                finally:self.assertTrue(await host.close())
        finally:server.shutdown();thread.join();server.server_close()
