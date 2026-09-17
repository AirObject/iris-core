"""Original writer exits; a distinct new interpreter confirms without dispatch."""
import asyncio
import json
import sys
import tempfile
import unittest


class DreamControlProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_committed_control_survives_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = []
            for mode, code in (('CREATE_NEW', 23), ('OPEN_EXISTING', 0)):
                process = await asyncio.create_subprocess_exec(sys.executable, '-m',
                    'tests.dream_maintenance.control_process_worker', directory, mode,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    out, err = await asyncio.wait_for(process.communicate(), 30)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise
                self.assertEqual(process.returncode, code, (out.decode(), err.decode()))
                reports.append(json.loads(out.decode().strip().splitlines()[-1]))
            original, recovered = reports
            self.assertNotEqual(original['process'], recovered['process'])
            self.assertEqual(recovered['source'], 'EXISTING')
            self.assertEqual(original['commit_id'], recovered['commit_id'])
            self.assertEqual(original['deadline_at_us'], recovered['deadline_at_us'])
            self.assertEqual(recovered['revision'], 2)
            self.assertEqual(recovered['state'], 'RUNNING')
            self.assertFalse(recovered['dispatch_enabled'])
            self.assertEqual(recovered['network_connect_attempts'], 0)
            print(json.dumps({'original': original, 'new_interpreter': recovered}, sort_keys=True))
