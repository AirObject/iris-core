"""Unknown remote responsibility cannot be revoked by management or a restart."""
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class UnknownProcessTests(unittest.TestCase):
    def test_native_provider_unknown_in_distinct_interpreters(self):
        with TemporaryDirectory() as root:
            reports=[]
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                process=subprocess.run([sys.executable,'-m','tests.dream_maintenance.unknown_process_worker',root,mode],capture_output=True,text=True,timeout=45)
                self.assertEqual(process.returncode,0,(process.stdout,process.stderr))
                reports.append(json.loads(process.stdout.strip().splitlines()[-1]))
            self.assertEqual(reports[0]['request_id'],reports[1]['request_id'])
            self.assertEqual(reports[1]['credentials'],0);self.assertEqual(reports[1]['attempts'],1)
            self.assertEqual(reports[1]['remote_result'],'UNKNOWN');self.assertFalse(reports[1]['dispatch'])
            print(json.dumps({'unknown_original_process':reports[0],'unknown_new_interpreter':reports[1]}))
