"""Kill actual host processes before and after SQLite owner publication commits."""
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
import selectors
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from tests.text_learning.test_provider import response
from tests.cognition.test_text_output import proposal


class PersonaHostRecoveryTests(unittest.TestCase):
    def test_original_prepare_handoff_candidate_and_publication_process_cuts(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size=int(self.headers['Content-Length'])
                if not 0<size<=131072:self.send_error(400);return
                raw=self.rfile.read(size);requests.append(raw)
                user=json.loads(json.loads(raw)['messages'][1]['content'])
                if 'initial_input' in user:
                    output={'schema_version':1,'text':'Only the supplied absence of a preset is known.',
                        'initial_input_ids':[user['initial_input']['object_id']]}
                else:
                    target=next(item for item in user['members'] if item['member']['role']=='TARGET')
                    item=proposal();item.update(body='A controlled factual claim.',subject_ids=['self'],speaker_subject_id=None,
                        target_anchors=[{'message_id':target['member']['message_id'],'part':'BODY','item_index':None,'start_utf8':None,'end_utf8':None}])
                    output={'schema_version':1,'memories':[item]}
                body=response(json.dumps(output)).split(b'\r\n\r\n',1)[1]
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):pass
        listener=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=lambda:listener.serve_forever(poll_interval=.01));thread.start()
        try:
            for stage in ('prepare','handoff','candidate','publication','learning_context','learning_handoff','learning_candidate','learning_finalize','learning_readmission'):
                for edge in ('before','after'):
                    with self.subTest(stage=stage,edge=edge),TemporaryDirectory() as directory:
                        root=Path(directory).resolve();start=len(requests)
                        command=[sys.executable,'-m','tests.text_learning.persona_host_process_worker',str(root),str(listener.server_port)]
                        child=subprocess.Popen(command+['create',stage,edge],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                        try:
                            assert child.stdout is not None
                            with selectors.DefaultSelector() as selector:
                                selector.register(child.stdout,selectors.EVENT_READ)
                                self.assertTrue(selector.select(30),'The real owner transaction did not reach its commit barrier.')
                            line=child.stdout.readline()
                            if line!=b'COMMIT_BARRIER\n':
                                _,error=child.communicate(timeout=5)
                                self.fail(repr(line)+' '+error.decode())
                            child.kill();child.wait(timeout=5)
                        finally:
                            if child.poll() is None:child.kill();child.wait(timeout=5)
                            for stream in (child.stdin,child.stdout,child.stderr):
                                if stream is not None:stream.close()
                        sends=0 if stage=='prepare' else 2 if stage.startswith('learning_') and stage!='learning_context' else 1
                        self.assertEqual(len(requests)-start,sends)
                        resumed=subprocess.run(command+['inspect','none','none'],capture_output=True,text=True,timeout=30)
                        self.assertEqual(resumed.returncode,0,resumed.stderr)
                        value=json.loads(resumed.stdout)
                        if stage.startswith('learning_'):
                            self.assertTrue(value['publication']);self.assertTrue(value['learning_ready'])
                            expected='FROZEN' if stage=='learning_context' else 'REQUEST_ASSOCIATED' if stage=='learning_handoff' and edge=='before' else 'TERMINAL'
                            self.assertEqual(value['learning_phases'],[expected])
                        elif stage=='publication' and edge=='after':
                            self.assertEqual(value['run_state'],'PUBLISHED');self.assertTrue(value['publication']);self.assertEqual(value['mode'],'NORMAL')
                        else:
                            self.assertFalse(value['publication']);self.assertFalse(value['learning_ready'])
                            expected=(None if edge=='before' else 'PREPARED') if stage=='prepare' else 'APPROVED' if stage=='publication' else 'WAITING_REVIEW' if stage=='candidate' and edge=='after' else 'REQUEST_ASSOCIATED'
                            self.assertEqual(value['run_state'],expected)
                        self.assertEqual(len(requests)-start,sends,'Opening original work must not generate.')
                        if (stage=='handoff' and edge=='after') or (stage=='candidate' and edge=='before'):
                            local=subprocess.run(command+['resume','none','none'],capture_output=True,text=True,timeout=30)
                            self.assertEqual(local.returncode,0,local.stderr)
                            self.assertEqual(json.loads(local.stdout)['run_state'],'WAITING_REVIEW')
                            self.assertEqual(len(requests)-start,sends,'Explicit original handoff recovery must remain local.')
        finally:
            listener.shutdown();listener.server_close();thread.join(3);self.assertFalse(thread.is_alive())
