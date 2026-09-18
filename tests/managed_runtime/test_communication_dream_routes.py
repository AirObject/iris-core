"""Native dream work preserves the trusted routes frozen with its evidence."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from typing import cast

from companion_memory.dream.port import OPERATIONS
from companion_memory.information.management import HostIdentity
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, Found
from tests.daily_cognition.test_reasoning import responses
from tests.dream_maintenance.host_support import make_dream_host
from tests.dream_maintenance.seed_support import establish_memory
from tests.dream_maintenance.test_review_host import graph_output


class DreamRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_routes_do_not_reinterpret_frozen_dream_candidate(self):
        def proposal(request):
            result = graph_output(request)
            result['actions'][-1]['route_id'] = 'route-a'
            return result
        with TemporaryDirectory() as directory, responses((proposal,)) as (port, requests, failures):
            host = make_dream_host(Path(directory), port, [], with_self=True)
            routes = ('route-a',)
            async def registered(entry):
                self.assertEqual(entry, 'entry')
                return routes
            host.route_source = registered
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')), Found)
                self.assertIs(type(await host.register_entry('register', 'entry', 'host', 'sample_platform', 'external')), Committed)
                await establish_memory(host, with_self=True)
                admin = await host.bind_dream(HostIdentity('admin', 'supervisor', 'host', 'entry', OPERATIONS, (), time.monotonic() + 120))
                self.assertIs(type(await admin.start_dream('start', 'run', 1, 1, mode='BACKGROUND')), Committed)
                self.assertIs(type(await admin.resume_dream('resume', 'run', 1, 1)), Committed)
                for stage in ('time', 'freeze'):
                    result = await host.advance_dream()
                    self.assertIs(type(result), Committed, (stage, result))
                routes = ('route-a', 'route-b')
                for stage in ('receive', 'plan', 'apply'):
                    result = await host.advance_dream()
                    self.assertIs(type(result), Committed, (stage, result))
                assert type(result) is Committed and host.goals is not None
                goals = record(record(record(result.receipt.result)['facts'])['goals'])
                goal_id = cast(str, record(sequence(goals['targets'])[0])['object_id'])
                current = await host.goals.lookup(goal_id)
                assert current is not None
                self.assertEqual(current['route_id'], 'route-a')
                self.assertEqual(json.loads(json.loads(requests[0])['messages'][1]['content'])['routes'], ['route-a'])
                self.assertEqual(len(requests), 1)
                self.assertFalse(failures)
            finally:
                self.assertTrue(await host.close())
