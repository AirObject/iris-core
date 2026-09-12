"""Bounded local dataset construction under an independent resource supervisor.

All records use actual owners and SQLite. The candidate participant and Provider
are explicitly SYNTHETIC/SIMULATED. Two real generations are published and
physically retired; the bounded dataset does not prove large-scale performance.
"""
import asyncio
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.results import ExecutionResult
from companion_memory.persistence.service import OperationPort
from companion_memory.provider import SimulationAdapter
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record
from tests.information.host_support import host
from tests.information.test_http import exchange
from tests.information.test_queries import query
from tests.information.test_expiry import scores
from tests.provider.support import success
from tests.runtime.configuration_support import event


class ProfileStopped(Exception):
    """The parent denied another write; only confirmation and cleanup may proceed."""


async def run(root: Path, size: int, action: str) -> None:
    recovering = action != 'create'
    if size not in (0, 100, 1000): raise ValueError('Only the bounded local sizes are supported.')
    h = host(root, automatic=recovering)
    h.candidates = SyntheticCandidateInput('local_profile', ('北京公园看展', '北京周末阅读', '上海图书馆阅读', '周末广州运动'), 50)
    h.adapter = SimulationAdapter(tuple(success() for _ in range(max(1, size // 4))))
    original = OperationPort.execute
    lock = asyncio.Lock(); closing = False; ordinal = 0
    async def guarded(port: OperationPort, operation_key: object, local_command: object) -> ExecutionResult:
        nonlocal ordinal
        if not closing:
            async with lock:
                ordinal += 1
                print(json.dumps({'checkpoint': ordinal}), flush=True)
                answer = await asyncio.to_thread(sys.stdin.readline)
                if answer.strip() != 'ALLOW': raise ProfileStopped()
        return await original(port, operation_key, local_command)
    started = time.monotonic()
    with patch.object(OperationPort, 'execute', guarded):
        try:
            initialized = await h.initialize('OPEN_EXISTING' if recovering else 'CREATE_NEW')
            if type(initialized) is not Found: raise RuntimeError('Actual initialization failed: ' + repr(initialized))
            if h.runtime is None or h.retrieval is None: raise RuntimeError('Expected initialized owners.')
            if not recovering:
                for entry_id in ('entry', 'second_entry', 'third_entry'):
                    result = await h.register_entry('register:' + entry_id, entry_id, 'host', 'sample_platform', entry_id)
                    if type(result) is not Committed: raise RuntimeError('Actual entry registration failed.')
                entry = h.runtime.bind_entry('entry'); object_ids: list[str] = []
                for group in range(size // 4):
                    for offset in range(3 if group == 0 else 2):
                        key = 'learn:' + str(group) + ':' + str(offset)
                        raw = event(key, '北京周末的本地活动'); raw['event_version'] = 2
                        accepted = await entry.accept_event(key, raw)
                        if type(accepted) is not Committed: raise RuntimeError('Actual input was not committed: ' + repr(accepted))
                    learned = await entry.run_learning('learn:' + str(group))
                    if type(learned) is not Committed: raise RuntimeError('Actual learning was not committed: ' + repr(learned))
                    refs = record(learned.receipt.result)['object_refs']
                    if type(refs) is not tuple or len(refs) != 4: raise RuntimeError('Expected exactly four actual memory objects.')
                    for reference in refs:
                        oid = record(reference)['object_id']
                        if type(oid) is not str: raise RuntimeError('Expected actual memory identity.')
                        object_ids.append(oid)
                del entry
                for oid in sorted(object_ids)[:100]:
                    if type(await scores(h, oid, 19)) is not Committed: raise RuntimeError('Actual forgetting transition failed.')
                for entry_id in ('entry', 'second_entry', 'third_entry'):
                    entry = h.runtime.bind_entry(entry_id)
                    for offset in range(100):
                        key = 'pending:' + entry_id + ':' + str(offset)
                        raw = event(key, '只供当前入口的待处理输入'); raw['event_version'] = 2
                        result = await entry.accept_event(key, raw)
                        if type(result) is not Committed: raise RuntimeError('Actual pending input was not committed.')
                    del entry
                port = await h.bind_management(HostIdentity('goals', 'principal', 'host', 'entry', frozenset(('goal_inject_external',)), (), time.monotonic() + 300))
                for offset in range(100):
                    result = await port.execute('goal_inject_external', 'goal:' + str(offset), {'content': '本地待办 ' + str(offset),
                        'subject_ids': (), 'world_scope': 'REAL', 'deadline': None, 'reminder_lead_seconds': None, 'route_id': None, 'source_id': 'external:' + str(offset)})
                    if type(result) is not Committed: raise RuntimeError('Actual goal was not committed: ' + repr(result))
            else:
                if h.scheduler is None: raise RuntimeError('Expected native automatic maintenance after recovery.')
                local_index = h.retrieval; scheduler = h.scheduler
                async def wait_active() -> str:
                    async with asyncio.timeout(240):
                        while True:
                            coordinator, active = await local_index.query_generation()
                            goals_finished = h.goals is not None and not await h.goals.pending_tasks() and not await h.goals.running_tasks()
                            if (active is not None and coordinator['building_generation'] is None
                                    and not active['pending_count'] and not scheduler.index.jobs and goals_finished):
                                return str(active['generation_id'])
                            await asyncio.sleep(0.05)
                first = await wait_active()
                if action == 'recover':
                    rebuild = await h.bind_management(HostIdentity('profile-rebuild', 'principal', 'host', 'entry',
                        frozenset(('index_begin',)), (), time.monotonic() + 600))
                    begun = await rebuild.execute('index_begin', 'second-generation', {'expected_generation': first})
                    if type(begun) is not Committed: raise RuntimeError('Actual second generation was not registered: ' + repr(begun))
                    if size:
                        memory = h.assembly.memory.information
                        if memory is None: raise RuntimeError('Expected actual memory owner.')
                        existing = await memory.current_page()
                        # A real revision while both targets exist must be covered twice.
                        if type(await scores(h, str(existing[0]['object_id']), 18 if existing[0]['lifecycle'] == 'FORGOTTEN' else 49)) is not Committed:
                            raise RuntimeError('Actual concurrent rebuild mutation failed.')
                    second = await wait_active()
                    if first == second: raise RuntimeError('Expected a real second publication.')
                    async with asyncio.timeout(120):
                        while await h.retrieval.retirement_page() is not None: await asyncio.sleep(0.05)
            business = await h.bind_business(HostIdentity('http-profile', 'principal', 'host', 'entry',
                frozenset(('search_memory', 'deep_recall', 'prepare_reply', 'resolve_recall', 'record_usage', 'get_state_view', 'list_open_goals')), (), time.monotonic() + 300), include_forgotten=True)
            if h.http is None: raise RuntimeError('Expected actual HTTP owner.')
            address = await h.http.start(); token = h.http.issue_test_session(business, time.monotonic() + 300)
            status, response = await exchange(address, token, '/api/host/memory/search', query('query:' + str(recovering)))
            if status != 200: raise RuntimeError('Actual HTTP query failed: ' + repr((status, response)))
            deep_status, deep_response = await exchange(address, token, '/api/host/memory/deep-recall', query('deep:' + str(recovering)))
            if deep_status != 200: raise RuntimeError('Actual deep HTTP query failed: ' + repr((deep_status, deep_response)))
            print(json.dumps({'result': 'COMPLETED', 'recovering': recovering, 'objects_requested': size, 'elapsed_seconds': time.monotonic() - started,
                'model_calls': len(h.adapter.calls), 'operations': ordinal, 'http_status': status, 'response': response, 'deep_response': deep_response,
                'qualification': 'FUNCTIONAL_ACTIVE_DUAL_GENERATION' if recovering else 'FUNCTIONAL_DIRTY_CREATE', 'storage': 'ACTUAL', 'model': 'SIMULATED', 'candidate': 'SYNTHETIC'}, ensure_ascii=False), flush=True)
        finally:
            closing = True
            if not await h.close(): raise RuntimeError('Actual profile owner cleanup remains incomplete.')


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]).resolve(), int(sys.argv[2]), sys.argv[3]))
