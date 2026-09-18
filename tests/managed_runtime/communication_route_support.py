"""Registered ownership, explicit token ACLs and actual HTTP goal destinations."""
import time
from datetime import datetime, timezone, timedelta
from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from .http_support import request


async def exercise_routes(test, application, port, call):
    identity = application.identity
    host = application.business.host
    assert host is not None and host.goals is not None
    async def route(name):
        status, result = await call('/api/connections/routes/create', {'key': 'create-' + name,
            'route_id': name, 'host_id': 'host', 'entries': ['entry'], 'event_types': ['goal.upcoming', 'goal.due']})
        test.assertEqual(status, 200, result)
        test.assertEqual(result['outcome'], 'COMMITTED', result)
        return result
    first = await route('route-a')
    repeated = await route('route-a')
    test.assertEqual(first['data']['receipt']['commit_id'], repeated['data']['receipt']['commit_id'])
    expires = time.time_ns() // 1000 + 60000000
    for owner, entries in (('unregistered', ('entry',)), ('host', ('unregistered',))):
        with test.assertRaises(OwnerFailure):
            await identity.create_token('invalid-token', owner, entries, ('goal_write',), expires)
    created, token = await identity.create_token('goal-token', 'host', ('entry',), ('goal_write', 'goal_read', 'confirm'), expires,
        route_ids=('route-a',), event_types=('goal.upcoming', 'goal.due'))
    test.assertIs(type(created), Committed, created)
    assert token is not None
    _, legacy = await identity.create_token('legacy-goal-token', 'host', ('entry',), ('goal_write',), expires)
    assert legacy is not None
    deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    value = dict(operation_key='routed-goal', content='受控路由目标', subject_ids=['self'], world_scope='REAL',
        deadline=deadline, reminder_lead_seconds=60, route_id=None, source_id='routed-source')
    async def inject(payload, bearer=token):
        return await request(port, '/api/host/goals/inject', {'entry_id': 'entry', 'input': payload}, bearer)
    status, denied = await inject(value | {'operation_key': 'denied-legacy-goal'}, legacy)
    test.assertEqual(status, 403, denied)
    status, accepted = await inject(value)
    test.assertEqual(status, 200, accepted)
    test.assertEqual(accepted['outcome'], 'COMMITTED', accepted)
    status, goals = await request(port, '/api/host/goals', {'entry_id': 'entry', 'input': {}}, token)
    test.assertEqual(status, 200, goals)
    goal = next(item for item in goals['data']['value']['items'] if item['content'] == value['content'])
    test.assertEqual(goal['route_id'], 'route-a')
    from companion_memory.information.records import identity as stable_id
    task_id = stable_id('goal_dedup', goal['goal_id'])
    original_task = await host.goals._records.read('dedup_task', {'task_id': task_id})
    original_goal = await host.goals.lookup(goal['goal_id'])
    original_plans = await host.goals._records.rows.read('reminder_plan_children', {'goal_id': goal['goal_id']})
    await route('route-b')
    status, repeated = await inject(value)
    test.assertEqual(status, 200, repeated)
    test.assertEqual(repeated['data']['receipt']['commit_id'], accepted['data']['receipt']['commit_id'])
    status, denied = await inject(value | {'operation_key': 'foreign-route', 'route_id': 'route-b'})
    test.assertEqual(status, 403, denied)
    status, ambiguous = await call('/api/administration/goals/inject', value | {'operation_key': 'ambiguous-admin', 'source_id': 'admin-source'})
    test.assertEqual(status, 403, ambiguous)
    status, changed = await call('/api/administration/goals/deadline', {'operation_key': 'reroute', 'goal_id': goal['goal_id'],
        'expected_revision': goal['revision'], 'deadline': deadline, 'reminder_lead_seconds': 60, 'route_id': 'route-b'})
    test.assertEqual(status, 200, changed)
    test.assertEqual(changed['outcome'], 'COMMITTED', changed)
    current = await host.goals.lookup(goal['goal_id'])
    test.assertEqual(current['route_id'], 'route-b')
    test.assertEqual(await host.goals._records.read('dedup_task', {'task_id': task_id}), original_task)
    for plan in original_plans:
        retired = await host.goals._records.read('reminder_plan', {'plan_id': plan['plan_id']})
        test.assertEqual(retired['status'], 'CANCELLED')
        test.assertEqual(retired['route_id'], 'route-a')
    due_at = time.time_ns() // 1000 + 2 * 86400000000
    test.assertIsNone(await host.goals.route_due_plan('route-a', due_at))
    routed_plan = await host.goals.route_due_plan('route-b', due_at)
    test.assertEqual(routed_plan['goal_id'], goal['goal_id'])
    test.assertEqual(routed_plan['deadline_revision'], current['revision'])
    # A reroute updates the indexed structure used by future exact comparisons.
    from companion_memory.goals.service import signature
    test.assertNotEqual(signature(current), signature(original_goal))
    raw_goal = (await host.goals._records.rows.read('goal_get', {'goal_id': goal['goal_id']}))[0]
    test.assertEqual(raw_goal['signature'], signature(current))
    # Existing binding remains owned by ingress; no second current-state writer.
    registration = {'key': 'register-second-host', 'entry_id': 'second-entry', 'host_id': 'second-host',
        'platform_id': 'sample_platform', 'external_entry_id': 'second-conversation'}
    status, registered = await call('/api/connections/hosts/register', registration)
    test.assertEqual(status, 200, registered)
    status, confirmed = await call('/api/connections/hosts/confirm', registration)
    test.assertEqual(status, 200, confirmed)
    test.assertEqual(registered['data']['receipt']['commit_id'], confirmed['data']['receipt']['commit_id'])
    test.assertTrue(await host.assembly.ingress.verify_host_entry('second-entry', 'second-host'))
    test.assertFalse(await host.assembly.ingress.verify_host_entry('second-entry', 'host'))
    status, listed = await call('/api/connections/routes/list', {'after': ''})
    test.assertEqual(status, 200, listed)
    test.assertEqual([row['object_id'] for row in listed['data']['items']], ['route-a', 'route-b'])
    test.assertTrue(all(row['enabled'] is False for row in listed['data']['items']))


async def exercise_internal_route(test, host, learned, object_id):
    from companion_memory.information.management import HostIdentity
    assert host.learning is not None and host.runtime is not None
    test.assertEqual(host.learning.scopes['entry'].routes, ('route-a', 'route-b'))
    read = host.runtime.memory.bind_read((object_id, 'self'), ('get_current', 'read_subject'))
    runs = await host.combination.reasoning.rows.page('reasoning_runs', '')
    candidate_id = next(run['candidate_id'] for run in runs if run['candidate_id'] is not None)
    proposed = {'content': '由正式依据形成的内部路由目标', 'subject_ids': ('self',), 'world_scope': 'REAL',
        'deadline': time.time_ns() // 1000 + 86400000000, 'reminder_lead_seconds': 60,
        'route_id': 'route-a', 'source_id': candidate_id, 'basis_id': object_id}
    work = await host.bind_formed_goal_work(HostIdentity('internal-route', 'internal-worker', 'host', 'entry',
        frozenset(('goal_inject_internal',)), ('invented-route',), time.monotonic() + 60), 'formed-routed-goal', proposed, read)
    try:
        result = await work.execute()
        test.assertIs(type(result), Committed, result)
        repeated = await work.resolve()
        test.assertIs(type(repeated), Committed, repeated)
        test.assertEqual(result.receipt.commit_id, repeated.receipt.commit_id)
    finally:
        work.revoke()
        host.runtime.memory.release_query_scope(read)
