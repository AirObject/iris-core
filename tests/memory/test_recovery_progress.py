"""Growth recovery resumes verified holders under the same isolated service."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.persistence import Committed, Found
from companion_memory.memory.source_rows import MemorySourceRows
from companion_memory.memory.formats import record
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


class RecoveryProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_initialization_makes_progress_with_one_holder_per_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal = SyntheticCandidateInput('recovery_input:1', tuple('object:' + str(i) for i in range(8)), 50)
            fixture = await Fixture(root, candidate_input=proposal).initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry')
                for ordinal in range(3):
                    raw = event(str(ordinal)); raw['event_version'] = 2
                    assert type(await entry.accept_event('event:' + str(ordinal), raw)) is Committed
                learned = await entry.run_learning('original'); assert type(learned) is Committed, learned
            finally: await fixture.close()
            now = [0.0]; inspected = []
            original_read = MemorySourceRows.read
            async def paced_read(rows, name, values):
                result = await original_read(rows, name, values)
                if name == 'review_manifest':
                    inspected.append(values['object_id'])
                    now[0] += 100
                return result
            fixture = Fixture(root, candidate_input=proposal)
            try:
                with patch('companion_memory.memory.recovery.time', SimpleNamespace(monotonic=lambda: now[0])), patch.object(MemorySourceRows, 'read', paced_read):
                    await fixture.initialize('OPEN_EXISTING')
                    runtime = fixture.runtime; assert runtime is not None
                    self.assertEqual(runtime.state, 'RECOVERING')
                    for _ in range(10):
                        if runtime.state == 'READY': break
                        result = await runtime.initialize(); assert type(result) is Found, result
                        self.assertIn(record(result.value)['state'], ('READY', 'RECOVERY_PENDING'))
                    self.assertEqual(runtime.state, 'READY', (inspected, runtime._memory_recovery.phase, runtime._memory_recovery.holder_after))
                    self.assertEqual(len(inspected), 8)
                    self.assertEqual(len(set(inspected)), 8)
                    self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
