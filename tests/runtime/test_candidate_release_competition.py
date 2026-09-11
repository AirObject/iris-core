"""Persisted batch mutations recheck shared ownership and original object revisions."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
from companion_memory.ingress.events import plain
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, Found, NotFound, Value
from companion_memory.persistence.content_codec import decode_content
from companion_memory.runtime.results import NotCommitted
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


class CandidateReleaseCompetitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_holders_and_object_revision_preserve_whole_candidate(self):
        for competition in ('ACQUIRE', 'RELEASE', 'REVISE'):
            with self.subTest(competition=competition), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = await Fixture(root, candidate_input=SyntheticCandidateInput('create:1', ('A', 'B'), 50)).initialize()
                runtime = fixture.runtime; assert runtime is not None
                async def accept(entry, index):
                    raw = event('event:' + str(index)); raw['event_version'] = 2
                    result = await entry.accept_event('input:' + str(index), raw)
                    assert type(result) is Committed, result
                try:
                    entry = runtime.bind_entry('entry')
                    for i in range(3): await accept(entry, i)
                    created = await entry.run_learning('create'); assert type(created) is Committed, created
                    value = record(created.receipt.result)
                    a, b = (cast(str, record(ref)['object_id']) for ref in sequence(value['object_refs']))
                    source = cast(str, value['source_id'])
                    if competition == 'ACQUIRE':
                        for i in range(3, 5): await accept(entry, i)
                        second = await entry.run_learning('second'); assert type(second) is Committed, second
                        old_b = b
                        b = cast(str, record(sequence(record(second.receipt.result)['object_refs'])[0])['object_id'])
                        deleted = await runtime.maintenance.bind((old_b,)).delete_object('old-b', old_b, 1)
                        assert type(deleted) is Committed, deleted
                finally: await fixture.close()
                change = isolate_change({'change_version': 1, 'action': 'DELETE_OBJECT', 'target_id': a,
                    'expected_revision': 1, 'proposed_value': None, 'links': None}, 8192)
                mutation = SyntheticMutationInput('delete:1', (change,), source_ids=(source,))
                fixture = await Fixture(root, candidate_input=mutation).initialize('OPEN_EXISTING')
                runtime = fixture.runtime; assert runtime is not None
                reached = asyncio.Event(); release = asyncio.Event(); pending = None
                try:
                    entry = runtime.bind_entry('entry')
                    for i in range(5, 7): await accept(entry, i)
                    reader = runtime.memory.bind_read((a, b), ('get_current',))
                    execute = runtime.execute
                    async def barrier(kind, key, values):
                        if kind.startswith('apply_candidate_changes') and not reached.is_set():
                            reached.set(); await release.wait()
                        return await execute(kind, key, values)
                    runtime.execute = barrier
                    pending = asyncio.create_task(entry.run_learning('delete'))
                    await asyncio.wait_for(reached.wait(), 5)
                    if competition == 'RELEASE':
                        changed = await runtime.maintenance.bind((b,)).delete_object('release-b', b, 1)
                    else:
                        target = a if competition == 'REVISE' else b
                        current = await reader.get_current(target); assert type(current) is Found
                        proposed = cast(dict, plain(current.value)); proposed['revision'] = 2
                        proposed['scores']['belief_reason'] = 'Explicit concurrent revision'
                        links = cast(dict, decode_content(cast(str, (await fixture.assembly.memory.rows.read('links_get', {'object_id': a}))[0]['body']).encode(), 2048))
                        for link in links['sources']: link.update(object_id=target, object_revision=2)
                        changed = await runtime.maintenance.bind((target,), (source,)).replace_current('concurrent', {
                            'change_version': 1, 'action': 'REPLACE_CURRENT', 'target_id': target, 'expected_revision': 1,
                            'proposed_value': proposed, 'links': links})
                    assert type(changed) is Committed, changed
                    release.set()
                    failed = await asyncio.wait_for(pending, 5)
                    assert type(failed) is NotCommitted and failed.error is not None, failed
                    self.assertEqual(failed.error.reason, 'REVISION_CONFLICT' if competition == 'REVISE' else 'OWNERSHIP_CHANGED')
                    current = await reader.get_current(a); assert type(current) is Found, current
                    self.assertEqual(record(current.value)['revision'], 2 if competition == 'REVISE' else 1)
                    calls = len(fixture.adapter.calls)
                    self.assertEqual(calls, 1)
                    repeated = await entry.run_learning('delete')
                    if competition == 'REVISE':
                        assert type(repeated) is NotCommitted and repeated.error is not None, repeated
                        self.assertEqual(repeated.error.reason, 'REVISION_CONFLICT')
                    else:
                        assert type(repeated) is Committed, repeated
                        self.assertIs(type(await reader.get_current(a)), NotFound)
                        confirmed = await entry.run_learning('delete'); assert type(confirmed) is Committed
                        self.assertEqual(confirmed.receipt, repeated.receipt)
                    self.assertEqual(len(fixture.adapter.calls), calls)
                finally:
                    release.set()
                    if pending: await pending
                    await fixture.close()
                if competition == 'REVISE':
                    fixture = await Fixture(root, candidate_input=mutation).initialize('OPEN_EXISTING')
                    try:
                        runtime = fixture.runtime; assert runtime is not None
                        self.assertEqual(runtime.state, 'READY')
                        observed = await runtime.observations.bind(('entry',)).read_batch_status({})
                        from companion_memory.runtime.results import Found as Observed
                        assert type(observed) is Observed, observed
                        status = record(record(sequence(record(cast(Value, observed.value))['rows'])[0])['work'])
                        self.assertEqual((status['state'], status['reason'], status['local_commit']),
                            ('SYSTEM_BLOCKED', 'REVISION_CONFLICT', 'NOT_COMMITTED'))
                        blocked = await runtime.bind_entry('entry').run_learning('delete')
                        assert type(blocked) is NotCommitted and blocked.error is not None, blocked
                        self.assertEqual(blocked.error.reason, 'REVISION_CONFLICT')
                        self.assertEqual(len(fixture.adapter.calls), 0)
                        self.assertEqual((await fixture.assembly.rows.read('observe_entry', {'entry_id': 'entry'}))[0]['active_batches'], 1)
                    finally: await fixture.close()
