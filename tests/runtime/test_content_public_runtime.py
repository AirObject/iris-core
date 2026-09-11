"""Public native entry flow with actual owners, gate and original Provider work."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, Found
from companion_memory.provider import SimulationAdapter, Scenario
from tests.memory.support import Fixture
from tests.provider.support import success
from tests.runtime.configuration_support import event


class ContentPublicRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_missing_media_to_formal_memory_repeats_original_result(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            proposals = SyntheticCandidateInput('explicit_test_input:1', ('Explicit synthetic fact A.', 'Explicit synthetic fact B.'), 50)
            fixture = Fixture(Path(directory), media, proposals)
            fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'A simulated description of stored bytes.', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
            await fixture.initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                ingress = runtime.bind_entry('entry'); upload = media.bind_upload('entry')
                begun = await upload.begin_upload('upload', 'IMAGE'); assert type(begun) is Committed
                uid = cast(str, record(begun.receipt.result)['upload_id'])
                await upload.append_upload(uid, 0, b'actual raw bytes')
                ready = await upload.finish_upload(uid); assert type(ready) is Committed
                for i in range(3):
                    raw = event('source' + str(i)); raw['event_version'] = 2
                    if i == 0:
                        isolated = isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512)
                        mid, _, _ = event_identity(('instance', 'host', 'entry'), isolated)
                        raw['media'] = [{'reference_id': uid, 'occurrence_id': identity('occurrence', mid, 0), 'modality': 'IMAGE', 'interpretation': None}]
                    result = await ingress.accept_event('input:' + str(i), raw); assert type(result) is Committed, result
                    repeated = await ingress.accept_event('input:' + str(i), raw); assert type(repeated) is Committed, repeated
                    self.assertEqual(repeated.receipt, result.receipt)
                finished = await ingress.run_learning('trigger'); assert type(finished) is Committed, finished
                value = record(finished.receipt.result)
                self.assertEqual(value['terminal'], 'SUCCEEDED')
                self.assertEqual(len(sequence(value['object_refs'])), 2)
                repeated = await ingress.run_learning('trigger'); assert type(repeated) is Committed, repeated
                self.assertEqual(repeated.receipt, finished.receipt)
                self.assertEqual(len(fixture.adapter.calls), 2)
                assert fixture.memory is not None
                ids = tuple(cast(str, record(ref)['object_id']) for ref in sequence(value['object_refs']))
                read = fixture.memory.bind_read(ids, ('get_current',))
                for oid in ids: self.assertIs(type(await read.get_current(oid)), Found)
                from companion_memory.memory.service import MemoryError
                from companion_memory.persistence import NotFound
                maintenance = runtime.maintenance.bind(ids)
                denied = await maintenance.delete_object('denied', 'not-granted', 1)
                assert type(denied) is MemoryError
                self.assertEqual(denied.code, 'ACCESS_DENIED')
                deleted = await maintenance.delete_object('delete', ids[0], 1)
                assert type(deleted) is Committed, deleted
                original_delete = await maintenance.delete_object('delete', ids[0], 1)
                assert type(original_delete) is Committed, original_delete
                self.assertEqual(original_delete.receipt, deleted.receipt)
                self.assertIs(type(await read.get_current(ids[0])), NotFound)
                self.assertIs(type(await read.get_current(ids[1])), Found)
                conflict = await maintenance.delete_object('delete', ids[0], 2)
                assert type(conflict) is MemoryError
                self.assertEqual(conflict.code, 'IDEMPOTENCY_CONFLICT')
                self.assertEqual(runtime.gate._grants, {})
                self.assertEqual(fixture.provider._stored_media, {})
                self.assertEqual(runtime.media._held if runtime.media else {}, {})
            finally: await fixture.close()
