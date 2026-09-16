"""A process exits after durable no-send evidence and before terminal settlement."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Found
from .test_host import make_host

CHILD=r'''
import asyncio,json,os,sqlite3,sys
from pathlib import Path
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.daily_cognition.test_host import make_host
from tests.runtime.configuration_support import event
async def run():
    root=Path(sys.argv[1]);credentials=[];host=make_host(root,9,credentials)
    assert type(await host.initialize('CREATE_NEW')) is Found
    assert type(await host.register_entry('register','entry','host','sample_platform','external')) is Committed
    entry=host.bind_entry('entry')
    for ordinal in range(3):
        value=event('event-'+str(ordinal),'合成未发送恢复。');value['event_version']=2
        assert type(await entry.accept_event('accept-'+str(ordinal),value)) is Committed
    provider=host.provider;original=provider.ledger.mutate
    async def stop_registration(request,receipt):
        raise OwnerFailure('RESULT_UNCONFIRMED','authorization','COMMIT_UNCONFIRMED')
    async def exit_before_terminal(kind,*args,**kwargs):
        if kind=='terminate':
            with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                observed=db.execute("SELECT json_extract(body,'$.state'),json_extract(body,'$.confirmed_started'),json_extract(body,'$.evidence_revision') FROM provider_attempts").fetchall()
                assert observed==[('PREPARED',0,1)],observed
            assert not credentials
            with (root/'unsent-process.json').open('w') as stream:
                json.dump({'pid':os.getpid(),'observed':observed,'credential_reads':0},stream);stream.flush();os.fsync(stream.fileno())
            os._exit(24)
        return await original(kind,*args,**kwargs)
    with patch.object(provider,'after_first_registration',side_effect=stop_registration),patch.object(provider.ledger,'mutate',side_effect=exit_before_terminal):
        assert type(await host.resume_learning('resume')) is Committed
        await entry.run_learning('learn')
    raise AssertionError('Expected exit before terminal write')
asyncio.run(run())
'''


class UnsentProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exited_no_send_evidence_settles_locally_in_new_interpreter_without_unknown_or_send(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);command=(sys.executable,'-c',CHILD,str(root))
            process=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await asyncio.wait_for(process.communicate(),30)
            print(json.dumps({'command':command,'exit_code':process.returncode,'stdout':stdout.decode(),'stderr':stderr.decode()}))
            self.assertEqual(process.returncode,24,(stdout.decode(),stderr.decode()))
            old=json.loads((root/'unsent-process.json').read_bytes());self.assertNotEqual(old['pid'],os.getpid())
            credentials=[];host=make_host(root,9,credentials)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                self.assertFalse(credentials)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.phase'),json_extract(body,'$.ever_unknown') FROM provider_requests").fetchall(),[('TERMINAL',0)])
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.state'),json_extract(body,'$.confirmed_started') FROM provider_attempts").fetchall(),[('NOT_SENT',0)])
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.held_atoms'),json_extract(body,'$.known_subtotal_atoms') FROM provider_budget_windows").fetchall(),[(0,0)])
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchall(),[('FAILED_DROPPED',)])
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                print(json.dumps({'child_exit_code':24,'original_process':old['pid'],'recovery_process':os.getpid(),
                    'original_attempts':1,'recovery_new_sends':0,'recovery_credential_reads':0,'state':'NOT_SENT'}))
            finally:self.assertTrue(await host.close())
