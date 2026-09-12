"""Isolated process death around actual ticket and feedback commits.

Only disposable test databases are used; model responses and learned content
are explicitly simulated by the shared native host fixture.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record
from companion_memory.persistence import Found, Committed
from tests.information.host_support import host, learn_one
from tests.information.test_feedback_boundaries import payload
from tests.information.test_queries import query
from tests.persistence.support import Hooks


async def run(root: Path, action: str) -> None:
    hooks = Hooks(); h = host(root, hooks.connect)
    try:
        initialized = await h.initialize('CREATE_NEW' if action == 'setup' else 'OPEN_EXISTING')
        if type(initialized) is not Found: raise RuntimeError('Isolated initialization failed: ' + repr(initialized))
        if action == 'setup': await learn_one(h)
        port = await h.bind_business(HostIdentity('process', 'principal', 'host', 'entry',
            frozenset(('search_memory', 'resolve_recall', 'record_usage')), (), time.monotonic() + 300))
        armed = False
        staged = False
        def before(sql: str) -> None:
            nonlocal staged
            table = 'memory_usage_receipt' if action.startswith('usage_') else 'retrieval_ticket'
            if armed and sql.startswith('INSERT INTO ' + table + ' '): staged = True
            if staged and sql == 'COMMIT' and action.endswith('_before'): os._exit(71)
        def after(sql: str) -> None:
            if staged and sql == 'COMMIT' and action.endswith('_after'): os._exit(72)
        hooks.before, hooks.after = before, after
        result: object = initialized
        if action == 'prepare_usage':
            supplied = payload(await port.search_memory(query('usage-ticket')), 'use')
            (root / 'usage.json').write_text(json.dumps(supplied), encoding='utf-8')
        elif action.startswith('ticket_'):
            armed = True
            result = await (port.resolve_recall(query('ticket')) if action == 'ticket_recover' else port.search_memory(query('ticket')))
        elif action.startswith('usage_'):
            supplied = json.loads((root / 'usage.json').read_text(encoding='utf-8'))
            armed = True
            result = await (port.resolve_usage(supplied) if action == 'usage_recover' else port.record_usage(supplied))
        details = record(result.value) if type(result) is Found else None
        print(json.dumps({'result': type(result).__name__, 'state': h.state, 'model_calls': len(h.adapter.calls),
            'commit_id': result.receipt.commit_id if type(result) is Committed else details.get('commit_id') if details else None,
            'availability': details.get('availability') if details else None, 'has_sections': bool(details and 'sections' in details)}, sort_keys=True), flush=True)
    finally:
        if not await h.close(): raise RuntimeError('Isolated process cleanup did not finish.')


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]).resolve(), sys.argv[2]))
