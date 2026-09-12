"""Read-only owner counts and unchanged Provider statistics over actual HTTP."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlencode
import unittest
from companion_memory.persistence import Found
from companion_memory.provider import ObserverGrant
from companion_memory.provider.values import Found as ProviderFound
from companion_memory.information.observation import owned_projection
from companion_memory.information.errors import InformationRejected
from companion_memory.information.records import record
from tests.information.host_support import host, learn_one
from tests.information.test_http import exchange


class InformationObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_observation_has_no_business_body_or_write_capability(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                if h.information_observations is None or h.runtime is None or h.http is None: self.fail('Expected actual owners.')
                observer = h.information_observations.bind(frozenset(('retrieval', 'state', 'goals')))
                h.runtime.gate.close_ordinary()
                for scope in ('retrieval', 'state', 'goals'):
                    result = await observer.read(scope, {})
                    self.assertIs(type(result), Found, result)
                    self.assertNotIn('周末去北京看展', repr(result))
                address = await h.http.start(); token = h.http.issue_observation_session(observer, time.monotonic() + 300)
                status, view = await exchange(address, token, '/api/observe/retrieval', {}, body_override=b'', method='GET')
                self.assertEqual(status, 200, view); self.assertIn('occupied', repr(view))
                status, _ = await exchange(address, token, '/api/host/goals/inject', {})
                self.assertEqual(status, 403)
                h.information_observations.revoke(observer)
                self.assertIs(type(await observer.read('state', {})), InformationRejected)
            finally:self.assertTrue(await h.close())

    async def test_provider_usage_projection_keeps_ledger_categories_and_scope(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                if h.information_observations is None or h.http is None: self.fail('Expected observations.')
                provider = h.provider.bind_observer(ObserverGrant(('instance',), ('sample_account',)))
                observer = h.information_observations.bind(frozenset(('provider/usage', 'provider/budget')), provider)
                now = datetime.now(timezone.utc)
                query = {'start': (now - timedelta(days=1)).isoformat(), 'end': (now + timedelta(days=1)).isoformat(),
                    'caller_scope': None, 'capability': None, 'task_role': None, 'profile_id': None, 'account_id': None, 'group_by': 'NONE'}
                direct = await provider.query_usage(query)
                if type(direct) is not ProviderFound: self.fail('Expected actual Provider ledger projection.')
                result = await observer.read('provider/usage', query)
                self.assertIs(type(result), Found, result)
                if type(result) is Found:
                    self.assertEqual(record(result.value)['rows'], record(owned_projection(direct.value))['rows'])
                    self.assertEqual(record(result.value)['source'], 'SIMULATED')
                    self.assertEqual(record(result.value)['sample_count'], 1)
                address = await h.http.start(); token = h.http.issue_observation_session(observer, time.monotonic() + 300)
                path = '/api/observe/provider/usage?' + urlencode({k: v for k, v in query.items() if v is not None})
                status, result = await exchange(address, token, path, {}, body_override=b'', method='GET')
                self.assertEqual(status, 200, result); self.assertIn('SIMULATED', repr(result))
                denied = await observer.read('provider/usage', query | {'caller_scope': 'other'})
                self.assertIs(type(denied), InformationRejected, denied)
            finally:self.assertTrue(await h.close())
