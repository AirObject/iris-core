"""Public storage transactions connect actual owners to controlled object reads."""
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import cast
from companion_memory.ingress.events import canonical_event
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.memory.formats import record, sequence
from companion_memory.memory.service import MemoryError
from companion_memory.persistence import Found
from tests.runtime.configuration_support import event
from tests.memory.support import Fixture


class ContentOwnerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_candidate_objects_and_source_survive_queue_rotation_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixture = await Fixture(root).initialize()
            try:
                for ordinal in range(3):
                    raw = event(str(ordinal)); raw['event_version'] = 2
                    encoded = canonical_event(isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))
                    await fixture.execute('accept_media_event', 'accept:' + str(ordinal), {'entry_id': 'entry', 'event': encoded.decode()})
                source = await fixture.freeze(); finished = await fixture.learn(source, 3)
                ids = tuple(cast(str, record(v)['object_id']) for v in sequence(record(finished.receipt.result)['object_refs']))
                self.assertEqual(len(ids), 3)
                assert fixture.memory is not None
                port = fixture.memory.bind_read(ids, ('get_current',))
                original = []
                for oid in ids:
                    found = await port.get_current(oid); self.assertIs(type(found), Found)
                    assert type(found) is Found
                    original.append(found.value)
                source_rows = await fixture.assembly.memory.rows.read('sources_get', {'source_id': source['source_id']})
                self.assertEqual(source_rows[0]['holder_count'], 3)
                from companion_memory.memory.source_access import SourceAccess
                inspector = SourceAccess(fixture.assembly.memory, None).bind_inspection(ids)
                actual_source = await inspector.read_source_manifest(ids[0], source['source_id'])
                assert type(actual_source) is Found, actual_source
                self.assertEqual(actual_source.value, source)
                for ordinal in range(len(sequence(source['ordered_members']))):
                    member = await inspector.read_source_member(ids[0], source['source_id'], ordinal)
                    assert type(member) is Found, member
                    self.assertEqual(record(member.value)['member'], sequence(source['ordered_members'])[ordinal])
                candidate_rows = await fixture.assembly.cognition._rows.read('leaf', {'candidate_id': record(finished.receipt.result)['candidate_id'], 'ordinal': 0})
                self.assertEqual(candidate_rows, ())
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally:
                await fixture.close()
            reopened = await Fixture(root).initialize('OPEN_EXISTING')
            try:
                assert reopened.memory is not None
                port = reopened.memory.bind_read(ids, ('get_current',))
                for oid, value in zip(ids, original):
                    found = await port.get_current(oid); assert type(found) is Found
                    self.assertEqual(found.value, value)
                self.assertEqual(reopened.adapter.calls, ())
            finally:
                await reopened.close()

    async def test_current_read_authority_and_mode_do_not_grant_raw_source_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory)).initialize()
            try:
                assert fixture.memory is not None
                port = fixture.memory.bind_read(('missing',), ('get_current',))
                denied = await port.get_current('another'); self.assertIs(type(denied), MemoryError)
                denied = await port.get_for_deep_read('missing'); self.assertIs(type(denied), MemoryError)
                fixture.gate.mode = 'FOCUSED'
                denied = await port.get_current('missing'); self.assertIs(type(denied), MemoryError)
                assert type(denied) is MemoryError
                self.assertEqual(denied.reason, 'DREAMING')
                self.assertFalse(hasattr(port, 'read_object_history'))
                self.assertFalse(hasattr(port, 'read_source_member'))
            finally:
                await fixture.close()
