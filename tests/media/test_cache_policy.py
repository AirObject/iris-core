"""Public cache reuse follows native profile, prompt, domain and modality policy."""
import copy
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed
from companion_memory.provider import Scenario, SimulationAdapter
from companion_memory.runtime.content_media import MediaPolicy
from tests.configuration.content_support import candidate
from tests.media.test_guard_dispatch_competition import window
from tests.memory.support import Fixture
from tests.provider.support import success
from tests.runtime.configuration_support import event


def description(text='description', profile='sample_media', modality='IMAGE'):
    return Scenario('SUCCEEDED', {'text': text, 'modality': modality,
        'task': 'TRANSCRIBE' if modality == 'AUDIO' else 'DESCRIBE', 'source': 'SIMULATED',
        'profile_id': profile, 'model_id': profile + '_model'},
        {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16})


async def append_window(fixture, modality='IMAGE', entry=None):
    media = fixture.media; runtime = fixture.runtime
    assert media is not None and runtime is not None
    upload = media.bind_upload('entry')
    begun = await upload.begin_upload('next-upload', modality); assert type(begun) is Committed
    uid = record(begun.receipt.result)['upload_id']
    await upload.append_upload(uid, 0, b'bytes shared across two entries')
    assert type(await upload.finish_upload(uid)) is Committed
    raw = event('next-media'); raw['event_version'] = 2
    mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
    oid = identity('occurrence', mid, 0)
    raw['media'] = [{'reference_id': uid, 'occurrence_id': oid, 'modality': modality, 'interpretation': None}]
    entry = entry or runtime.bind_entry('entry')
    assert type(await entry.accept_event('next-media', raw)) is Committed
    raw = event('next-context'); raw['event_version'] = 2
    assert type(await entry.accept_event('next-context', raw)) is Committed
    return entry, oid


class CachePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_cache_key_changes_and_previous_failed_occurrence(self):
        for change in ('SAME', 'EMPTY', 'SENSITIVE_PROFILE', 'PROMPT', 'PROFILE', 'DOMAIN', 'MODALITY', 'FAILED'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); settings = candidate(root)[1][0]['explicit_values']
                profiles = copy.deepcopy(settings['provider.profiles'])
                other = copy.deepcopy(next(p for p in profiles if p['profile_id'] == 'sample_media'))
                other.update(profile_id='other_media', model_id='other_media_model'); profiles.append(other)
                roles = copy.deepcopy(settings['provider.role_profiles']); roles['MEDIA'].append('other_media')
                foundation = {'provider.profiles': profiles, 'provider.role_profiles': roles}
                proposal = SyntheticCandidateInput('cache:1', ('explicit result',), 50)
                fixture = Fixture(root, MediaService(), proposal, foundation_changes=foundation)
                initial = Scenario('SENSITIVE_REFUSAL', None, {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}) if change == 'SENSITIVE_PROFILE' else description(' ' if change == 'FAILED' else '' if change == 'EMPTY' else 'description')
                fixture.adapter = SimulationAdapter((initial, success()))
                await fixture.initialize()
                try:
                    entry, first_oid = await window(fixture, 'entry')
                    first = await entry.run_learning('first'); assert type(first) is Committed, first
                    repeated = await entry.run_learning('first'); assert type(repeated) is Committed
                    self.assertEqual(first.receipt, repeated.receipt); self.assertEqual(len(fixture.adapter.calls), 2)
                    assert fixture.media is not None
                    original = (await fixture.media.rows.read('occurrences_get', {'occurrence_id': first_oid}))[0]
                finally: await fixture.close()
                profile = 'other_media' if change in ('PROFILE', 'SENSITIVE_PROFILE') else 'sample_media'
                reuse = change in ('SAME', 'EMPTY', 'SENSITIVE_PROFILE')
                modality = 'AUDIO' if change == 'MODALITY' else 'IMAGE'
                policy = MediaPolicy('other-domain' if change == 'DOMAIN' else 'domain', profile,
                    'describe:2' if change == 'PROMPT' else 'describe:1')
                fixture = Fixture(root, MediaService(), proposal, foundation_changes=foundation, media_policy=policy)
                fixture.adapter = SimulationAdapter(((description(profile=profile, modality=modality),) if not reuse else ()) + (success(),))
                await fixture.initialize('OPEN_EXISTING')
                try:
                    self.assertEqual(len(fixture.adapter.calls), 0)
                    entry, oid = await append_window(fixture, modality)
                    learned = await entry.run_learning('next'); assert type(learned) is Committed, learned
                    assert fixture.media is not None
                    current = (await fixture.media.rows.read('occurrences_get', {'occurrence_id': oid}))[0]
                    self.assertEqual(current['interpretation_id'] == original['interpretation_id'], reuse)
                    self.assertEqual(len(fixture.adapter.calls), 1 if reuse else 2)
                    if change == 'FAILED':
                        old_wid = identity('occurrence_work', fixture.expected_id, first_oid, 'DESCRIBE')
                        wid = identity('occurrence_work', fixture.expected_id, oid, 'DESCRIBE')
                        work = (await fixture.media.rows.read('work_get', {'work_id': wid}))[0]
                        self.assertEqual(work['previous_failure'], old_wid)
                        self.assertEqual(work['terminal_status'], 'COMPLETE')
                    repeated = await entry.run_learning('next'); assert type(repeated) is Committed
                    self.assertEqual(repeated.receipt, learned.receipt)
                finally: await fixture.close()

    async def test_all_external_reports_remain_event_scoped_without_automatic_completion(self):
        for status in ('COMPLETE', 'EMPTY', 'PARTIAL', 'FAILED', 'REFUSED'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = Fixture(root, MediaService(), SyntheticCandidateInput('external:1', ('result',), 50))
                fixture.adapter = SimulationAdapter((success(), description(), success()))
                await fixture.initialize()
                try:
                    report = {'status': status, 'text': '' if status == 'EMPTY' else None if status == 'FAILED' else
                        '敏感信息无法访问' if status == 'REFUSED' else 'event-specific contextual report',
                        'coverage': 'COMPLETE' if status in ('COMPLETE', 'EMPTY') else 'EXPLICIT_PARTIAL' if status == 'PARTIAL' else 'UNSPECIFIED', 'source_ref': 'reporter'}
                    entry, oid = await window(fixture, 'entry', report)
                    first = await entry.run_learning('first'); assert type(first) is Committed, first
                    self.assertEqual(len(fixture.adapter.calls), 1)
                    media = fixture.media; assert media is not None
                    occurrence = (await media.rows.read('occurrences_get', {'occurrence_id': oid}))[0]
                    from companion_memory.media.interpretations import decode_interpretation
                    body = decode_interpretation(cast(str, (await media.rows.read('interpretations_get', {'interpretation_id': occurrence['interpretation_id']}))[0]['body']).encode())
                    self.assertEqual((body['status'], body['origin'], body['scope_kind']), (status, 'EXTERNAL', 'EVENT'))
                    next_entry, next_oid = await append_window(fixture, entry=entry)
                    second = await next_entry.run_learning('second'); assert type(second) is Committed, second
                    current = (await media.rows.read('occurrences_get', {'occurrence_id': next_oid}))[0]
                    self.assertNotEqual(current['interpretation_id'], occurrence['interpretation_id'])
                    self.assertEqual(len(fixture.adapter.calls), 3)
                finally: await fixture.close()
