"""Public maintenance races recheck complete original release plans in both orders."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.ingress.events import plain
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, Found, NotFound
from companion_memory.persistence.content_codec import decode_content
from companion_memory.runtime.content_assembly import stable
from companion_memory.runtime.results import NotCommitted
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


class ReleaseCompetitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_shared_ownership_rejects_whole_old_plan_and_replans_only_on_next_call(self):
        for add_holder in (False, True):
            with self.subTest(add_holder=add_holder), tempfile.TemporaryDirectory() as directory:
                bodies = ('Object A.',) if add_holder else ('Object A.', 'Object B.')
                fixture = await Fixture(Path(directory), candidate_input=SyntheticCandidateInput('explicit:1', bodies, 50), runtime_changes={'runtime.max_active_entries': 2}, foundation_changes={'provider.max_in_flight': 3}).initialize()
                runtime = fixture.runtime; assert runtime is not None
                try:
                    entry = runtime.bind_entry('entry')
                    async def accept(index):
                        value = event('event:' + str(index)); value['event_version'] = 2
                        assert type(await entry.accept_event('input:' + str(index), value)) is Committed
                    for i in range(3): await accept(i)
                    first = await entry.run_learning('first'); assert type(first) is Committed
                    value = record(first.receipt.result)
                    a = cast(str, record(sequence(value['object_refs'])[0])['object_id']); source = cast(str, value['source_id'])
                    if add_holder:
                        for i in range(3, 5): await accept(i)
                        second = await entry.run_learning('second'); assert type(second) is Committed
                        b = cast(str, record(sequence(record(second.receipt.result)['object_refs'])[0])['object_id'])
                    else: b = cast(str, record(sequence(value['object_refs'])[1])['object_id'])
                    a_port = runtime.maintenance.bind((a,)); b_port = runtime.maintenance.bind((b,), (source,) if add_holder else ())
                    read = runtime.memory.bind_read((a, b), ('get_current',))
                    reached = asyncio.Event(); release = asyncio.Event()
                    execute = runtime.execute
                    blocked = False
                    async def barrier(kind, key, values):
                        nonlocal blocked
                        if kind.startswith('apply_memory_') and not blocked:
                            blocked = True; reached.set(); await release.wait()
                        return await execute(kind, key, values)
                    runtime.execute = barrier
                    pending = asyncio.create_task(a_port.delete_object('delete-a', a, 1))
                    try:
                        await asyncio.wait_for(reached.wait(), 5)
                        if add_holder:
                            current = await read.get_current(b); assert type(current) is Found
                            proposed = cast(dict, plain(current.value)); proposed['revision'] = 2
                            links = cast(dict, decode_content(cast(str, (await fixture.assembly.memory.rows.read('links_get', {'object_id': a}))[0]['body']).encode(), 2048))
                            for link in links['sources']: link.update(object_id=b, object_revision=2)
                            changed = await b_port.replace_current('attach-source', {'change_version': 1, 'action': 'REPLACE_CURRENT', 'target_id': b,
                                'expected_revision': 1, 'proposed_value': proposed, 'links': links})
                        else: changed = await b_port.delete_object('delete-b', b, 1)
                        assert type(changed) is Committed, changed
                    finally: release.set()
                    failed = await asyncio.wait_for(pending, 5)
                    assert type(failed) is NotCommitted and failed.error is not None, failed
                    self.assertEqual(failed.error.reason, 'OWNERSHIP_CHANGED')
                    original = await read.get_current(a); assert type(original) is Found
                    self.assertEqual(record(original.value)['revision'], 1)
                    root = stable('memory_root', fixture.expected_id, a, 'delete-a')
                    old = (await fixture.assembly.memory.rows.read('plan_for_root', {'root_id': root, 'ordinal': 1}))[0]
                    receipt = await fixture.assembly.operations[cast(str, old['command_kind'])].read_receipt(old['execution_key'])
                    self.assertIs(type(receipt), NotFound)
                    retried = await a_port.delete_object('delete-a', a, 1); assert type(retried) is Committed, retried
                    self.assertIs(type(await read.get_current(a)), NotFound)
                    final = (await fixture.assembly.memory.rows.read('plan_for_root', {'root_id': root, 'ordinal': 2}))[0]
                    self.assertEqual(final['command_kind'], 'apply_memory_none' if add_holder else 'apply_memory_ingress')
                    root_row = (await fixture.assembly.memory.rows.read('release_roots_get', {'root_id': root}))[0]
                    self.assertEqual(root_row['successful_plan'], final['plan_id'])
                    again = await a_port.delete_object('delete-a', a, 1); assert type(again) is Committed
                    self.assertEqual(again.receipt, retried.receipt)
                    self.assertEqual(len(fixture.adapter.calls), 2 if add_holder else 1)
                finally: await fixture.close()
