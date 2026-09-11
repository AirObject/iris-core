"""Fixed failure and refusal remain unapplied when actual SQLite quota is exhausted."""
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService
from companion_memory.media.interpretations import decode_interpretation
from companion_memory.persistence import Committed
from companion_memory.provider import SimulationAdapter, Scenario
from tests.memory.support import Fixture
from tests.memory.test_storage_capacity import LimitedHooks
from tests.runtime.test_preparation_lifecycle import upload_window
from tests.provider.support import success


class ResultStorageCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_failed_and_sensitive_refused_preserve_original_provider_terminal_when_save_fails(self):
        for outcome, expected in (('SUCCEEDED', 'FAILED'), ('SENSITIVE_REFUSAL', 'REFUSED')):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                proposal = SyntheticCandidateInput('capacity:1', ('explicit',), 50)
                fixture = Fixture(Path(directory), MediaService(), proposal)
                hooks = LimitedHooks(); fixture.hooks = hooks
                fixture.adapter = SimulationAdapter((Scenario(outcome, {'text': '\x00' * 512, 'modality': 'IMAGE', 'task': 'DESCRIBE',
                    'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                    {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
                await fixture.initialize()
                try:
                    runtime = fixture.runtime; assert runtime is not None
                    entry, _ = await upload_window(fixture)
                    execute = runtime.execute
                    async def limited(kind, key, values):
                        if kind == 'store_occurrence_result': hooks.limited = True
                        return await execute(kind, key, values)
                    runtime.execute = limited
                    result = await entry.run_learning('original')
                    self.assertIsNot(type(result), Committed)
                    self.assertIn(sqlite3.SQLITE_FULL, hooks.codes)
                    self.assertEqual(len(fixture.adapter.calls), 1)
                    hooks.limited = False
                    await fixture.close()
                    with closing(sqlite3.connect(fixture.path)) as connection:
                        work = connection.execute('SELECT provider_request_id,phase,original_operation_key FROM media_work').fetchone()
                        self.assertEqual(work[1], 'REQUEST_ASSOCIATED')
                        self.assertEqual(connection.execute('SELECT count(*) FROM media_interpretations').fetchone()[0], 0)
                        self.assertEqual(connection.execute("SELECT count(*) FROM media_references WHERE owner_kind='PROCESSING'").fetchone()[0], 1)
                        self.assertEqual(connection.execute('SELECT count(*) FROM media_guards').fetchone()[0], 0)
                    fixture = Fixture(Path(directory), MediaService(), proposal)
                    restored = LimitedHooks(); restored.restore = True; fixture.hooks = restored
                    await fixture.initialize('OPEN_EXISTING')
                    self.assertEqual(len(fixture.adapter.calls), 0)
                    assert fixture.media is not None
                    rows = await fixture.media.rows.read('work_active_page', {'after': '', 'limit': 16})
                    self.assertEqual(rows, ())
                    with closing(sqlite3.connect(fixture.path)) as connection:
                        saved = connection.execute('SELECT body FROM media_interpretations').fetchone()[0]
                        self.assertEqual(connection.execute('SELECT original_operation_key FROM media_work').fetchone()[0], work[2])
                        self.assertEqual(connection.execute('SELECT count(*) FROM media_guards').fetchone()[0], int(expected == 'REFUSED'))
                    interpretation = decode_interpretation(saved.encode())
                    self.assertEqual(interpretation['status'], expected)
                    self.assertEqual(interpretation['provider_request_id'], work[0])
                    if expected == 'REFUSED': self.assertEqual(interpretation['text'], '敏感信息无法访问')
                    else: self.assertEqual(interpretation['failure_reason'], 'RESULT_LIMIT_EXCEEDED')
                finally:
                    hooks.limited = False
                    await fixture.close()
