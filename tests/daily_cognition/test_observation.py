"""Native mixed observation uses the same host, scoped ports and loopback HTTP."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from companion_memory.information.records import record
from typing import cast
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlencode
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.information.errors import InformationRejected
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.information.test_http import exchange
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses
from .test_usage_only_host import usage_inputs


class DailyObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_namespaced_owner_and_shared_usage_views_http_revocation(self):
        with TemporaryDirectory() as directory, responses(({'schema_version': 1, 'kind': 'FINAL', 'actions': []},)) as (port, requests, failures):
            root = Path(directory); host = make_host(root, port, [], configuration_input=usage_inputs(root))
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')), Found)
                self.assertIs(type(await host.register_entry('register', 'entry', 'host', 'sample_platform', 'external')), Committed)
                entry = host.bind_entry('entry')
                for i in range(3):
                    value = event('event-' + str(i), '禁止通过观察泄漏的合成正文'); value['event_version'] = 2
                    self.assertIs(type(await entry.accept_event('accept-' + str(i), value)), Committed)
                self.assertIs(type(await host.resume_learning('resume')), Committed)
                self.assertIs(type(await entry.run_learning('learn')), Committed)
                self.assertEqual(len(requests), 1)
                scopes = frozenset(('learning', 'media', 'goal_dedup', 'provider/usage', 'provider/budget', 'provider/requests'))
                assert host.observations is not None and host.http is not None
                observer = host.observations.bind(scopes)
                address = await host.http.start()
                token = host.http.issue_observation_session(observer, time.monotonic() + 300)
                for scope in ('learning', 'media', 'goal_dedup'):
                    result = await observer.read(scope, {})
                    self.assertIs(type(result), Found, result)
                    assert type(result) is Found
                    self.assertIn(scope, result.value)
                    self.assertEqual(set(record(record(result.value)['adapters']).values()), {'CONTROLLED'})
                    self.assertNotIn('禁止通过观察泄漏', repr(result))
                    status, view = await exchange(address, token, '/api/observe/' + scope, {}, body_override=b'', method='GET')
                    self.assertEqual(status, 200, view)
                now = datetime.now(timezone.utc)
                query = {'start': (now - timedelta(days=1)).isoformat(), 'end': (now + timedelta(days=1)).isoformat(),
                    'caller_scope': None, 'capability': None, 'task_role': None, 'profile_id': None, 'account_id': None, 'group_by': 'TASK_ROLE'}
                result = await observer.read('provider/usage', query)
                self.assertIs(type(result), Found, result)
                assert type(result) is Found
                row = record(cast(tuple,record(result.value)['rows'])[0])
                self.assertEqual(row['group_id'], 'LEARNING')
                self.assertEqual(row['request_count'], 1)
                self.assertEqual(row['incomplete_cost_count'], 1)
                self.assertEqual(row['held_atoms'], 0)
                self.assertIn('output_tokens_sum', row)
                status, view = await exchange(address, token, '/api/observe/provider/usage?' + urlencode({k: v for k, v in query.items() if v is not None}), {}, body_override=b'', method='GET')
                self.assertEqual(status, 200, view)
                for invalid in (query | {'account_id': 'foreign'}, query | {'caller_scope': 'foreign'}):
                    self.assertIs(type(await observer.read('provider/usage', invalid)), InformationRejected)
                self.assertIs(type(await observer.read('learning', {'after': 'x' * 129})), InformationRejected)
                limited = host.observations.bind(frozenset(('learning',)))
                self.assertIs(type(await limited.read('media', {})), InformationRejected)
                self.assertFalse(hasattr(entry, 'observations'))
                host.observations.revoke(observer)
                self.assertIs(type(await observer.read('media', {})), InformationRejected)
                status, _ = await exchange(address, token, '/api/observe/media', {}, body_override=b'', method='GET')
                self.assertEqual(status, 403)
            finally:
                self.assertTrue(await host.close())
            self.assertIs(type(await limited.read('learning', {})), InformationRejected)
