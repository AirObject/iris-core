"""Reject corrupted coverage roots and either missing side of actual evidence."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.information.index_worker import LocalIndexWorker
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, text
from companion_memory.persistence import Found, Committed
from tests.information.host_support import host, learn_one
from tests.information.test_feedback_boundaries import payload
from tests.information.test_queries import query
from tests.information.test_expiry import scores


class InformationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_forgotten_legacy_remove_and_deep_index_upsert_recover_without_reinterpretation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                self.assertIs(type(await scores(h, oid, 19)), Committed)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT revision,action FROM memory_index_dirty').fetchone(), (2, 'REMOVE'))
                    self.assertEqual(connection.execute("SELECT revision,json_extract(body,'$.action') FROM memory_index_gap").fetchone(), (2, 'UPSERT'))
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                port = await h.bind_business(HostIdentity('read', 'principal', 'host', 'entry', frozenset(('search_memory', 'deep_recall')), (), time.monotonic() + 300), include_forgotten=True)
                normal = await port.search_memory(query('normal'))
                deep = await port.deep_recall(query('deep'))
                if type(normal) is not Found or type(deep) is not Found: self.fail('Expected actual separate access paths.')
                self.assertEqual(record(record(normal.value)['sections'])['memories'], ())
                memories = record(record(deep.value)['sections'])['memories']
                if type(memories) is not tuple: self.fail('Expected finite deep snapshot.')
                self.assertEqual(tuple(record(item)['object_id'] for item in memories), (oid,))
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_extra_roots_and_noncurrent_activity_damage_are_rejected_without_repair(self):
        for damage in ('state_root', 'retrieval_root', 'historical_activity', 'orphan_candidate'):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); h = host(root)
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    self.assertIs(type(await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')), Committed)
                    if damage == 'historical_activity':
                        port = await h.bind_management(HostIdentity('state', 'principal', 'host', 'entry', frozenset(('state_set', 'state_end')), (), time.monotonic() + 300))
                        for index in range(3):
                            result = await port.execute('state_set', 'begin-' + str(index), {'activity_id': None, 'expected_revision': None, 'replace_activity': False,
                                'patch': {'activity_value': '阅读', 'reported_at': int(time.time() * 1000000), 'reported_offset_minutes': 0}})
                            if type(result) is not Committed: self.fail('Expected actual activity.')
                            reference = record(record(record(result.receipt.result)['facts'])['state'])
                            self.assertIs(type(await port.execute('state_end', 'end-' + str(index), {'activity_id': reference['object_id'], 'expected_revision': 1})), Committed)
                    self.assertTrue(await h.close())
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        if damage == 'orphan_candidate':
                            scope = connection.execute('SELECT scope_id FROM goals_metadata').fetchone()[0]
                            connection.execute('INSERT INTO goals_dedup_candidate VALUES (?,?,?,?,?)',
                                (scope, 'missing-task', 'missing-goal', 1, '{"candidate_id":"missing-goal","revision":1,"task_id":"missing-task"}'))
                        elif damage == 'historical_activity':
                            row = connection.execute("SELECT activity_id,body FROM state_activity WHERE activity_id<>(SELECT json_extract(body,'$.last_ended_id') FROM state_current_pointer) ORDER BY activity_id LIMIT 1").fetchone()
                            value = json.loads(row[1]); value['instance_id'] = 'wrong-instance'
                            connection.execute('UPDATE state_activity SET instance_id=?,body=? WHERE activity_id=?',
                                ('wrong-instance', json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')), row[0]))
                        else:
                            table = 'state_current_pointer' if damage == 'state_root' else 'retrieval_coordinator'
                            row = list(connection.execute('SELECT * FROM ' + table).fetchone())
                            value = json.loads(row[-1]); value['instance_id'] = 'extra-instance'
                            row[1] = 'extra-instance'; row[-1] = json.dumps(value, sort_keys=True, separators=(',', ':'))
                            connection.execute('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in row) + ')', row)
                        connection.commit()
                        before = tuple(connection.iterdump())
                    h = host(root)
                    self.assertIsNot(type(await h.initialize('OPEN_EXISTING')), Found)
                    self.assertNotEqual(h.state, 'READY'); self.assertEqual(h.adapter.calls, ())
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        self.assertEqual(tuple(connection.iterdump()), before)
                finally: self.assertTrue(await h.close())

    async def test_sequence_gap_index_digest_and_reverse_usage_mirror_corruption_never_enter_ready(self):
        for damage in ('sequence', 'gap', 'acknowledgment', 'lost_index', 'index_digest', 'reverse_mirror'):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); h = host(root)
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    await learn_one(h)
                    if damage == 'reverse_mirror':
                        port = await h.bind_business(HostIdentity('usage', 'principal', 'host', 'entry', frozenset(('search_memory', 'record_usage')), (), time.monotonic() + 300))
                        self.assertIs(type(await port.record_usage(payload(await port.search_memory(query('ticket')), 'use'))), Committed)
                    elif damage in ('acknowledgment', 'lost_index', 'index_digest'):
                        native = await h.bind_management(HostIdentity('index', 'principal', 'host', 'entry', frozenset(('index_begin', 'index_claim', 'index_apply_object', 'index_confirm_page')), (), time.monotonic() + 300))
                        begun = await native.execute('index_begin', 'begin', {'expected_generation': None})
                        if type(begun) is not Committed or h.runtime is None or h.retrieval is None: self.fail('Expected actual index work.')
                        generation = text(record(record(record(begun.receipt.result)['facts'])['retrieval'])['object_id'])
                        self.assertIs(type(await LocalIndexWorker(h.runtime, h.retrieval, native, 'index').run(generation)), Committed)
                    self.assertTrue(await h.close())
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        if damage == 'reverse_mirror': connection.execute('DELETE FROM retrieval_consumption')
                        elif damage == 'acknowledgment': connection.execute('DELETE FROM memory_generation_ack')
                        elif damage == 'lost_index':
                            connection.execute('DELETE FROM retrieval_posting')
                            connection.execute('DELETE FROM retrieval_index_object')
                        else:
                            table = 'memory_change_sequence' if damage == 'sequence' else 'memory_index_gap' if damage == 'gap' else 'retrieval_index_object'
                            value = json.loads(connection.execute('SELECT body FROM ' + table).fetchone()[0])
                            if damage == 'sequence':
                                value['revision'] += 1
                                connection.execute('UPDATE ' + table + ' SET revision=?', (value['revision'],))
                            elif damage == 'gap':
                                value['first_uncovered_seq'] = 0
                                connection.execute('UPDATE ' + table + ' SET first_uncovered_seq=0')
                            else: value['body_digest'] = '0' * 64
                            connection.execute('UPDATE ' + table + ' SET body=?', (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')),))
                        connection.commit()
                    h = host(root)
                    self.assertIsNot(type(await h.initialize('OPEN_EXISTING')), Found)
                    self.assertNotEqual(h.state, 'READY')
                    self.assertEqual(h.adapter.calls, ())
                finally: self.assertTrue(await h.close())
