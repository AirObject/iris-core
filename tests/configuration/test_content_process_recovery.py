"""Real process exit around configuration COMMIT and recovery before any receipt.

Pipes establish ordering. A child is confirmed dead before a new interpreter
reopens the same owned SQLite resource and original initialization intent.
"""
import json
from pathlib import Path
import selectors
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class ContentProcessRecoveryTests(unittest.TestCase):
    def test_process_death_before_and_after_commit_preserves_original_intent(self):
        for barrier, source in (('before','NEW'),('after','EXISTING')):
            with self.subTest(barrier=barrier), TemporaryDirectory() as directory:
                root=Path(directory).resolve()
                (root/'expected-identity.json').write_text(json.dumps({'identity':'content-database','path':str(root/'database'/'runtime.sqlite3')}))
                args=[sys.executable,'-m','tests.configuration.content_process_worker',str(root)]
                process=subprocess.Popen(args+['CREATE_NEW',barrier],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    assert process.stdout is not None
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout,selectors.EVENT_READ)
                        self.assertTrue(selector.select(20),'Configuration transaction did not reach its barrier.')
                    self.assertEqual(process.stdout.readline().decode().strip(),barrier.upper()+'_COMMIT')
                    process.kill()
                    process.wait(timeout=10)
                    self.assertIsNotNone(process.returncode)
                finally:
                    if process.poll() is None:
                        process.kill();process.wait(timeout=10)
                    for stream in (process.stdin,process.stdout,process.stderr):
                        if stream is not None:stream.close()
                recovered=subprocess.run(args+['OPEN_EXISTING','none'],capture_output=True,text=True,timeout=20)
                self.assertEqual(recovered.returncode,0,recovered.stderr)
                value=json.loads(recovered.stdout)
                self.assertEqual(value['source'],source)
                repeated=subprocess.run(args+['OPEN_EXISTING','none'],capture_output=True,text=True,timeout=20)
                self.assertEqual(repeated.returncode,0,repeated.stderr)
                again=json.loads(repeated.stdout)
                self.assertEqual(again['source'],'EXISTING')
                self.assertEqual(again['receipt'],value['receipt'])
                self.assertEqual(again['snapshot_id'],value['snapshot_id'])
