"""Actual local queries issue durable tickets and isolate reply entry context."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import time
import unittest
from companion_memory.cognition.synthetic_graph import SyntheticGraphInput, SubjectProposal, FactProposal
from companion_memory.persistence import Found, Committed
from companion_memory.information.management import HostIdentity
from companion_memory.information.errors import InformationRejected
from companion_memory.information.records import record, text
from tests.information.host_support import host, learn_one, learn_objects
from tests.runtime.configuration_support import event


def query(key: str, **changes: object) -> dict[str, object]:
    return {'request_key': key, 'entry_id': 'entry', 'query_text': '北京看展', 'subject_ids': (), 'object_ids': (),
        'category': None, 'world_scope': None, 'time_range': None, 'allow_partial': True, 'require_complete': False,
        'retrieval_mode': 'LOCAL_LEXICAL_V1', 'rerank': False, 'include_state': True, 'include_goals': True} | changes


class QueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_subject_and_world_filters_do_not_infer_unknown_event_times(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); h = host(root)
            proposals = SyntheticGraphInput('structured_literals', (
                SubjectProposal('first', 'PLATFORM_PERSON', '同名人物', 'sample_platform', 'first'),
                SubjectProposal('second', 'PLATFORM_PERSON', '同名人物', 'other_platform', 'second'),
                SubjectProposal('scene', 'CONTEXT', 'REAL', None, None),
                FactProposal('real_first', '没有去北京看展', ('first',), 'REAL', None),
                FactProposal('real_second', '周末去北京看展', ('second',), 'REAL', None),
                FactProposal('fiction', '周末去北京看展', ('first',), 'FICTIONAL', 'scene'),
                FactProposal('role', '周末去北京看展', ('first',), 'ROLEPLAY', 'scene'),
            ), 50)
            h.candidates = proposals
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_objects(h)
                memory = h.assembly.memory.information
                if memory is None: self.fail('Expected actual memory owner.')
                current = await memory.current_page()
                self.assertEqual(len(current), 4)
                real = [item for item in current if record(record(item['content'])['world_scope'])['kind'] == 'REAL']
                first = next(item for item in real if record(item['content'])['body'] == '没有去北京看展')
                subjects = record(first['content'])['subject_ids']
                if type(subjects) is not tuple: self.fail('Expected actual subject references.')
                scene = next(record(record(item['content'])['world_scope'])['context_id'] for item in current
                             if record(record(item['content'])['world_scope'])['kind'] == 'FICTIONAL')
                for phase in ('dirty', 'active', 'reopened'):
                    reopened = phase == 'reopened'
                    if phase == 'active':
                        from tests.information.publication_support import index_port, build, publish
                        native = await index_port(h)
                        _, generation = await build(h, native, 'structure-index')
                        await publish(native, 'structure-publish', generation)
                    if reopened:
                        self.assertTrue(await h.close()); h = host(root); h.candidates = proposals
                        self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    calls = h.adapter.calls
                    port = await h.bind_query(HostIdentity('structure-' + phase, 'principal', 'host', 'entry',
                        frozenset(('search_memory',)), (), time.monotonic() + 300))
                    cases: tuple[tuple[dict[str, object], int], ...] = (
                        ({'subject_ids': subjects, 'world_scope': 'REAL', 'category': 'FACT'}, 1),
                        ({'subject_ids': subjects, 'world_scope': 'FICTIONAL:' + text(scene)}, 1),
                        ({'subject_ids': subjects, 'world_scope': 'ROLEPLAY:' + text(scene)}, 1),
                        ({'world_scope': 'REAL'}, 2),
                        ({'subject_ids': subjects, 'category': 'EVENT'}, 0),
                        ({'object_ids': (first['object_id'],), 'world_scope': 'REAL'}, 1),
                        ({'object_ids': (first['object_id'],), 'world_scope': 'FICTIONAL:' + text(scene)}, 0),
                        ({'time_range': {'start': '2026-01-01T00:00:00Z', 'end': None}}, 0),
                    )
                    for ordinal, (filters, count) in enumerate(cases):
                        with self.subTest(reopened=reopened, filters=filters):
                            result = await port.search_memory(query('structure:' + phase + ':' + str(ordinal), **filters))
                            if type(result) is not Found: self.fail('Expected actual filtered result: ' + repr(result))
                            value = record(result.value); memories = record(value['sections'])['memories']
                            if type(memories) is not tuple: self.fail('Expected finite memory section.')
                            self.assertEqual(len(memories), count)
                            if ordinal == 0:
                                self.assertEqual(record(memories[0])['object_id'], first['object_id'])
                                self.assertEqual(record(record(memories[0])['content'])['body'], '没有去北京看展')
                            if not count: self.assertIsNone(value['recall_id'])
                            if phase != 'dirty': self.assertEqual(value['availability'], 'COMPLETE')
                    self.assertEqual(h.adapter.calls, calls)
            finally: self.assertTrue(await h.close())

    async def test_dirty_chinese_recall_confirmation_and_permission(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                calls = h.adapter.calls
                port = await h.bind_query(HostIdentity('query-binding', 'principal', 'host', 'entry',
                    frozenset(('search_memory', 'resolve_recall')), (), time.monotonic() + 300))
                result = await port.search_memory(query('search'))
                self.assertIs(type(result), Found, result)
                if type(result) is not Found: self.fail('Expected actual current memory.')
                value = record(result.value); memories = record(value['sections'])['memories']
                if type(memories) is not tuple: self.fail('Expected bounded memory objects.')
                self.assertEqual(len(memories), 1); self.assertEqual(record(memories[0])['object_id'], oid)
                self.assertEqual(value['availability'], 'DEGRADED'); self.assertIsNotNone(value['recall_id'])
                original = await port.search_memory(query('search'))
                self.assertIs(type(original), Found, original)
                if type(original) is Found:
                    self.assertEqual(record(original.value)['availability'], 'CONFIRMED_ONLY')
                    self.assertNotIn('sections', record(original.value))
                mismatch = await port.search_memory(query('search', query_text='不同意图'))
                self.assertIs(type(mismatch), InformationRejected, mismatch)
                denied = await port.deep_recall(query('deep'))
                self.assertIs(type(denied), InformationRejected, denied)
                self.assertEqual(h.adapter.calls, calls)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM retrieval_ticket').fetchone()[0], 1)
                missing = await port.search_memory(query('no-match', query_text='火星洋葱'))
                self.assertIs(type(missing), Found, missing)
                if type(missing) is Found: self.assertIsNone(record(missing.value)['recall_id'])
            finally: self.assertTrue(await h.close())

    async def test_reply_missing_persona_and_other_entry_metadata_only(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                self.assertIs(type(await h.register_entry('other-key', 'other-entry', 'host', 'sample_platform', 'other-external')), Committed)
                if h.runtime is None: self.fail('Expected actual runtime.')
                for entry_id in ('entry', 'other-entry'):
                    raw = event('pending:' + entry_id); raw['event_version'] = 2
                    self.assertIs(type(await h.runtime.bind_entry(entry_id).accept_event('pending-key:' + entry_id, raw)), Committed)
                port = await h.bind_query(HostIdentity('reply-binding', 'principal', 'host', 'entry', frozenset(('prepare_reply',)), (), time.monotonic() + 300))
                supplied = query('prepare', participant_ids=(), situation='当前对话')
                result = await port.prepare_reply(supplied)
                self.assertIs(type(result), Found, result)
                if type(result) is Found:
                    sections = record(record(result.value)['sections'])
                    self.assertEqual(record(sections['persona'])['availability'], 'UNAVAILABLE')
                    other = record(sections['other_pending'])
                    self.assertEqual(other['entry_count'], 1)
                    self.assertNotIn('other-entry', repr(other))
                    recent = record(sections['recent_context'])['items']
                    if type(recent) is not tuple: self.fail('Expected entry context.')
                    self.assertEqual(tuple(text(record(record(item)['event'])['client_event_key']) for item in recent), ('input:2', 'pending:entry'))
                    self.assertNotIn('other-entry', repr(recent))
                strict = await port.prepare_reply(query('strict', participant_ids=(), situation='', allow_partial=False))
                self.assertIs(type(strict), InformationRejected, strict)
                if type(strict) is InformationRejected: self.assertEqual(strict.error.reason, 'PUBLICATION_MISSING')
            finally: self.assertTrue(await h.close())
