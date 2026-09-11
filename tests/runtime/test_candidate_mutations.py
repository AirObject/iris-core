"""Public original learning applies a durable mutation set through real owners."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, NotFound
from companion_memory.runtime.content_assembly import stable
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


class CandidateMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_candidate_deletes_shared_source_atomically_and_recovers_original_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = await Fixture(root, candidate_input=SyntheticCandidateInput('create:1', tuple('Fact ' + str(i) for i in range(8)), 50)).initialize()
            runtime = fixture.runtime; assert runtime is not None
            entry = runtime.bind_entry('entry')
            try:
                for i in range(3):
                    raw = event('input:' + str(i)); raw['event_version'] = 2
                    assert type(await entry.accept_event('input:' + str(i), raw)) is Committed
                created = await entry.run_learning('create'); assert type(created) is Committed, created
                value = record(created.receipt.result)
                ids = tuple(cast(str, record(ref)['object_id']) for ref in sequence(value['object_refs']))
                self.assertEqual(len(ids), 8)
                source_id = cast(str, value['source_id'])
            finally: await fixture.close()
            changes = tuple(isolate_change({'change_version': 1, 'action': 'DELETE_OBJECT', 'target_id': oid,
                'expected_revision': 1, 'proposed_value': None, 'links': None}, 8192) for oid in ids)
            mutation = SyntheticMutationInput('delete:1', changes, source_ids=(source_id,))
            fixture = await Fixture(root, candidate_input=mutation).initialize('OPEN_EXISTING')
            runtime = fixture.runtime; assert runtime is not None
            entry = runtime.bind_entry('entry')
            try:
                for i in range(3, 5):
                    raw = event('input:' + str(i)); raw['event_version'] = 2
                    assert type(await entry.accept_event('input:' + str(i), raw)) is Committed
                deleted = await entry.run_learning('delete'); assert type(deleted) is Committed, deleted
                deleted_value = record(deleted.receipt.result)
                self.assertEqual(len(sequence(deleted_value['object_refs'])), 8)
                self.assertEqual(len(sequence(deleted_value['history'])), 8)
                self.assertIsNone(deleted_value['source_id'])
                reader = runtime.memory.bind_read(ids, ('get_current',))
                for oid in ids: self.assertIs(type(await reader.get_current(oid)), NotFound)
                source = (await fixture.assembly.memory.rows.read('sources_get', {'source_id': source_id}))[0]
                self.assertEqual(source['holder_count'], 0)
                root_id = stable('candidate_root', deleted_value['batch_id'], deleted_value['candidate_id'])
                release_root = (await fixture.assembly.memory.rows.read('release_roots_get', {'root_id': root_id}))[0]
                self.assertIsNotNone(release_root['successful_plan'])
                original = await entry.run_learning('delete'); assert type(original) is Committed, original
                self.assertEqual(original.receipt, deleted.receipt)
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally: await fixture.close()
            fixture = await Fixture(root, candidate_input=mutation).initialize('OPEN_EXISTING')
            runtime = fixture.runtime; assert runtime is not None
            try:
                original = await runtime.bind_entry('entry').run_learning('delete'); assert type(original) is Committed, original
                self.assertEqual(original.receipt, deleted.receipt)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
