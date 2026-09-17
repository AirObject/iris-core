"""Actual loopback requests against the managed listener using synthetic credentials."""
import asyncio
import json
from pathlib import Path
import time
from datetime import datetime, timezone
from companion_memory.management.managed_http import ManagedHTTP
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_host_http import ManagedHostHTTP
from companion_memory.persistence import Committed
from tests.runtime.configuration_support import event


async def request(port: int, path: str, payload: dict, token: str, *, origin: str | None = None, session: dict[str, str] | None = None):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = f'POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nAuthorization: Bearer {token}\r\n'
    if session is not None:
        headers += f'Cookie: iris_session={session["session"]}\r\nX-CSRF-Token: {session["csrf"]}\r\n'
    if origin is not None:
        headers += f'Origin: {origin}\r\n'
    writer.write(headers.encode() + b'\r\n' + body)
    await writer.drain()
    wire = await asyncio.wait_for(reader.read(), 20)
    writer.close()
    await writer.wait_closed()
    head, encoded = wire.split(b'\r\n\r\n', 1)
    return int(head.split(b' ')[1]), json.loads(encoded)


async def exercise_host_http(test, bootstrap, business, root: Path):
    """Exercise actual scoped reads/writes and revoke the same bearer afterward."""
    identity = bootstrap.assembly.identity
    assert identity is not None
    # This test controller attaches to the already initialized synthetic host.
    application = object.__new__(ManagedApplication)
    application.bootstrap, application.identity, application.resources = bootstrap, identity, bootstrap.resources
    application.business = business
    application.control_lock = asyncio.Lock()
    from companion_memory.runtime.managed_logging import ManagedLogging
    application.logging = ManagedLogging()
    application.logging.open(bootstrap)
    application.observer = None
    from companion_memory.management.managed_operations import ManagedOperations
    application.operations = ManagedOperations(application)
    application.host_http = ManagedHostHTTP(business, identity)
    static = root.parent / ('test-static-' + root.name)
    static.mkdir()
    for name in ('index.html', 'app.js', 'style.css'):
        (static / name).write_text('synthetic static fixture')
    http = ManagedHTTP(bootstrap.settings, identity, application.dispatch, application.health, static)
    created, token = await identity.create_token('http-token', 'host', ('entry',), ('accept', 'state_read'), time.time_ns() // 1000 + 60000000)
    test.assertIs(type(created), Committed, created)
    assert token is not None
    principal = await identity.authenticate(token, host=True)
    try:
        await http.start()
        port = bootstrap.settings.integer('deployment.port')
        status, state = await request(port, '/api/host/state', {'entry_id': 'entry', 'input': {}}, token)
        test.assertEqual(status, 200, state)
        test.assertEqual(state['data']['status'], 'FOUND', state)
        status, denied = await request(port, '/api/host/state', {'entry_id': 'other-entry', 'input': {}}, token)
        test.assertEqual(status, 403, denied)
        status, denied = await request(port, '/api/host/goals', {'entry_id': 'entry', 'input': {}}, token)
        test.assertEqual(status, 403, denied)
        status, denied = await request(port, '/api/host/state', {'entry_id': 'entry', 'input': {}}, token, origin='https://invalid.example')
        test.assertEqual(status, 403, denied)
        value = event('managed-event', '合成宿主输入。')
        value['event_version'] = 2
        payload = {'entry_id': 'entry', 'input': {'key': 'managed-accept', 'event': value}}
        status, accepted = await request(port, '/api/host/accept', payload, token)
        test.assertEqual(status, 200, accepted)
        test.assertEqual(accepted['outcome'], 'COMMITTED', accepted)
        test.assertIn('receipt', accepted['data'], accepted)
        status, repeated = await request(port, '/api/host/accept/resolve', payload, token)
        test.assertEqual(status, 200, repeated)
        test.assertEqual(accepted['data']['receipt']['commit_id'], repeated['data']['receipt']['commit_id'])
        _, confirmation_token = await identity.create_token('confirmation-token', 'host', ('entry',), ('confirm',), time.time_ns() // 1000 + 60000000)
        assert confirmation_token is not None
        status, confirmation = await request(port, '/api/host/accept/resolve', payload, confirmation_token)
        test.assertEqual(status, 200, confirmation)
        test.assertEqual(confirmation['data']['receipt']['commit_id'], accepted['data']['receipt']['commit_id'])
        status, denied = await request(port, '/api/host/accept', payload, confirmation_token)
        test.assertEqual(status, 403, denied)
        _, state_token = await identity.create_token('state-writer-token', 'host', ('entry',), ('state_write',), time.time_ns() // 1000 + 60000000)
        assert state_token is not None
        state_input = {'operation_key': 'original-state', 'activity_id': None, 'expected_revision': None, 'replace_activity': False,
            'patch': {'activity_value': '合成活动', 'reported_at': datetime.now(timezone.utc).isoformat(), 'reported_offset_minutes': 0}}
        status, changed = await request(port, '/api/host/state/set', {'entry_id': 'entry', 'input': state_input}, state_token)
        test.assertEqual(status, 200, changed)
        test.assertEqual(changed['outcome'], 'COMMITTED', changed)
        previous_writer = await identity.authenticate(state_token, host=True)
        test.assertIs(type(await identity.revoke('revoke-state-writer', previous_writer.identity, previous_writer.revision, host=True)), Committed)
        status, confirmed_state = await request(port, '/api/host/operations/resolve',
            {'entry_id': 'entry', 'input': {'operation': 'set_state', 'input': state_input}}, confirmation_token)
        test.assertEqual(status, 200, confirmed_state)
        test.assertEqual(confirmed_state['data']['receipt']['commit_id'], changed['data']['receipt']['commit_id'])
        revoked = await identity.revoke('revoke-http', principal.identity, principal.revision, host=True)
        test.assertIs(type(revoked), Committed, revoked)
        status, denied = await request(port, '/api/host/state', {'entry_id': 'entry', 'input': {}}, token)
        test.assertEqual(status, 401, denied)
        await exercise_administrator_http(test, application, port)
    finally:
        test.assertTrue(await http.close())
        test.assertTrue(await application.logging.close())


async def exercise_administrator_http(test, application, port: int):
    identity = application.identity
    await identity.establish('http-administrator', application.resources.read_secret('bootstrap').decode(), 'synthetic-administrator-password')
    _, session = await identity.login('http-administrator-login', 'synthetic-administrator-password')
    assert session is not None
    async def call(path, payload):
        return await request(port, path, payload, '', origin=f'http://127.0.0.1:{port}', session=session)
    status, current_persona = await call('/api/persona/current', {})
    test.assertEqual(status, 200, current_persona)
    test.assertIn('认真倾听', json.dumps(current_persona, ensure_ascii=False))
    status, observed = await call('/api/administration/state', {})
    test.assertEqual(status, 200, observed)
    test.assertIn('合成活动', json.dumps(observed, ensure_ascii=False))
    status, goals = await call('/api/administration/goals', {})
    test.assertEqual(status, 200, goals)
    goal_input = {'operation_key': 'admin-goal-inject', 'content': '合成管理员目标',
        'subject_ids': ['self'], 'world_scope': 'REAL', 'deadline': None,
        'reminder_lead_seconds': None, 'route_id': None, 'source_id': 'admin-goal-source'}
    status, injected = await call('/api/administration/goals/inject', goal_input)
    test.assertEqual(status, 200, injected)
    test.assertEqual(injected['outcome'], 'COMMITTED', injected)
    _, repeated = await call('/api/administration/goals/inject', goal_input)
    test.assertEqual(repeated['data']['receipt']['commit_id'], injected['data']['receipt']['commit_id'])
    _, goals = await call('/api/administration/goals', {})
    goal = goals['data']['value']['items'][0]
    status, completed = await call('/api/administration/goals/status', {'operation_key': 'admin-goal-complete',
        'goal_id': goal['goal_id'], 'expected_revision': goal['revision'], 'status': 'COMPLETED'})
    test.assertEqual(status, 200, completed)
    _, goals = await call('/api/administration/goals', {})
    test.assertEqual(goals['data']['value']['items'], [])
    log_query = {'cursor': None, 'limit': 8, 'start': None, 'end': None, 'minimum_level': None,
        'modules': [], 'event_codes': [], 'entry_id': None, 'run_id': None, 'request_id': None, 'attempt_id': None}
    status, logs = await call('/api/logs', {'query': log_query})
    test.assertEqual(status, 200, logs)
    test.assertTrue(logs['data']['page']['value']['events'])
    test.assertNotIn('synthetic-administrator-password', json.dumps(logs))
    test.assertNotIn('合成管理员目标', json.dumps(logs, ensure_ascii=False))
    status, memories = await call('/api/memory/list', {'after': ''})
    test.assertEqual(status, 200, memories)
    test.assertEqual(memories['data']['items'], [])
    status, content = await call('/api/content/entries', {})
    test.assertEqual(status, 200, content)
    queue = content['data']['value']['rows'][0]['buffers']
    test.assertGreaterEqual(queue['normal_pending'], 1)
    test.assertEqual(queue['focus_pending'], 0)
    test.assertNotIn('合成宿主输入', json.dumps(content, ensure_ascii=False))
    host = application.business.host
    assert host is not None and host.runtime is not None and host.combination.dream is not None
    root = await host.combination.dream.schedule()
    assert root is not None
    start = {'key': 'admin-focused-start', 'run_id': 'admin-focused-run', 'expected_revision': root['revision'],
        'mode_epoch': host.runtime.gate.epoch, 'mode': 'FOCUSED'}
    status, started = await call('/api/dream/start', start)
    test.assertEqual(status, 200, started)
    test.assertEqual(started['outcome'], 'COMMITTED', started)
    test.assertEqual(host.runtime.gate.state, 'DREAM_FOCUSED')
    status, logs = await call('/api/logs', {'query': log_query})
    test.assertEqual(status, 200, logs)
    status, blocked = await call('/api/administration/state', {})
    test.assertEqual(status, 409, blocked)
    status, inspected = await call('/api/dream/inspect', {'run_id': 'admin-focused-run'})
    test.assertEqual(status, 200, inspected)
    run = inspected['data']['run']
    status, aborted = await call('/api/dream/abort', {'key': 'admin-focused-abort', 'run_id': run['run_id'],
        'expected_revision': run['revision'], 'mode_epoch': run['mode_epoch']})
    test.assertEqual(status, 200, aborted)
    test.assertEqual(host.runtime.gate.state, 'NORMAL')
    test.assertFalse(application.business.sends_enabled)
    status, logged_out = await call('/api/logout', {'key': 'admin-http-logout'})
    test.assertEqual(status, 200, logged_out)
    test.assertEqual(logged_out['outcome'], 'COMMITTED', logged_out)
    status, denied = await call('/api/logs', {'query': log_query})
    test.assertEqual(status, 401, denied)
