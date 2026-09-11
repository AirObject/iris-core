"""Sixteen real sources retire in one eight-object candidate with every owner."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
from companion_memory.ingress.events import plain, event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.formats import record, sequence, MEMORY_SCHEMA, isolate_object
from companion_memory.persistence import Committed, Found, ResultBoundCommand, OperationIdentity
from companion_memory.persistence.schema import freeze_value, encode_value
from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.persistence._codec import receipt_value, prepare_command, command_descriptor
from companion_memory.provider import SimulationAdapter
from tests.memory.support import Fixture
from tests.provider.support import success
from tests.runtime.configuration_support import event


class ReleaseCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixteen_sources_and_shared_media_retire_atomically_at_maximum_object_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Fixture(root, MediaService(), SyntheticCandidateInput('sources:1', ('x' * 2048,), 50))
            fixture.adapter = SimulationAdapter(tuple(success() for _ in range(16)))
            await fixture.initialize()
            runtime = fixture.runtime; assert runtime is not None and fixture.media is not None
            objects = []; sources = []; next_event = 0
            try:
                entry = runtime.bind_entry('entry'); upload = fixture.media.bind_upload('entry')
                for batch in range(16):
                    for _ in range(3 if batch == 0 else 2):
                        ordinal = next_event; next_event += 1
                        begun = await upload.begin_upload('upload:' + str(ordinal), 'IMAGE'); assert type(begun) is Committed
                        uid = record(begun.receipt.result)['upload_id']
                        await upload.append_upload(uid, 0, b'exact bytes shared by all occurrences')
                        assert type(await upload.finish_upload(uid)) is Committed
                        raw = event('event:' + str(ordinal)); raw['event_version'] = 2
                        mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
                        raw['media'] = [{'reference_id': uid, 'occurrence_id': identity('occurrence', mid, position), 'modality': 'IMAGE',
                            'interpretation': {'status': 'COMPLETE', 'text': 'external description', 'coverage': 'COMPLETE', 'source_ref': 'external:' + str(ordinal)}} for position in range(2)]
                        accepted = await entry.accept_event('accept:' + str(ordinal), raw); assert type(accepted) is Committed, accepted
                    learned = await entry.run_learning('learn:' + str(batch)); assert type(learned) is Committed, learned
                    value = record(learned.receipt.result)
                    objects.append(cast(str, record(sequence(value['object_refs'])[0])['object_id'])); sources.append(cast(str, value['source_id']))
                reader = runtime.memory.bind_read(tuple(objects), ('get_current',))
                for index in range(8):
                    target, donor = objects[index], objects[index + 8]
                    current = await reader.get_current(target); assert type(current) is Found
                    proposed = cast(dict, plain(current.value)); proposed['revision'] = 2
                    proposed['scores']['belief_reason'] = 'x'
                    missing = 4096 - len(encode_content(freeze_value(MEMORY_SCHEMA, proposed), 4096))
                    proposed['scores']['belief_reason'] += '\x00' * (missing // 6) + 'x' * (missing % 6)
                    complete = isolate_object(proposed)
                    self.assertEqual(len(encode_content(complete, 4096)), 4096)
                    links = cast(dict, decode_content(cast(str, (await fixture.assembly.memory.rows.read('links_get', {'object_id': target}))[0]['body']).encode(), 2048))
                    donor_links = cast(dict, decode_content(cast(str, (await fixture.assembly.memory.rows.read('links_get', {'object_id': donor}))[0]['body']).encode(), 2048))
                    links['sources'] += donor_links['sources']
                    for link in links['sources']: link.update(object_id=target, object_revision=2)
                    maintenance = runtime.maintenance.bind((target, donor), (sources[index + 8],))
                    updated = await maintenance.replace_current('attach', {'change_version': 1, 'action': 'REPLACE_CURRENT', 'target_id': target,
                        'expected_revision': 1, 'proposed_value': complete, 'links': links})
                    assert type(updated) is Committed, updated
                    deleted = await maintenance.delete_object('remove_donor', donor, 1); assert type(deleted) is Committed, deleted
                changes = tuple(isolate_change({'change_version': 1, 'action': 'DELETE_OBJECT', 'target_id': oid, 'expected_revision': 2,
                    'proposed_value': None, 'links': None}, 8192) for oid in objects[:8])
                proposal = SyntheticMutationInput('retire_all:1', changes, source_ids=tuple(sources))
            finally: await fixture.close()
            fixture = await Fixture(root, MediaService(), proposal).initialize('OPEN_EXISTING')
            runtime = fixture.runtime; assert runtime is not None
            command_sizes = {}
            execute = runtime.execute
            async def measured_execute(kind, key, values):
                definition = next(d for d in fixture.assembly.commands if d.operation_kind == kind)
                command = ResultBoundCommand(1, {'operation_id': key, **values}, {a.event_slot: {'actor': 'content_scheduler'} for a in definition.required_audits})
                _, owned, intents = prepare_command(definition, OperationIdentity(fixture.expected_id, definition.owner_namespace, kind, 'instance', key), command, 1048576)
                encoded = encode_value(MappingProxyType({'definition': command_descriptor(definition), 'values': owned, 'intentions': intents}), 1048576)
                command_sizes[kind] = max(command_sizes.get(kind, 0), len(encoded))
                return await execute(kind, key, values)
            runtime.execute = measured_execute
            try:
                entry = runtime.bind_entry('entry')
                for i in range(next_event, next_event + 2):
                    raw = event('event:' + str(i)); raw['event_version'] = 2
                    assert type(await entry.accept_event('accept:' + str(i), raw)) is Committed
                deleted = await entry.run_learning('retire'); assert type(deleted) is Committed, deleted
                result = record(deleted.receipt.result)
                self.assertLessEqual(command_sizes['plan_candidate_application'], 876544)
                self.assertLessEqual(command_sizes['apply_candidate_changes_with_media'], 876544)
                self.assertEqual(set(cast(tuple[str, ...], result['retired_source_ids'])), set(sources))
                self.assertEqual(len(sequence(result['history'])), 8)
                self.assertEqual(len(sequence(result['object_refs'])), 8)
                self.assertLessEqual(len(encode_value(result, 65536)), 8192)
                self.assertLessEqual(len(encode_value(receipt_value(deleted.receipt), 65536)), 16384)
                for source in sources:
                    row = (await fixture.assembly.memory.rows.read('sources_get', {'source_id': source}))[0]
                    self.assertEqual(row['holder_count'], 0); self.assertIsNone(row['body'])
                repeated = await entry.run_learning('retire'); assert type(repeated) is Committed
                self.assertEqual(repeated.receipt, deleted.receipt)
                self.assertEqual(len(fixture.adapter.calls), 1)
                await fixture.close()
                with closing(sqlite3.connect(fixture.path)) as connection:
                    leaf_sizes = [row[0] for row in connection.execute('SELECT length(body) FROM memory_release_leaves WHERE plan_id=(SELECT successful_plan FROM memory_release_roots WHERE root_id IN (SELECT root_id FROM memory_batch_intents))')]
                    self.assertEqual(len(leaf_sizes), 16)
                    self.assertLessEqual(max(leaf_sizes), 8192)
                    self.assertEqual(connection.execute("SELECT count(*) FROM media_references WHERE owner_kind='SOURCE'").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM media_interpretation_holders WHERE owner_kind='SOURCE'").fetchone()[0], 0)
                    for table, column in (('memory_sources', 'body'), ('runtime_content_preparations', 'manifest'), ('runtime_content_batches', 'manifest')):
                        maximum = connection.execute('SELECT max(length(CAST(' + column + ' AS BLOB))) FROM ' + table).fetchone()[0]
                        self.assertLessEqual(maximum or 0, 4096)
                    for table in ('memory_source_members', 'runtime_content_members'):
                        maximum = connection.execute('SELECT max(length(CAST(body AS BLOB))) FROM ' + table).fetchone()[0]
                        self.assertLessEqual(maximum or 0, 2048)
                    evidence_size = connection.execute('SELECT length(manifest) FROM required_audit_events WHERE commit_id=?', (deleted.receipt.commit_id,)).fetchone()[0]
                    audit_sizes = [row[0] for row in connection.execute('SELECT length(record) FROM audit_records WHERE commit_id=?', (deleted.receipt.commit_id,))]
                    self.assertLessEqual(evidence_size, 32768); self.assertLessEqual(max(audit_sizes), 4096)
                    self.assertLessEqual(sum(audit_sizes) + 8192, 40960)
                    self.assertEqual(len(audit_sizes), 7)
                print('CONTENT_RELEASE_BUDGET', json.dumps({'result': len(encode_value(result, 65536)), 'commands': command_sizes, 'leaf_sizes': leaf_sizes,
                    'audit_sizes': audit_sizes, 'evidence': evidence_size}, sort_keys=True))
            finally: await fixture.close()
