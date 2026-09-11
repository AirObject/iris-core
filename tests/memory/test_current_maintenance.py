"""Actual owner mutation and history verification in the original SQLite commit."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.ingress.events import canonical_event, plain
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Found, NotFound
from tests.runtime.configuration_support import event
from tests.memory.support import Fixture


class CurrentMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def setup_objects(self, fixture):
        for i in range(3):
            raw = event('e' + str(i)); raw['event_version'] = 2
            encoded = canonical_event(isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))
            await fixture.execute('accept_media_event', 'accept' + str(i), {'entry_id': 'entry', 'event': encoded.decode()})
        source = await fixture.freeze(); result = await fixture.learn(source, 2)
        return source, tuple(cast(str, record(v)['object_id']) for v in sequence(record(result.receipt.result)['object_refs']))

    async def test_delete_shared_then_last_source_preserves_history_payload_and_archives_old_body(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory)).initialize()
            try:
                source, ids = await self.setup_objects(fixture)
                assert fixture.memory is not None
                port = fixture.memory.bind_read(ids, ('get_current', 'get_for_deep_read'))
                for ordinal, oid in enumerate(ids):
                    found = await port.get_current(oid); assert type(found) is Found
                    old = found.value
                    change = isolate_change({'change_version': 1, 'action': 'DELETE_OBJECT', 'target_id': oid, 'expected_revision': 1, 'proposed_value': None, 'links': None}, 8192)
                    deleted = await fixture.maintenance('delete:' + str(ordinal), change)
                    self.assertIs(type(await port.get_current(oid)), NotFound)
                    self.assertIs(type(await port.get_for_deep_read(oid)), NotFound)
                    refs = sequence(record(deleted.receipt.result)['history']); self.assertEqual(len(refs), 1)
                    audits = await fixture.storage.bind_audit_reader('instance').read(deleted.receipt.identity)
                    assert type(audits) is Found, audits
                    history_audit = next(item for item in audits.value if item.owner_module == 'logging_service')
                    self.assertEqual(record(history_audit.change)['history_ids'], refs)
                    history = fixture.assembly.history.bind_inspection((oid,))
                    inspected = await history.read_object_history(deleted.receipt.identity, refs[0], oid)
                    assert type(inspected) is Found, inspected
                    self.assertEqual(inspected.value['previous_value'], old)
                    sources = await fixture.assembly.memory.rows.read('sources_get', {'source_id': source['source_id']})
                    self.assertEqual(sources[0]['holder_count'], 1 - ordinal)
                sources = await fixture.assembly.memory.rows.read('sources_get', {'source_id': source['source_id']})
                self.assertEqual(sources[0]['state'], 'RELEASED')
                self.assertIsNone(sources[0]['body'])
                members = tuple(record(m) for m in sequence(source['ordered_members']))
                remaining = [bool(await fixture.assembly.ingress.rows.read('payload', {'message_id': m['message_id']})) for m in members]
                self.assertEqual(remaining, [False, True, True])
            finally:
                await fixture.close()

    async def test_score_hysteresis_keeps_forgotten_timestamp_and_history_is_not_agent_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory)).initialize()
            try:
                _, ids = await self.setup_objects(fixture); oid = ids[0]
                assert fixture.memory is not None
                port = fixture.memory.bind_read((oid,), ('get_current', 'get_for_deep_read'))
                for revision, retention in enumerate((19, 20, 34, 35), 2):
                    old_result = await port.get_for_deep_read(oid); assert type(old_result) is Found
                    raw = cast(dict, plain(old_result.value))
                    raw['revision'] = revision; raw['scores']['retention'] = retention
                    raw['lifecycle'] = 'ACTIVE' if retention == 35 else 'FORGOTTEN'
                    raw['forgotten_since_us'] = None if retention == 35 else 1000000
                    links_row = (await fixture.assembly.memory.rows.read('links_get', {'object_id': oid}))[0]
                    from companion_memory.persistence.content_codec import decode_content
                    links = cast(dict, decode_content(cast(str, links_row['body']).encode(), 2048))
                    for link in links['sources']: link['object_revision'] = revision
                    change = isolate_change({'change_version': 1, 'action': 'SET_SCORES', 'target_id': oid, 'expected_revision': revision - 1, 'proposed_value': raw, 'links': links}, 8192)
                    await fixture.maintenance('scores:' + str(revision), change)
                    current = await port.get_current(oid)
                    self.assertIs(type(current), Found if retention == 35 else NotFound)
                self.assertFalse(hasattr(port, 'read_object_history'))
            finally:
                await fixture.close()

    async def test_active_thresholds_low_belief_and_repeated_reads_do_not_change_retention(self):
        from companion_memory.persistence.content_codec import decode_content
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory)).initialize()
            try:
                _, ids = await self.setup_objects(fixture); oid = ids[0]
                assert fixture.memory is not None
                port = fixture.memory.bind_read((oid,), ('get_current', 'get_for_deep_read'))
                previous_state = 'ACTIVE'
                for revision, retention in enumerate((20, 34, 35, 19, 20, 34, 35), 2):
                    old = await port.get_for_deep_read(oid); assert type(old) is Found
                    raw = cast(dict, plain(old.value)); raw['revision'] = revision
                    raw['scores'].update(retention=retention, belief=0)
                    state = 'FORGOTTEN' if retention < 20 or previous_state == 'FORGOTTEN' and retention < 35 else 'ACTIVE'
                    raw['lifecycle'] = state; raw['forgotten_since_us'] = 1000000 if state == 'FORGOTTEN' else None
                    links = cast(dict, decode_content(cast(str, (await fixture.assembly.memory.rows.read('links_get', {'object_id': oid}))[0]['body']).encode(), 2048))
                    for link in links['sources']: link['object_revision'] = revision
                    change = isolate_change({'change_version': 1, 'action': 'SET_SCORES', 'target_id': oid, 'expected_revision': revision - 1,
                        'proposed_value': raw, 'links': links}, 8192)
                    result = await fixture.maintenance('boundary:' + str(revision), change)
                    repeated = await fixture.maintenance('boundary:' + str(revision), change)
                    self.assertEqual(repeated.receipt, result.receipt)
                    before = await port.get_for_deep_read(oid); assert type(before) is Found
                    ordinary = await port.get_current(oid)
                    self.assertIs(type(ordinary), Found if state == 'ACTIVE' else NotFound)
                    after = await port.get_for_deep_read(oid); assert type(after) is Found
                    self.assertEqual(after.value, before.value)
                    self.assertEqual(record(record(after.value)['scores'])['belief'], 0)
                    previous_state = state
            finally: await fixture.close()
