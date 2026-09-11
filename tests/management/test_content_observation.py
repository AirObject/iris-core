"""Actual loopback HTTP sees scoped owner counts and never body/file capabilities."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.configuration.observation_settings import content_observation_settings
from companion_memory.management import ReadOnlyHTTP
from companion_memory.media.service import MediaService
from companion_memory.runtime.results import Found, Failed
from tests.provider.support import record
from tests.management.test_http_observation import request
from tests.memory.support import Fixture
from tests.persistence.support import sqlite_fault
from tests.runtime.configuration_support import event


class ContentObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_scopes_limits_failures_and_http_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), MediaService(), SyntheticCandidateInput('explicit:1', (), 50)).initialize()
            runtime = fixture.runtime; assert runtime is not None
            server = ReadOnlyHTTP(content_observation_settings(fixture.candidate))
            address = await server.start()
            try:
                entry = runtime.bind_entry('entry')
                body = event('private', 'private event text <script>')
                body['event_version'] = 2
                await entry.accept_event('input', body)
                await runtime.execute('register_content_entry', 'second-entry', {'entry_id': 'second', 'host_id': 'host',
                    'platform_id': 'sample_platform', 'external_entry_id': 'second-conversation'})
                scoped = runtime.observations.bind(('entry', 'second'))
                global_view = runtime.observations.bind(('entry',), True)
                def session(observer=scoped): return server.issue_test_session(observer, None, time.monotonic() + 60)
                self.assertEqual((await request(address, '/api/observe/memory'))[0], 401)
                self.assertEqual((await request(address, '/api/observe/runtime', session()))[0], 403)
                self.assertEqual((await request(address, '/api/observe/media?entry_id=unseen', session()))[0], 403)
                self.assertEqual((await request(address, '/api/observe/memory', session(), 'POST'))[0], 405)
                self.assertEqual((await request(address, '/api/objects/private', session()))[0], 404)
                from companion_memory.runtime.results import NotCommitted, RuntimeError
                runtime.observations.record_work('entry', NotCommitted(RuntimeError('PRECONDITION_FAILED', 'run_learning', 'revision', 'REVISION_CONFLICT')))
                for kind in ('memory', 'media', 'runtime'):
                    status, encoded = await request(address, '/api/observe/' + kind, session(global_view))
                    self.assertEqual(status, 200, encoded)
                    data = json.loads(encoded)
                    self.assertEqual(data['consistency'], 'COMPOSITE_OBSERVATION')
                    for row in data['rows']:
                        self.assertTrue(row['owners'])
                        largest = dict(row); largest['entry_id'] = 'e' * 128
                        for key in largest:
                            if type(largest[key]) is int: largest[key] = 2**63 - 1
                        largest['owners'] = {owner: {**observed, 'revision': {key: 2**63 - 1 for key in observed['revision']}} for owner, observed in row['owners'].items()}
                        self.assertLessEqual(len(json.dumps(largest, ensure_ascii=False, separators=(',', ':')).encode()), 1024)
                        for observation in row['owners'].values():
                            self.assertIn('observed_at', observation)
                            self.assertIn('revision', observation)
                    if kind in ('media', 'runtime'):
                        self.assertEqual(data['instance_media']['pending_deletions'], 0)
                        self.assertFalse(data['instance_media']['cleanup_pending'])
                    self.assertEqual((data['storage_execution'], data['model_adapter'], data['candidate_origin']), ('ACTUAL', 'SIMULATED', 'SYNTHETIC'))
                    if kind in ('memory', 'media'):
                        self.assertEqual(data['rows'][0]['work']['state'], 'SYSTEM_BLOCKED')
                        self.assertEqual(data['rows'][0]['work']['reason'], 'REVISION_CONFLICT')
                    for secret in (b'private event text', b'source_id', b'sha256', str(fixture.root).encode(), b'original_request', b'previous_value'):
                        self.assertNotIn(secret, encoded)
                page = await scoped.read_memory_status({'limit': 1}); assert type(page) is Found
                self.assertTrue(record(page.value)['has_more'])
                cursor = record(page.value)['next_cursor']
                next_page = await scoped.read_memory_status({'limit': 1, 'cursor': cursor}); assert type(next_page) is Found
                self.assertFalse(record(next_page.value)['has_more'])
                self.assertIs(type(await global_view.read_memory_status({'limit': 1, 'cursor': cursor})), Failed)
                self.assertIs(type(await scoped.read_media_status({'limit': 1, 'cursor': cursor})), Failed)
                self.assertEqual((await request(address, '/api/observe/media?limit=999', session()))[0], 400)
                status, scoped_media = await request(address, '/api/observe/media', session())
                self.assertEqual(status, 200)
                self.assertNotIn('instance_media', json.loads(scoped_media))
                repeat_token = session()
                self.assertEqual((await request(address, '/api/observe/media', repeat_token))[0], 200)
                self.assertEqual((await request(address, '/api/observe/media', repeat_token))[0], 429)
                def fail(sql):
                    if 'FROM memory_sources' in sql: raise sqlite_fault(sqlite3.SQLITE_IOERR)
                fixture.hooks.before = fail
                cached = await scoped.read_memory_status({'limit': 1}); assert type(cached) is Found
                self.assertEqual(record(cached.value)['availability'], 'STALE')
                fresh = runtime.observations.bind(('entry',))
                status, failed = await request(address, '/api/observe/memory', session(fresh))
                self.assertEqual(status, 503, failed)
                self.assertNotIn('rows', json.loads(failed))
                status, script = await request(address, '/status.js', session())
                self.assertEqual(status, 200); self.assertIn(b'"memory","media"', script)
                self.assertNotIn(b'innerHTML', script)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally:
                fixture.hooks.before = lambda sql: None
                self.assertTrue(await server.close())
                await fixture.close()
                self.assertFalse(runtime.observations.jobs)
