"""Durable original grace under native focus, overlapping maintenance and reopen.

Only the trusted UTC source is controlled. SQLite, native mode transitions,
configuration activation, normal background scheduling and recovery are real.
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from typing import cast
from companion_memory.persistence.daily_records import Record
from unittest.mock import patch
from companion_memory.persistence import Committed, Ready
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.notification_routes import NotificationRoutes
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.goals.communication_ledger import grace_from
from companion_memory.runtime.communication_gate import clock_from
from companion_memory.memory.formats import record
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from clients.iris_client import IrisClient
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application, enable_communication, confirm_original
from .test_business import controlled_resources


class CommunicationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_window_focus_overlap_reopen_and_expiry(self):
        with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
            app, http = await ready_application(self, Path(directory)/'instance', provider_port, port=18183)
            now = [time.time_ns()]
            with patch('time.time_ns', side_effect=lambda: now[0]):
                try:
                    await enable_communication(self, app)
                    routes = NotificationRoutes(app.identity)
                    self.assertIs(type(await routes.create('create', 'offline', 'host', ('entry',), ('goal.due',))), Committed)
                    await confirm_original(self, lambda: routes.enable('enable', 'offline', 1, True))
                    _, token = await app.identity.create_token('token', 'host', ('entry',), ('goal_write', 'confirm'),
                        now[0]//1000+3600000000, route_ids=('offline',), event_types=('goal.due',))
                    assert token is not None
                    original = {'entry_id': 'entry', 'input': {'operation_key': 'expired-goal', 'content': '离线窗口保留',
                        'subject_ids': ['self'], 'world_scope': 'REAL', 'deadline': datetime.fromtimestamp(now[0]/1e9-1, timezone.utc).isoformat(),
                        'reminder_lead_seconds': 0, 'route_id': 'offline', 'source_id': 'original-goal'}}
                    result = await asyncio.to_thread(IrisClient('http://127.0.0.1:18183', token).request, '/api/host/goals/inject', original)
                    self.assertEqual(result['outcome'], 'COMMITTED', result)
                    host = app.business.host
                    assert host is not None and host.runtime is not None
                    ledger = host.combination.communication_ledger
                    clock = host.assembly.communication_gate
                    assert ledger is not None and clock is not None
                    async def wait_grace() -> Record:
                        assert ledger is not None
                        deadline = time.monotonic()+15
                        while time.monotonic()<deadline:
                            rows = await ledger.rows.page('communication_grace', '')
                            if rows: return rows[0]
                            await asyncio.sleep(.1)
                        self.fail('Normal background scheduler did not start offline grace.')
                    original_grace = await wait_grace()
                    window = grace_from(record(original_grace['value']))
                    self.assertEqual(window.started_us, now[0]//1000)
                    dream = await host.bind_dream(HostIdentity('focus-port', 'reviewer', 'host', 'entry', OPERATIONS, (), time.monotonic()+300))
                    now[0] += 100000000000
                    assert host.combination.dream is not None
                    schedule = await host.combination.dream.schedule()
                    assert schedule is not None
                    focused = await dream.start_dream('focus', 'focus', cast(int, schedule['revision']), host.runtime.gate.epoch, mode='FOCUSED')
                    self.assertIs(type(focused), Committed, focused)
                    now[0] += 20000000000
                    await clock.maintenance('overlap', True)
                    now[0] += 400000000000
                    state = await dream.inspect_dream('focus')
                    assert state is not None
                    self.assertIs(type(await dream.abort_dream('abort', 'focus', cast(int, state['revision']), cast(int, state['mode_epoch']))), Committed)
                    self.assertTrue(await ledger.synchronize())
                    observed = await ledger.rows.read('communication_clock', 'communication-clock')
                    assert observed is not None
                    self.assertEqual(window.remaining(clock_from(record(observed['value'])), now[0]//1000), (200000000, None))
                    # Reopen while a retained maintenance owner still holds the clock.
                    settings = app.bootstrap.settings
                    self.assertTrue(await http.close())
                    while not await app.business.close(): await asyncio.sleep(.05)
                    self.assertTrue(await app.bootstrap.close())
                    now[0] += 400000000000
                    bootstrap = ManagedBootstrap(settings)
                    self.assertIs(type(await bootstrap.open()), Ready)
                    app = ManagedApplication(bootstrap, resource_factory=controlled_resources(provider_port))
                    await app.business.recover()
                    if app.business.task is not None: await app.business.task
                    host = app.business.host
                    assert host is not None
                    clock = host.assembly.communication_gate;ledger = host.combination.communication_ledger
                    assert clock is not None and ledger is not None
                    self.assertEqual(await ledger.rows.read('communication_grace', window.plan_id), original_grace)
                    self.assertTrue(await ledger.synchronize())
                    observed = await ledger.rows.read('communication_clock', 'communication-clock')
                    assert observed is not None
                    self.assertEqual(window.remaining(clock_from(record(observed['value'])), now[0]//1000), (200000000, None))
                    await clock.maintenance('overlap', False)
                    self.assertTrue(await ledger.synchronize())
                    now[0] += 50000000000
                    observed = await ledger.rows.read('communication_clock', 'communication-clock')
                    assert observed is not None
                    self.assertEqual(window.remaining(clock_from(record(observed['value'])), now[0]//1000)[0], 150000000)
                    while not await app.business.close(): await asyncio.sleep(.05)
                    self.assertTrue(await app.bootstrap.close())
                    now[0] += 151000000000
                    bootstrap = ManagedBootstrap(settings)
                    self.assertIs(type(await bootstrap.open()), Ready)
                    app = ManagedApplication(bootstrap, resource_factory=controlled_resources(provider_port))
                    await app.business.recover()
                    if app.business.task is not None: await app.business.task
                    host = app.business.host
                    assert host is not None and host.combination.communication_ledger is not None
                    ledger = host.combination.communication_ledger
                    self.assertEqual(await ledger.rows.read('communication_grace', window.plan_id), original_grace)
                    observed = await ledger.rows.read('communication_clock', 'communication-clock')
                    assert observed is not None
                    self.assertEqual(window.remaining(clock_from(record(observed['value'])), now[0]//1000)[0], 0)
                    # A DUE plan that first becomes eligible inside a long focus
                    # period starts its window only after the native gate reopens.
                    original['input'] = {**original['input'], 'operation_key': 'future-goal', 'source_id': 'future-source',
                        'content': '专注期间首次到期', 'deadline': datetime.fromtimestamp(now[0]/1e9+10, timezone.utc).isoformat()}
                    principal = await app.identity.authenticate(token, host=True)
                    injected = await app.host_http.dispatch(principal, 'POST', '/api/host/goals/inject', original)
                    self.assertIs(type(injected), Committed, injected)
                    assert host.runtime is not None and host.goals is not None
                    dream = await host.bind_dream(HostIdentity('focus-future-port', 'reviewer', 'host', 'entry', OPERATIONS, (), time.monotonic()+300))
                    assert host.combination.dream is not None
                    schedule = await host.combination.dream.schedule()
                    assert schedule is not None
                    self.assertIs(type(await dream.start_dream('future-focus', 'future-focus', cast(int, schedule['revision']), host.runtime.gate.epoch, mode='FOCUSED')), Committed)
                    now[0] += 400000000000
                    dispatcher = app.business.communication_dispatcher
                    assert dispatcher is not None
                    await dispatcher.tick()
                    self.assertEqual(len(await ledger.rows.page('communication_grace', '')), 1)
                    state = await dream.inspect_dream('future-focus')
                    assert state is not None
                    self.assertIs(type(await dream.abort_dream('future-abort', 'future-focus', cast(int, state['revision']), cast(int, state['mode_epoch']))), Committed)
                    windows: tuple[Record, ...] = ()
                    for _ in range(100):
                        windows = await ledger.rows.page('communication_grace', '')
                        if len(windows) == 2: break
                        await asyncio.sleep(.1)
                    self.assertEqual(len(windows), 2)
                    new_window = next(grace_from(record(w['value'])) for w in windows if w['object_id'] != window.plan_id)
                    self.assertEqual(new_window.started_us, now[0]//1000)
                    self.assertEqual(new_window.duration_us, 300000000)
                    self.assertEqual(app.business.communication.ack_received, 0)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(failures, [])
                    print({'original_duration_us': 300000000, 'paused_remaining_us': 200000000,
                        'overlap_and_paused_reopen': 'PRESERVED', 'active_downtime': 'EXPIRED', 'supplier_requests': 0})
                finally:
                    await http.close()
                    while not await app.business.close(): await asyncio.sleep(.05)
                    await app.bootstrap.close()
