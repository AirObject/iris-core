"""The creating interpreter exits before original receipt and pointer recovery."""
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class ReviewProcessTests(unittest.TestCase):
    def test_full_review_publication_after_original_process_ends(self):
        with TemporaryDirectory() as directory:
            results=[]
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                child=subprocess.run([sys.executable,'-m','tests.dream_maintenance.review_reopen_worker',directory,mode],capture_output=True,text=True,timeout=130)
                self.assertEqual(child.returncode,0,child.stderr)
                value=json.loads(child.stdout);results.append(value)
                print(json.dumps(value,ensure_ascii=False))
            left,right=results
            for field in ('publication_id','persona_revision','persona_digest','memory_revision','belief','request_count','confirmed_receipts','receipt_digest','static_digest'):
                self.assertEqual(left[field],right[field],field)
            self.assertEqual(right['request_count'],3);self.assertEqual(right['new_credentials'],0)
            self.assertEqual(right['memory_revision'],3);self.assertEqual(right['persona_revision'],2)
            self.assertEqual(right['belief'],73)
