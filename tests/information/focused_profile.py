"""Bounded 100-goal persistent recovery profile in independent native processes.

Storage and local recovery are actual; the publication participant is synthetic.
There is no publication generation, model execution or reminder transport.
"""
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import sys
import time
from companion_memory.persistence import Found
from tests.information.focused_recovery_support import focused_host, prepare, facts


def footprint(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob('*') if path.is_file())


def budget(root: Path) -> None:
    if footprint(root) >= 12 * 1024**3 or shutil.disk_usage(root).free <= 4 * 1024**3:
        raise RuntimeError('Isolated profile reached the disk stop threshold.')
    path = root / 'database/runtime.sqlite3'
    if path.exists():
        with closing(sqlite3.connect(path)) as connection:
            if connection.execute('SELECT count(*) FROM operation_receipts').fetchone()[0] >= 20000:
                raise RuntimeError('Isolated profile reached the operation stop threshold.')


async def run(root: Path, mode: str, action: str) -> None:
    if mode not in ('DREAM_PREPARING', 'DREAM_FOCUSED') or action not in ('setup', 'recover', 'verify'):
        raise ValueError('Explicit bounded recovery profile phase required.')
    if action == 'setup': root.mkdir(parents=False, exist_ok=False)
    budget(root); started = time.monotonic()
    if action == 'setup':
        await prepare(root, mode, goal_count=100, before_write=lambda: budget(root))
        (root / 'initial-facts.json').write_text(json.dumps(facts(root), sort_keys=True))
        model_calls = 0
    else:
        h = focused_host(root)
        try:
            initialized = await h.initialize('OPEN_EXISTING')
            if type(initialized) is not Found or h.runtime is None or h.state != 'READY' or h.runtime.gate.state != 'DREAM_FOCUSED':
                raise RuntimeError('Expected verified focused recovery: ' + repr(initialized))
            if h.management.jobs or h.runtime.external_work_pending or h.adapter.calls:
                raise RuntimeError('Recovery leaked actual work or called a model.')
            current = facts(root)
            if any(row[1] == 'RUNNING' for row in current['tasks']) or any(row[1] != 'UNKNOWN' for row in current['attempts']):
                raise RuntimeError('Recovery failed to terminalize retained local work.')
            before = json.loads((root / 'initial-facts.json').read_text())
            if not all(tuple(row) in current['receipts'] for row in before['receipts']):
                raise RuntimeError('Original receipts changed.')
            if current['audits'] - before['audits'] != 100 + int(mode == 'DREAM_PREPARING'):
                raise RuntimeError('Actual recovery audit count differs from retained work.')
            encoded = json.dumps(current, sort_keys=True)
            if action == 'recover': (root / 'recovered-facts.json').write_text(encoded)
            elif encoded != (root / 'recovered-facts.json').read_text(): raise RuntimeError('Repeated recovery wrote new facts.')
            model_calls = len(h.adapter.calls)
        finally:
            if not await h.close(): raise RuntimeError('Actual owners failed to close.')
    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
        operations = connection.execute('SELECT count(*) FROM operation_receipts').fetchone()[0]
        goals = connection.execute('SELECT count(*) FROM goals_goal').fetchone()[0]
        logical = connection.execute('SELECT sum(length(CAST(body AS BLOB))) FROM goals_goal').fetchone()[0]
    measured = footprint(root); budget(root)
    print(json.dumps({'mode': mode, 'action': action, 'seconds': time.monotonic() - started, 'platform': platform.platform(),
        'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'uid': os.getuid(), 'operations_total': operations, 'goal_count': goals,
        'goal_body_bytes': logical, 'directory_bytes': measured, 'physical_to_goal_body_ratio': measured / logical,
        'free_bytes': shutil.disk_usage(root).free, 'process_peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1024 if sys.platform == 'linux' else 1),
        'model_calls': model_calls, 'fact_sha256': hashlib.sha256(json.dumps(facts(root), sort_keys=True).encode()).hexdigest()}), flush=True)


if __name__ == '__main__': asyncio.run(run(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]))
