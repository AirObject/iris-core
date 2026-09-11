"""Local original-work recovery with finite metadata pages and point reads.

No call to this module grants a send permit. A pending original outcome retains
its existing processing references and blocks only the affected entry.
Completed candidates are applied using their original finalization identity.
"""
from __future__ import annotations
import asyncio
import time
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.memory.formats import record, sequence
from companion_memory.memory.sources import decode_source
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.owned_statements import OwnerFailure
from .results import Rejected, RuntimeError, NotCommitted
if TYPE_CHECKING:
    from .content_service import ContentRuntimeService


async def recover_original_work(runtime: ContentRuntimeService):
    """Advance one isolated owner's scan; unfinished work remains at its watermark."""
    from .content_learning import learn_batch
    deadline = time.monotonic() + runtime.settings.integer('runtime.recovery_timeout_ms') / 1000
    page_limit = runtime.settings.integer('runtime.read_page_size')
    async def checkpoint():
        if time.monotonic() >= deadline:
            raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED', bool(runtime._commands))
        await asyncio.sleep(0)
    cursor = runtime._recovery_cursor
    if cursor[0] == 'media':
        if runtime.media is not None:
            while True:
                await checkpoint()
                rows = await runtime.media.media.rows.read('work_recovery_page', {'after': cursor[1], 'limit': runtime.media.media.settings.integer('media.gc_page_size')})
                if not rows: break
                for metadata in rows:
                    await checkpoint()
                    work_id = cast(str, metadata['work_id'])
                    work = (await runtime.media.media.rows.read('work_get', {'work_id': work_id}))[0]
                    result = await runtime.media.drive(work, fresh=False)
                    runtime.observations.record_work(cast(str, work['entry_id']), result)
                    if type(result) is not Found or record(result.value)['state'] not in ('RESULT_STORED', 'REMOTE_UNKNOWN', 'ORIGINAL_REQUEST_UNCONFIRMED', 'WAITING_ADMISSION'):
                        return Found(MappingProxyType({'state': 'RECOVERY_PENDING', 'owner': 'media', 'reason': 'ORIGINAL_WORK_UNRESOLVED'}))
                    cursor = 'media', work_id
                    runtime._recovery_cursor = cursor
        cursor = 'preparations', ''
        runtime._recovery_cursor = cursor
    if cursor[0] == 'preparations':
        from .preparation_format import decode_preparation
        from .preparation_disposal import dispose_preparation
        from .content_assembly import stable
        while True:
            await checkpoint()
            rows = await runtime.assembly.rows.read('preparations_page', {'after': cursor[1], 'limit': page_limit})
            if not rows: break
            for metadata in rows:
                await checkpoint()
                pid = cast(str, metadata['preparation_id'])
                prep = (await runtime.assembly.rows.read('preparations_get', {'preparation_id': pid}))[0]
                source = decode_preparation(cast(str, prep['manifest']))
                if prep['phase'] in ('SELECTED', 'CLAIMED', 'MEDIA_READY', 'PARKED'):
                    state = (await runtime.assembly.buffers.rows.read('get', {'entry_id': prep['entry_id']}))[0]
                    if (state['reservation_id'], state['reservation_kind']) != (pid, 'PREPARATION'):
                        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                    import hashlib
                    for raw in sequence(source['ordered_members']):
                        member = record(raw)
                        payload = await runtime.assembly.ingress.rows.read('payload', {'message_id': member['message_id']})
                        if not payload or hashlib.sha256(cast(str, payload[0]['body']).encode()).hexdigest() != member['payload_digest']:
                            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                    now = runtime.assembly.utc_now_us()
                    if now >= cast(int, prep['deadline_at_us']) or now < cast(int, prep['last_observed_at_us']):
                        result = await dispose_preparation(runtime, pid, allow_replan=False)
                        if type(result) is not Committed: return result
                    elif prep['phase'] != 'PARKED':
                        parked = await runtime.execute('park_content_preparation', stable('recover_preparation', pid, prep['revision']),
                            {'preparation_id': pid, 'expected_revision': prep['revision']})
                        if type(parked) is not Committed: return parked
                cursor = 'preparations', pid
                runtime._recovery_cursor = cursor
        cursor = 'learning', ''
        runtime._recovery_cursor = cursor
    if cursor[0] == 'learning':
        while True:
            await checkpoint()
            rows = await runtime.assembly.rows.read('work_page', {'after': cursor[1], 'limit': page_limit})
            if not rows: break
            for metadata in rows:
                await checkpoint()
                bid = cast(str, metadata['batch_id'])
                batch = (await runtime.assembly.rows.read('batches_get', {'batch_id': bid}))[0]
                result = await learn_batch(runtime, decode_source(cast(str, batch['manifest'])), fresh=False, observe=metadata['phase'] != 'TERMINAL')
                parked = type(result) is Found and record(result.value).get('state') in ('PARKED', 'REMOTE_UNKNOWN', 'ORIGINAL_REQUEST_UNCONFIRMED', 'WAITING_ADMISSION')
                blocked = type(result) is NotCommitted and result.error is not None and not result.error.cleanup_pending and result.error.reason in ('REVISION_CONFLICT', 'OWNERSHIP_CHANGED')
                if type(result) is not Committed and not parked and not blocked:
                    return Found(MappingProxyType({'state': 'RECOVERY_PENDING', 'owner': 'cognition', 'reason': 'ORIGINAL_WORK_UNRESOLVED'}))
                cursor = 'learning', bid
                runtime._recovery_cursor = cursor
        runtime._recovery_cursor = 'complete', ''
    return Found(MappingProxyType({'state': 'ORIGINAL_WORK_VERIFIED'}))
