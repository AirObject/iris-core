"""Independent processes verify real COMMIT boundaries and original-key recovery."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from tests.information.test_initialization import ROOTS


class InitializationProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_before_and_after_commit_keep_atomic_roots_and_original_key(self):
        for boundary, expected_code in (('before', 71), ('after', 72)):
            with self.subTest(boundary=boundary), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                async def child(action: str) -> tuple[int | None, dict[str, object]]:
                    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'tests.information.process_initialization', str(root), action,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try: output, error = await asyncio.wait_for(process.communicate(), 30)
                    except TimeoutError:
                        process.kill(); await process.wait(); self.fail('Isolated child exceeded its deadline.')
                    self.assertFalse(error, error.decode())
                    return process.returncode, json.loads(output) if output else {}
                code, _ = await child(boundary); self.assertEqual(code, expected_code)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    for name in ROOTS: self.assertEqual(connection.execute('SELECT count(*) FROM ' + name).fetchone()[0], int(boundary == 'after'))
                    audit_count = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                code, recovered = await child('recover'); self.assertEqual(code, 0)
                self.assertEqual(recovered['model_calls'], 0)
                self.assertEqual(recovered['state'] == 'READY', boundary == 'after')
                if boundary == 'after':
                    code, repeated = await child('recover'); self.assertEqual(code, 0)
                    self.assertEqual(recovered['original_commit'], repeated['original_commit'])
                    self.assertIsNotNone(recovered['original_commit'])
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], audit_count)
