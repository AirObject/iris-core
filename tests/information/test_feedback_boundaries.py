"""Mixed-use atomicity, exact ticket clocks and real forgetting hysteresis.

UTC observations are explicitly simulated; SQLite commits, memory owners,
receipts and audits are actual. Expired tickets still occupy physical slots.
"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sqlite3
import time
import unittest
import asyncio
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.information.errors import InformationRejected, InformationNotCommitted
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import Record, record, text, integer
from companion_memory.persistence import Found, Committed
from tests.information.host_support import host, learn_one, learn_objects
from tests.information.test_expiry import scores
from tests.information.test_queries import query


def payload(result: object, key: str) -> dict[str, object]:
    if type(result) is not Found: raise RuntimeError('Expected actual ticket response: ' + repr(result))
    view = record(result.value); memories = record(view['sections'])['memories']
    if type(memories) is not tuple: raise RuntimeError('Expected finite current objects.')
    return {'operation_key': key, 'recall_id': view['recall_id'], 'used_at': None,
        'used_members': tuple({'object_id': item['object_id'], 'returned_revision': item['revision']}
            for item in sorted((record(raw) for raw in memories), key=lambda item: text(item['object_id'])))}


class FeedbackBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def rejected_reason(self, result: object, reason: str) -> None:
        if type(result) is not InformationRejected and type(result) is not InformationNotCommitted:
            self.fail('Expected explicit rejected effect: ' + repr(result))
        self.assertEqual(result.error.reason, reason)

    async def test_concurrent_first_use_commits_only_one_effect_and_loser_can_confirm(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                port = await h.bind_business(HostIdentity('usage', 'principal', 'host', 'entry', frozenset(('search_memory', 'record_usage')), (), time.monotonic() + 300))
                request = payload(await port.search_memory(query('concurrent')), 'first')
                results = await asyncio.gather(port.record_usage(request), port.record_usage(request | {'operation_key': 'second'}))
                self.assertEqual(sum(type(result) is Committed for result in results), 1, results)
                for result in results:
                    self.assertIn(type(result), (Committed, Found, InformationNotCommitted, InformationRejected))
                for key in ('first', 'second'):
                    confirmed = await port.record_usage(request | {'operation_key': key})
                    self.assertIn(type(confirmed), (Committed, Found), confirmed)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute("SELECT revision,json_extract(body,'$.scores.retention') FROM memory_objects").fetchall(), [(2, 58)])
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], 1)
                    self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_consumption').fetchone()[0], 1)
                    audits = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                self.assertIs(type(await port.record_usage(request | {'operation_key': 'third'})), Found)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], audits)
            finally: self.assertTrue(await h.close())

    async def test_mixed_already_used_and_unused_are_atomic_and_do_not_repeat_audits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            h.candidates = SyntheticCandidateInput('two_objects', ('北京看展', '北京公园看展'), 50)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_objects(h)
                port = await h.bind_business(HostIdentity('usage', 'principal', 'host', 'entry', frozenset(('search_memory', 'record_usage')), (), time.monotonic() + 300))
                both = payload(await port.search_memory(query('recall')), 'mixed')
                members = both['used_members']
                if type(members) is not tuple or len(members) != 2: self.fail('Expected two real returned members.')
                first = await port.record_usage(both | {'operation_key': 'first', 'used_members': members[:1]})
                self.assertIs(type(first), Committed, first)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    before = c.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                    scores_before = c.execute("SELECT object_id,revision,json_extract(body,'$.scores.retention') FROM memory_objects ORDER BY object_id").fetchall()
                invalid = (members[0], dict(members[1]) | {'returned_revision': 999})
                self.rejected_reason(await port.record_usage(both | {'operation_key': 'invalid', 'used_members': invalid}), 'REVISION_CONFLICT')
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM audit_records').fetchone()[0], before)
                    self.assertEqual(c.execute("SELECT object_id,revision,json_extract(body,'$.scores.retention') FROM memory_objects ORDER BY object_id").fetchall(), scores_before)
                mixed = await port.record_usage(both)
                self.assertIs(type(mixed), Committed, mixed)
                if type(mixed) is Committed:
                    result = record(mixed.receipt.result); items = result['items']; targets = result['targets']
                    if type(items) is not tuple or type(targets) is not tuple: self.fail('Expected bound item/target results.')
                    self.assertEqual(tuple(record(item)['status'] for item in items), ('ALREADY_APPLIED', 'APPLIED'))
                    self.assertEqual(len(targets), 1); self.assertEqual(record(targets[0])['object_id'], members[1]['object_id'])
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    before = c.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], 2)
                confirmed = await port.record_usage(both | {'operation_key': 'all-used'})
                self.assertIs(type(confirmed), Found, confirmed)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM audit_records').fetchone()[0], before)
                    self.assertEqual(c.execute("SELECT revision,json_extract(body,'$.scores.retention') FROM memory_objects").fetchall(), [(2, 58), (2, 58)])
            finally: self.assertTrue(await h.close())

    async def test_exact_expiry_clock_rollback_and_original_confirmation_after_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root); instant = int(time.time()) + 2
            binding = HostIdentity('usage', 'principal', 'host', 'entry', frozenset(('search_memory', 'record_usage')), (), time.monotonic() + 300)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h); port = await h.bind_business(binding)
                with patch('time.time', return_value=instant):
                    first = payload(await port.search_memory(query('first')), 'first-use')
                    second = payload(await port.search_memory(query('second')), 'second-use')
                with patch('time.time', return_value=instant - 0.001):
                    self.rejected_reason(await port.record_usage(first), 'CLOCK_UNCERTAIN')
                with patch('time.time', return_value=instant + 86400 - 0.000001):
                    used = await port.record_usage(first)
                    self.assertIs(type(used), Committed, used)
                with patch('time.time', return_value=instant + 86400):
                    # Expiry precedes a stale object revision in the error order.
                    self.rejected_reason(await port.record_usage(second), 'TICKET_EXPIRED')
                    self.assertEqual((await h.management.tickets.occupancy((instant + 86400) * 1000000))['occupied'], 2)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                port = await h.bind_business(binding)
                with patch('time.time', return_value=instant - 10):
                    confirmed = await port.resolve_usage(first)
                    self.assertIs(type(confirmed), Committed, confirmed)
                    if type(used) is Committed and type(confirmed) is Committed: self.assertEqual(used.receipt, confirmed.receipt)
                    self.rejected_reason(await port.record_usage(second), 'CLOCK_UNCERTAIN')
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_forgotten_restore_threshold_and_saturation_keep_belief_and_single_use(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory)); instant = int(time.time()) + 2
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                self.assertIs(type(await scores(h, oid, 19)), Committed)
                port = await h.bind_business(HostIdentity('usage', 'principal', 'host', 'entry', frozenset(('deep_recall', 'record_usage')), (), time.monotonic() + 300), include_forgotten=True)
                memory = h.assembly.memory.information
                if memory is None: self.fail('Expected actual memory owner.')
                current = (await memory.current_page())[0]; since = current['forgotten_since_us']; belief = record(current['scores'])['belief']
                with patch('time.time', return_value=instant):
                    old = payload(await port.deep_recall(query('forgotten')), 'forgotten-use')
                    self.assertIs(type(await port.record_usage(old)), Committed)
                    current = (await memory.current_page())[0]
                    self.assertEqual((record(current['scores'])['retention'], current['lifecycle'], current['forgotten_since_us']), (27, 'FORGOTTEN', since))
                    self.assertIs(type(await port.record_usage(old | {'operation_key': 'repeat'})), Found)
                    fresh = payload(await port.deep_recall(query('fresh')), 'restore')
                    restored = await port.record_usage(fresh)
                    self.assertIs(type(restored), Committed, restored)
                    current = (await memory.current_page())[0]
                    self.assertEqual((record(current['scores'])['retention'], current['lifecycle'], current['forgotten_since_us']), (35, 'ACTIVE', None))
                    self.assertEqual(record(current['scores'])['belief'], belief)
                    self.assertIs(type(await scores(h, oid, 100)), Committed)
                    before_revision = integer((await memory.current_page())[0]['revision'])
                    saturated = payload(await port.deep_recall(query('saturated')), 'saturated-use')
                    consumed = await port.record_usage(saturated)
                    self.assertIs(type(consumed), Committed, consumed)
                    if type(consumed) is Committed:
                        self.assertEqual(set(record(record(consumed.receipt.result)['facts'])), {'memory', 'retrieval'})
                    current = (await memory.current_page())[0]
                    self.assertEqual(current['revision'], before_revision)
                    self.assertEqual(record(current['scores'])['retention'], 100)
                    self.assertEqual(record(current['scores'])['belief'], belief)
            finally: self.assertTrue(await h.close())
