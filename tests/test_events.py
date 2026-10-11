"""Read-only reminders, bounded resume cursors and server-driven shutdown."""
import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from threading import Event, Thread

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import authorize_host, msg
from iris.api import create_app
from iris.events import EventHub, EventResponse, drain_signals
from iris.goals import Goals
from iris.tokens import TokenError, Tokens
from test_host_api_contract import error


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


@pytest.fixture
def setup(store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    msg(store, 0, '入口 A', entry='A')
    msg(store, 1, '入口 B', entry='B')
    msg(store, 2, '入口 C', entry='group:C')
    tokens = Tokens(store)

    def principal(scope=None):
        value = tokens.create(host='sse-test', scope=scope or {'kind': 'all'})
        return tokens.authenticate('Bearer ' + value['token'])

    goals = Goals(store, clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))

    def publish(content, entry_id=None):
        goal = goals.create(content=content, entry_id=entry_id, deadline='2020-01-01')['goal']
        with store.read() as conn:
            return dict(conn.execute('SELECT * FROM notifications WHERE goal_id=?', (goal['id'],)).fetchone())

    return principal, publish, goals


def reminders(frames):
    return [frame.data for frame in frames if frame.event == 'reminder']


def test_read_only_scope_filter_replay_and_pull_do_not_double_take(store, setup):
    principal, publish, goals = setup
    first = publish('整理望远镜镜片', 'A')
    secret = publish('修理自行车刹车', 'B')
    global_note = publish('准备出行行李')
    clock = Clock()
    hub = EventHub(store, clock=clock)
    who = principal({'kind': 'entries', 'entries': ['A']})
    stream = hub.open(who)
    frames = hub.poll(stream)
    assert [n['id'] for n in reminders(frames)] == [first['id'], global_note['id']]
    assert all(n['status'] == 'pending' and n['taken_at'] is None for n in reminders(frames))
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE status='pending'").fetchone()[0] == 3
    resume = frames[0].id
    hub.close(stream)
    replay = hub.open(who, resume)
    assert [n['id'] for n in reminders(hub.poll(replay))] == [global_note['id']]
    pulled = goals.pull(scope=who.scope)
    assert [n['id'] for n in pulled['items']] == [first['id'], global_note['id']]
    stamp = pulled['items'][0]['taken_at']
    again = goals.pull(scope=who.scope)
    assert again['items'] == pulled['items'] and again['items'][0]['taken_at'] == stamp
    hub.close(replay)
    replay = hub.open(who, resume)
    assert reminders(hub.poll(replay)) == [pulled['items'][1]]
    with store.read() as conn:
        assert conn.execute('SELECT status FROM notifications WHERE id=?', (secret['id'],)).fetchone()[0] == 'pending'
    hub.close(replay)


def test_prefix_scope_and_current_cancelled_or_reassigned_reminder_not_replayed(store, setup):
    principal, publish, _ = setup
    note = publish('预约图书馆活动', 'group:C')
    publish('检查汽车轮胎', 'B')
    hub = EventHub(store)
    who = principal({'kind': 'prefix', 'prefix': 'group:'})
    stream = hub.open(who)
    assert [n['id'] for n in reminders(hub.poll(stream))] == [note['id']]
    hub.close(stream)
    with store.write() as conn:
        conn.execute("UPDATE goals SET entry_id='B' WHERE id=?", (note['goal_id'],))
    stream = hub.open(who)
    assert reminders(hub.poll(stream)) == []
    hub.close(stream)
    with store.write() as conn:
        conn.execute("UPDATE notifications SET status='cancelled' WHERE id=?", (note['id'],))
    stream = hub.open(principal())
    assert note['id'] not in [n['id'] for n in reminders(hub.poll(stream))]
    hub.close(stream)


def test_heartbeat_monotonic_ids_cursor_window_and_restart(store, setup):
    principal, publish, _ = setup
    clock = Clock()
    hub = EventHub(store, clock=clock, replay_seconds=30, cursor_limit=3)
    who = principal()
    stream = hub.open(who)
    first = hub.poll(stream)
    assert [frame.event for frame in first] == ['heartbeat']
    clock.value = 14
    assert hub.poll(stream) == []
    clock.value = 15
    second = hub.poll(stream)
    assert second[0].event == 'heartbeat'
    assert int(second[0].id.split(':')[1]) > int(first[0].id.split(':')[1])
    note = publish('购买新的笔记本', 'A')
    hub.close(stream)
    stream = hub.open(who, first[0].id)
    assert reminders(hub.poll(stream))[0]['id'] == note['id']
    hub.close(stream)
    clock.value = 50
    expired = hub.open(who, first[0].id)
    assert hub.poll(expired)[0].event == 'resync'
    hub.close(expired)
    restarted = EventHub(store, clock=clock)
    stream = restarted.open(who, second[0].id)
    assert restarted.poll(stream)[0].event == 'resync'
    restarted.close(stream)


def test_resume_window_by_count_and_cursor_token_binding(store, setup):
    principal, publish, _ = setup
    hub = EventHub(store, notification_window=2, cursor_limit=2)
    who = principal()
    stream = hub.open(who)
    cursor = hub.poll(stream)[0].id
    hub.close(stream)
    publish('借阅一本植物图鉴', 'A')
    publish('购买火车票', 'A')
    publish('清洗厨房油烟机', 'A')
    stream = hub.open(who, cursor)
    assert hub.poll(stream)[0].event == 'resync'
    hub.close(stream)
    stream = hub.open(who)
    frames = hub.poll(stream)
    assert frames[0].event == 'resync'
    assert len(reminders(frames)) == 2
    assert len(hub._cursors) <= 2
    hub.close(stream)
    stream = hub.open(principal(), frames[-1].id)
    assert hub.poll(stream)[0].event == 'resync'
    hub.close(stream)


def test_connection_limits_revocation_and_timeouts(store, setup):
    principal, publish, _ = setup
    clock = Clock()
    hub = EventHub(store, clock=clock, per_token_limit=1, global_limit=2, idle_seconds=30, max_seconds=60)
    a, b, c = principal(), principal(), principal()
    first, second = hub.open(a), hub.open(b)
    for who, code in ((a, 'event_token_limit'), (c, 'event_global_limit')):
        with pytest.raises(TokenError) as caught:
            hub.open(who)
        assert caught.value.status == 429 and caught.value.code == code and caught.value.retry_after
    hub.close(first)
    hub.close(first)  # Disconnect cleanup is idempotent.
    assert hub.status()['connections'] == 1
    Tokens(store).revoke(b.id)
    assert hub.poll(second) == [] and second.closed
    assert hub.status()['connections'] == 0
    stream = hub.open(c)
    clock.value = 30
    assert hub.poll(stream) == [] and stream.closed
    stream = hub.open(c)
    clock.value = 50
    publish('记录家中水表读数', 'A')
    hub.poll(stream)
    clock.value = 70
    publish('更新图书借阅日期', 'A')
    hub.poll(stream)
    clock.value = 90
    assert hub.poll(stream) == [] and stream.closed
    hub.shutdown()
    with pytest.raises(TokenError) as caught:
        hub.open(a)
    assert caught.value.status == 503


def test_indexed_read_snapshots_no_writer_lock_and_safe_statistics(store, setup, caplog):
    principal, publish, _ = setup
    note = publish('正文不应进入日志 SSE-private-prose', 'A')
    hub = EventHub(store)
    who = principal()
    caplog.set_level(logging.INFO, logger='iris.events')
    stream = hub.open(who)
    # Holding the single-writer lock must not prevent a reader in another thread.
    done = Event()
    frames = []
    def read():
        frames.extend(hub.poll(stream))
        done.set()
    with store.write() as conn:
        thread = Thread(target=read)
        thread.start()
        assert done.wait(1)
        plans = conn.execute('EXPLAIN QUERY PLAN SELECT n.* FROM notifications n LEFT JOIN goals g '
                             'ON g.id=n.goal_id WHERE n.id>? AND n.id<=? ORDER BY n.id LIMIT 100', (0, note['id'])).fetchall()
    thread.join(1)
    assert any('INTEGER PRIMARY KEY' in row['detail'] for row in plans)
    for frame in frames:
        hub.sent(frame)
    assert hub.status()['reminders_pushed'] == 1
    hub.close(stream)
    assert hub.status()['connections'] == 0
    assert 'connections=' in caplog.text and 'reminders_pushed=1' in caplog.text
    assert note['content'] not in caplog.text and who.host not in caplog.text and who.id not in caplog.text


def test_response_disconnect_and_shutdown_release_slots(store, setup):
    principal, _, _ = setup
    async def run():
        hub = EventHub(store)
        stream = hub.open(principal())
        response = EventResponse(hub, stream)
        started = asyncio.Event()
        output = []
        async def send(message):
            output.append(message)
            if message['type'] == 'http.response.body':
                started.set()
        async def receive():
            await asyncio.Event().wait()
        task = asyncio.create_task(response({'type': 'http'}, receive, send))
        await asyncio.wait_for(started.wait(), 1)
        hub.shutdown()
        await asyncio.wait_for(task, 1)
        assert hub.status()['connections'] == 0
        assert output[-1]['type'] == 'http.response.body' and not output[-1].get('more_body')
    asyncio.run(run())


@pytest.fixture
def client(store, monkeypatch):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1') as client:
        client.app.state.scheduler.stop()
        authorize_host(client)
        # TestClient buffers responses. End after one real read batch, without
        # relying on a millisecond wall-clock deadline under CI contention.
        hub = client.app.state.events
        async def one_batch(stream):
            for frame in await asyncio.to_thread(hub.poll, stream):
                yield frame
        monkeypatch.setattr(hub, 'frames', one_batch)
        yield client


def test_http_auth_host_cursor_validation_and_openapi_compatibility(client, setup):
    error(client.get('/api/v1/events', headers={'Authorization': ''}), 401)
    error(client.get('/api/v1/events', headers={'Host': 'evil.invalid'}), 400)
    for suffix, headers in (('?last_event_id=bad', {}), ('', {'Last-Event-ID': 'bad'}),
                            ('?last_event_id=', {}), ('?unknown=1', {})):
        error(client.get('/api/v1/events' + suffix, headers=headers), 400)
    operation = client.app.openapi()['paths']['/api/v1/events']['get']
    assert operation['security'] and '422' not in operation['responses']
    assert 'text/event-stream' in operation['responses']['200']['content']
    assert 'application/json' not in operation['responses']['200']['content']
    assert {p['name'] for p in operation['parameters']} >= {'last_event_id', 'Last-Event-ID'}
    for status in ('400', '401', '429', '503'):
        assert operation['responses'][status]['content']['application/json']['schema']
    _, publish, _ = setup
    note = publish('设置明天的闹钟', 'A')
    response = client.get('/api/v1/events')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert response.headers['cache-control'] == 'no-store'
    frames = [dict(line.split(': ', 1) for line in frame.splitlines()) for frame in response.text.strip().split('\n\n')]
    reminder = next(f for f in frames if f['event'] == 'reminder')
    assert json.loads(reminder['data'])['id'] == note['id']
    for headers, suffix in (({'Last-Event-ID': reminder['id']}, ''), ({}, '?last_event_id=' + reminder['id'])):
        resumed = client.get('/api/v1/events' + suffix, headers=headers)
        assert resumed.status_code == 200 and 'event: reminder' not in resumed.text
    stats = client.get('/api/v1/events/status').json()
    assert stats['connections'] == 0 and stats['reminders_pushed'] == 1


def test_http_limit_error_and_no_scope_bypass(client):
    hub = client.app.state.events
    who = client.app.state.tokens.authenticate(client.headers['Authorization'])
    streams = [hub.open(who) for _ in range(hub.per_token_limit)]
    try:
        e = error(client.get('/api/v1/events'), 429)
        assert e['code'] == 'event_token_limit'
    finally:
        for stream in streams:
            hub.close(stream)


def test_real_uvicorn_sigterm_drains_live_stream(store):
    value = Tokens(store).create(host='shutdown-test', scope={'kind': 'all'})
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    script = ('import sys, uvicorn; from iris.api import create_app; '
              'uvicorn.run(create_app(sys.argv[1], configs={}), host="127.0.0.1", '
              'port=int(sys.argv[2]), access_log=False, log_level="critical")')
    process = subprocess.Popen([sys.executable, '-c', script, str(store.path), str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env={k: v for k, v in os.environ.items() if not k.startswith('IRIS_')})
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'Authorization': 'Bearer ' + value['token']}, timeout=3) as client:
            deadline = time.monotonic() + 10
            while True:
                try:
                    if client.get('/api/v1/status').status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(.02)
            with client.stream('GET', '/api/v1/events') as response:
                lines = response.iter_lines()
                assert next(lines).startswith('id: ')
                created = client.post('/api/v1/goals', json={'content': '关闭前确认提醒流', 'deadline': '2020-01-01'})
                assert created.status_code == 201
                while next(lines) != 'event: reminder':
                    pass
                payload = json.loads(next(lines).removeprefix('data: '))
                assert payload['status'] == 'pending' and payload['taken_at'] is None
                pulled = client.get('/api/v1/notifications').json()['items']
                assert payload['id'] == pulled[0]['id'] and pulled[0]['status'] == 'taken'
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                list(lines)
                process.wait(timeout=3)
                assert time.monotonic() - started < 3
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_http_scope_matches_pull_including_global_goals(client, setup):
    _, publish, _ = setup
    visible = publish('核对电费缴费日期', 'A')
    hidden = publish('养护阳台多肉植物', 'B')
    shared = publish('准备周末徒步装备')
    token = client.app.state.tokens.create(host='scoped-stream', scope={'kind': 'entries', 'entries': ['A']})
    response = client.get('/api/v1/events', headers={'Authorization': 'Bearer ' + token['token']})
    assert response.status_code == 200
    assert visible['content'] in response.text and shared['content'] in response.text
    assert hidden['content'] not in response.text
    assert client.app.state.events.status()['connections'] == 0


def test_signal_observer_preserves_and_restores_server_handlers(store, monkeypatch):
    calls = []
    handlers = {sig: (lambda number, frame: calls.append(number)) for sig in (signal.SIGTERM, signal.SIGINT)}
    originals = dict(handlers)
    monkeypatch.setattr(signal, 'getsignal', handlers.get)
    monkeypatch.setattr(signal, 'signal', lambda sig, callback: handlers.__setitem__(sig, callback))
    async def run():
        hub = EventHub(store)
        with drain_signals(hub):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            await asyncio.sleep(0)
            assert hub.stopped.is_set()
            assert calls == [signal.SIGTERM]
        assert handlers == originals
    asyncio.run(run())


def test_shutdown_cancels_stream_blocked_at_outer_asgi_send(store, setup):
    from iris.events import EventDrainMiddleware
    principal, _, _ = setup
    async def run():
        hub = EventHub(store)
        response = EventResponse(hub, hub.open(principal()))
        entered = asyncio.Event()
        from types import SimpleNamespace
        middleware = EventDrainMiddleware(response, state=SimpleNamespace(events=hub))
        async def send(message):
            if message['type'] == 'http.response.body' and message.get('more_body'):
                entered.set()
                await asyncio.Event().wait()
        async def receive():
            await asyncio.Event().wait()
        task = asyncio.create_task(middleware({'type': 'http', 'method': 'GET', 'path': '/api/v1/events'}, receive, send))
        await asyncio.wait_for(entered.wait(), 1)
        hub.shutdown()
        await asyncio.wait_for(task, 1)
        assert hub.status()['connections'] == 0
    asyncio.run(run())


def test_http_resume_conflict_rejected_and_auth_failure_uses_no_slot(client):
    hub = client.app.state.events
    who = client.app.state.tokens.authenticate(client.headers['Authorization'])
    stream = hub.open(who)
    cursor = hub.poll(stream)[0].id
    hub.close(stream)
    other = cursor.split(':')[0] + ':9999'
    error(client.get('/api/v1/events?last_event_id=' + cursor + '&last_event_id=' + cursor), 400)
    error(client.get('/api/v1/events?last_event_id=' + cursor, headers={'Last-Event-ID': other}), 400)
    error(client.get('/api/v1/events/status', headers={'Authorization': ''}), 401)
    assert hub.status()['connections'] == 0


def test_disconnection_and_failed_logging_never_leak_slots(store, setup, monkeypatch):
    from iris import events
    principal, _, _ = setup
    monkeypatch.setattr(events.log, 'info', lambda *args: (_ for _ in ()).throw(OSError()))
    async def run():
        hub = EventHub(store)
        response = EventResponse(hub, hub.open(principal()))
        async def send(message):
            pass
        async def receive():
            return {'type': 'http.disconnect'}
        await asyncio.wait_for(response({'type': 'http'}, receive, send), 1)
        assert hub.status()['connections'] == 0
    asyncio.run(run())


def test_sse_json_escaping_and_storage_failure_end_without_ack(store, setup, monkeypatch):
    from iris.events import Frame
    frame = Frame('1', 'reminder', {'content': 'text\nevent: forged\ndata: attack'})
    assert frame.encode().count(b'\nevent: ') == 1
    principal, _, _ = setup
    hub = EventHub(store)
    stream = hub.open(principal())
    import sqlite3
    monkeypatch.setattr(store, 'read', lambda: (_ for _ in ()).throw(sqlite3.OperationalError('not logged')))
    assert hub.poll(stream) == [] and stream.closed
    assert hub.status()['connections'] == 0
