"""The dream purpose ledger stops durably and reopening never grants a send."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.configuration.dream_codec import candidate_values
from companion_memory.runtime.daily_trial_package import canonical
from companion_memory.runtime.dream_trial_authorization import DreamTrialAuthority,LIMITS
from tests.provider.test_chat_transport import server
from .host_support import make_dream_host
from .seed_support import establish_memory


def activation(host,selector):
    if host.stored is None:raise AssertionError('Missing configuration')
    binding={'format':'DREAM_TRIAL_AUTH_V1','package_id':'controlled-dream-journal','config':{'database_id':host.stored.database_id,
        'instance_id':'instance','snapshot_id':host.stored.snapshot_id},'code_digest':'f'*64,
        'configuration_digest':sha256(canonical(candidate_values(host.configuration))).hexdigest(),'resources_digest':'f'*64,
        'materials_digest':'f'*64,'decision_ref':'controlled-only','account_evidence_digest':'f'*64,
        'input_evidence_digest':'f'*64,'execution':'CONTROLLED','expires_at':time.time_ns()//1000+600000000}
    return DreamTrialAuthority(lambda value:dict(value)==binding and value['execution']=='CONTROLLED',selector).activate(binding)


class DreamTrialTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_daily_learning_in_dream_package_stops_before_next_dispatch(self):
        from tests.daily_cognition.test_reasoning import responses
        from tests.runtime.configuration_support import event
        from companion_memory.memory.formats import record
        with TemporaryDirectory() as directory,responses(({'schema_version':1,'kind':'FINAL','actions':[{'action':'EXECUTE_GOAL'}]},)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[])
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                auth=host.bind_trial_activation(activation(host,lambda request:'learning:0'));await auth.resume()
                entry=host.bind_entry('entry')
                for index in range(3):
                    value=event('event-'+str(index),'合成试验中的表达应明确区分观察、转述与推测。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('accept-'+str(index),value)),Committed)
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                failed=await entry.run_learning('invalid-original');self.assertIs(type(failed),Committed,failed)
                if type(failed) is not Committed:raise AssertionError(failed)
                self.assertEqual(record(failed.receipt.result)['state'],'FAILED_DROPPED')
                self.assertTrue(auth.stopped);self.assertFalse(auth.active);self.assertTrue(auth.stop_path.is_file())
                self.assertEqual(tuple(auth.entries),('learning:0',));self.assertEqual(len(requests),1);self.assertFalse(failures)
                again=await entry.run_learning('invalid-original');self.assertIs(type(again),Committed,again)
                self.assertEqual(len(requests),1)
            finally:self.assertTrue(await host.close())

    async def test_native_protocol_failure_fsyncs_stop_and_reopen_keeps_original_slot(self):
        raw=b'{"error":{"type":"invalid_request_error","message":"controlled rejection"}}'
        response=b'HTTP/1.1 400 Error\r\nContent-Length: '+str(len(raw)).encode()+b'\r\nConnection: close\r\n\r\n'+raw
        with TemporaryDirectory() as directory,server(response) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_dream_host(root,port,credentials,with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                await establish_memory(host,with_self=True)
                grant=activation(host,lambda request:'dream_review:0')
                auth=host.bind_trial_activation(grant);await auth.resume()
                self.assertEqual(len(auth.slots),32)
                for role,count in LIMITS.items():self.assertEqual(sum(v==role for v in auth.slots.values()),count)
                c=host.combination.dream
                if c is None:raise AssertionError('Missing native control')
                self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                for _ in range(3):self.assertIs(type(await host.advance_dream()),Committed)
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                self.assertTrue(auth.stopped);self.assertTrue(auth.stop_path.is_file());self.assertFalse(auth.active)
                self.assertEqual(tuple(auth.entries),('dream_review:0',))
                with self.assertRaises(OwnerFailure):await auth.resume()
                self.assertEqual(len(requests),1)
            finally:self.assertTrue(await host.close())
            before=len(credentials);reopened=make_dream_host(root,port,credentials,with_self=True)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')),Found)
                restored=reopened.bind_trial_activation(grant)
                self.assertFalse(restored.active);self.assertTrue(restored.stopped)
                self.assertEqual(tuple(restored.entries),('dream_review:0',))
                with self.assertRaises(OwnerFailure):await restored.resume()
                self.assertEqual(len(credentials),before);self.assertEqual(len(requests),1)
            finally:self.assertTrue(await reopened.close())
