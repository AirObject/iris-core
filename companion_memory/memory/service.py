"""Finite capability-bound current and deep reads of actual memory-owned records.

Read grants do not grant source-window, original-media or history access. Object
reads never modify scores or lifecycle. A business gate is checked on each call;
developer observation is a separate projection and authority.
"""
from __future__ import annotations
from companion_memory.persistence.completion import finish_owned
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from weakref import WeakValueDictionary
from companion_memory.persistence import Found, NotFound, Value
from companion_memory.persistence.schema import InvalidValue, valid_identifier
from companion_memory.persistence.owned_statements import OwnerFailure
from .transactions import MemoryTransactions


@dataclass(frozen=True, slots=True)
class MemoryError:
    """Fixed public cause; the operation never exposes a payload or storage error."""
    code: str
    operation: str
    field: str
    reason: str
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class MemoryReadPort:
    """Native finite object authority, independent of raw-input entry permission."""
    _service: MemoryService
    _objects: frozenset[str]
    _operations: frozenset[str]

    def __init__(self):
        raise TypeError('Memory read authority is issued by trusted assembly.')

    def _native(self):
        if type(self) is not MemoryReadPort: return None
        try: owner = object.__getattribute__(self, '_service')
        except AttributeError: return None
        return owner if type(owner) is MemoryService else None

    async def get_current(self, object_id: object):
        """Return only an ACTIVE current value; absence includes forgotten/deleted."""
        owner = MemoryReadPort._native(self)
        if owner is None: return MemoryError('ACCESS_DENIED', 'get_current', 'capability', 'BINDING_MISMATCH')
        return await owner.read(self, object_id, 'get_current')

    async def get_for_deep_read(self, object_id: object):
        """Read the current existing value with its actual state, without restoring."""
        owner = MemoryReadPort._native(self)
        if owner is None: return MemoryError('ACCESS_DENIED', 'get_for_deep_read', 'capability', 'BINDING_MISMATCH')
        return await owner.read(self, object_id, 'get_for_deep_read')

    async def read_subject(self, subject_id: object):
        """Return only explicitly granted registered identity, without equivalence inference."""
        owner = MemoryReadPort._native(self)
        if owner is None: return MemoryError('ACCESS_DENIED', 'read_subject', 'capability', 'BINDING_MISMATCH')
        return await owner.read(self, subject_id, 'read_subject')

    async def read_basis_status(self, object_ids: object):
        """Read authorized current states or tombstones, never previous bodies."""
        owner = MemoryReadPort._native(self)
        if owner is None: return MemoryError('ACCESS_DENIED', 'read_basis_status', 'capability', 'BINDING_MISMATCH')
        return await owner.read_basis_status(self, object_ids)


    async def list_dirty_dependencies(self, query: object):
        """Inspect one explicitly granted object's pending events without consuming them."""
        owner = MemoryReadPort._native(self)
        if owner is None: return MemoryError('ACCESS_DENIED', 'list_dirty_dependencies', 'capability', 'BINDING_MISMATCH')
        return await owner.list_dirty_dependencies(self, query)


class MemoryService:
    """Trusted binding plus public bounded reads; mutation remains a UoW participant."""
    def __init__(self, transactions: MemoryTransactions, business_gate: Callable[[], str | None]):
        if type(transactions) is not MemoryTransactions:
            raise ValueError('A native memory owner is required.')
        self._owner, self._gate = transactions, business_gate
        self._grants: WeakValueDictionary[int, MemoryReadPort] = WeakValueDictionary()
        self._active = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._limit = transactions.configuration.candidate.content.integer('memory.read_concurrency')
        self._page = transactions.configuration.candidate.content.integer('memory.read_page_size')

    def bind_read(self, object_ids: tuple[str, ...], operations: tuple[str, ...]) -> MemoryReadPort:
        """Trusted assembly grants exact objects and independent current/deep operations."""
        if (self._closed or len(self._grants) >= self._page or type(object_ids) is not tuple or not 1 <= len(object_ids) <= 128
                or any(not valid_identifier(v) for v in object_ids) or type(operations) is not tuple
                or any(type(v) is not str for v in operations) or not set(operations) <= {'get_current', 'get_for_deep_read', 'read_basis_status', 'read_subject', 'list_dirty_dependencies'}):
            raise ValueError('Invalid memory read scope.')
        port = object.__new__(MemoryReadPort)
        object.__setattr__(port, '_service', self); object.__setattr__(port, '_objects', frozenset(object_ids))
        object.__setattr__(port, '_operations', frozenset(operations))
        self._grants[id(port)] = port
        return port

    def _authorize(self, port: object, oid: object, operation: str) -> MemoryError | None:
        if (type(port) is not MemoryReadPort or self._grants.get(id(port)) is not port
                or operation not in port._operations or type(oid) is not str or oid not in port._objects):
            return MemoryError('ACCESS_DENIED', operation, 'capability', 'OPERATION_NOT_GRANTED')
        if self._closed:
            return MemoryError('INVALID_STATE', operation, 'state', 'SERVICE_CLOSED')
        if not valid_identifier(oid):
            return MemoryError('INVALID_INPUT', operation, 'object', 'INVALID_IDENTIFIER')
        reason = self._gate()
        if reason is not None:
            return MemoryError('MODE_BLOCKED', operation, 'state', reason)
        return None

    async def _bounded(self, operation, work):
        if self._active >= self._limit: return MemoryError('RESOURCE_BUSY', operation, 'state', 'ADMISSION_FULL')
        self._active += 1
        async def owned():
            try: return await work()
            except OwnerFailure as failure: return MemoryError(failure.code, operation, failure.field, failure.reason, failure.cleanup_pending)
            except (InvalidValue, UnicodeError): return MemoryError('STORAGE_FAILED', operation, 'storage', 'INTEGRITY_FAILURE')
        task = asyncio.create_task(finish_owned(owned())); self._tasks.add(task)
        def ended(job):
            if not job.cancelled(): job.exception()
            self._tasks.discard(job)
            self._active -= 1
        task.add_done_callback(ended)
        timeout = self._owner.configuration.candidate.content.integer('memory.read_timeout_ms') / 1000
        done, _ = await asyncio.wait((task,), timeout=timeout)
        if not done: return MemoryError('TIMEOUT', operation, 'state', 'DEADLINE_EXCEEDED', True)
        return task.result()

    async def read(self, port: object, oid: object, operation: str):
        """Bound the whole read and recheck authority before releasing its value."""
        error = self._authorize(port, oid, operation)
        if error is not None: return error
        async def inspect():
            rows = await self._owner.rows.read('subjects_get', {'subject_id': oid}) if operation == 'read_subject' else await self._owner.rows.read('objects_get', {'object_id': oid})
            error = self._authorize(port, oid, operation)
            if error is not None: return error
            if not rows: return NotFound()
            if operation == 'read_subject':
                from .formats import isolate_subject
                from companion_memory.persistence.content_codec import decode_content, encode_content
                value = isolate_subject(decode_content(cast(str, rows[0]['body']).encode(), 1024))
                if value['instance_id'] != self._owner.instance_id or any(value[k] != rows[0][k] for k in ('subject_id', 'kind', 'revision', 'platform_id', 'external_subject_id')) or encode_content(value, 1024).decode() != rows[0]['body']: raise InvalidValue()
                return Found(value)
            value = self._owner.decode_current(rows[0])
            if operation == 'get_current' and value['lifecycle'] != 'ACTIVE': return NotFound()
            return Found(value)
        return await self._bounded(operation, inspect)

    async def read_basis_status(self, port: object, object_ids: object):
        """One finite page shares its total deadline and never reads historical text."""
        operation = 'read_basis_status'
        if type(port) is not MemoryReadPort or self._grants.get(id(port)) is not port or operation not in port._operations:
            return MemoryError('ACCESS_DENIED', operation, 'capability', 'OPERATION_NOT_GRANTED')
        if type(object_ids) is not tuple or not 1 <= len(object_ids) <= self._page:
            return MemoryError('INVALID_INPUT', operation, 'query', 'LIMIT_EXCEEDED')
        for oid in object_ids:
            error = self._authorize(port, oid, operation)
            if error is not None: return error
        async def inspect():
            results: list[Value] = []
            for oid in object_ids:
                rows = await self._owner.rows.read('objects_get', {'object_id': oid})
                if rows:
                    value = self._owner.decode_current(rows[0])
                    results.append(MappingProxyType({'object_id': cast(str, oid), 'revision': value['revision'], 'state': value['lifecycle']}))
                else:
                    rows = await self._owner.rows.read('tombstones_get', {'object_id': oid})
                    results.append(MappingProxyType({'object_id': cast(str, oid), 'state': 'DELETED' if rows else 'NOT_FOUND'}))
                error = self._authorize(port, oid, operation)
                if error is not None: return error
            return Found(tuple(results))
        return await self._bounded(operation, inspect)

    async def list_dirty_dependencies(self, port: object, query: object):
        """Use a fixed indexed query and independent internal inspection authority."""
        operation = 'list_dirty_dependencies'
        if type(port) is not MemoryReadPort or self._grants.get(id(port)) is not port or operation not in port._operations:
            return MemoryError('ACCESS_DENIED', operation, 'capability', 'OPERATION_NOT_GRANTED')
        if type(query) is not dict or set(query) - {'object_id', 'after_revision', 'after_reason', 'limit'}:
            return MemoryError('INVALID_INPUT', operation, 'query', 'INVALID_SHAPE')
        oid = query.get('object_id'); error = self._authorize(port, oid, operation)
        if error is not None: return error
        revision = query.get('after_revision', 0); reason = query.get('after_reason', ''); limit = query.get('limit', self._page)
        if (type(revision) is not int or not 0 <= revision < 2**63 or type(reason) is not str
                or reason not in ('', 'CONTENT_CHANGED', 'FORGOTTEN', 'RESTORED', 'DELETED')
                or type(limit) is not int or not 1 <= limit <= self._page):
            return MemoryError('INVALID_INPUT', operation, 'query', 'LIMIT_EXCEEDED')
        async def inspect():
            rows = await self._owner.rows.read('dependency_for_object', {'object_id': oid, 'after_revision': revision, 'after_reason': reason, 'limit': limit})
            error = self._authorize(port, oid, operation)
            if error is not None: return error
            following = MappingProxyType({'after_revision': rows[-1]['changed_revision'], 'after_reason': rows[-1]['reason']}) if len(rows) == limit else None
            return Found(MappingProxyType({'items': rows, 'next': following}))
        return await self._bounded(operation, inspect)

    def close(self) -> bool:
        """Stop new reads and retain owner isolation until active operations finish."""
        self._closed = True
        self._grants.clear()
        return self._active == 0 and self._owner.close()
