"""A new interpreter recovers after the sending process exits without a response."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Found
from .test_host import make_host

CHILD=r'''
import asyncio,json,os,sys,threading
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from companion_memory.persistence import Committed,Found
from tests.daily_cognition.test_host import make_host
from tests.runtime.configuration_support import event
root=Path(sys.argv[1]);evidence=Path(sys.argv[2])
class Handler(BaseHTTPRequestHandler):
 def do_POST(self):
  raw=self.rfile.read(int(self.headers['Content-Length']))
  with evidence.open('w') as stream:
   json.dump({'request_bytes':len(raw),'process':os.getpid(),'response_sent':False},stream);stream.flush();os.fsync(stream.fileno())
  os._exit(23)
 def log_message(self,*args):pass
server=HTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever).start()
async def run():
 host=make_host(root,server.server_port,[])
 opened=await host.initialize('CREATE_NEW')
 if type(opened) is not Found:raise AssertionError(opened)
 registered=await host.register_entry('entry','entry','host','sample_platform','conversation')
 if type(registered) is not Committed:raise AssertionError(registered)
 port=host.bind_entry('entry')
 for n in range(3):
  value=event('event-'+str(n),'原进程中的目标事件。');value['event_version']=2
  accepted=await port.accept_event('accept-'+str(n),value)
  if type(accepted) is not Committed:raise AssertionError(accepted)
 await host.resume_learning('resume')
 result=await port.run_learning('learn')
 raise AssertionError(('Process should have exited at actual HTTP receipt',result))
try:asyncio.run(run())
except BaseException:
 import traceback;traceback.print_exc();os._exit(24)
'''

class DailyProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exited_sender_unknown_is_retained_and_new_interpreter_never_resends(self):
        with TemporaryDirectory() as directory:
            base=Path(directory);root=base/'instance';root.mkdir();evidence=base/'wire.json'
            child=await asyncio.create_subprocess_exec(sys.executable,'-c',CHILD,str(root),str(evidence),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:out,err=await asyncio.wait_for(child.communicate(),30)
            except TimeoutError:
                child.kill();await child.wait();raise
            self.assertEqual(child.returncode,23,(out.decode(),err.decode()))
            sent=json.loads(evidence.read_text());self.assertGreater(sent['request_bytes'],0);self.assertFalse(sent['response_sent'])
            with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                self.assertEqual(db.execute("SELECT json_extract(body,'$.phase') FROM provider_requests").fetchone()[0],'OPEN')
                self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],1)
            credentials=[];host=make_host(root,9,credentials)
            try:
                recovered=await host.initialize('OPEN_EXISTING');self.assertIs(type(recovered),Found,recovered)
                self.assertEqual(host.state,'READY');self.assertFalse(credentials);self.assertFalse(host.scheduling())
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    request=json.loads(db.execute('SELECT body FROM provider_requests').fetchone()[0])
                    self.assertEqual(request['phase'],'REMOTE_RESULT_UNKNOWN');self.assertTrue(request['ever_unknown'])
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'FAILED_DROPPED')
                    self.assertGreater(json.loads(db.execute('SELECT body FROM provider_budget_windows').fetchone()[0])['held_atoms'],0)
                print(json.dumps({'child_exit_code':child.returncode,'old_process':sent['process'],'recovery_process':__import__('os').getpid(),
                    'original_requests':1,'recovery_credential_reads':len(credentials),'recovery_new_sends':0,'remote_state':'REMOTE_RESULT_UNKNOWN'},sort_keys=True))
            finally:self.assertTrue(await host.close())
