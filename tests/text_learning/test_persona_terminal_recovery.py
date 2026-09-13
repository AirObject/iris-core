"""Actual interpreter termination across absent and length terminal commits."""
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
import selectors
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from tests.text_learning.persona_terminal_support import length_response


class PersonaTerminalRecoveryTests(unittest.TestCase):
    def test_absent_resolution_retry_and_length_provider_persona_commit_cuts(self):
        requests=[];usage='complete'
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size=int(self.headers['Content-Length']);requests.append(self.rfile.read(size))
                body=length_response(usage).split(b'\r\n\r\n',1)[1]
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
                self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):pass
        listener=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=lambda:listener.serve_forever(poll_interval=.01));thread.start()
        try:
            cases=[(stage,edge,'complete') for stage in ('absent_resolution','absent_retry','length_settle','length_resolution','registered_retry') for edge in ('before','after')]
            cases.append(('length_resolution','after','missing'))
            for stage,edge,usage in cases:
                with self.subTest(stage=stage,edge=edge,usage=usage),TemporaryDirectory() as directory:
                    root=Path(directory).resolve();start=len(requests)
                    command=[sys.executable,'-m','tests.text_learning.persona_terminal_process_worker',str(root),str(listener.server_port)]
                    child=subprocess.Popen(command+['create',stage,edge],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    try:
                        assert child.stdout is not None
                        with selectors.DefaultSelector() as selector:
                            selector.register(child.stdout,selectors.EVENT_READ)
                            self.assertTrue(selector.select(30),'Native terminal transaction did not reach its commit barrier.')
                        line=child.stdout.readline()
                        if line!=b'COMMIT_BARRIER\n':
                            _,error=child.communicate(timeout=5);self.fail(repr(line)+' '+error.decode())
                        child.kill();child.wait(timeout=5)
                    finally:
                        if child.poll() is None:child.kill();child.wait(timeout=5)
                        for stream in (child.stdin,child.stdout,child.stderr):
                            if stream is not None:stream.close()
                    sends=0 if stage.startswith('absent') else 1
                    self.assertEqual(len(requests)-start,sends)
                    inspected=subprocess.run(command+['inspect',stage,'none'],capture_output=True,text=True,timeout=30)
                    self.assertEqual(inspected.returncode,0,inspected.stderr);value=json.loads(inspected.stdout)
                    self.assertEqual(value['handoff_count'],0);self.assertEqual(len(requests)-start,sends)
                    if stage.startswith('absent'):
                        self.assertEqual(value['requests'],[]);self.assertEqual(value['attempts'],[])
                        expected=0 if stage=='absent_resolution' and edge=='before' else 1
                        self.assertEqual(len(value['candidates']),expected)
                        resumed=subprocess.run(command+['resume',stage,'none'],capture_output=True,text=True,timeout=30)
                        self.assertEqual(resumed.returncode,0,resumed.stderr);after=json.loads(resumed.stdout)
                        self.assertEqual((after['run_state'],after['generation']),('PREPARED',2))
                        self.assertEqual(len(after['candidates']),1);self.assertEqual(after['requests'],[])
                    else:
                        if stage=='registered_retry':self.assertEqual(value['generation'],2 if edge=='after' else 1)
                        request=value['requests'][0];attempt=value['attempts'][0]
                        if stage=='length_settle' and edge=='before':
                            self.assertEqual(request['phase'],'REMOTE_RESULT_UNKNOWN');self.assertGreater(attempt['usage']['held_atoms'],0)
                        else:
                            self.assertEqual((request['phase'],request['outcome']),('TERMINAL','FAILED'))
                            self.assertEqual(request['first_error']['reason'],'OUTPUT_LIMIT');self.assertEqual(attempt['state'],'COMPLETED')
                            self.assertEqual(attempt['usage']['cost_complete'],usage=='complete')
                            if value['candidates']:self.assertEqual(value['candidates'][0]['failure_reason'],'OUTPUT_LIMIT')
                    self.assertEqual(len(requests)-start,sends,'All original confirmation and retry preparation must be local.')
        finally:
            listener.shutdown();listener.server_close();thread.join(3);self.assertFalse(thread.is_alive())
