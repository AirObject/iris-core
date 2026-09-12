"""Complete reply partitions, exact pagination and actual provenance failures.

All mutations use native owner commands except deliberate corruption of an
isolated source-links row. Model participants and persona text are synthetic.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.parse import urlencode
from companion_memory.persistence import Found, Committed
from companion_memory.provider import Scenario, SimulationAdapter
from companion_memory.information.errors import InformationRejected
from companion_memory.information.management import HostIdentity
from companion_memory.information.maintenance import LocalMaintenance
from companion_memory.information.records import record, text
from companion_memory.retrieval.query_service import TestPersona
from tests.information.host_support import host, learn_one
from tests.information.test_http import exchange
from tests.information.test_queries import query
from tests.runtime.configuration_support import event


class ReplyPartitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_eight_goals_do_not_imply_more_and_full_sources_paginate_by_bytes(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')
                kinds = frozenset(('goal_inject_external', 'goal_dedup_claim', 'goal_dedup_finish', 'goal_exact_merge', 'goal_plan_advance', 'ticket_expire'))
                native = await h.bind_management(HostIdentity('inject', 'principal', 'host', 'entry', kinds, (), time.monotonic() + 300))
                content = '文' * 680
                async def inject(group: int, source: int) -> None:
                    result = await native.execute('goal_inject_external', 'goal:' + str(group) + ':' + str(source),
                        {'content': content, 'subject_ids': (), 'world_scope': 'FICTIONAL:story' + str(group),
                         'deadline': None, 'reminder_lead_seconds': None, 'route_id': None,
                         'source_id': (str(group) + ':' + str(source) + ':').ljust(128, 's')})
                    self.assertIs(type(result), Committed, result)
                for group in range(8): await inject(group, 0)
                business = await h.bind_business(HostIdentity('read', 'principal', 'host', 'entry', frozenset(('list_open_goals',)), (), time.monotonic() + 300))
                page = await business.list_open_goals()
                if type(page) is not Found: self.fail('Expected real goal page: ' + repr(page))
                items = record(page.value)['items']
                if type(items) is not tuple: self.fail('Expected complete goal items.')
                self.assertEqual(len(items), 8)
                self.assertFalse(record(page.value)['has_more'])
                for group in range(8):
                    for source in range(1, 8): await inject(group, source)
                if h.runtime is None or h.goals is None or h.http is None: self.fail('Expected native owners.')
                worker = LocalMaintenance(h.runtime, h.goals, h.management.tickets, native, 'dedup')
                for _ in range(4):
                    result = await worker.run(reclaim_tickets=False)
                    self.assertIs(type(result), Found, result)
                self.assertFalse(await h.goals.pending_tasks())
                address = await h.http.start(); token = h.http.issue_test_session(business, time.monotonic() + 300)
                cursor: str | None = None; seen: set[str] = set(); pages = 0
                while True:
                    path = '/api/host/goals' + ('?' + urlencode({'cursor': cursor}) if cursor is not None else '')
                    status, body = await exchange(address, token, path, {}, body_override=b'', method='GET')
                    self.assertEqual(status, 200, body)
                    self.assertLessEqual(len(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()), 36864)
                    value = body['value']
                    if type(value) is not dict or type(value['items']) is not list: self.fail('Expected bounded goal wire page.')
                    pages += 1
                    for goal in value['items']:
                        self.assertNotIn(goal['goal_id'], seen); seen.add(goal['goal_id'])
                        self.assertEqual(goal['content'], content)
                        self.assertEqual(goal['source_count'], 8); self.assertEqual(len(goal['source_refs']), 8)
                        self.assertTrue(all(len(source['source_id']) == 128 for source in goal['source_refs']))
                    if not value['has_more']: break
                    cursor = value['next_cursor']
                    self.assertIs(type(cursor), str)
                    self.assertLess(pages, 8)
                self.assertEqual(len(seen), 8); self.assertGreater(pages, 1)
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_full_persona_envelope_limit_is_explicit_without_cutting_text(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                if h.queries is None: self.fail('Expected query owner.')
                h.queries.test_persona = TestPersona('p' * 8192, 1, time.time_ns() // 1000)
                port = await h.bind_query(HostIdentity('query', 'principal', 'host', 'entry', frozenset(('prepare_reply',)), (), time.monotonic() + 300))
                result = await port.prepare_reply(query('partial', participant_ids=(), situation=''))
                self.assertIs(type(result), Found, result)
                if type(result) is Found:
                    value = record(result.value)
                    persona = record(record(value['sections'])['persona'])
                    self.assertEqual(persona['availability'], 'UNAVAILABLE')
                    self.assertEqual(persona['reason'], 'LIMIT_EXCEEDED'); self.assertNotIn('text', persona)
                    reasons = record(value['truncation'])['reasons']
                    if type(reasons) is not tuple: self.fail('Expected closed section reasons.')
                    self.assertIn('SECTION_LIMIT', reasons)
                result = await port.prepare_reply(query('strict', participant_ids=(), situation='', allow_partial=False))
                self.assertIs(type(result), InformationRejected, result)
                if type(result) is InformationRejected: self.assertEqual(result.error.reason, 'LIMIT_EXCEEDED')
            finally: self.assertTrue(await h.close())

    async def test_missing_actual_source_links_are_not_an_empty_success(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    c.execute('DELETE FROM memory_links WHERE object_id=?', (oid,)); c.commit()
                port = await h.bind_query(HostIdentity('query', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300))
                result = await port.search_memory(query('corrupt'))
                self.assertIs(type(result), InformationRejected, result)
                if type(result) is InformationRejected: self.assertEqual(result.error.reason, 'INTEGRITY_FAILURE')
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM retrieval_ticket').fetchone()[0], 0)
            finally: self.assertTrue(await h.close())

    async def test_entry_failure_and_refusal_counts_do_not_cross_reply_scope(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            h.adapter = SimulationAdapter(tuple(Scenario(outcome, None, {'coverage': 'COMPLETE', 'billing_input_units': 16,
                'billing_output_units': 0, 'known_cost_atoms': 16}) for outcome in ('OTHER_REFUSAL', 'SENSITIVE_REFUSAL')))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                if h.runtime is None: self.fail('Expected real runtime.')
                for entry_id in ('entry', 'other'):
                    await h.register_entry('register:' + entry_id, entry_id, 'host', 'sample_platform', entry_id)
                    entry = h.runtime.bind_entry(entry_id)
                    for ordinal in range(3):
                        raw = event(entry_id + ':' + str(ordinal), entry_id + ': original ' + str(ordinal)); raw['event_version'] = 2
                        self.assertIs(type(await entry.accept_event('input:' + str(ordinal), raw)), Committed)
                    result = await entry.run_learning('learn')
                    self.assertIs(type(result), Committed, result)
                    del entry
                port = await h.bind_query(HostIdentity('query', 'principal', 'host', 'entry', frozenset(('prepare_reply',)), (), time.monotonic() + 300))
                result = await port.prepare_reply(query('reply', participant_ids=(), situation=''))
                self.assertIs(type(result), Found, result)
                if type(result) is Found:
                    sections = record(record(result.value)['sections'])
                    terminals = record(record(sections['runtime'])['entry_terminals'])
                    self.assertEqual((terminals['failed_batches'], terminals['refused_batches']), (1, 0))
                    recent = record(sections['recent_context'])['items']
                    if type(recent) is not tuple: self.fail('Expected bounded current-entry items.')
                    self.assertEqual(tuple(text(record(record(item)['event'])['client_event_key']) for item in recent), ('entry:2',))
                    self.assertNotIn('other: original', repr(sections))
            finally: self.assertTrue(await h.close())
