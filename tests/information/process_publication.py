"""Isolated process exits around an actual publication COMMIT and original resolve."""
import asyncio
import json
import os
from pathlib import Path
import sys
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.content_codec import encode_content
from companion_memory.information.records import record
from tests.information.host_support import host
from tests.information.publication_support import index_port, build
from tests.persistence.support import Hooks


async def run(root: Path, action: str) -> None:
    hooks = Hooks(); h = host(root, hooks.connect); armed = False
    def before(sql: str) -> None:
        nonlocal armed
        if sql.startswith('UPDATE memory_change_sequence'): armed = True
        if armed and sql == 'COMMIT' and action == 'before': os._exit(71)
    def after(sql: str) -> None:
        if armed and sql == 'COMMIT' and action == 'after': os._exit(72)
    hooks.before, hooks.after = before, after
    try:
        initialized = await h.initialize('CREATE_NEW' if action == 'setup' else 'OPEN_EXISTING')
        if type(initialized) is not Found: raise RuntimeError('Actual host recovery failed: ' + repr(initialized))
        port = await index_port(h)
        if action == 'setup':
            gid, generation = await build(h, port, 'build')
            payload = {'generation_id': gid, 'expected_revision': generation['revision']}
            (root / 'publication-input.json').write_text(json.dumps(payload))
            result = None
        else:
            payload = json.loads((root / 'publication-input.json').read_text())
            result = await (port.resolve('index_publish', 'publish', payload) if action == 'recover' else port.execute('index_publish', 'publish', payload))
        print(json.dumps({'result': type(result).__name__, 'state': h.state,
            'commit_id': result.receipt.commit_id if type(result) is Committed else None,
            'facts': json.loads(encode_content(record(result.receipt.result), 16384)) if type(result) is Committed else None,
            'model_calls': len(h.adapter.calls)}), flush=True)
    finally:
        if not await h.close(): raise RuntimeError('Actual publication process did not release its owners.')


if __name__ == '__main__': asyncio.run(run(Path(sys.argv[1]).resolve(), sys.argv[2]))
