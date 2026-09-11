"""Real bounded upload, nonoverwriting deduplication and durable generation checks."""
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import cast
from companion_memory.media.service import MediaService, MediaError, VolatileProgress
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from tests.memory.support import Fixture


class FileOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_upload_deduplication_and_original_ready_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
            try:
                port = media.bind_upload('entry'); ids = []
                content = bytes(range(256)) * 4096
                for key in ('first', 'second'):
                    begun = await port.begin_upload(key, 'IMAGE'); assert type(begun) is Committed, begun
                    uid = cast(str, record(begun.receipt.result)['upload_id']); ids.append(uid)
                    for offset in range(0, len(content), 65536):
                        progress = await port.append_upload(uid, offset, content[offset:offset + 65536])
                        assert type(progress) is VolatileProgress, progress
                    finished = await port.finish_upload(uid); assert type(finished) is Committed, finished
                    blobs = tuple((fixture.root / 'media' / 'published').iterdir())
                    self.assertEqual(len(blobs), 1)
                    self.assertEqual(blobs[0].read_bytes(), content)
                    resolved = await port.resolve_upload(key, 'IMAGE'); assert type(resolved) is Found, resolved
                    self.assertEqual(resolved.value, finished.receipt)
                self.assertNotEqual(ids[0], ids[1])
                self.assertEqual(tuple((fixture.root / 'media' / 'staging').iterdir()), ())
                wrong = await port.append_upload(ids[0], 0, b'x')
                self.assertIs(type(wrong), MediaError)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally:
                await fixture.close()

    async def test_external_media_selection_is_saved_with_event_and_frozen_source(self):
        with tempfile.TemporaryDirectory() as directory:
            from companion_memory.ingress.events import canonical_event, event_identity
            from companion_memory.ingress.media_events import isolate_media_event
            from companion_memory.media.service import identity
            from tests.runtime.configuration_support import event
            media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
            try:
                port = media.bind_upload('entry')
                begun = await port.begin_upload('image', 'IMAGE'); assert type(begun) is Committed
                uid = cast(str, record(begun.receipt.result)['upload_id'])
                await port.append_upload(uid, 0, b'actual owned image bytes')
                done = await port.finish_upload(uid); assert type(done) is Committed, done
                for ordinal in range(3):
                    raw = event('media' + str(ordinal)); raw['event_version'] = 2
                    isolated = isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512)
                    mid, _, _ = event_identity(('instance', 'host', 'entry'), isolated)
                    if ordinal == 0:
                        raw['media'] = [{'reference_id': uid, 'occurrence_id': identity('occurrence', mid, 0), 'modality': 'IMAGE',
                            'interpretation': {'status': 'COMPLETE', 'text': 'An external image report.', 'source_ref': 'sender', 'coverage': 'COMPLETE'}}]
                    encoded = canonical_event(isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))
                    await fixture.execute('accept_media_event', 'event' + str(ordinal), {'entry_id': 'entry', 'event': encoded.decode()})
                source = await fixture.freeze()
                members = cast(tuple[MappingProxyType, ...], source['ordered_members'])
                self.assertEqual(len(members[0]['media']), 1)
                self.assertEqual(len(fixture.adapter.calls), 0)
                finished = await fixture.learn(source, 2)
                self.assertEqual(len(cast(tuple, record(finished.receipt.result)['object_refs'])), 2)
                self.assertEqual(len(fixture.adapter.calls), 1)
                from companion_memory.media.original_access import OriginalChunk, OriginalInspection
                from companion_memory.memory.changes import isolate_change
                from companion_memory.memory.formats import sequence
                occurrence_id = cast(str, members[0]['media'][0]['occurrence_id'])
                reader = media.bind_original_inspection('entry', (occurrence_id,))
                metadata = await reader.read_occurrence(occurrence_id); assert type(metadata) is Found, metadata
                self.assertEqual(record(record(metadata.value)['interpretation'])['origin'], 'EXTERNAL')
                self.assertEqual(record(record(metadata.value)['interpretation'])['text'], 'An external image report.')
                self.assertNotIn('blob_id', record(metadata.value))
                self.assertNotIn('upload_id', record(metadata.value))
                read = await reader.read_original(occurrence_id, 0, 65536)
                assert type(read) is OriginalChunk, read
                self.assertEqual(read.content, b'actual owned image bytes')
                self.assertTrue(read.eof)
                wrong = media.bind_original_inspection('another_entry', (occurrence_id,))
                self.assertIs(type(await wrong.read_original(occurrence_id, 0, 16)), MediaError)
                from companion_memory.persistence import NotFound
                self.assertIs(type(await wrong.read_occurrence(occurrence_id)), NotFound)
                forged = object.__new__(OriginalInspection)
                self.assertIs(type(await forged.read_original(occurrence_id, 0, 16)), MediaError)
                self.assertIs(type(await forged.read_occurrence(occurrence_id)), MediaError)
                # A rejected cross-entry request has no physical READ holder.
                self.assertEqual((await media.rows.read('reference_count', {
                    'blob_id': record(done.receipt.result)['blob_id'], 'generation': 1}))[0]['count'], 2)
                for ordinal, reference in enumerate(sequence(record(finished.receipt.result)['object_refs'])):
                    change = isolate_change({'change_version': 1, 'action': 'DELETE_OBJECT', 'target_id': record(reference)['object_id'],
                        'expected_revision': 1, 'proposed_value': None, 'links': None}, 8192)
                    deleted = await fixture.maintenance('delete_media:' + str(ordinal), change)
                    audit_read = await fixture.storage.bind_audit_reader('instance').read(deleted.receipt.identity)
                    assert type(audit_read) is Found, audit_read
                    owners = {audit.owner_module for audit in audit_read.value}
                    self.assertEqual('media' in owners, ordinal == 1)
                blob = (await media.rows.read('blobs_get', {'blob_id': record(done.receipt.result)['blob_id']}))[0]
                self.assertEqual(blob['reference_count'], 0)
                self.assertEqual(blob['state'], 'READY')
                self.assertIs(type(await reader.read_occurrence(occurrence_id)), NotFound)
                self.assertIs(type(await reader.read_original(occurrence_id, 0, 16)), MediaError)
                from unittest.mock import patch
                with patch('companion_memory.media.service.time.time_ns', return_value=(cast(int, blob['unreferenced_at_us']) + 600000000) * 1000):
                    collected = await media.collect_unreferenced()
                assert type(collected) is Found, collected
                self.assertEqual(record(collected.value)['deleted'], 1)
                self.assertEqual(tuple(media.published.iterdir()), ())

            finally:
                await fixture.close()

    async def test_equal_hash_and_length_still_compare_actual_bytes_without_overwrite(self):
        import hashlib
        from types import SimpleNamespace
        from unittest.mock import patch
        class Collision:
            def update(self, value): pass
            def hexdigest(self): return '1' * 64
        def checksum(*args):
            return hashlib.sha256(*args) if args else Collision()
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
            try:
                port = media.bind_upload('entry')
                with patch('companion_memory.media.service.hashlib', SimpleNamespace(sha256=checksum)):
                    for ordinal, content in enumerate((b'first content', b'other content')):
                        begun = await port.begin_upload('collision:' + str(ordinal), 'IMAGE'); assert type(begun) is Committed, begun
                        uid = cast(str, record(begun.receipt.result)['upload_id'])
                        await port.append_upload(uid, 0, content)
                        finished = await port.finish_upload(uid)
                        if ordinal == 0: self.assertIs(type(finished), Committed)
                        else:
                            assert type(finished) is MediaError, finished
                            self.assertEqual(finished.reason, 'HASH_COLLISION')
                    files = tuple(media.published.iterdir())
                    self.assertEqual(len(files), 1)
                    self.assertEqual(files[0].read_bytes(), b'first content')
                    self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()

    async def test_actual_readonly_descriptor_write_failure_never_publishes_empty_content(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
            try:
                port = media.bind_upload('entry')
                begun = await port.begin_upload('readonly', 'IMAGE'); assert type(begun) is Committed, begun
                uid = cast(str, record(begun.receipt.result)['upload_id']); path = media.staging / uid
                original_open = os.open
                def readonly(file, flags, *args, **kwargs):
                    if Path(file) == path or str(file) == uid: flags &= ~(os.O_RDWR | os.O_WRONLY)
                    return original_open(file, flags, *args, **kwargs)
                with patch('companion_memory.media.service.os.open', readonly):
                    failed = await port.append_upload(uid, 0, b'owned file bytes')
                assert type(failed) is MediaError, failed
                self.assertEqual(failed.code, 'FILE_FAILED')
                self.assertEqual(path.stat().st_size, 0)
                empty = await port.finish_upload(uid); assert type(empty) is MediaError, empty
                self.assertEqual(empty.reason, 'CONTENT_CORRUPT')
                self.assertFalse(tuple(media.published.iterdir()))
                progress = await port.append_upload(uid, 0, b'owned file bytes'); assert type(progress) is VolatileProgress, progress
                final = await port.finish_upload(uid); assert type(final) is Committed, final
                self.assertEqual(next(media.published.iterdir()).read_bytes(), b'owned file bytes')
                self.assertFalse(media._jobs)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
