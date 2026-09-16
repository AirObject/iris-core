"""The complete daily host persists unknown money and continues legal work."""
from pathlib import Path
from typing import cast
from tempfile import TemporaryDirectory
import unittest
import sqlite3
import json
from companion_memory.persistence import Found, Committed
from .configuration_support import inputs
from .test_host import make_host
from .test_reasoning import responses
from tests.runtime.configuration_support import event


def usage_inputs(root):
    supplied=inputs(root)
    values=supplied[0]['explicit_values']
    for account in values['provider.accounts']:
        account.update(billing_mode='USAGE_ONLY_TRIAL',price=None,cost_limit_atoms=None,quota=None)
    for profile in values['provider.profiles']:
        profile['billing_mode']='USAGE_ONLY_TRIAL'
    return supplied


class UsageOnlyHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_and_authentication_http_failures_block_trial_without_another_send(self):
        from .trial_support import controlled_activation
        from tests.provider.test_chat_transport import server
        from companion_memory.persistence.owned_statements import OwnerFailure
        for status,reason in ((400,'INPUT_FORMAT_UNSUPPORTED'),(401,'AUTHENTICATION_FAILED')):
            raw=b'{"error":{"type":"invalid_request_error","message":"controlled rejection"}}'
            response=b'HTTP/1.1 '+str(status).encode()+b' Error\r\nContent-Length: '+str(len(raw)).encode()+b'\r\nConnection: close\r\n\r\n'+raw
            with self.subTest(status=status),TemporaryDirectory() as directory,server(response) as (port,requests,failures):
                root=Path(directory);credentials=[];host=make_host(root,port,credentials,configuration_input=usage_inputs(root))
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    authorization=host.bind_trial_activation(controlled_activation(host));await authorization.resume()
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','conversation')),Committed)
                    entry=host.bind_entry('entry')
                    for n in range(3):
                        value=event('event-'+str(n),'合成协议停止验证。');value['event_version']=2
                        self.assertIs(type(await entry.accept_event('accept-'+str(n),value)),Committed)
                    self.assertIs(type(await host.resume_learning('resume')),Committed)
                    await entry.run_learning('learn')
                    with self.assertRaises(OwnerFailure):await authorization.verify_native()
                    with self.assertRaises(OwnerFailure):await authorization.resume()
                    self.assertEqual(len(requests),1);self.assertFalse(failures)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        value=json.loads(db.execute('SELECT body FROM provider_requests').fetchone()[0])
                        self.assertEqual(value['first_error']['reason'],reason)
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0],1)
                finally:
                    if host.dispatch is not None:await host.dispatch.wait_actual()
                    self.assertTrue(await host.close())

    async def test_original_trial_slot_is_not_reusable_by_later_legal_batch(self):
        from .trial_support import controlled_activation
        outputs=({'schema_version':1,'kind':'FINAL','actions':[]},)
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials,configuration_input=usage_inputs(root))
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                authorization=host.bind_trial_activation(controlled_activation(host));await authorization.resume()
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                for n in range(5):
                    value=event('event-'+str(n),'合成原用途槽不可复用。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('accept-'+str(n),value)),Committed)
                    if n in (2,4):await entry.run_learning('learn-'+str(n))
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                await authorization.verify_native()
                self.assertEqual(tuple(authorization.entries),('learning:0',))
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0],1)
            finally:
                if host.dispatch is not None:await host.dispatch.wait_actual()
                self.assertTrue(await host.close())

    async def test_original_unsent_trial_recovers_without_money_or_credentials(self):
        import asyncio
        import sys
        from .test_unsent_process_recovery import CHILD
        code=CHILD.replace('from tests.daily_cognition.test_host import make_host',
            'from tests.daily_cognition.test_host import make_host\nfrom tests.daily_cognition.test_usage_only_host import usage_inputs')
        code=code.replace('make_host(root,9,credentials)','make_host(root,9,credentials,configuration_input=usage_inputs(root))')
        with TemporaryDirectory() as directory:
            root=Path(directory)
            child=await asyncio.create_subprocess_exec(sys.executable,'-c',code,str(root),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await asyncio.wait_for(child.communicate(),30)
            self.assertEqual(child.returncode,24,(out,err))
            credentials=[];host=make_host(root,9,credentials,configuration_input=usage_inputs(root))
            try:
                self.assertIs(type(await host.initialize('OPEN_EXISTING')),Found)
                self.assertFalse(credentials)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    attempt=json.loads(db.execute('SELECT body FROM provider_attempts').fetchone()[0])
                    self.assertEqual(attempt['state'],'NOT_SENT');self.assertFalse(attempt['confirmed_started'])
                    self.assertEqual(attempt['usage']['held_atoms'],0)
                    self.assertEqual(attempt['usage']['known_cost_atoms'],0)
                    self.assertIsNone(attempt['usage']['price_revision'])
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0],1)
            finally:self.assertTrue(await host.close())

    async def test_real_adapter_without_native_grant_and_old_money_limit_send_nothing(self):
        from dataclasses import replace
        import time
        from companion_memory.runtime.daily_host import DailyCognitionHost
        from companion_memory.provider.chat_transport import ChatTransport
        from companion_memory.provider.credentials import CredentialResolver,CredentialUnavailable
        for real_without_grant in (True,False):
            with self.subTest(real_without_grant=real_without_grant),TemporaryDirectory() as directory:
                root=Path(directory);credentials=[]
                supplied=usage_inputs(root) if real_without_grant else inputs(root)
                if not real_without_grant:supplied[0]['explicit_values']['provider.accounts'][0]['cost_limit_atoms']=1
                host=make_host(root,9,credentials,configuration_input=supplied)
                if real_without_grant:
                    def unavailable(*args):credentials.append('unexpected');return CredentialUnavailable('UNAVAILABLE')
                    resolver=CredentialResolver(unavailable)
                    transports={t['role']:ChatTransport.daily(t,resolver,time.monotonic) for t in cast(tuple,host.configuration.text.record('provider.transport')['roles'])}
                    host=DailyCognitionHost(host.configuration,replace(host.resources,transports=transports))
                    host.configure_entry('entry','partition',('self',),({'kind':'REAL','context_id':None},))
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','conversation')),Committed)
                    entry=host.bind_entry('entry')
                    for n in range(3):
                        v=event('event-'+str(n),'合成准入验证。');v['event_version']=2
                        self.assertIs(type(await entry.accept_event('accept-'+str(n),v)),Committed)
                    self.assertIs(type(await host.resume_learning('resume')),Committed)
                    await entry.run_learning('learn')
                    self.assertFalse(credentials)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                finally:
                    if host.dispatch is not None:await host.dispatch.wait_actual()
                    self.assertTrue(await host.close())

    async def test_remote_unknown_reopen_has_zero_hold_but_stops_new_sends(self):
        import asyncio
        import sys
        from .test_process_recovery import CHILD
        from companion_memory.persistence.owned_statements import OwnerFailure
        child_code = CHILD.replace('from tests.daily_cognition.test_host import make_host',
            'from tests.daily_cognition.test_host import make_host\nfrom tests.daily_cognition.test_usage_only_host import usage_inputs')
        child_code = child_code.replace('make_host(root,server.server_port,[])',
            'make_host(root,server.server_port,[],configuration_input=usage_inputs(root))')
        with TemporaryDirectory() as directory:
            root=Path(directory); wire=root/'wire.json'
            child=await asyncio.create_subprocess_exec(sys.executable,'-c',child_code,str(root),str(wire),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await asyncio.wait_for(child.communicate(),30)
            self.assertEqual(child.returncode,23,(out,err))
            credentials=[];host=make_host(root,9,credentials,configuration_input=usage_inputs(root))
            try:
                result=await host.initialize('OPEN_EXISTING');self.assertIs(type(result),Found,result)
                assert host.observations is not None
                observer=host.observations.bind(frozenset(('provider/budget',)))
                budgets=await observer.read('provider/budget',{})
                self.assertIs(type(budgets),Found,budgets)
                self.assertIn("'held_atoms': 0",repr(budgets))
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    request=json.loads(db.execute('SELECT body FROM provider_requests').fetchone()[0])
                    self.assertEqual(request['phase'],'REMOTE_RESULT_UNKNOWN')
                with self.assertRaises(OwnerFailure):await host.resume_learning('new-resume')
                self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())

    async def test_known_outputs_unknown_cost_continue_and_reopen_without_recount(self):
        from .test_protocol import response
        outputs=(response({'schema_version':1,'kind':'FINAL','actions':[]}),response({'schema_version':1,'kind':'FINAL','actions':[]},usage=False))
        credentials=[]
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);supplied=usage_inputs(root)
            host=make_host(root,port,credentials,configuration_input=supplied)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                for n in range(5):
                    value=event('event-'+str(n),'合成展板上有蓝色圆形。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('accept-'+str(n),value)),Committed)
                    if n in (2,4):
                        result=await entry.run_learning('learn-'+str(n));self.assertIs(type(result),Committed,result)
                self.assertEqual(len(requests),2);self.assertFalse(failures)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    rows=[json.loads(r[0]) for r in db.execute('SELECT body FROM provider_attempts')]
                    self.assertEqual(len(rows),2)
                    self.assertEqual(sum(row['usage']['fields']['input_tokens'] is None for row in rows),1)
                    for row in rows:
                        self.assertIsNone(row['usage']['known_cost_atoms'])
                        self.assertFalse(row['usage']['cost_complete'])
                        self.assertEqual(row['usage']['held_atoms'],0)
                self.assertIs(type(await entry.run_learning('learn-4')),Committed)
                self.assertEqual(len(requests),2)
            finally:self.assertTrue(await host.close())
            reopened=make_host(root,port,credentials,configuration_input=supplied)
            before=len(credentials)
            try:
                result=await reopened.initialize('OPEN_EXISTING');self.assertIs(type(result),Found,result)
                self.assertEqual(len(requests),2);self.assertEqual(len(credentials),before)
            finally:self.assertTrue(await reopened.close())
