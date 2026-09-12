"""Isolated process interruptions of real information initialization COMMIT."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
from companion_memory.persistence import Found, Committed
from companion_memory.runtime.content_assembly import stable
from companion_memory.information.records import record
from tests.information.host_support import host
from tests.persistence.support import Hooks


async def run(root: Path, action: str) -> None:
    hooks = Hooks(); value = host(root, hooks.connect); armed = False
    original = value.initializer.initialize
    async def initialize(key: str, at_us: int):
        nonlocal armed
        armed = True
        return await original(key, at_us)
    value.initializer.initialize = initialize
    def before(sql: str) -> None:
        if armed and sql == 'COMMIT' and action == 'before': os._exit(71)
    def after(sql: str) -> None:
        if armed and sql == 'COMMIT' and action == 'after': os._exit(72)
    hooks.before, hooks.after = before, after
    try:
        result = await value.initialize('OPEN_EXISTING' if action == 'recover' else 'CREATE_NEW')
        confirmation = await value.initializer.recover(stable('initialize_information', 'instance')) if value.state == 'READY' else None
        print(json.dumps({'result': type(result).__name__, 'state': value.state,
            'original_commit': confirmation.receipt.commit_id if type(confirmation) is Committed else None,
            'model_calls': len(value.adapter.calls)}, sort_keys=True), flush=True)
    finally:
        if not await value.close(): raise RuntimeError('Isolated owner cleanup did not complete.')


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]).resolve(), sys.argv[2]))
