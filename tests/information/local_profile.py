"""Supervise isolated functional-size processes with disk and operation stops.

The root must be newly created and remains available for evidence. Thresholds
stop new writes while leaving space for confirmation and actual owner cleanup.
No system disk, quota, daemon or user database settings are changed.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import platform
import resource
import select
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import TypedDict


class DatabaseMeasurement(TypedDict):
    counts: dict[str, int]
    logical_body_bytes: dict[str, int]
    page_count: int
    page_size: int
    freelist_count: int
    database_bytes: int
    wal_bytes: int
    index_physical_bytes: int | None
    index_measurement_error: str | None


def disk_usage(root: Path) -> dict[str, int]:
    logical = allocated = 0
    for path in root.rglob('*'):
        if path.is_file():
            stat = path.stat(); logical += stat.st_size; allocated += stat.st_blocks * 512
    return {'file_bytes': logical, 'allocated_bytes': allocated, 'free_bytes': shutil.disk_usage(root).free}


def inspect_database(root: Path) -> DatabaseMeasurement:
    path = root / 'database/runtime.sqlite3'
    with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as connection:
        counts = {table: connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in
            ('memory_objects', 'memory_index_gap', 'memory_generation_ack', 'retrieval_index_object', 'retrieval_posting', 'goals_goal', 'retrieval_ticket', 'operation_receipts', 'audit_records')}
        counts['published_generations'] = connection.execute("SELECT count(*) FROM retrieval_index_generation WHERE status IN ('ACTIVE','RETIRING')").fetchone()[0]
        counts['active_generations'] = connection.execute("SELECT count(*) FROM retrieval_index_generation WHERE status='ACTIVE'").fetchone()[0]
        counts['forgotten_objects'] = connection.execute("SELECT count(*) FROM memory_objects WHERE lifecycle='FORGOTTEN'").fetchone()[0]
        logical = {table: connection.execute('SELECT coalesce(sum(length(CAST(body AS BLOB))),0) FROM ' + table).fetchone()[0] for table in
            ('memory_objects', 'retrieval_posting', 'retrieval_index_object', 'goals_goal')}
        physical: int | None = None; measurement_error: str | None = None
        try:
            physical = connection.execute("SELECT coalesce(sum(pgsize),0) FROM dbstat WHERE name IN (SELECT name FROM sqlite_schema WHERE tbl_name IN ('retrieval_posting','retrieval_index_object'))").fetchone()[0]
        except sqlite3.OperationalError as failure:
            measurement_error = str(failure)
        return {'counts': counts, 'logical_body_bytes': logical, 'page_count': connection.execute('PRAGMA page_count').fetchone()[0],
            'page_size': connection.execute('PRAGMA page_size').fetchone()[0], 'freelist_count': connection.execute('PRAGMA freelist_count').fetchone()[0],
            'index_physical_bytes': physical, 'index_measurement_error': measurement_error,
            'database_bytes': path.stat().st_size, 'wal_bytes': Path(str(path) + '-wal').stat().st_size if Path(str(path) + '-wal').exists() else 0}


def run(root: Path, sizes: tuple[int, ...]) -> None:
    if not sizes or len(set(sizes)) != len(sizes) or any(size not in (0, 100, 1000) for size in sizes):
        raise ValueError('Only independent empty, hundred and thousand object profiles are allowed.')
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    report: dict[str, object] = {'platform': platform.platform(), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
        'root': str(root), 'sizes': sizes, 'qualification': 'FUNCTIONAL_ACTIVE_DUAL_GENERATION', 'volume_quota_reserved': False,
        'limits': {'allocated_stop_bytes': 12 * 1024**3, 'free_stop_bytes': 4 * 1024**3, 'operation_stop': 20000}, 'started_unix': time.time()}
    steps: list[dict[str, object]] = []; operations = 0; peak_allocated = 0; minimum_free = shutil.disk_usage(root).free
    report['steps'] = steps
    try:
        for size in sizes:
            dataset = root / ('objects-' + str(size)); dataset.mkdir(mode=0o700)
            for action in ('create', 'recover', 'verify'):
                argv = [sys.executable, '-m', 'tests.information.profile_child', str(dataset), str(size), action]
                output_path = root / (str(size) + '-' + action + '.jsonl')
                error_path = root / (str(size) + '-' + action + '.stderr')
                result: dict[str, object] | None = None
                with output_path.open('w') as output, error_path.open('w') as errors:
                    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
                    if process.stdin is None or process.stdout is None: raise RuntimeError('Expected owned process channels.')
                    stopped = False
                    try:
                        while True:
                            if not select.select((process.stdout,), (), (), 90)[0]:
                                raise TimeoutError('Isolated profile stopped reporting bounded progress.')
                            line = process.stdout.readline()
                            if not line: break
                            output.write(line); output.flush()
                            value = json.loads(line)
                            if 'checkpoint' in value:
                                measured = disk_usage(root); operations += 1
                                peak_allocated = max(peak_allocated, measured['allocated_bytes']); minimum_free = min(minimum_free, measured['free_bytes'])
                                stopped = stopped or measured['allocated_bytes'] >= 12 * 1024**3 or measured['file_bytes'] >= 12 * 1024**3 or measured['free_bytes'] <= 4 * 1024**3 or operations >= 20000
                                process.stdin.write('STOP\n' if stopped else 'ALLOW\n'); process.stdin.flush()
                            else: result = value
                        code = process.wait(timeout=30)
                    except BaseException:
                        # Termination affects only this explicitly owned test process.
                        process.kill(); process.wait(timeout=30); raise
                    finally:
                        process.stdin.close(); process.stdout.close()
                step = {'argv': argv, 'exit_code': code, 'stopped': stopped, 'result': result, 'resource': disk_usage(root),
                    'stdout': str(output_path), 'stderr': str(error_path), 'stdout_sha256': hashlib.sha256(output_path.read_bytes()).hexdigest()}
                steps.append(step)
                if code or stopped or result is None: raise RuntimeError('Isolated profile did not complete; retained outputs describe its limit or failure.')
                database = inspect_database(dataset)
                step['database'] = database
                counts = database['counts']
                if counts['memory_objects'] != size or counts['goals_goal'] != 100: raise RuntimeError('Actual dataset counts differ from the requested profile.')
                if counts['forgotten_objects'] != min(size, 100): raise RuntimeError('Actual forgotten population differs from the requested profile.')
                if action != 'create' and (counts['retrieval_index_object'] != size or counts['memory_index_gap'] != 0 or result['model_calls'] != 0):
                    raise RuntimeError('Actual recovered index coverage or model isolation failed.')
                if action != 'create' and (counts['published_generations'] != 2 or counts['active_generations'] != 1):
                    raise RuntimeError('Actual dual publication facts differ from the profile.')
                print(json.dumps({'size': size, 'action': action, 'operations': operations, 'resource': step['resource']}, sort_keys=True), flush=True)
    finally:
        report.update(ended_unix=time.time(), operations=operations, peak_allocated_bytes=peak_allocated, minimum_free_bytes=minimum_free,
            children_max_rss=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss, children_max_rss_unit='bytes' if sys.platform == 'darwin' else 'KiB')
        cgroup = Path('/sys/fs/cgroup/memory.peak')
        report['cgroup_memory_peak_bytes'] = int(cgroup.read_text()) if cgroup.exists() else None
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(), tuple(int(value) for value in sys.argv[2:]))
