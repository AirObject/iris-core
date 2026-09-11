"""Creation and last-source retirement share one real candidate transaction."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.cognition.synthetic_graph import SyntheticGraphInput, FactProposal
from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
from companion_memory.cognition.synthetic_mixed import SyntheticMixedInput
from companion_memory.media.service import MediaService
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, NotFound
from companion_memory.provider import SimulationAdapter, Scenario
from tests.memory.support import Fixture
from tests.runtime.test_preparation_lifecycle import upload_window
from tests.runtime.configuration_support import event
from tests.provider.support import success


class MixedCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_and_delete_preserve_shared_window_and_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Fixture(root, MediaService(), SyntheticCandidateInput('original:1', ('old fact',), 50))
            fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
            await fixture.initialize()
            try:
                entry, _ = await upload_window(fixture)
                created = await entry.run_learning('first'); assert type(created) is Committed, created
                value = record(created.receipt.result); source = cast(str, value['source_id'])
                oid = cast(str, record(sequence(value['object_refs'])[0])['object_id'])
                deletion = isolate_change({'change_version': 1, 'action': 'DELETE_OBJECT', 'target_id': oid,
                    'expected_revision': 1, 'proposed_value': None, 'links': None}, 8192)
                proposal = SyntheticMixedInput(SyntheticGraphInput('mixed:1', (FactProposal('new', 'New target fact.', (), 'REAL', None),), 50),
                    SyntheticMutationInput('mixed:1', (deletion,), source_ids=(source,)))
            finally: await fixture.close()
            fixture = await Fixture(root, MediaService(), proposal).initialize('OPEN_EXISTING')
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry')
                for i in range(2):
                    raw = event('later:' + str(i)); raw['event_version'] = 2
                    assert type(await entry.accept_event('later:' + str(i), raw)) is Committed
                result = await entry.run_learning('mixed'); assert type(result) is Committed, result
                value = record(result.receipt.result)
                self.assertEqual(len(sequence(value['object_refs'])), 2)
                self.assertEqual(value['retired_source_ids'], (source,))
                self.assertNotEqual(value['source_id'], source)
                reader = runtime.memory.bind_read((oid,), ('get_current',))
                self.assertIs(type(await reader.get_current(oid)), NotFound)
                retained = (await fixture.assembly.memory.rows.read('sources_get', {'source_id': value['source_id']}))[0]
                self.assertEqual(retained['holder_count'], 1)
                retired = (await fixture.assembly.memory.rows.read('sources_get', {'source_id': source}))[0]
                self.assertEqual(retired['state'], 'RELEASED')
                self.assertEqual(len(fixture.adapter.calls), 1)
                again = await entry.run_learning('mixed'); assert type(again) is Committed, again
                self.assertEqual(result.receipt, again.receipt)
                original_receipt = result.receipt
            finally: await fixture.close()
            fixture = await Fixture(root, MediaService(), proposal).initialize('OPEN_EXISTING')
            try:
                runtime = fixture.runtime; assert runtime is not None
                self.assertEqual(runtime.state, 'READY')
                recovered = await runtime.bind_entry('entry').run_learning('mixed'); assert type(recovered) is Committed, recovered
                self.assertEqual(recovered.receipt, original_receipt)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
