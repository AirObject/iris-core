"""Independent process recovery never replays ticket delivery or reinforcement."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest


class FeedbackProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_and_usage_process_death_confirm_original_without_duplicate_effects(self):
        for operation in ('ticket', 'usage'):
            for boundary, code in (('before', 71), ('after', 72)):
                with self.subTest(operation=operation, boundary=boundary), TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    async def child(action: str) -> tuple[int | None, dict[str, object]]:
                        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'tests.information.process_feedback', str(root), action,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        try: output, error = await asyncio.wait_for(process.communicate(), 30)
                        except TimeoutError:
                            process.kill(); await process.wait(); self.fail('Isolated child exceeded its deadline.')
                        self.assertFalse(error, error.decode())
                        return process.returncode, json.loads(output) if output else {}
                    self.assertEqual((await child('setup'))[0], 0)
                    if operation == 'usage': self.assertEqual((await child('prepare_usage'))[0], 0)
                    self.assertEqual((await child(operation + '_' + boundary))[0], code)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        table = 'retrieval_ticket' if operation == 'ticket' else 'memory_usage_receipt'
                        self.assertEqual(connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], int(boundary == 'after'))
                        before = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                    recovered_code, recovered = await child(operation + '_recover')
                    self.assertEqual(recovered_code, 0); self.assertEqual(recovered['state'], 'READY'); self.assertEqual(recovered['model_calls'], 0)
                    self.assertFalse(recovered['has_sections'])
                    if boundary == 'before': self.assertEqual(recovered['result'], 'NotFound')
                    else:
                        self.assertEqual(recovered['result'], 'Found' if operation == 'ticket' else 'Committed')
                        self.assertIsNotNone(recovered['commit_id'])
                        if operation == 'ticket': self.assertEqual(recovered['availability'], 'CONFIRMED_ONLY')
                        self.assertEqual((await child(operation + '_recover'))[1]['commit_id'], recovered['commit_id'])
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], before)
                        self.assertEqual(connection.execute('SELECT revision FROM memory_objects').fetchone()[0], 2 if operation == 'usage' and boundary == 'after' else 1)
