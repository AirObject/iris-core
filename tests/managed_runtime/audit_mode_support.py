"""Real committed audit reads remain independent of ordinary focus and write fences.

The synthetic host enters and exits focus through its native dream port. The
only delays are explicit barriers around native owners and first HTTP delivery;
SQLite records, receipts, permissions and cleanup are never replaced with mocks.
"""
import asyncio
from contextlib import closing
import json
import sqlite3
import time
from unittest.mock import patch

from companion_memory.information.management import HostIdentity
from companion_memory.information.errors import InformationRejected
from tests.information.test_queries import query
from companion_memory.dream.port import OPERATIONS
from companion_memory.memory.service import MemoryError
from companion_memory.persistence import Committed


async def exercise_audit_modes(test, app, http, call, grant_path, reference, historical, object_id):
    host = app.business.host
    assert host is not None and host.runtime is not None
    runtime = host.runtime
    gate = runtime.gate
    dream = await host.bind_dream(HostIdentity(
        "audit-observation-focus", "synthetic-reviewer", "host", "entry", OPERATIONS, (), time.monotonic() + 300))
    original_grant = grant_path.read_bytes()
    routes = (('/api/audit/operation', reference), ('/api/audit/history', historical))
    sequence = 0
    ordinary = await host.bind_query(HostIdentity(
        "audit-ordinary-query", "synthetic-host", "host", "entry", frozenset(("search_memory",)), (), time.monotonic() + 300))

    def database_snapshot():
        with closing(sqlite3.connect(app.resources.root / 'db/memory.sqlite3')) as connection:
            return tuple(connection.iterdump())

    async def start_focus():
        nonlocal sequence
        sequence += 1
        schedule = await host.combination.dream.schedule()
        assert schedule is not None
        run = 'audit-observation-' + str(sequence)
        result = await dream.start_dream(run, run, schedule['revision'], gate.epoch, mode='FOCUSED')
        test.assertIs(type(result), Committed, result)
        return run

    async def finish_focus(run):
        value = await dream.inspect_dream(run)
        result = await dream.abort_dream(run + '-abort', run, value['revision'], value['mode_epoch'])
        test.assertIs(type(result), Committed, result)
        test.assertEqual(gate.state, 'NORMAL')

    async def inspect_committed():
        before = database_snapshot()
        for path, payload in routes:
            code, body, _ = await call(path, payload)
            test.assertEqual(code, 200, body)
            if path.endswith('history'):
                test.assertEqual(body['data']['value']['previous_value']['object_id'], object_id)
            else:
                test.assertEqual(body['data']['status'], 'FOUND')
                test.assertTrue(body['data']['records'])
            denied = []
            for key in ('absent', reference['operation_key'] + '-outside'):
                code, body, _ = await call(path, payload | {'operation_key': key})
                test.assertEqual(code, 403, body)
                test.assertNotIn('data', body)
                denied.append(body)
            test.assertEqual(*denied)
        test.assertEqual(database_snapshot(), before, 'Audit inspection must not strengthen, restore or write any record.')

    async def ordinary_reads_stay_blocked():
        read = runtime.memory.bind_read((object_id,), ('get_current',))
        result = await read.get_current(object_id)
        test.assertIs(type(result), MemoryError, result)
        test.assertEqual(result.reason, 'DREAMING')
        test.assertFalse(hasattr(read, 'read_source_manifest'))
        test.assertFalse(hasattr(read, 'read_object_history'))
        recalled = await ordinary.search_memory(query('audit-focused-denied-' + str(sequence)))
        test.assertIs(type(recalled), InformationRejected, recalled)
        test.assertEqual(recalled.error.reason, 'DREAMING', recalled)

    # Occupy the actual publication fence used by normal information writers.
    gate.begin_information_change('audit-concurrent-writer')
    try:
        test.assertEqual(gate.state, 'NORMAL')
        test.assertIsNone(gate.information_checkpoint())
        app.admission.exclusive = True
        try:
            await asyncio.wait_for(inspect_committed(), 10)
        finally:
            app.admission.exclusive = False
        test.assertIsNone(gate.information_checkpoint(), 'Audit cannot release the business writer slot.')
    finally:
        gate.finish_information_change('audit-concurrent-writer')

    # Hold the native transition after its PREPARING transaction, before drain.
    entered, release = asyncio.Event(), asyncio.Event()
    drain = runtime.focus.drain
    async def delayed_drain(run):
        entered.set()
        await release.wait()
        return await drain(run)
    with patch.object(runtime.focus, 'drain', new=delayed_drain):
        starting = asyncio.create_task(start_focus())
        try:
            await asyncio.wait_for(entered.wait(), 10)
            test.assertEqual(gate.state, 'DREAM_PREPARING')
            mode = (await runtime.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'}))[0]
            test.assertEqual(mode['state'], 'DREAM_PREPARING')
            await inspect_committed()
            await ordinary_reads_stay_blocked()
        finally:
            release.set()
            run = await starting
    try:
        test.assertEqual(gate.state, 'DREAM_FOCUSED')
        await inspect_committed()
        await ordinary_reads_stay_blocked()
    finally:
        await finish_focus(run)

    # The real native read finishes in NORMAL, then focus commits before send.
    send = http.send
    for path, payload in routes:
        for invalidation in ('NONE', 'REVOKED', 'EXPIRED'):
            run = None
            async def switch_before_delivery(writer, status, body, **kwargs):
                nonlocal run
                if kwargs.get('authorize') is not None:
                    run = await start_focus()
                    if invalidation == 'REVOKED':
                        grant_path.unlink()
                    elif invalidation == 'EXPIRED':
                        grant = json.loads(original_grant)
                        grant['expires_at_us'] = 1
                        grant_path.write_text(json.dumps(grant))
                await send(writer, status, body, **kwargs)
            try:
                with patch.object(http, 'send', new=switch_before_delivery):
                    code, body, _ = await call(path, payload)
                test.assertIsNotNone(run)
                test.assertEqual(code, 200 if invalidation == 'NONE' else 403, body)
                if invalidation != 'NONE':
                    test.assertNotIn('data', body)
                    test.assertNotIn('records', json.dumps(body))
                    test.assertNotIn('previous_value', json.dumps(body))
                await ordinary_reads_stay_blocked()
            finally:
                grant_path.write_bytes(original_grant)
                if run is not None:
                    await finish_focus(run)

    # A stopped history owner retains its real outstanding task and refuses data.
    owner = host.assembly.history
    entered, release = asyncio.Event(), asyncio.Event()
    native_read = owner._read_object_history
    async def delayed_history(*args):
        entered.set()
        await release.wait()
        return await native_read(*args)
    with patch.object(owner, '_read_object_history', new=delayed_history):
        reading = asyncio.create_task(call('/api/audit/history', historical))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            test.assertFalse(owner.close(), 'Closing cannot release a still-running native history task.')
            test.assertFalse(reading.done())
        finally:
            release.set()
        code, body, _ = await reading
        test.assertGreaterEqual(code, 400, body)
        test.assertNotIn('previous_value', json.dumps(body))
    test.assertTrue(owner.close())
    code, body, _ = await call('/api/audit/history', historical)
    test.assertEqual(code, 409, body)
    test.assertEqual(body['outcome'], 'REJECTED')
    test.assertFalse(body['cleanup_pending'], 'The refused new request owns no native work.')
    test.assertNotIn('previous_value', json.dumps(body))
