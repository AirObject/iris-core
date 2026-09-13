"""Known late terminal before/after real COMMIT across new interpreters.

A synthetic protocol result is received once. UNKNOWN is committed before that
same worker is released, and recovery never generates a replacement request.
"""
import json
from pathlib import Path
import selectors,subprocess,sys
from tempfile import TemporaryDirectory
import unittest
from tests.provider.test_chat_transport import server
from tests.text_learning.persona_terminal_support import length_response


class LateTerminalRecoveryTests(unittest.TestCase):
    def test_original_late_commit_cut_preserves_first_final_cost_and_completion(self):
        for usage in ('complete','missing'):
            for edge in ('before','after'):
                with self.subTest(usage=usage,edge=edge),TemporaryDirectory() as directory,server(length_response(usage)) as (port,requests,failures):
                    root=Path(directory).resolve()
                    args=[sys.executable,'-m','tests.text_learning.late_terminal_process_worker',str(root),str(port)]
                    child=subprocess.Popen(args+['create',edge],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    try:
                        assert child.stdout is not None
                        with selectors.DefaultSelector() as selector:
                            selector.register(child.stdout,selectors.EVENT_READ)
                            self.assertTrue(selector.select(30),'Late completion did not reach the actual commit barrier.')
                        line=child.stdout.readline()
                        if line!=b'LATE_COMMIT_BARRIER\n':
                            _,error=child.communicate(timeout=5);self.fail(repr(line)+' '+error.decode())
                        child.kill();child.wait(timeout=5)
                    finally:
                        if child.poll() is None:child.kill();child.wait(timeout=5)
                        for stream in (child.stdin,child.stdout,child.stderr):
                            if stream is not None:stream.close()
                    self.assertEqual(len(requests),1);unknown=json.loads((root/'unknown-evidence.json').read_text())
                    previous=None
                    for _ in range(2):
                        reopened=subprocess.run(args+['inspect',edge],capture_output=True,text=True,timeout=30)
                        self.assertEqual(reopened.returncode,0,reopened.stderr);value=json.loads(reopened.stdout)
                        request,attempt=value['request'],value['attempt']
                        self.assertEqual(attempt['first_error'],unknown['first_error']);self.assertEqual(request['first_error'],unknown['first_error'])
                        self.assertEqual(value['handoffs'],0);self.assertEqual(len(requests),1)
                        if edge=='before':
                            self.assertEqual(request['phase'],'REMOTE_RESULT_UNKNOWN');self.assertIsNone(attempt['terminal_error'])
                            self.assertGreater(attempt['usage']['held_atoms'],0);self.assertEqual(value['candidates'],[])
                        else:
                            self.assertEqual((request['phase'],request['outcome'],attempt['state'],attempt['logical_outcome']),('TERMINAL','FAILED','COMPLETED','FAILED'))
                            self.assertEqual(attempt['terminal_error'],{'code':'ADAPTER_FAILED','field':'adapter','reason':'OUTPUT_LIMIT'})
                            self.assertEqual(attempt['usage']['cost_complete'],usage=='complete');self.assertEqual(attempt['usage']['held_atoms']==0,usage=='complete')
                            self.assertEqual(value['completion']['operation_kind'],'evidence')
                            self.assertEqual(value['candidates'][0]['failure_reason'],'OUTPUT_LIMIT')
                        if previous is not None:self.assertEqual(value,previous)
                        previous=value
                    self.assertEqual(failures,[])
