"""Independent local validation processes under finite disk/write supervision.

The parent alone permits each new persistence command. Test databases and all
outputs stay under a newly created private directory; stopping never erases
receipts or claims that outstanding I/O has been cleaned up.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import resource
import select
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch
from companion_memory.persistence.results import ExecutionResult
from companion_memory.persistence.service import OperationPort
from tests.information.lexical_bounds import census
from tests.information.lexical_pressure import pressure
from tests.information.lexical_quality import capture_quality
from tests.information.local_profile import disk_usage


async def child(root: Path, workload: str, action: str) -> None:
    original = OperationPort.execute; stopped = False; ordinal = 0
    async def guarded(port: OperationPort, key: object, command: object) -> ExecutionResult:
        nonlocal stopped, ordinal
        if not stopped:
            ordinal += 1
            print(json.dumps({'checkpoint': ordinal}), flush=True)
            if (await asyncio.to_thread(sys.stdin.readline)).strip() != 'ALLOW':
                stopped = True
                raise RuntimeError('Supervisor stopped new writes; only owner cleanup remains.')
        return await original(port, key, command)
    started = time.monotonic()
    with patch.object(OperationPort, 'execute', guarded):
        if workload == 'quality': result = await capture_quality(root, action)
        elif workload in ('pressure', 'expansion'): result = await pressure(root, action, expanded=workload == 'expansion')
        else: raise ValueError('Unknown bounded verification workload.')
    result.update(elapsed_seconds=time.monotonic() - started,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
        resource=disk_usage(root))
    print(json.dumps({'result': result}, ensure_ascii=False, sort_keys=True), flush=True)


def supervise(root: Path) -> None:
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    (root / 'unicode-census.json').write_text(json.dumps(census(), indent=2) + '\n')
    results: list[dict[str, object]] = []; operations = peak = 0
    report: dict[str, object] = {'platform': platform.platform(), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
        'root': str(root), 'started_unix': time.time(), 'steps': results,
        'limits': {'allocated_stop_bytes': 12 * 1024**3, 'free_stop_bytes': 4 * 1024**3, 'operation_stop': 20000},
        'qualification': 'REACHABLE_LOWER_BOUND_AND_PENDING_HUMAN_REVIEW', 'maximum_proved': False}
    try:
        for workload in ('pressure', 'expansion', 'quality'):
            dataset = root / workload; dataset.mkdir(mode=0o700)
            for action in ('create', 'reopen'):
                argv = [sys.executable, '-m', 'tests.information.lexical_qualification_run', 'child', str(dataset), workload, action]
                stdout = root / (workload + '-' + action + '.jsonl'); stderr = root / (workload + '-' + action + '.stderr')
                result: object = None; stopped = False; started = time.time()
                with stdout.open('w') as output, stderr.open('w') as errors:
                    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
                    if process.stdin is None or process.stdout is None: raise RuntimeError('Expected owned process channels.')
                    try:
                        while True:
                            if not select.select((process.stdout,), (), (), 60)[0]:
                                raise TimeoutError('Owned process stopped reporting bounded progress.')
                            line = process.stdout.readline()
                            if not line: break
                            output.write(line); output.flush(); message = json.loads(line)
                            if 'checkpoint' in message:
                                usage = disk_usage(root); operations += 1; peak = max(peak, usage['allocated_bytes'])
                                stopped = stopped or usage['file_bytes'] >= 12 * 1024**3 or usage['allocated_bytes'] >= 12 * 1024**3 or usage['free_bytes'] <= 4 * 1024**3 or operations >= 20000
                                process.stdin.write('STOP\n' if stopped else 'ALLOW\n'); process.stdin.flush()
                            else: result = message['result']
                        code = process.wait(timeout=30)
                    except BaseException:
                        process.kill(); process.wait(timeout=30)
                        raise
                    finally:
                        process.stdin.close(); process.stdout.close()
                step = {'argv': argv, 'exit_code': code, 'started_unix': started, 'ended_unix': time.time(),
                    'stopped': stopped, 'result': result, 'stdout': str(stdout), 'stderr': str(stderr),
                    'stdout_sha256': hashlib.sha256(stdout.read_bytes()).hexdigest(), 'resource': disk_usage(root)}
                results.append(step)
                if code or stopped or result is None: raise RuntimeError('Verification incomplete; retained outputs describe the failure.')
                print(json.dumps({'workload': workload, 'action': action, 'result': result}, ensure_ascii=False), flush=True)
    finally:
        peak_file = Path('/sys/fs/cgroup/memory.peak')
        report.update(ended_unix=time.time(), operations=operations, peak_allocated_at_command_boundaries=peak,
            resource=disk_usage(root), cgroup_memory_peak_bytes=int(peak_file.read_text()) if peak_file.exists() else None)
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'parent': supervise(Path(sys.argv[2]).resolve())
    elif len(sys.argv) == 5 and sys.argv[1] == 'child': asyncio.run(child(Path(sys.argv[2]).resolve(), sys.argv[3], sys.argv[4]))
    else: raise ValueError('Expected parent root, or child root workload action.')
