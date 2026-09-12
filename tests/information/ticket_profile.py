"""Actual finite ticket-capacity boundary in an explicitly owned test directory.

Run create and recover in separate processes. Models are simulated only during
one initial learning batch; all tickets, receipts, audits and recovery are real.
Expired occupancy uses an explicitly simulated observation, without time travel
writes, expiration cleanup, external delivery or a sustained-load claim.
"""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import platform
import resource
import sqlite3
import sys
import time
from unittest.mock import patch
from companion_memory.persistence import Found
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.results import ExecutionResult
from companion_memory.persistence.service import OperationPort
from companion_memory.information.errors import RecallCommitted, InformationNotCommitted
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record
from companion_memory.retrieval.tickets import RecallAuthority
from tests.information.host_support import host, learn_one
from tests.information.local_profile import disk_usage


async def run(root: Path, recovering: bool) -> None:
    if not recovering: root.mkdir(mode=0o700, parents=False, exist_ok=False)
    if not root.is_dir(): raise ValueError('The owned capacity directory must exist for recovery.')
    report: dict[str, object] = {'platform': platform.platform(), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
        'root': str(root), 'recovering': recovering, 'storage': 'ACTUAL', 'model': 'SIMULATED', 'candidate': 'SYNTHETIC',
        'expiry_observation': 'SIMULATED', 'started_unix': time.time(), 'volume_quota_reserved': False}
    h = host(root); operations = 0; closing = False; peak = 0; minimum_free = disk_usage(root)['free_bytes']
    original = OperationPort.execute
    async def guarded(port: OperationPort, operation_key: object, local_command: object) -> ExecutionResult:
        nonlocal operations, peak, minimum_free
        if not closing:
            measured = disk_usage(root); operations += 1
            peak = max(peak, measured['allocated_bytes']); minimum_free = min(minimum_free, measured['free_bytes'])
            if (max(measured['file_bytes'], measured['allocated_bytes']) >= 12 * 1024**3
                    or measured['free_bytes'] <= 4 * 1024**3 or operations >= 20000):
                raise RuntimeError('Owned profile stop threshold reached; no further load is authorized.')
        return await original(port, operation_key, local_command)
    started = time.monotonic()
    with patch.object(OperationPort, 'execute', guarded):
        try:
            initialized = await h.initialize('OPEN_EXISTING' if recovering else 'CREATE_NEW')
            report['initialization_seconds'] = time.monotonic() - started
            if type(initialized) is not Found: raise RuntimeError('Actual host initialization failed: ' + repr(initialized))
            if not recovering: await learn_one(h)
            if h.runtime is None or h.assembly.memory.information is None: raise RuntimeError('Expected actual initialized memory.')
            current = (await h.assembly.memory.information.current_page())[0]
            oid = current['object_id']
            if type(oid) is not str: raise RuntimeError('Expected actual object identity.')
            port = await h.bind_management(HostIdentity('capacity', 'principal', 'host', 'entry', frozenset(('ticket_issue_normal',)), (), time.monotonic() + 300))
            authority = RecallAuthority(port.binding_id, 'host', 'entry', h.runtime.memory, h.runtime.memory.bind_read((oid,), ('get_current',)))
            h.management.bind_recall_authority(port, authority)
            if not recovering:
                for ordinal in range(10001):
                    if ordinal and ordinal % 1000 == 0:
                        h.management.revoke(port)
                        port = await h.bind_management(HostIdentity('capacity', 'principal', 'host', 'entry', frozenset(('ticket_issue_normal',)), (), time.monotonic() + 300))
                        h.management.bind_recall_authority(port, authority)
                    key = 'ticket:' + str(ordinal); recall_id = 'capacity:' + str(ordinal)
                    now = int(time.time() * 1000000)
                    ticket = {'version': 1, 'recall_id': recall_id, 'database_id': 'information-database', 'instance_id': 'instance',
                        'principal_binding_id': port.binding_id, 'host_id': 'host', 'entry_id': 'entry', 'query_mode': 'NORMAL',
                        'config_snapshot_id': h.assembly.configuration.snapshot_id, 'issued_at_us': now, 'expires_at_us': now + 86400000000,
                        'clock_observation': now, 'response_digest': sha256(encode_content((current,), 8192)).hexdigest(), 'intent_digest': 'a' * 64,
                        'request_key': key, 'member_count': 1}
                    result = await port.execute('ticket_issue_normal', key, {'ticket': ticket,
                        'members': ({'recall_id': recall_id, 'object_id': oid, 'returned_revision': current['revision'], 'returned_lifecycle': current['lifecycle']},)})
                    if ordinal < 10000:
                        if type(result) is not RecallCommitted: raise RuntimeError('Actual ticket issue failed: ' + repr(result))
                    elif type(result) is not InformationNotCommitted or result.error.reason != 'CAPACITY_REACHED':
                        raise RuntimeError('The full pool did not reject the next ticket: ' + repr(result))
                    if ordinal % 1000 == 0: print(json.dumps({'tickets_attempted': ordinal + 1, 'operations': operations, 'elapsed_seconds': time.monotonic() - started}), flush=True)
            now = int(time.time() * 1000000)
            live = await h.management.tickets.occupancy(now)
            expired = await h.management.tickets.occupancy(now + 86400000000)
            h.management.tickets.protect('capacity:0')
            try: protected = await h.management.tickets.occupancy(now + 86400000000)
            finally: h.management.tickets.release('capacity:0')
            if (live['occupied'], live['live'], expired['occupied'], expired['expired_pending'], protected['protected_pending'], protected['expired_pending']) != (10000, 10000, 10000, 10000, 1, 9999):
                raise RuntimeError('Actual occupancy or expiry protection is inconsistent.')
            report.update(live=dict(live), expired=dict(expired), protected=dict(protected), model_calls=len(h.adapter.calls), result='COMPLETED')
            if recovering and h.adapter.calls: raise RuntimeError('Recovery called the simulated model.')
        finally:
            closing = True
            closed = await h.close()
            report.update(closed=closed, operations=operations, peak_allocated_bytes=peak, minimum_free_bytes=minimum_free,
                elapsed_seconds=time.monotonic() - started, process_peak_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                process_peak_rss_unit='bytes' if sys.platform == 'darwin' else 'KiB', resource=disk_usage(root))
            cgroup = Path('/sys/fs/cgroup/memory.peak')
            report['cgroup_memory_peak_bytes'] = int(cgroup.read_text()) if cgroup.exists() else None
            (root / ('recover-report.json' if recovering else 'create-report.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if not closed: raise RuntimeError('The owned profile cleanup did not finish.')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[2] not in ('create', 'recover'): raise ValueError('Expected owned directory and create/recover.')
    asyncio.run(run(Path(sys.argv[1]).resolve(), sys.argv[2] == 'recover'))
