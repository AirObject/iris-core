"""Actual missing-media preparation, protected bytes and local Provider results."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import cast
from companion_memory.ingress.events import canonical_event, event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed
from companion_memory.persistence.content_codec import decode_content
from companion_memory.provider import Scenario, SimulationAdapter, WorkGrant, CancellationSource, Completed, ResultGrant
from companion_memory.provider.stored_media import StoredMediaAuthorized
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.values import freeze, as_record
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event
from tests.provider.support import success


class InternalWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_media_work_stores_success_failure_and_sensitive_result(self):
        for text, outcome, expected in (('Actual simulated description', 'SUCCEEDED', 'COMPLETE'),
                ('', 'SUCCEEDED', 'EMPTY'), (' ' * 8, 'SUCCEEDED', 'FAILED'), ('\x00' * 512, 'SUCCEEDED', 'FAILED'),
                ('untrusted variable refusal body' * 100, 'SENSITIVE_REFUSAL', 'REFUSED')):
            with self.subTest(expected=expected, length=len(text)), tempfile.TemporaryDirectory() as directory:
                media = MediaService(); fixture = Fixture(Path(directory), media)
                fixture.adapter = SimulationAdapter((Scenario(outcome, {'text': text, 'modality': 'IMAGE', 'task': 'DESCRIBE',
                    'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                    {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
                await fixture.initialize()
                try:
                    upload = media.bind_upload('entry')
                    begun = await upload.begin_upload('missing_image', 'IMAGE'); assert type(begun) is Committed
                    uid = cast(str, record(begun.receipt.result)['upload_id'])
                    await upload.append_upload(uid, 0, b'actual raw bytes')
                    finished = await upload.finish_upload(uid); assert type(finished) is Committed
                    occurrence_id = ''
                    for i in range(3):
                        raw = event('input' + str(i)); raw['event_version'] = 2
                        mid, _, _ = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))
                        if i == 0:
                            occurrence_id = identity('occurrence', mid, 0)
                            raw['media'] = [{'reference_id': uid, 'occurrence_id': occurrence_id, 'modality': 'IMAGE', 'interpretation': None}]
                        encoded = canonical_event(isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))
                        await fixture.execute('accept_media_event', 'input:' + str(i), {'entry_id': 'entry', 'event': encoded.decode()})
                    await fixture.execute('select_content_preparation', 'select', {'entry_id': 'entry', 'preparation_id': 'preparation', 'batch_id': 'batch', 'run_id': 'run'})
                    await fixture.execute('claim_content_preparation', 'claim', {'preparation_id': 'preparation', 'expected_revision': 1, 'owner_generation': 1})
                    registered = await fixture.execute('register_occurrence_work', 'register_media', {'preparation_id': 'preparation', 'owner_generation': 1,
                        'occurrence_id': occurrence_id, 'authorization_domain_id': 'domain', 'profile_id': 'sample_media', 'prompt_revision': 'describe:1',
                        'interpretation_fingerprint': hashlib.sha256(b'explicit media policy').hexdigest()})
                    wid = cast(str, record(registered.receipt.result)['operation_id'])
                    work = (await media.rows.read('work_get', {'work_id': wid}))[0]
                    descriptor_leaf = (await media.rows.read('work_descriptors_get', {'work_id': wid, 'admission_generation': 1}))[0]
                    self.assertEqual(descriptor_leaf['body'], work['original_request_descriptor'])
                    self.assertLessEqual(len(cast(str, descriptor_leaf['body']).encode()), 4096)
                    descriptor = as_record(freeze(decode_content(cast(str, work['original_request_descriptor']).encode(), 8192), 8192))
                    from companion_memory.media.provider_source import stored_media_source
                    authority = fixture.provider.bind_stored_media_authority(stored_media_source(media), 'instance', 'media')
                    authorized = await authority.authorize_stored_media(wid, occurrence_id)
                    assert type(authorized) is StoredMediaAuthorized, authorized
                    grant = WorkGrant('media', 'instance', None, 'MEDIA', ('sample_media',), ('MEDIA_UNDERSTANDING',), 'media', 'scheduler', ('run',), ('entry',), prompt_revisions=('describe:1',))
                    port = fixture.provider.bind_work(grant)
                    raw_request = {**descriptor, 'entry_ids': ['entry'], 'deadline': time.monotonic() + 30, 'cancellation': CancellationSource().token,
                        'payload': {'media': authorized.media, 'modality': 'IMAGE', 'task': 'DESCRIBE'}}
                    result = await port.understand_media(raw_request)
                    assert type(result) is Completed, result
                    rid = cast(str, result.record['object_id'])
                    evidence = await fixture.provider.bind_result_owner(ResultGrant('media', (rid,))).verify_terminal(rid, descriptor)
                    assert type(evidence) is TerminalVerified, evidence
                    media.work.retain_terminal(evidence.value)
                    saved = await fixture.execute('store_occurrence_result', 'store_media', {'work_id': wid, 'request_id': rid, 'expected_revision': 1})
                    iid = record(saved.receipt.result)['operation_id']
                    from companion_memory.media.interpretations import decode_interpretation
                    value = decode_interpretation(cast(str, (await media.rows.read('interpretations_get', {'interpretation_id': iid}))[0]['body']).encode())
                    self.assertEqual(value['status'], expected)
                    if expected == 'REFUSED': self.assertEqual(value['text'], '敏感信息无法访问')
                    if text == '\x00' * 512: self.assertEqual(value['failure_reason'], 'RESULT_LIMIT_EXCEEDED')
                    self.assertTrue(authority.release_media_authorization(authorized.media))
                    self.assertEqual(authorized.media._content, b'')
                    await fixture.execute('release_occurrence_processing', 'release_media', {'work_id': wid})
                    self.assertEqual((await media.rows.read('processing_count', {}))[0]['count'], 0)
                    from companion_memory.runtime.content_media import complete_preparation
                    completed = await complete_preparation(fixture.assembly, fixture.execute, 'preparation')
                    assert type(completed) is Committed, completed
                    await fixture.execute('freeze_content_batch', 'freeze', {'preparation_id': 'preparation', 'expected_revision': 3, 'owner_generation': 1})
                    from companion_memory.memory.sources import decode_source
                    source = decode_source(cast(str, (await fixture.assembly.rows.read('batches_get', {'batch_id': 'batch'}))[0]['manifest']))
                    self.assertEqual(record(sequence(record(sequence(source['ordered_members'])[0])['media'])[0])['interpretation_id'], iid)
                    learned = await fixture.learn(source, 2)
                    self.assertEqual(len(sequence(record(learned.receipt.result)['object_refs'])), 2)
                    self.assertEqual(len(fixture.adapter.calls), 2)
                finally: await fixture.close()
