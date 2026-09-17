"""Original process must end before a new interpreter confirms native receipts."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class NativeProcessTests(unittest.TestCase):
    def test_completed_atomic_memory_reopens_without_registration_or_send(self):
        with TemporaryDirectory() as directory:
            results=[]
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                process=subprocess.run([sys.executable,'-m','tests.dream_maintenance.native_process_worker',directory,mode],capture_output=True,text=True,timeout=45)
                self.assertEqual(process.returncode,0,process.stderr)
                results.append(json.loads(process.stdout))
            self.assertEqual(results[0]['commit_id'],results[1]['commit_id'])
            self.assertEqual(results[0]['fingerprint'],results[1]['fingerprint'])
            self.assertEqual(results[1]['requests'],0)
            self.assertEqual(results[1]['credentials'],0)
            self.assertEqual(results[1]['object_revision'],2)
