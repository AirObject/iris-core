"""Public ingestion distinguishes invalid shape, conflicting states and full limits."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.configuration.content_resolution import resolve_content_configuration, ContentConfigurationOk
from companion_memory.ingress.events import canonical_event, event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, Found, Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.runtime.results import Rejected
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


def single_target_fixture(root: Path) -> Fixture:
    """Resolve the actual legal large-event/single-target combination before opening."""
    fixture = Fixture(root, MediaService(), SyntheticCandidateInput('large_source:1', ('explicit candidate',), 50))
    fixture.supplied[1]['explicit_values']['ingress.event_max_bytes'] = 8192
    fixture.supplied[2][0]['explicit_values'].update({
        'platforms.sample_platform.buffer.history_context_count': 0,
        'platforms.sample_platform.buffer.target_count': 1,
        'platforms.sample_platform.buffer.recent_context_count': 0})
    resolved = resolve_content_configuration(*fixture.supplied)
    assert type(resolved) is ContentConfigurationOk, resolved
    fixture.candidate = resolved.value
    return fixture


def counts(path: Path) -> tuple[tuple[str, int], ...]:
    """Compare every actual persisted table across an invalid public input."""
    with sqlite3.connect(path) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return tuple((name, connection.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]) for name in names)


async def uploaded_event(fixture: Fixture, key: str) -> dict[str, object]:
    media = fixture.media; assert media is not None
    port = media.bind_upload('entry')
    begun = await port.begin_upload(key, 'IMAGE'); assert type(begun) is Committed
    uid = record(begun.receipt.result)['upload_id']
    await port.append_upload(uid, 0, b'actual input bytes')
    assert type(await port.finish_upload(uid)) is Committed
    raw = event(key); raw['event_version'] = 2
    mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 8192, occurrence_limit=2, text_limit=512))[0]
    raw['media'] = [{'reference_id': uid, 'occurrence_id': identity('occurrence', mid, 0), 'modality': 'IMAGE',
        'interpretation': {'status': 'COMPLETE', 'text': 'original', 'source_ref': 'attribution', 'coverage': 'COMPLETE'}}]
    return raw


class ContentInputBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_report_first_cause_and_no_new_persistent_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await single_target_fixture(Path(directory)).initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                raw = await uploaded_event(fixture, 'bad-report')
                medium = cast(list[dict], raw['media'])[0]
                before = counts(fixture.path)
                reports = (
                    ({'status': [], 'text': None, 'source_ref': None, 'coverage': 'UNSPECIFIED'}, 'INVALID_SHAPE'),
                    ({'status': 'EMPTY', 'text': 'contradiction', 'source_ref': 'x' * 129, 'coverage': 'COMPLETE'}, 'INVALID_STATE_COMBINATION'),
                    ({'status': 'COMPLETE', 'text': 'x' * 513, 'source_ref': None, 'coverage': 'bad'}, 'LIMIT_EXCEEDED'),
                    ({'status': 'COMPLETE', 'text': 'x', 'source_ref': '\x00' * 22, 'coverage': 'COMPLETE'}, 'LIMIT_EXCEEDED'),
                    ({'status': 'COMPLETE', 'text': '\x00' * 512, 'source_ref': 'valid', 'coverage': 'COMPLETE'}, 'LIMIT_EXCEEDED'),
                    ({'status': 'MISSING', 'text': None, 'source_ref': None, 'coverage': 'COMPLETE'}, 'INVALID_STATE_COMBINATION'),
                )
                entry = runtime.bind_entry('entry')
                for report, reason in reports:
                    with self.subTest(reason=reason, status=report['status']):
                        medium['interpretation'] = report
                        result = await entry.accept_event('original', raw)
                        self.assertIs(type(result), Rejected); assert type(result) is Rejected
                        self.assertEqual(result.error.reason, reason)
                        self.assertEqual(counts(fixture.path), before)
                medium['interpretation'] = None; raw['body'] = 'x' * 8193
                result = await entry.accept_event('original', raw); assert type(result) is Rejected
                self.assertEqual(result.error.reason, 'LIMIT_EXCEEDED')
                self.assertEqual(counts(fixture.path), before); self.assertEqual(fixture.adapter.calls, ())
            finally: await fixture.close()

    async def test_maximum_event_and_two_complete_interpretations_round_trip(self):
        for with_media in (False, True):
            with self.subTest(with_media=with_media), tempfile.TemporaryDirectory() as directory:
                fixture = await single_target_fixture(Path(directory)).initialize()
                try:
                    runtime = fixture.runtime; media = fixture.media
                    assert runtime is not None and media is not None
                    raw = await uploaded_event(fixture, 'max') if with_media else event('max')
                    raw['event_version'] = 2
                    mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 8192, occurrence_limit=2, text_limit=512))[0]
                    if with_media:
                        first = cast(list[dict], raw['media'])[0]
                        raw['media'] = [first, {**first, 'occurrence_id': identity('occurrence', mid, 1)}]
                        for medium in cast(list[dict], raw['media']):
                            medium['interpretation'] = {'status': 'COMPLETE', 'text': 'x', 'source_ref': 's' * 128, 'coverage': 'COMPLETE'}
                            upload = (await media.rows.read('uploads_get', {'upload_id': medium['reference_id']}))[0]
                            owned = isolate_media_event(raw, 8192, occurrence_limit=2, text_limit=512)
                            part = next(record(v) for v in sequence(owned['media']) if record(v)['occurrence_id'] == medium['occurrence_id'])
                            iid = identity('interpretation', part['occurrence_id'], 'EXTERNAL')
                            base = len(encode_content(media.external_interpretation(upload, part, mid, iid), 2048)) - 1
                            content_size = 2048 - base
                            controls = max(0, (content_size - 512 + 4) // 5)
                            medium['interpretation']['text'] = '\x00' * controls + 'x' * (content_size - 6 * controls)
                    raw['body'] = ''
                    # The complete event limit includes all reports and JSON escaping.
                    length = len(canonical_event(isolate_media_event({**raw, 'body': 'x'}, 8192, occurrence_limit=2, text_limit=512))) - 1
                    raw['body'] = 'x' * (8192 - length)
                    owned = isolate_media_event(raw, 8192, occurrence_limit=2, text_limit=512)
                    self.assertEqual(len(canonical_event(owned)), 8192)
                    entry = runtime.bind_entry('entry')
                    self.assertIs(type(await entry.accept_event('maximum', raw)), Committed)
                    result = await entry.run_learning('maximum'); assert type(result) is Committed, result
                    values = record(result.receipt.result)
                    oid = cast(str, record(sequence(values['object_refs'])[0])['object_id'])
                    inspector = runtime.sources.bind_inspection((oid,))
                    member = await inspector.read_source_member(oid, values['source_id'], 0)
                    assert type(member) is Found, member
                    self.assertEqual(record(member.value)['event'], owned)
                    interpretations = sequence(record(member.value)['interpretations'])
                    self.assertEqual(len(interpretations), 2 if with_media else 0)
                    for interpretation in interpretations: self.assertEqual(len(encode_content(interpretation, 2048)), 2048)
                    self.assertGreater(len(encode_content(member.value, 16384)), 8192)
                    self.assertEqual(len(fixture.adapter.calls), 1)
                finally: await fixture.close()
