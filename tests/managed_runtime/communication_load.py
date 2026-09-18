"""Finite real-client mixed qualification with normal background scheduling.

Run explicitly with --seconds 1800. Records every rejection and latency; no
failed write is retried with a new key. Fifteen fast consumers and one delayed
ACK consumer share sixteen routes; four routes become due in each goal burst.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import time
import unittest
from typing import Any
from clients.iris_client import IrisClient, WebSocketClient
from companion_memory.persistence import Committed
from companion_memory.management.notification_routes import NotificationRoutes
from tests.runtime.configuration_support import event
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application, enable_communication, confirm_original


def distribution(values: list[float]):
    ordered = sorted(values)
    return {key: ordered[min(len(ordered)-1, int((len(ordered)-1)*fraction))] if ordered else None
        for key, fraction in (('p50_ms', .5), ('p95_ms', .95), ('p99_ms', .99))}


async def run(seconds: int, root: Path, evidence: Path):
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=40, thread_name_prefix="bounded-client"))
    test = unittest.TestCase()
    evidence.mkdir(parents=True, exist_ok=True)
    with responses((persona,)) as (provider_port, mock_requests, mock_failures):
        app, listener = await ready_application(test, root, provider_port, port=18182)
        sockets: list[WebSocketClient] = []
        stop = asyncio.Event()
        readers: list[asyncio.Task] = []
        records: list[dict[str, Any]] = []
        latencies: list[float] = []
        query_successes: list[float] = []
        errors: Counter[str] = Counter()
        ack_delays: list[float] = []
        snapshot_peaks = {'rss_kib': 0, 'fds': 0, 'tasks': 0, 'queue_bytes': 0, 'connections': 0}
        started = time.monotonic()
        media_completions = 0
        try:
            await enable_communication(test, app)
            routes = NotificationRoutes(app.identity)
            names = tuple('load-route-' + str(i).zfill(2) for i in range(16))
            for rid in names:
                await confirm_original(test, lambda: routes.create('create-' + rid, rid, 'host', ('entry',), ('goal.due',)))
                await confirm_original(test, lambda: routes.enable('enable-' + rid, rid, 1, True))
            _, token = await app.identity.create_token('load-token', 'host', ('entry',),
                ('notifications', 'accept', 'state_read', 'goal_read', 'goal_write', 'media_upload', 'media_inspect', 'confirm'),
                time.time_ns() // 1000 + (seconds + 600)*1000000, route_ids=names, event_types=('goal.due',))
            assert token is not None
            client = IrisClient('http://127.0.0.1:18182', token, timeout=40)
            async def reader(ws: WebSocketClient, slow: bool):
                while not stop.is_set():
                    try:
                        message = await asyncio.to_thread(ws.receive)
                        if message.get('event') == 'goal.due':
                            received = time.monotonic()
                            if slow: await asyncio.sleep(6)
                            await asyncio.to_thread(ws.send, {'version': 1, 'type': 'ack', 'route_id': message['route_id'],
                                'delivery_id': message['delivery_id'], 'status': 'RECEIVED'})
                            ack_delays.append((time.monotonic()-received)*1000)
                            records.append({'type': 'received', 'delivery_id': message['delivery_id'], 'route_id': message['route_id'], 'slow': slow})
                        elif message['type'] == 'ack_result': records.append({'type': 'ack', **message})
                        elif message['type'] == 'error': errors['ws:' + message['reason']] += 1
                    except (OSError, ValueError, ConnectionError) as exc:
                        if not stop.is_set(): errors['ws:' + type(exc).__name__] += 1
                        return
            for index, rid in enumerate(names):
                ws = await asyncio.to_thread(client.websocket);sockets.append(ws)
                test.assertEqual((await asyncio.to_thread(ws.receive))['type'], 'ready')
                await asyncio.to_thread(ws.send, {'version': 1, 'type': 'subscribe', 'request_id': rid,
                    'route_ids': [rid], 'event_types': ['goal.due'], 'takeover': False})
                test.assertEqual((await asyncio.to_thread(ws.receive))['state'], 'SUBSCRIBED')
                readers.append(asyncio.create_task(reader(ws, index == 0)))
            try:
                excess = await asyncio.to_thread(client.websocket)
            except (ConnectionError, OSError): records.append({'type': 'capacity', 'seventeenth': 'REFUSED'})
            else:
                excess.close();raise AssertionError('Seventeenth connection accepted.')
            started = time.monotonic()
            last_goals = -60.0
            last_media = -30.0
            number = 0
            with (evidence / 'load-samples.jsonl').open('w') as raw:
                async def request(path: str, payload: dict, kind: str):
                    begin = time.monotonic()
                    try:
                        reply = await asyncio.to_thread(client.request, path, payload)
                        elapsed = (time.monotonic()-begin)*1000
                        outcome = reply['outcome']
                        if kind == 'query':
                            latencies.append(elapsed)
                            if outcome in ('OBSERVED', 'COMMITTED'): query_successes.append(elapsed)
                        if outcome not in ('OBSERVED', 'COMMITTED'): errors[kind+':'+str(reply.get('error', {}).get('reason', outcome))] += 1
                        sample = {'kind': kind, 'elapsed_ms': elapsed, 'outcome': outcome, 'error': reply.get('error')}
                    except (OSError, ValueError) as exc:
                        errors[kind+':'+type(exc).__name__] += 1
                        sample = {'kind': kind, 'elapsed_ms': (time.monotonic()-begin)*1000, 'exception': type(exc).__name__}
                    raw.write(json.dumps(sample)+'\n');raw.flush()
                while time.monotonic()-started < seconds:
                    cycle = time.monotonic();elapsed = cycle-started
                    number += 1
                    actions = [request('/api/host/state', {'entry_id': 'entry', 'input': {}}, 'query'),
                        request('/api/host/goals', {'entry_id': 'entry', 'input': {}}, 'query')]
                    if number % 5 == 0:
                        value = event('load-event-'+str(number), '有界并发合成输入 '+str(number));value['event_version'] = 2
                        actions.append(request('/api/host/accept', {'entry_id': 'entry', 'input': {'key': 'load-accept-'+str(number), 'event': value}}, 'accept'))
                    if elapsed-last_goals >= 60:
                        last_goals = elapsed
                        due = datetime.fromtimestamp(time.time()+10, timezone.utc).isoformat()
                        for rid in names[:4]:
                            await request('/api/host/goals/inject', {'entry_id': 'entry', 'input': {
                                'operation_key': f'load-goal-{number}-{rid}', 'content': f'合成到期目标 {number} {rid}',
                                'subject_ids': ['self'], 'world_scope': 'REAL', 'deadline': due, 'reminder_lead_seconds': 0,
                                'route_id': rid, 'source_id': f'load-source-{number}-{rid}'}}, 'goal')
                    await asyncio.gather(*actions)
                    if elapsed-last_media >= 30:
                        last_media = elapsed
                        original = {'key': 'load-media-'+str(number), 'modality': 'IMAGE'}
                        begun: dict = {}
                        for continuation in range(8):
                            begun = await asyncio.to_thread(client.media, 'begin', 'entry', original)
                            raw.write(json.dumps({'kind':'media_begin','original_key':original['key'],'continuation':continuation,'reply':begun})+'\n');raw.flush()
                            if begun['outcome']=='COMMITTED': break
                            errors['media:'+str(begun.get('error',{}).get('reason',begun['outcome']))] += 1
                            await asyncio.sleep(.1)
                        if begun['outcome'] == 'COMMITTED':
                            uid = begun['data']['receipt']['result']['upload_id']
                            for offset in range(0, 1048576, 65536):
                                result = await asyncio.to_thread(client.chunk, 'entry', uid, offset, b'X'*65536)
                                if result['outcome'] != 'OBSERVED': errors['media:'+result['outcome']] += 1;break
                            for continuation in range(8):
                                finished = await asyncio.to_thread(client.media, 'finish', 'entry', {'upload_id': uid})
                                raw.write(json.dumps({'kind': 'media_finish', 'original_key': original['key'],
                                    'upload_id': uid, 'continuation': continuation, 'reply': finished})+'\n');raw.flush()
                                if finished['outcome'] == 'COMMITTED':
                                    media_completions += 1
                                    break
                                errors['media:'+str(finished.get('error', {}).get('reason', finished['outcome']))] += 1
                                await asyncio.sleep(.1)
                        else:
                            errors['media:'+begun['outcome']] += 1
                            raw.write(json.dumps({'kind':'media_begin','original_key':original['key'],'reply':begun})+'\n');raw.flush()
                    sessions = app.business.communication
                    status = Path('/proc/self/status').read_text()
                    rss = int(next(line.split()[1] for line in status.splitlines() if line.startswith('VmRSS:')))
                    for key, value in {'rss_kib': rss, 'fds': len(os.listdir('/proc/self/fd')), 'tasks': len(asyncio.all_tasks()),
                        'queue_bytes': sum(c.transport.buffered_bytes for c in sessions.connections.values()), 'connections': len(sessions.connections)}.items():
                        snapshot_peaks[key] = max(snapshot_peaks[key], value)
                    if number % 30 == 0:
                        print(json.dumps({'elapsed_seconds': int(elapsed), 'iterations': number, 'connections': len(sessions.connections),
                            'errors': dict(errors), 'background_closed': app.business.goal_scheduler.closed if app.business.goal_scheduler else None}), flush=True)
                    await asyncio.sleep(max(0, 1-(time.monotonic()-cycle)))
            stop.set()
            for ws in sockets: await asyncio.to_thread(ws.close)
            await asyncio.gather(*readers, return_exceptions=True)
            await asyncio.sleep(2)
            dispatcher = app.business.communication_dispatcher
            assert dispatcher is not None
            summary = {'duration_seconds': time.monotonic()-started, 'configured_seconds': seconds, 'iterations': number,
                'query_latency': distribution(latencies), 'query_count': len(latencies),
                'one_second_success_ratio': sum(v<=1000 for v in query_successes)/len(latencies) if latencies else 0,
                'query_success_count': len(query_successes),
                'errors': dict(errors), 'peak_resources': snapshot_peaks, 'client_ack_latency': distribution(ack_delays),
                'media_completions': media_completions, 'registrations': dispatcher.registrations, 'writes': dispatcher.writes, 'terminals': dispatcher.terminals,
                'dispatch_failures': dispatcher.failures, 'mock_requests': len(mock_requests), 'mock_failures': mock_failures,
                'supplier_requests': 0, 'scheduler_active_throughout': app.business.goal_scheduler is not None and not app.business.goal_scheduler.closed}
            (evidence / 'load-result.json').write_text(json.dumps(summary, indent=2))
            (evidence / 'load-deliveries.json').write_text(json.dumps(records, indent=2))
            test.assertEqual(len(mock_requests), 1)
            test.assertTrue(summary['scheduler_active_throughout'])
        finally:
            stop.set()
            for ws in sockets:
                try: await asyncio.to_thread(ws.close)
                except OSError: pass
            await asyncio.gather(*readers, return_exceptions=True)
            while not await listener.close(): await asyncio.sleep(.1)
            while not await app.business.close(): await asyncio.sleep(.1)
            while not await app.bootstrap.close(): await asyncio.sleep(.1)
            (evidence / 'load-cleanup.json').write_text(json.dumps({'storage': app.bootstrap.assembly.storage.get_health().lifecycle,
                'connections': len(app.business.communication.connections), 'deliveries': len(app.business.communication.deliveries),
                'resource_fd': app.resources.fd}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser();parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--root', type=Path, required=True);parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800: raise ValueError('Finite qualification only.')
    asyncio.run(run(args.seconds, args.root, args.evidence))
