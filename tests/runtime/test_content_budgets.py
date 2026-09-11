"""Actual recommended material/request encodings and complete storage assembly."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from companion_memory.buffers.content_material import build_content_material
from companion_memory.provider import WorkGrant, CancellationSource, Completed
from companion_memory.provider.values import dump
from companion_memory.media.service import MediaService
from companion_memory.runtime.content_budget import check_content_assembly
from tests.media.test_complete_material import maximum_members
from tests.memory.support import Fixture


class ContentBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_window_crosses_actual_provider_and_complete_durable_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), MediaService()).initialize()
            try:
                cfg = fixture.candidate
                material = build_content_material(tuple('x' * 128 for _ in range(7)), maximum_members(), event_limit=2048,
                    interpretation_limit=2048, material_limit=49152)
                units = sum(len(message['text'].encode()) for message in material)
                self.assertLessEqual(units, 40180)
                ids = {key: key[0] * 128 for key in ('run', 'entry', 'batch', 'parent', 'trace', 'dream')}
                grant = WorkGrant('cognition', 's' * 128, None, 'LEARNING', ('sample_learning',), ('GENERATION',), 'cognition', 'scheduler',
                    run_ids=(ids['run'],), entry_ids=(ids['entry'],), batch_ids=(ids['batch'],), dream_run_ids=(ids['dream'],), parent_request_ids=(ids['parent'],), trace_ids=(ids['trace'],), prompt_revisions=('p' * 64,))
                port = fixture.provider.bind_work(grant)
                request = {'operation_key': 'k' * 128, 'run_id': ids['run'], 'entry_ids': [ids['entry']], 'batch_id': ids['batch'],
                    'parent_request_id': ids['parent'], 'trace_id': ids['trace'], 'dream_run_id': ids['dream'], 'prompt_revision': 'p' * 64,
                    'profile_id': 'sample_learning', 'deadline': time.monotonic() + 30, 'cancellation': CancellationSource().token,
                    'payload': {'messages': [dict(message) for message in material], 'input_units_limit': 49152, 'output_units_limit': 2048}}
                normalized, _, _ = fixture.provider._normalize(request, grant, 'generate', time.monotonic())
                encoded = dump(normalized, 65536).encode()
                self.assertLessEqual(len(encoded), 44290)
                result = await port.generate(request); assert type(result) is Completed, result
                self.assertEqual(result.record['outcome'], 'SUCCEEDED')
                self.assertEqual(len(fixture.adapter.calls), 1)
                self.assertEqual(json.loads(encoded)['payload']['messages'], [dict(message) for message in material])
                assert fixture.media is not None
                budget = check_content_assembly(fixture.config_assembly.repositories + fixture.assembly.repositories + fixture.ledger.repositories,
                    fixture.config_assembly.commands + fixture.assembly.commands + fixture.ledger.commands + fixture.media.commands)
                self.assertLessEqual(budget['command_descriptors'], 655360)
                self.assertLessEqual(budget['repository_descriptors'], 131072)
                self.assertLessEqual(budget['assembly'], 794624)
                print('CONTENT_BUDGET', json.dumps({'material': units, 'provider_request': len(encoded), **budget}, sort_keys=True))
            finally: await fixture.close()

    async def test_eight_complete_current_updates_persist_full_candidates_history_receipts_and_audits(self):
        from contextlib import closing
        import sqlite3
        from typing import cast
        from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
        from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
        from companion_memory.ingress.events import plain
        from companion_memory.memory.formats import MEMORY_SCHEMA, isolate_object, record, sequence
        from companion_memory.memory.changes import isolate_change
        from companion_memory.persistence import Committed, Found, Value, ResultBoundCommand, OperationIdentity
        from companion_memory.persistence.schema import freeze_value, encode_value
        from companion_memory.persistence._codec import prepare_command, command_descriptor, receipt_value
        from companion_memory.persistence.content_codec import encode_content, decode_content
        from tests.runtime.configuration_support import event
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = await Fixture(root, candidate_input=SyntheticCandidateInput('full_body:1', tuple(str(i) + 'x' * 2047 for i in range(8)), 50)).initialize()
            runtime = fixture.runtime; assert runtime is not None
            try:
                entry = runtime.bind_entry('entry')
                for i in range(3):
                    raw = event('event:' + str(i)); raw['event_version'] = 2
                    assert type(await entry.accept_event('accept:' + str(i), raw)) is Committed
                created = await entry.run_learning('create'); assert type(created) is Committed, created
                value = record(created.receipt.result)
                ids = tuple(cast(str, record(ref)['object_id']) for ref in sequence(value['object_refs']))
                reader = runtime.memory.bind_read(ids, ('get_current',))
                changes = []
                for oid in ids:
                    found = await reader.get_current(oid); assert type(found) is Found
                    proposed = cast(dict, plain(found.value)); proposed['revision'] = 2
                    proposed['scores']['belief_reason'] = 'x'
                    size = len(encode_content(freeze_value(MEMORY_SCHEMA, proposed), 4096))
                    missing = 4096 - size
                    proposed['scores']['belief_reason'] += '\x00' * (missing // 6) + 'x' * (missing % 6)
                    complete = isolate_object(proposed)
                    self.assertEqual(len(encode_content(complete, 4096)), 4096)
                    links = cast(dict, decode_content(cast(str, (await fixture.assembly.memory.rows.read('links_get', {'object_id': oid}))[0]['body']).encode(), 2048))
                    for link in links['sources']: link['object_revision'] = 2
                    changes.append(isolate_change({'change_version': 1, 'action': 'SET_SCORES', 'target_id': oid,
                        'expected_revision': 1, 'proposed_value': complete, 'links': links}, 8192))
                proposal = SyntheticMutationInput('full_updates:1', tuple(changes), source_ids=(cast(str, value['source_id']),))
            finally: await fixture.close()
            fixture = await Fixture(root, candidate_input=proposal).initialize('OPEN_EXISTING')
            runtime = fixture.runtime; assert runtime is not None
            measured = {}
            try:
                execute = runtime.execute
                async def measured_execute(kind, key, values):
                    definition = next(d for d in fixture.assembly.commands if d.operation_kind == kind)
                    command = ResultBoundCommand(1, {'operation_id': key, **values}, {a.event_slot: {'actor': 'content_scheduler'} for a in definition.required_audits})
                    _, owned, intents = prepare_command(definition, OperationIdentity(fixture.expected_id, definition.owner_namespace, kind, 'instance', key), command, 1048576)
                    encoded = encode_value(MappingProxyType({'definition': command_descriptor(definition), 'values': owned, 'intentions': intents}), 1048576)
                    measured[kind] = max(measured.get(kind, 0), len(encoded))
                    return await execute(kind, key, values)
                runtime.execute = measured_execute
                entry = runtime.bind_entry('entry')
                for i in range(3, 5):
                    raw = event('event:' + str(i)); raw['event_version'] = 2
                    assert type(await entry.accept_event('accept:' + str(i), raw)) is Committed
                result = await entry.run_learning('update'); assert type(result) is Committed, result
                value = record(result.receipt.result)
                self.assertEqual(len(sequence(value['object_refs'])), 8)
                self.assertEqual(len(sequence(value['history'])), 8)
                self.assertLessEqual(measured['store_content_candidate'], 483328)
                self.assertLessEqual(len(encode_value(result.receipt.result, 65536)), 8192)
                self.assertLessEqual(len(encode_value(receipt_value(result.receipt), 65536)), 16384)
                reader = runtime.memory.bind_read(ids, ('get_current',))
                for oid in ids:
                    current = await reader.get_current(oid); assert type(current) is Found
                    self.assertEqual(len(encode_content(current.value, 4096)), 4096)
                self.assertEqual(len(fixture.adapter.calls), 1)
                await fixture.close()
                with closing(sqlite3.connect(fixture.path)) as connection:
                    evidence_bytes = connection.execute('SELECT length(manifest) FROM required_audit_events WHERE commit_id=?', (result.receipt.commit_id,)).fetchone()[0]
                    audit_sizes = [row[0] for row in connection.execute('SELECT length(record) FROM audit_records WHERE commit_id=?', (result.receipt.commit_id,))]
                self.assertLessEqual(evidence_bytes, 32768)
                self.assertLessEqual(max(audit_sizes), 4096)
                self.assertLessEqual(sum(audit_sizes) + 8192, 40960)
                print('CONTENT_TRANSACTION_BUDGET', json.dumps({'commands': measured, 'result': len(encode_value(value, 65536)),
                    'receipt': len(encode_value(receipt_value(result.receipt), 65536)), 'evidence': evidence_bytes, 'audit_sizes': audit_sizes}, sort_keys=True))
            finally: await fixture.close()

    async def test_maximum_configuration_persists_and_reopens_with_all_real_owners(self):
        from companion_memory.configuration.content_codec import candidate_values
        from companion_memory.configuration.content_resolution import resolve_content_configuration, ContentConfigurationOk
        from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
        from companion_memory.persistence import ResultBoundCommand, OperationIdentity
        from companion_memory.persistence._codec import prepare_command, command_descriptor
        from companion_memory.persistence.schema import encode_value
        from tests.configuration.content_support import maximum_inputs
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supplied, _ = maximum_inputs(root)
            resolved = resolve_content_configuration(*supplied); assert type(resolved) is ContentConfigurationOk, resolved
            original = None
            for mode in ('CREATE_NEW', 'OPEN_EXISTING'):
                fixture = Fixture(root, MediaService(), SyntheticCandidateInput('maximum_config:1', (), 50))
                fixture.candidate, fixture.supplied = resolved.value, supplied
                try:
                    await fixture.initialize(mode)
                    runtime = fixture.runtime; assert runtime is not None
                    self.assertEqual(runtime.state, 'READY')
                    definition = fixture.config_assembly.commands[0]
                    values = candidate_values(resolved.value)
                    command = ResultBoundCommand(1, values, {'configuration_initialized': {'actor': 'bootstrap'}})
                    handle, owned, intentions = prepare_command(definition,
                        OperationIdentity(fixture.expected_id, 'configuration', 'initialize_content', 'instance', 'content-config'), command, 1048576)
                    encoded = encode_value(MappingProxyType({'definition': command_descriptor(definition), 'values': owned, 'intentions': intentions}), 1048576)
                    self.assertLessEqual(len(encoded), 933888)
                    self.assertEqual(candidate_values(fixture.assembly.configuration.candidate), values)
                    self.assertEqual(len(fixture.adapter.calls), 0)
                    identity = (handle.fingerprint, fixture.assembly.configuration.snapshot_id, fixture.assembly.configuration.revisions)
                    if original is not None: self.assertEqual(identity, original)
                    original = identity
                    print('CONTENT_CONFIGURATION_BUDGET', mode, len(encoded))
                finally: await fixture.close()
