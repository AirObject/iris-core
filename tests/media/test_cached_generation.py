"""Retained interpretation attribution survives physical collection and new generations."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.interpretations import decode_interpretation
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, Found, NotFound
from companion_memory.provider import Scenario, SimulationAdapter
from tests.media.test_guard_dispatch_competition import window
from tests.memory.support import Fixture
from tests.provider.support import success
from tests.runtime.configuration_support import event


class CachedGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_republished_bytes_reuse_original_interpretation_and_reopen_without_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); proposal = SyntheticCandidateInput('cache:1', ('synthetic fact',), 50)
            media = MediaService(); fixture = Fixture(root, media, proposal, content_changes={'memory.initial_retention': 19})
            fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'content description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success(), success()))
            await fixture.initialize()
            try:
                entry, old_occurrence = await window(fixture, 'entry')
                first = await entry.run_learning('first'); assert type(first) is Committed
                runtime = fixture.runtime; assert runtime is not None
                oid = cast(str, record(sequence(record(first.receipt.result)['object_refs'])[0])['object_id'])
                old = (await media.rows.read('occurrences_get', {'occurrence_id': old_occurrence}))[0]
                read = runtime.memory.bind_read((oid,), ('get_current', 'get_for_deep_read'))
                self.assertIs(type(await read.get_current(oid)), NotFound)
                deep = await read.get_for_deep_read(oid); assert type(deep) is Found
                self.assertEqual(record(deep.value)['lifecycle'], 'FORGOTTEN')
                with patch('companion_memory.media.service.time.time_ns', return_value=1000000000000):
                    protected = await media.collect_unreferenced()
                assert type(protected) is Found, protected
                self.assertEqual(record(protected.value)['deleted'], 0)
                self.assertEqual(len(tuple(media.published.iterdir())), 1)
                removed = await runtime.maintenance.bind((oid,)).delete_object('delete', oid, 1); assert type(removed) is Committed
                blob = (await media.rows.read('blobs_get', {'blob_id': old['blob_id']}))[0]
                self.assertEqual(blob['reference_count'], 0)
                with patch('companion_memory.media.service.time.time_ns', return_value=(cast(int, blob['unreferenced_at_us']) + 600000000) * 1000):
                    collected = await media.collect_unreferenced()
                assert type(collected) is Found, collected
                self.assertEqual(record(collected.value)['deleted'], 1)
                upload = media.bind_upload('entry')
                begun = await upload.begin_upload('new_generation', 'IMAGE'); assert type(begun) is Committed
                uid = record(begun.receipt.result)['upload_id']
                await upload.append_upload(uid, 0, b'bytes shared across two entries')
                assert type(await upload.finish_upload(uid)) is Committed
                raw = event('next_media'); raw['event_version'] = 2
                mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
                occurrence = identity('occurrence', mid, 0)
                raw['media'] = [{'reference_id': uid, 'occurrence_id': occurrence, 'modality': 'IMAGE', 'interpretation': None}]
                assert type(await entry.accept_event('next_media', raw)) is Committed
                raw = event('next_context'); raw['event_version'] = 2
                assert type(await entry.accept_event('next_context', raw)) is Committed
                second = await entry.run_learning('second'); assert type(second) is Committed, second
                current = (await media.rows.read('occurrences_get', {'occurrence_id': occurrence}))[0]
                self.assertEqual(current['generation'], 2)
                self.assertEqual(current['interpretation_id'], old['interpretation_id'])
                interpretation = decode_interpretation(cast(str, (await media.rows.read('interpretations_get', {'interpretation_id': current['interpretation_id']}))[0]['body']).encode())
                self.assertEqual(interpretation['generation'], 1)
                self.assertEqual(len(fixture.adapter.calls), 3)
            finally: await fixture.close()
            fixture = await Fixture(root, MediaService(), proposal, content_changes={'memory.initial_retention': 19}).initialize('OPEN_EXISTING')
            try:
                runtime = fixture.runtime; assert runtime is not None
                self.assertEqual(runtime.state, 'READY')
                repeated = await runtime.bind_entry('entry').run_learning('second'); assert type(repeated) is Committed, repeated
                self.assertEqual(repeated.receipt, second.receipt)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
