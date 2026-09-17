"""Scoped content counts for developer observation, without any original text.

The finite native grant controls each entry before every owner query. Failed
reads are unavailable, never fabricated zero counts. No object/source/body/file
identifier or history payload is exported to HTTP through this capability.
"""
from __future__ import annotations
from companion_memory.persistence.completion import finish_owned
import asyncio
from collections import OrderedDict
import hashlib
import hmac
import secrets
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from weakref import WeakValueDictionary
from companion_memory.persistence import Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import valid_identifier
from companion_memory.media.service import MediaService
from .results import Found, Failed, RuntimeError
if TYPE_CHECKING:
    from .content_service import ContentRuntimeService


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ContentObserver:
    """Observation-only native capability, with no business or original-read API."""
    _binding: ContentObservations
    _entries: tuple[str, ...]
    _instance: bool
    _identity: str

    def __init__(self): raise TypeError('Content observation requires a native finite grant.')

    async def read_runtime_view(self, query: object): return await _read(self, 'runtime', query)
    async def read_entry_status(self, query: object): return await _read(self, 'entries', query)
    async def read_batch_status(self, query: object): return await _read(self, 'batches', query)
    async def read_memory_status(self, query: object): return await _read(self, 'memory', query)
    async def read_media_status(self, query: object): return await _read(self, 'media', query)


async def _read(port, kind, query):
    try: binding = object.__getattribute__(port, '_binding')
    except AttributeError: binding = None
    if type(port) is not ContentObserver or type(binding) is not ContentObservations:
        return Failed(RuntimeError('ACCESS_DENIED', 'observe_content', 'capability', 'BINDING_MISMATCH'))
    return await binding.read(port, kind, query)


class ContentObservations:
    """Bounded pending reads; original resources remain owned below timeouts."""
    def __init__(self, runtime: ContentRuntimeService):
        self.runtime = runtime
        self.grants: WeakValueDictionary[int, ContentObserver] = WeakValueDictionary()
        self.jobs: set[asyncio.Task] = set()
        self.closed = False
        self.secret = secrets.token_bytes(32)
        self.cache: OrderedDict[tuple, MappingProxyType] = OrderedDict()
        self.work: OrderedDict[str, MappingProxyType] = OrderedDict()

    def record_work(self, entry_id: str, result: object) -> None:
        """Retain safe current-process evidence separately from durable phase/receipt."""
        from companion_memory.persistence import Committed, Found as StoredFound, NotCommitted as StoredNotCommitted, Unconfirmed as StoredUnconfirmed
        from .results import NotCommitted, Unconfirmed
        state = 'SYSTEM_BLOCKED'; reason = None; local = 'UNAVAILABLE'; remote = 'UNAVAILABLE'
        error = getattr(result, 'error', None)
        cleanup = bool(getattr(error, 'cleanup_pending', False))
        if type(result) is Committed:
            state = 'COMPLETED'; local = 'COMMITTED'
        elif type(result) in (Unconfirmed, StoredUnconfirmed):
            state = 'LOCAL_UNCONFIRMED'; local = 'UNCONFIRMED'
        elif type(result) in (NotCommitted, StoredNotCommitted): local = 'NOT_COMMITTED'
        elif type(result) is StoredFound:
            from companion_memory.memory.formats import record
            value = record(result.value)
            state = cast(str, value.get('state', 'UNAVAILABLE'))
            reason = value.get('reason'); remote = cast(str, value.get('remote_result', 'UNAVAILABLE'))
            cleanup = bool(value.get('cleanup_pending', False))
        if error is not None:
            reason = getattr(error, 'reason', None)
            if getattr(error, 'code', None) in ('RESOURCE_BUSY', 'MODE_BLOCKED', 'BUDGET_EXHAUSTED'): state = 'WAITING'
        # Fixed owner enums only. Never copy identifiers, arbitrary exception text,
        # original descriptors or result payloads into developer observations.
        permitted = {'REVISION_CONFLICT', 'OWNERSHIP_CHANGED', 'READ_FAILED', 'WRITE_NOT_COMMITTED', 'COMMIT_UNCONFIRMED',
            'INTEGRITY_FAILURE', 'SYSTEM_BLOCKED', 'WAITING_ADMISSION', 'ADMISSION_FULL', 'DREAMING', 'DEADLINE_EXCEEDED',
            'ORIGINAL_REQUEST_UNCONFIRMED', 'REMOTE_UNKNOWN', 'EXISTING_PREPARATION', 'RECOVERY_NO_DISPATCH',
            'PREPARATION_EXPIRED', 'OCCURRENCE_EXPIRED', 'WINDOW_CHANGED'}
        if reason not in permitted: reason = None
        if state not in {'SYSTEM_BLOCKED', 'COMPLETED', 'LOCAL_UNCONFIRMED', 'WAITING', 'WAITING_ADMISSION',
                'REMOTE_UNKNOWN', 'ORIGINAL_REQUEST_UNCONFIRMED', 'PARKED', 'RESULT_STORED', 'MEDIA_READY', 'EXPIRED', 'INVALIDATED'}: state = 'UNAVAILABLE'
        if remote not in ('UNKNOWN', 'NOT_SENT'): remote = 'UNAVAILABLE'
        self.work[entry_id] = MappingProxyType({'state': state, 'reason': reason, 'local_commit': local,
            'remote_result': remote, 'cleanup_pending': cleanup, 'observed_at': datetime.now(timezone.utc).isoformat()})
        self.work.move_to_end(entry_id)
        while len(self.work) > self.runtime.settings.integer('management.observation_row_limit'): self.work.popitem(last=False)

    def origins(self):
        """Describe the selected native capabilities, including controlled transports."""
        from companion_memory.provider.daily_service import DailyProvider
        provider=self.runtime.provider
        if type(provider) is DailyProvider:
            return {'model_adapter':MappingProxyType({role:t.execution_kind for role,t in provider.transports.items()}),
                'candidate_origin':'MODEL_OUTPUT_VALIDATED'}
        return {'model_adapter':'SIMULATED','candidate_origin':'SYNTHETIC'}

    def bind(self, entry_ids: tuple[str, ...], instance_observe: bool = False) -> ContentObserver:
        limit = self.runtime.settings.integer('management.observation_row_limit')
        if (self.closed or len(self.grants) >= limit or type(entry_ids) is not tuple or not 1 <= len(entry_ids) <= limit or len(set(entry_ids)) != len(entry_ids)
                or any(not valid_identifier(e) for e in entry_ids) or type(instance_observe) is not bool): raise ValueError('Finite entry observation grant required.')
        result = object.__new__(ContentObserver)
        for key, value in (('_binding', self), ('_entries', entry_ids), ('_instance', instance_observe), ('_identity', secrets.token_hex(16))): object.__setattr__(result, key, value)
        self.grants[id(result)] = result
        return result

    async def read(self, port: ContentObserver, kind: str, query: object):
        r = self.runtime
        if self.closed or self.grants.get(id(port)) is not port: return Failed(RuntimeError('ACCESS_DENIED', 'observe_content', 'capability', 'BINDING_MISMATCH'))
        if type(query) is not dict or set(query) - {'entry_id', 'cursor', 'limit', 'batch_id', 'state'}:
            return Failed(RuntimeError('INVALID_INPUT', 'observe_content', 'query', 'INVALID_SHAPE'))
        entry = query.get('entry_id'); limit = query.get('limit', r.settings.integer('management.observation_row_limit'))
        if type(limit) is not int or not 1 <= limit <= r.settings.integer('management.observation_row_limit') or any(query.get(k) is not None for k in ('batch_id', 'state')):
            return Failed(RuntimeError('INVALID_INPUT', 'observe_content', 'query', 'LIMIT_EXCEEDED'))
        if entry is not None and (type(entry) is not str or entry not in port._entries):
            return Failed(RuntimeError('ACCESS_DENIED', 'observe_content', 'capability', 'OPERATION_NOT_GRANTED'))
        if kind == 'runtime' and entry is None and not port._instance:
            return Failed(RuntimeError('ACCESS_DENIED', 'observe_content', 'capability', 'OPERATION_NOT_GRANTED'))
        if len(self.jobs) >= r.settings.integer('management.observation_concurrency'):
            return Failed(RuntimeError('RESOURCE_BUSY', 'observe_content', 'state', 'ADMISSION_FULL', True))
        entries = (entry,) if type(entry) is str else port._entries
        def token(offset):
            message = json.dumps([port._identity, kind, entry, limit, offset], separators=(',', ':')).encode()
            return str(offset) + ':' + hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        offset = 0
        cursor = query.get('cursor')
        if cursor is not None:
            try:
                if type(cursor) is not str or len(cursor) > 128: raise ValueError()
                offset = int(cursor.split(':', 1)[0])
                if not 0 < offset < len(entries) or not hmac.compare_digest(cursor, token(offset)): raise ValueError()
            except (ValueError, TypeError):
                return Failed(RuntimeError('ACCESS_DENIED', 'observe_content', 'query', 'BINDING_MISMATCH'))
        cache_key = port._identity, kind, entry, limit, offset
        async def inspect():
            rows = []
            try:
                for eid in entries[offset:offset + limit]:
                    if self.closed or self.grants.get(id(port)) is not port: return Failed(RuntimeError('ACCESS_DENIED', 'observe_content', 'capability', 'BINDING_MISMATCH'))
                    registration = await r.assembly.ingress.rows.read('entry', {'entry_id': eid})
                    if not registration: continue
                    result: dict[str, Value] = {'entry_id': eid}
                    observations: dict[str, Value] = {}
                    def merge(owner, values):
                        revisions = {k: v for k, v in values.items() if k.endswith('_revision')}
                        from companion_memory.provider.daily_service import DailyProvider
                        if type(r.provider) is DailyProvider:
                            result[owner]=MappingProxyType({k:v for k,v in values.items() if k not in revisions})
                        else:result.update((k, v) for k, v in values.items() if k not in revisions)
                        observations[owner] = MappingProxyType({'observed_at': datetime.now(timezone.utc).isoformat(),
                            'revision': MappingProxyType(revisions)})
                    if kind in ('runtime', 'entries', 'batches'):
                        merge('runtime', (await r.assembly.rows.read('observe_entry', {'entry_id': eid}))[0])
                    if r.assembly.daily_format and kind in ('runtime', 'entries'):
                        merge('buffers', await r.assembly.buffers.observation(eid))
                    if kind in ('runtime', 'memory'):
                        merge('memory', (await r.assembly.memory.rows.read('observe_entry', {'entry_id': eid}))[0])
                    if kind in ('runtime', 'media'):
                        media = r.assembly.media
                        if type(media) is MediaService:
                            merge('media', (await cast(MediaService, media).rows.read('observe_entry', {'entry_id': eid}))[0])
                        else: result['media_availability'] = 'UNAVAILABLE'
                    result['owners'] = MappingProxyType(observations)
                    if kind in ('batches', 'memory', 'media'):
                        result['work'] = self.work.get(eid, MappingProxyType({'availability': 'UNAVAILABLE'}))
                    rows.append(MappingProxyType(result))
                instance = None
                media = r.assembly.media
                if port._instance and kind in ('runtime', 'media') and type(media) is MediaService:
                    cleanup = dict((await cast(MediaService, media).rows.read('observe_gc', {}))[0])
                    health = media.get_health()
                    cleanup.update({'observed_at': datetime.now(timezone.utc).isoformat(), 'collection_in_flight': health.collection_in_flight,
                        'file_workers': health.file_workers, 'cleanup_pending': health.cleanup_pending,
                        'processing_suspects': health.processing_suspects, 'processing_observed_at_us': health.processing_observed_at_us})
                    instance = MappingProxyType(cleanup)
                value = MappingProxyType({'availability': 'AVAILABLE', 'observed_at': datetime.now(timezone.utc).isoformat(),
                    'storage_execution': 'ACTUAL', **self.origins(), 'rows': tuple(rows),
                    **({'instance_media': instance} if instance is not None else {}),
                    'mode': r.gate.state if port._instance else None, 'consistency': 'COMPOSITE_OBSERVATION',
                    'revision_semantics': 'MAXIMUM_OBSERVED_MEMBER_REVISION',
                    'has_more': len(entries) > offset + limit, 'next_cursor': token(offset + limit) if len(entries) > offset + limit else None})
                def plain(value):
                    if type(value) is MappingProxyType: return {k: plain(v) for k, v in value.items()}
                    if type(value) is tuple: return [plain(v) for v in value]
                    return value
                if len(json.dumps(plain(value), ensure_ascii=False, separators=(',', ':')).encode()) > r.settings.integer('management.observation_max_bytes'):
                    return Failed(RuntimeError('INVALID_INPUT', 'observe_content', 'query', 'LIMIT_EXCEEDED'))
                self.cache[cache_key] = value; self.cache.move_to_end(cache_key)
                while len(self.cache) > r.settings.integer('management.observation_row_limit'): self.cache.popitem(last=False)
                return Found(value)
            except OwnerFailure:
                previous = self.cache.get(cache_key)
                if previous is not None: return Found(MappingProxyType({**previous, 'availability': 'STALE', 'reason': 'READ_FAILED'}))
                return Found(MappingProxyType({'availability': 'UNAVAILABLE', 'reason': 'READ_FAILED'}))
        task = asyncio.create_task(finish_owned(inspect())); self.jobs.add(task)
        def ended(job):
            if not job.cancelled(): job.exception()
            self.jobs.discard(job)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((task,), timeout=r.settings.integer('management.observation_timeout_ms') / 1000)
        if not done: return Failed(RuntimeError('TIMEOUT', 'observe_content', 'state', 'DEADLINE_EXCEEDED', True))
        return task.result()

    def close(self) -> bool:
        self.closed = True; self.grants.clear(); self.cache.clear(); self.work.clear()
        return not self.jobs
