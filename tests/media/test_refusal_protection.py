"""Verified sensitive protection differs from an external occurrence report."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.interpretations import decode_interpretation
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed
from companion_memory.provider import SimulationAdapter, Scenario
from tests.memory.support import Fixture
from tests.provider.support import success
from tests.runtime.configuration_support import event


class RefusalProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_report_does_not_protect_verified_provider_does_and_external_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            fixture = Fixture(Path(directory), media, SyntheticCandidateInput('explicit:1', (), 50))
            fixture.adapter = SimulationAdapter((Scenario('SENSITIVE_REFUSAL', {'text': 'untrusted variable text', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                {'coverage': 'COMPLETE', 'billing_input_units': 4, 'billing_output_units': 0, 'known_cost_atoms': 4}), success(), success()))
            await fixture.initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry'); upload = media.bind_upload('entry')
                async def accept(label, status=None):
                    begun = await upload.begin_upload('upload:' + label, 'IMAGE'); assert type(begun) is Committed
                    uid = cast(str, record(begun.receipt.result)['upload_id'])
                    await upload.append_upload(uid, 0, b'same bytes')
                    assert type(await upload.finish_upload(uid)) is Committed
                    raw = event(label); raw['event_version'] = 2
                    mid, _, _ = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))
                    oid = identity('occurrence', mid, 0)
                    interpretation = None if status is None else {'status': status,
                        'text': 'An external report.' if status == 'COMPLETE' else '敏感信息无法访问',
                        'source_ref': 'sender', 'coverage': 'COMPLETE' if status == 'COMPLETE' else 'UNSPECIFIED'}
                    raw['media'] = [{'reference_id': uid, 'occurrence_id': oid, 'modality': 'IMAGE', 'interpretation': interpretation}]
                    accepted = await entry.accept_event('accept:' + label, raw); assert type(accepted) is Committed, accepted
                    return oid
                async def selected(oid):
                    occurrence = (await media.rows.read('occurrences_get', {'occurrence_id': oid}))[0]
                    row = (await media.rows.read('interpretations_get', {'interpretation_id': occurrence['interpretation_id']}))[0]
                    return decode_interpretation(cast(str, row['body']).encode())
                external = await accept('external-refusal', 'REFUSED')
                missing = await accept('missing')
                raw = event('context'); raw['event_version'] = 2
                assert type(await entry.accept_event('context', raw)) is Committed
                initial = await selected(external)
                self.assertEqual((initial['origin'], initial['status']), ('EXTERNAL', 'REFUSED'))
                self.assertEqual(len(fixture.adapter.calls), 0)
                first = await entry.run_learning('first'); assert type(first) is Committed, first
                self.assertEqual(record(first.receipt.result)['terminal'], 'SUCCEEDED')
                internal = await selected(missing)
                self.assertEqual((internal['origin'], internal['status']), ('INTERNAL', 'REFUSED'))
                self.assertEqual(len(fixture.adapter.calls), 2)
                protected = await accept('protected-missing')
                reported = await accept('external-complete', 'COMPLETE')
                second = await entry.run_learning('second'); assert type(second) is Committed, second
                self.assertEqual(len(fixture.adapter.calls), 3)
                reused = await selected(protected)
                self.assertEqual(reused['interpretation_id'], internal['interpretation_id'])
                complete = await selected(reported)
                self.assertEqual((complete['origin'], complete['status']), ('EXTERNAL', 'COMPLETE'))
                self.assertEqual(complete['text'], 'An external report.')
                self.assertFalse(runtime.gate._guard_writers)
                self.assertFalse(fixture.provider._stored_media)
            finally: await fixture.close()
