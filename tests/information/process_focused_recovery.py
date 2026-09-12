"""New-process local recovery and isolated COMMIT interruption without transport."""
import asyncio
import json
import os
from pathlib import Path
import sys
from companion_memory.persistence import Found
from tests.information.focused_recovery_support import focused_host, prepare, facts
from tests.persistence.support import Hooks


async def run(root: Path, mode: str, action: str) -> None:
    if action == 'setup':
        await prepare(root, mode)
        print(json.dumps(facts(root))); return
    hooks = Hooks(); armed = False
    def before(sql: str) -> None:
        nonlocal armed
        if sql.startswith('UPDATE goals_dedup_task'): armed = True
        if armed and sql == 'COMMIT' and action == 'before': os._exit(71)
    def after(sql: str) -> None:
        if armed and sql == 'COMMIT' and action == 'after': os._exit(72)
    hooks.before, hooks.after = before, after
    h = focused_host(root, hooks.connect)
    try:
        result = await h.initialize('OPEN_EXISTING')
        if type(result) is not Found: raise RuntimeError('Expected completed local recovery: ' + repr(result))
        if h.runtime is None: raise RuntimeError('Expected real runtime.')
        print(json.dumps(facts(root) | {'state': h.state, 'gate': h.runtime.gate.state, 'model_calls': len(h.adapter.calls),
            'management_jobs': len(h.management.jobs), 'external_pending': h.runtime.external_work_pending}), flush=True)
    finally:
        if not await h.close(): raise RuntimeError('Expected actual close.')


if __name__ == '__main__': asyncio.run(run(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]))
