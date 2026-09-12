"""Actual host authority, finite query admission and independent activity clocks.

The query scheduling barrier is simulated; loopback requests, native state and
feedback commands, SQLite effects and recovery all use actual isolated owners.
"""
import asyncio
from dataclasses import replace
import threading
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.information.errors import InformationNotCommitted, InformationRejected
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import Record, record, text, integer
from companion_memory.retrieval.query_service import QueryPort
from companion_memory.retrieval.lexical import StructuralFilter
from tests.information.host_support import host, learn_one, ComponentHost
from tests.runtime.configuration_support import event
from tests.runtime.test_content_focus import ExplicitPublication
from tests.provider.support import success
from companion_memory.provider import SimulationAdapter
from tests.information.test_http import exchange
from tests.information.test_queries import query


class HostBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_host_cannot_consume_a_valid_ticket_or_change_its_audits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                self.assertIs(type(await h.register_entry('foreign', 'foreign_entry', 'foreign_host', 'sample_platform', 'foreign')), Committed)
                owner = await h.bind_business(HostIdentity('owner', 'principal', 'host', 'entry', frozenset(('search_memory', 'record_usage')), (), time.monotonic() + 300))
                recalled = await owner.search_memory(query('original'))
                if type(recalled) is not Found: self.fail('Expected committed actual recall.')
                recall_id = record(recalled.value)['recall_id']
                payload = {'operation_key': 'reported_use', 'recall_id': recall_id, 'used_members': ({'object_id': oid, 'returned_revision': 1},), 'used_at': None}
                if h.http is None: self.fail('Expected actual HTTP owner.')
                address = await h.http.start()
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    before = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                for principal in ('principal', 'another_principal'):
                    foreign = await h.bind_business(HostIdentity('foreign:' + principal, principal, 'foreign_host', 'foreign_entry', frozenset(('record_usage',)), (), time.monotonic() + 300))
                    token = h.http.issue_test_session(foreign, time.monotonic() + 300)
                    status, body = await exchange(address, token, '/api/host/usage', payload)
                    self.assertEqual(status, 403, body)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], before)
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_consumption').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT revision FROM memory_objects WHERE object_id=?', (oid,)).fetchone()[0], 1)
                self.assertIs(type(await owner.record_usage(payload)), Committed)
            finally: self.assertTrue(await h.close())

    async def test_two_actual_http_queries_hold_admission_until_their_work_finishes(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory)); entered = asyncio.Event(); release = asyncio.Event(); count = 0
            pending: list[asyncio.Task[tuple[int, dict[str, object]]]] = []
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                port = await h.bind_business(HostIdentity('query', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300))
                if h.queries is None or h.http is None: self.fail('Expected actual query and HTTP owners.')
                original = h.queries._candidates
                async def held(port: QueryPort, query: Record, selected: StructuralFilter, deep: bool) -> tuple[tuple[Record, ...], Record, tuple[str, ...]]:
                    nonlocal count
                    count += 1
                    if count == 2: entered.set()
                    await release.wait()
                    return await original(port, query, selected, deep)
                h.queries._candidates = held
                address = await h.http.start(); token = h.http.issue_test_session(port, time.monotonic() + 300)
                for ordinal in range(2):
                    pending.append(asyncio.create_task(exchange(address, token, '/api/host/memory/search', query('query:' + str(ordinal)))))
                await asyncio.wait_for(entered.wait(), 0.5)
                self.assertEqual(len(h.queries.jobs), 2)
                status, body = await exchange(address, token, '/api/host/memory/search', query('third'))
                self.assertEqual(status, 429, body); self.assertEqual(count, 2)
                release.set()
                replies = await asyncio.gather(*pending)
                # Competing ticket writes may be rejected by the sole management
                # admission; neither failure is counted as successful throughput.
                self.assertTrue(all(status in (200, 429) for status, _ in replies), replies)
                self.assertTrue(any(status == 200 for status, _ in replies), replies)
                async with asyncio.timeout(1):
                    while h.queries.jobs: await asyncio.sleep(0.001)
                self.assertEqual((await exchange(address, token, '/api/host/memory/search', query('after')))[0], 200)
            finally:
                release.set()
                if pending: await asyncio.gather(*pending, return_exceptions=True)
                self.assertTrue(await h.close())

    async def test_state_heartbeats_fields_replacement_future_and_staleness_preserve_time_facts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('entry', 'entry', 'host', 'sample_platform', 'entry')
                kinds = frozenset(('state_set', 'state_update', 'state_end'))
                port = await h.bind_management(HostIdentity('state', 'principal', 'host', 'entry', kinds, (), time.monotonic() + 300))
                now = time.time_ns() // 1000
                created = await port.execute('state_set', 'start', {'activity_id': None, 'expected_revision': None, 'replace_activity': False,
                    'patch': {'activity_value': '阅读', 'started_at': None, 'reported_at': now, 'reported_offset_minutes': 480,
                        'fields': {'progress': {'value': '第一章'}, 'emotion': {'value': '平静', 'started_at': now - 1000000}}}})
                if type(created) is not Committed or h.current_state is None: self.fail('Expected actual state creation.')
                oid = record(record(record(created.receipt.result)['facts'])['state'])['object_id']
                async def view(at: int) -> Record:
                    if h.current_state is None: self.fail('Expected state owner.')
                    value = await h.current_state.view(at)
                    if value is None: self.fail('Expected explicit state view.')
                    return value
                initial = await view(now + 2000000)
                self.assertEqual((initial['duration_basis'], initial['duration_us']), ('FIRST_REPORT', 2000000))
                self.assertIs(type(await port.execute('state_update', 'heartbeat', {'activity_id': oid, 'expected_revision': 1,
                    'patch': {'reported_at': now + 1000000, 'reported_offset_minutes': 480, 'fields': {'progress': {'value': '第一章'}}}})), Committed)
                heartbeat = record((await view(now + 2000000))['activity']); fields = record(heartbeat['fields'])
                self.assertEqual(heartbeat['first_reported_at'], now); self.assertIsNone(heartbeat['started_at'])
                self.assertEqual((record(fields['progress'])['first_reported_at'], record(fields['progress'])['updated_at']), (now, now + 1000000))
                self.assertIs(type(await port.execute('state_update', 'progress', {'activity_id': oid, 'expected_revision': 2,
                    'patch': {'reported_at': now + 2000000, 'reported_offset_minutes': 480, 'fields': {'progress': {'value': '第二章'}, 'emotion': None}}})), Committed)
                changed = record((await view(now + 3000000))['activity'])
                self.assertEqual(record(record(changed['fields'])['progress'])['first_reported_at'], now + 2000000)
                self.assertIsNone(record(changed['fields'])['emotion'])
                invalid = await port.execute('state_update', 'future', {'activity_id': oid, 'expected_revision': 3,
                    'patch': {'reported_at': now + 120000000, 'reported_offset_minutes': 480}})
                self.assertIs(type(invalid), InformationNotCommitted)
                self.assertEqual(record((await view(now))['activity']), changed)
                stale_after = integer(h.current_state.configuration.candidate.information.record('state.external')['stale_after_seconds']) * 1000000
                stale = await view(integer(changed['received_at']) + stale_after + 1)
                self.assertTrue(stale['stale']); self.assertEqual(stale['activity'], changed)
                replaced = await port.execute('state_set', 'replace', {'activity_id': oid, 'expected_revision': 3, 'replace_activity': True,
                    'patch': {'activity_value': '运动', 'started_at': now + 10000000, 'reported_at': now + 3000000, 'reported_offset_minutes': 480}})
                self.assertIs(type(replaced), Committed)
                current = await view(now); activity = record(current['activity'])
                self.assertNotEqual(activity['activity_id'], oid); self.assertEqual(activity['revision'], 1)
                self.assertEqual((current['duration_us'], current['clock']), (0, 'CLOCK_AHEAD'))
                self.assertEqual(record(current['last_ended'])['activity_id'], oid)
                refused = await port.execute('state_end', 'early-end', {'activity_id': activity['activity_id'], 'expected_revision': 1})
                self.assertIs(type(refused), InformationNotCommitted)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                self.assertEqual(record((await view(now))['activity']), activity); self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_three_entry_frozen_reply_and_dream_transfer_keep_only_current_context(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); template = host(root); publication = ExplicitPublication()
            started = threading.Event(); release = threading.Event()
            h = ComponentHost(template.configuration, template.resources,
                SimulationAdapter((success(), replace(success(), started=started, release=release))),
                template.candidates, template.learning_profile, template.media_policy, publication)
            learning: asyncio.Task[object] | None = None
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                if h.runtime is None: self.fail('Expected actual runtime.')
                entries = ('entry', 'other_a', 'other_b')
                await h.register_entry('register', 'entry', 'host', 'sample_platform', 'entry')
                entry = h.runtime.bind_entry('entry')
                for ordinal in range(3):
                    raw = event('input:' + str(ordinal)); raw['event_version'] = 2
                    self.assertIs(type(await entry.accept_event('accept:' + str(ordinal), raw)), Committed)
                self.assertIs(type(await entry.run_learning('learn')), Committed)
                del entry
                await asyncio.sleep(0)
                for entry_id in entries[1:]:
                    self.assertIs(type(await h.register_entry('register:' + entry_id, entry_id, 'host', 'sample_platform', entry_id)), Committed)
                    entry = h.runtime.bind_entry(entry_id)
                    for ordinal in range(3):
                        raw = event(entry_id + ':' + str(ordinal)); raw['event_version'] = 2
                        self.assertIs(type(await entry.accept_event('input:' + str(ordinal), raw)), Committed)
                    del entry
                    await asyncio.sleep(0)
                entry = h.runtime.bind_entry('entry')
                for ordinal in (3, 4):
                    raw = event('input:' + str(ordinal)); raw['event_version'] = 2
                    self.assertIs(type(await entry.accept_event('accept:' + str(ordinal), raw)), Committed)
                learning = asyncio.create_task(entry.run_learning('second_learning'))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                port = await h.bind_query(HostIdentity('reply', 'principal', 'host', 'entry', frozenset(('prepare_reply',)), (), time.monotonic() + 300))
                value = await port.prepare_reply(query('frozen', participant_ids=(), situation=''))
                if type(value) is not Found: self.fail('Expected bounded frozen-entry reply: ' + repr(value))
                sections = record(record(value.value)['sections']); recent = record(sections['recent_context'])['items']
                if type(recent) is not tuple: self.fail('Expected complete local context.')
                self.assertEqual(tuple(text(record(record(item)['event'])['client_event_key']) for item in recent), ('input:2', 'input:3', 'input:4'))
                self.assertEqual(tuple(record(item)['state'] for item in recent), ('FROZEN', 'FROZEN', 'PENDING'))
                self.assertNotIn('input:1', repr(recent)); self.assertNotIn('other_a:', repr(sections)); self.assertNotIn('other_b:', repr(sections))
                self.assertEqual(record(sections['other_pending'])['entry_count'], 2)
                release.set(); self.assertIs(type(await learning), Committed)
                del entry
                await asyncio.sleep(0)
                focus = h.runtime.focus.bind('dream')
                self.assertIs(type(await focus.enter_focus('enter', h.runtime.gate.epoch)), Committed)
                for entry_id in entries:
                    entry = h.runtime.bind_entry(entry_id)
                    raw = event(entry_id + ':staged'); raw['event_version'] = 2
                    self.assertIs(type(await entry.accept_event('staged', raw)), Committed)
                    del entry
                    await asyncio.sleep(0)
                blocked = await port.prepare_reply(query('focused', participant_ids=(), situation=''))
                self.assertIs(type(blocked), InformationRejected)
                if type(blocked) is InformationRejected: self.assertEqual(blocked.error.reason, 'DREAMING')
                epoch = h.runtime.gate.epoch
                published = await publication.publish('dream', h.assembly.configuration.snapshot_id, exit_key='finish', exit_epoch=epoch)
                if type(published) is not Committed: self.fail('Expected explicit synthetic publication evidence.')
                self.assertIs(type(await focus.finish_focus('finish', epoch, text(record(published.receipt.result)['publication_id']))), Committed)
                for entry_id in entries:
                    native = await h.bind_query(HostIdentity('reply:' + entry_id, 'principal', 'host', entry_id, frozenset(('prepare_reply',)), (), time.monotonic() + 300))
                    value = await native.prepare_reply(query('transferred:' + entry_id, entry_id=entry_id, participant_ids=(), situation=''))
                    if type(value) is not Found: self.fail('Expected no duplicate transfer holdings: ' + repr(value))
                    sections = record(record(value.value)['sections'])
                    self.assertEqual(record(sections['other_pending'])['entry_count'], 2)
                    pending = await h.assembly.buffers.other_pending(entry_id)
                    self.assertEqual(pending['messages'], 8 if entry_id == 'entry' else 6)
                    recent = record(sections['recent_context'])['items']
                    if type(recent) is not tuple: self.fail('Expected local transferred context.')
                    keys = tuple(text(record(record(item)['event'])['client_event_key']) for item in recent)
                    self.assertIn(entry_id + ':staged', keys)
                    self.assertEqual(len(keys), len(set(keys)))
                self.assertEqual(len(h.adapter.calls), 2)
            finally:
                release.set()
                if learning is not None: await asyncio.gather(learning, return_exceptions=True)
                self.assertTrue(await h.close())

    async def test_authored_literal_relevance_set_uses_real_current_objects_and_queries(self):
        import json
        from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
        raw_cases = json.loads(Path(__file__).with_name('lexical_cases.json').read_text())
        if type(raw_cases) is not list or len(raw_cases) != 60: self.fail('Expected sixty authored literal pairs.')
        cases: list[tuple[str, str]] = []
        for item in raw_cases:
            if type(item) is not list or len(item) != 2 or any(type(part) is not str for part in item): self.fail('Expected explicit text pairs.')
            cases.append((item[0], item[1]))
        with TemporaryDirectory() as directory:
            root = Path(directory); h = host(root); by_body: dict[str, str] = {}
            try:
                for group, start in enumerate(range(0, len(cases), 8)):
                    if group:
                        self.assertTrue(await h.close()); h = host(root)
                    h.candidates = SyntheticCandidateInput('authored_literals:' + str(group), tuple(body for _, body in cases[start:start + 8]), 50)
                    self.assertIs(type(await h.initialize('OPEN_EXISTING' if group else 'CREATE_NEW')), Found)
                    if not group: self.assertIs(type(await h.register_entry('register', 'entry', 'host', 'sample_platform', 'entry')), Committed)
                    if h.runtime is None or h.assembly.memory.information is None: self.fail('Expected actual learning owners.')
                    entry = h.runtime.bind_entry('entry')
                    for ordinal in (range(3) if not group else range(3 + (group - 1) * 2, 5 + (group - 1) * 2)):
                        raw = event('literal_input:' + str(ordinal)); raw['event_version'] = 2
                        self.assertIs(type(await entry.accept_event('accept:' + str(ordinal), raw)), Committed)
                    learned = await entry.run_learning('literal_group:' + str(group))
                    if type(learned) is not Committed: self.fail('Expected actual bounded candidate group: ' + repr(learned))
                    references = record(learned.receipt.result)['object_refs']
                    if type(references) is not tuple: self.fail('Expected actual current object references.')
                    for ref in references:
                        oid = text(record(ref)['object_id']); current = await h.assembly.memory.information.index_current(oid)
                        if current is None: self.fail('Expected committed current object.')
                        by_body[text(record(current['content'])['body'])] = oid
                self.assertEqual(len(by_body), 60)
                port = await h.bind_query(HostIdentity('quality', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300))
                calls = h.adapter.calls; recalled = 0; precision = 0.0; reciprocal = 0.0; truncated = 0
                for ordinal, (literal, body) in enumerate(cases):
                    result = await port.search_memory(query('literal:' + str(ordinal), query_text=literal, include_state=False, include_goals=False))
                    if type(result) is not Found: self.fail('Expected actual literal query: ' + repr(result))
                    value = record(result.value); memories = record(value['sections'])['memories']
                    if type(memories) is not tuple: self.fail('Expected finite ranked memories.')
                    ids = tuple(text(record(item)['object_id']) for item in memories); expected = by_body[body]
                    recalled += expected in ids; precision += int(expected in ids[:5]) / 5
                    reciprocal += 1 / (ids.index(expected) + 1) if expected in ids else 0
                    reasons = record(value['truncation'])['reasons']
                    if type(reasons) is not tuple: self.fail('Expected explicit coverage reasons.')
                    truncated += 'CANDIDATE_LIMIT' in reasons
                self.assertGreaterEqual(recalled / 60, 0.9); self.assertEqual(h.adapter.calls, calls)
                print({'scope': 'ACTUAL_SQLITE_AUTHORED_LITERAL_SET', 'cases': 60, 'recall_at_8': recalled / 60,
                    'precision_at_5': precision / 60, 'returned_mrr': reciprocal / 60, 'candidate_truncation_ratio': truncated / 60,
                    'human_reviewed': False, 'index_state': 'DIRTY_COVERAGE'})
            finally: self.assertTrue(await h.close())
