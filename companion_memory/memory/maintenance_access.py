"""Finite native maintenance authority over immutable original object intentions.

A public retry first confirms the original execution. Only a later explicit call
may replace an ownership-conflicted plan; the transaction verifies that the prior
writer has ended and could not commit. Object semantics never change on replan.
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from weakref import WeakValueDictionary
from companion_memory.persistence import Committed
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue, valid_identifier
from .changes import isolate_change
from .formats import record
from .release_plans import digest
from .service import MemoryError
if TYPE_CHECKING:
    from companion_memory.runtime.content_service import ContentRuntimeService


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class MemoryMaintenancePort:
    """Object-limited maintenance capability, independently issued from read ports."""
    _binding: MemoryMaintenance
    _objects: frozenset[str]
    _sources: tuple[str, ...]
    _readable_objects: tuple[str, ...]
    _subjects: tuple[str, ...]

    def __init__(self): raise TypeError('Maintenance is issued by trusted owner setup.')

    async def replace_current(self, key: object, change: object): return await _apply(self, key, change, 'REPLACE_CURRENT')
    async def set_scores(self, key: object, change: object): return await _apply(self, key, change, 'SET_SCORES')
    async def delete_object(self, key: object, object_id: object, expected_revision: object):
        return await _apply(self, key, {'change_version': 1, 'action': 'DELETE_OBJECT', 'target_id': object_id,
            'expected_revision': expected_revision, 'proposed_value': None, 'links': None}, 'DELETE_OBJECT')


async def _apply(port, key, change, action):
    try: binding = object.__getattribute__(port, '_binding')
    except AttributeError: binding = None
    if type(port) is not MemoryMaintenancePort or type(binding) is not MemoryMaintenance or binding.grants.get(id(port)) is not port:
        return MemoryError('ACCESS_DENIED', 'maintain_memory', 'capability', 'BINDING_MISMATCH')
    return await binding.apply(port, key, change, action)


class MemoryMaintenance:
    """Bounded whole operations keep authority and storage owners below a timeout."""
    def __init__(self, runtime: ContentRuntimeService):
        self.runtime = runtime
        self.grants: WeakValueDictionary[int, MemoryMaintenancePort] = WeakValueDictionary()
        self.jobs: dict[str, asyncio.Task] = {}
        self.closed = False

    def bind(self, object_ids: tuple[str, ...], source_ids: tuple[str, ...] = (), *, readable_object_ids: tuple[str, ...] = (), subject_ids: tuple[str, ...] = ()):
        limit = self.runtime.assembly.configuration.candidate.content.integer('memory.read_page_size')
        if self.closed or len(self.grants) >= limit or type(object_ids) is not tuple or not 1 <= len(object_ids) <= limit or any(not valid_identifier(v) for v in object_ids) or type(source_ids) is not tuple or len(source_ids) > limit or any(not valid_identifier(v) for v in source_ids):
            raise ValueError('A finite native maintenance object scope is required.')
        if any(type(ids) is not tuple or len(ids) > limit or any(not valid_identifier(v) for v in ids) for ids in (readable_object_ids, subject_ids)):
            raise ValueError('Finite independently granted basis and subject scopes are required.')
        port = object.__new__(MemoryMaintenancePort)
        object.__setattr__(port, '_binding', self); object.__setattr__(port, '_objects', frozenset(object_ids)); object.__setattr__(port, '_sources', tuple(sorted(set(source_ids))))
        object.__setattr__(port, '_readable_objects', tuple(sorted(set(readable_object_ids))))
        object.__setattr__(port, '_subjects', tuple(sorted(set(subject_ids))))
        self.grants[id(port)] = port
        return port

    async def apply(self, port, key, raw, action):
        from companion_memory.runtime.content_assembly import stable
        r = self.runtime
        if self.closed or r.state != 'READY': return MemoryError('INVALID_STATE', 'maintain_memory', 'state', 'NOT_READY')
        if type(raw) not in (dict, MappingProxyType): return MemoryError('INVALID_INPUT', 'maintain_memory', 'input', 'INVALID_SHAPE')
        if type(raw.get('target_id')) is not str or raw.get('target_id') not in port._objects: return MemoryError('ACCESS_DENIED', 'maintain_memory', 'capability', 'OPERATION_NOT_GRANTED')
        try:
            if not valid_identifier(key): raise InvalidValue()
            change = isolate_change(raw, r.assembly.configuration.candidate.content.integer('cognition.candidate_item_max_bytes'), text_format=r.assembly.memory.text_format)
            if change['action'] != action: raise InvalidValue()
        except InvalidValue: return MemoryError('INVALID_INPUT', 'maintain_memory', 'input', 'INVALID_SHAPE')
        root = stable('memory_root', r.assembly.configuration.database_id, change['target_id'], key)
        if root in self.jobs or len(self.jobs) >= r.settings.integer('runtime.max_active_entries'):
            return MemoryError('RESOURCE_BUSY', 'maintain_memory', 'state', 'OWNER_ACTIVE', root in self.jobs)
        async def run():
            token = r._request_deadline.set(time.monotonic() + r.settings.integer('runtime.operation_timeout_ms') / 1000)
            try: return await self.advance(root, change, port._sources, port._readable_objects, port._subjects)
            except OwnerFailure as failure: return MemoryError(failure.code, 'maintain_memory', failure.field, failure.reason, failure.cleanup_pending)
            finally: r._request_deadline.reset(token)
        task = asyncio.create_task(run()); self.jobs[root] = task
        def ended(job):
            if not job.cancelled(): job.exception()
            self.jobs.pop(root, None)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((task,), timeout=r.settings.integer('runtime.operation_timeout_ms') / 1000)
        if not done: return MemoryError('TIMEOUT', 'maintain_memory', 'state', 'DEADLINE_EXCEEDED', True)
        return task.result()

    async def advance(self, root, change, source_ids, readable_object_ids, subject_ids):
        from companion_memory.runtime.content_assembly import stable
        from companion_memory.runtime.results import NotCommitted
        r = self.runtime; rows = r.assembly.memory.rows
        roots = await rows.read('release_roots_get', {'root_id': root})
        previous = None; ordinal = 1
        if roots:
            current = roots[0]
            if current['semantic_digest'] != digest(change, 8192): return MemoryError('IDEMPOTENCY_CONFLICT', 'maintain_memory', 'input', 'CONTENT_MISMATCH')
            plans = await rows.read('plan_for_root', {'root_id': root, 'ordinal': current['last_ordinal']})
            if not plans: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            plan = plans[0]
            result = await r.execute(cast(str, plan['command_kind']), cast(str, plan['execution_key']), {'plan_id': plan['plan_id']})
            if type(result) is not NotCommitted or result.error is None or result.error.reason != 'OWNERSHIP_CHANGED' or result.error.cleanup_pending: return result
            previous = plan['plan_id']; ordinal = cast(int, current['last_ordinal']) + 1
        if r.gate.state not in ('NORMAL', 'DRAINING'): return MemoryError('MODE_BLOCKED', 'maintain_memory', 'state', 'DREAMING')
        planned = await r.execute('plan_memory_change', stable('memory_plan', root, ordinal), {'root_id': root,
            'change': encode_content(change, 8192).decode(), 'authorized_sources': source_ids, 'authorized_objects': readable_object_ids, 'authorized_subjects': subject_ids, 'previous_plan': previous})
        if type(planned) is not Committed: return planned
        plan_id = record(planned.receipt.result)['operation_id']
        plan = (await rows.read('release_plans_get', {'plan_id': plan_id}))[0]
        return await r.execute(cast(str, plan['command_kind']), cast(str, plan['execution_key']), {'plan_id': plan_id})

    def close(self):
        self.closed = True; self.grants.clear()
        return not self.jobs
