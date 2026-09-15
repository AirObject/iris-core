"""Independent finite source inspection, separate from ordinary memory reads.

Each point query verifies the authorized object's current source association.
Between bounded owner reads the immutable member is checked again, so a retired
source returns SOURCE_CHANGED instead of a fabricated partial complete window.
"""
from __future__ import annotations
from companion_memory.persistence.completion import finish_owned
import asyncio
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import cast
from weakref import WeakValueDictionary
from companion_memory.persistence import Found, Value
from companion_memory.persistence.schema import InvalidValue, valid_identifier
from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.ingress.media_events import decode_media_event
from companion_memory.ingress.events import EVENT_FORMAT_MAX_BYTES
from companion_memory.media.interpretations import INTERPRETATION_FORMAT_LIMIT
from companion_memory.media.interpretations import decode_interpretation
from companion_memory.media.service import MediaService
from .formats import isolate, record, sequence
from .sources import decode_source, MEMBER_SCHEMA
from .transactions import MemoryTransactions
from .service import MemoryError

SourceRead = Found[MappingProxyType[str, Value]] | MemoryError


# The member format permits two selections. Its own encoding is bounded at
# 2048; the remaining fixed outer keys and delimiters fit within 128 bytes.
SOURCE_MEMBER_RESULT_LIMIT = EVENT_FORMAT_MAX_BYTES + 2048 + 2 * INTERPRETATION_FORMAT_LIMIT + 128


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class SourceInspection:
    """Explicit developer/internal review authority for finite current object links."""
    _owner: SourceAccess
    _objects: frozenset[str]

    def __init__(self): raise TypeError('Source inspection is issued by trusted assembly.')

    async def read_source_manifest(self, object_id: object, source_id: object) -> SourceRead:
        """Read the complete immutable manifest for an authorized current link."""
        try: owner = object.__getattribute__(self, '_owner')
        except AttributeError: owner = None
        if type(self) is not SourceInspection or type(owner) is not SourceAccess:
            return MemoryError('ACCESS_DENIED', 'read_source_manifest', 'capability', 'BINDING_MISMATCH')
        return await owner.read(self, object_id, source_id, None)

    async def read_source_member(self, object_id: object, source_id: object, ordinal: object) -> SourceRead:
        """Read one original event and its exact selected interpretation versions."""
        try: owner = object.__getattribute__(self, '_owner')
        except AttributeError: owner = None
        if type(self) is not SourceInspection or type(owner) is not SourceAccess:
            return MemoryError('ACCESS_DENIED', 'read_source_member', 'capability', 'BINDING_MISMATCH')
        if type(ordinal) is not int or not 0 <= ordinal < 4:
            return MemoryError('INVALID_INPUT', 'read_source_member', 'query', 'INVALID_SHAPE')
        return await owner.read(self, object_id, source_id, ordinal)


class SourceAccess:
    """Native source review binds memory and the actual ingress/media owners."""
    def __init__(self, memory: MemoryTransactions, media: MediaService | None):
        self._memory, self._media = memory, media
        self._grants: WeakValueDictionary[int, SourceInspection] = WeakValueDictionary()
        self._active = 0; self._closed = False
        self._tasks: set[asyncio.Task] = set()

    def bind_inspection(self, object_ids: tuple[str, ...]) -> SourceInspection:
        """Trusted setup issues source.review/source.inspect independently of agents."""
        if self._closed or len(self._grants) >= self._memory.configuration.candidate.content.integer('memory.read_page_size') or type(object_ids) is not tuple or not 1 <= len(object_ids) <= 128 or any(not valid_identifier(v) for v in object_ids):
            raise ValueError('Invalid source inspection scope.')
        grant = object.__new__(SourceInspection)
        object.__setattr__(grant, '_owner', self); object.__setattr__(grant, '_objects', frozenset(object_ids))
        self._grants[id(grant)] = grant
        return grant

    async def read(self, grant: object, object_id: object, source_id: object, ordinal: int | None) -> SourceRead:
        operation = 'read_source_manifest' if ordinal is None else 'read_source_member'
        if type(grant) is not SourceInspection or self._grants.get(id(grant)) is not grant:
            return MemoryError('ACCESS_DENIED', operation, 'capability', 'BINDING_MISMATCH')
        if self._active >= self._memory.configuration.candidate.content.integer('memory.read_concurrency'):
            return MemoryError('RESOURCE_BUSY', operation, 'state', 'ADMISSION_FULL')
        self._active += 1
        async def owned():
            result = await self._read_owned(grant, object_id, source_id, ordinal)
            if self._closed or self._grants.get(id(grant)) is not grant:
                return MemoryError('ACCESS_DENIED', operation, 'capability', 'BINDING_MISMATCH')
            return result
        task = asyncio.create_task(finish_owned(owned())); self._tasks.add(task)
        def ended(job):
            if not job.cancelled(): job.exception()
            self._tasks.discard(job)
            self._active -= 1
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((task,), timeout=self._memory.configuration.candidate.content.integer('memory.read_timeout_ms') / 1000)
        if not done: return MemoryError('TIMEOUT', operation, 'state', 'DEADLINE_EXCEEDED', True)
        return task.result()

    async def _read_owned(self, grant: object, object_id: object, source_id: object, ordinal: int | None) -> SourceRead:
        operation = 'read_source_manifest' if ordinal is None else 'read_source_member'
        if type(grant) is not SourceInspection or self._grants.get(id(grant)) is not grant or type(object_id) is not str or object_id not in grant._objects:
            return MemoryError('ACCESS_DENIED', operation, 'capability', 'OPERATION_NOT_GRANTED')
        if self._closed: return MemoryError('INVALID_STATE', operation, 'state', 'SERVICE_CLOSED')
        if not valid_identifier(source_id): return MemoryError('INVALID_INPUT', operation, 'source', 'INVALID_IDENTIFIER')
        try:
            args = {'object_id': object_id, 'source_id': source_id}
            rows = await self._memory.rows.read('review_manifest', args)
            if not rows: return MemoryError('PRECONDITION_FAILED', operation, 'source', 'SOURCE_CHANGED')
            from .fixed_source import is_fixed_source,decode_fixed_source,fixed_digest
            if self._memory.semantic_format and is_fixed_source(cast(str,rows[0]['body'])):
                manifest=decode_fixed_source(cast(str,rows[0]['body']))
                if fixed_digest(manifest)!=rows[0]['digest']:raise InvalidValue()
                if await self._memory.rows.read('review_manifest',args)!=rows:
                    return MemoryError('PRECONDITION_FAILED',operation,'source','SOURCE_CHANGED')
                if ordinal is None:return Found(manifest)
                if ordinal!=0:return MemoryError('PRECONDITION_FAILED',operation,'source','SOURCE_CHANGED')
                return Found(MappingProxyType({'event':manifest['event'],'interpretations':()}))
            manifest = decode_source(cast(str, rows[0]['body']))
            if manifest['digest'] != rows[0]['digest']: raise InvalidValue()
            if ordinal is None: return Found(manifest)
            rows = await self._memory.rows.read('review_member', {**args, 'ordinal': ordinal})
            if not rows: return MemoryError('PRECONDITION_FAILED', operation, 'source', 'SOURCE_CHANGED')
            member = isolate(MEMBER_SCHEMA, decode_content(cast(str, rows[0]['body']).encode(), 2048), 2048)
            if ordinal >= len(sequence(manifest['ordered_members'])) or member != sequence(manifest['ordered_members'])[ordinal]:
                raise InvalidValue()
            from companion_memory.ingress.content_storage import ContentIngressTransactions
            if type(self._memory.sources) is not ContentIngressTransactions:
                return MemoryError('CAPABILITY_UNAVAILABLE', operation, 'source', 'OWNER_MISSING')
            ingress = cast(ContentIngressTransactions, self._memory.sources)
            payloads = await ingress.rows.read('payload', {'message_id': member['message_id']})
            if not payloads: return MemoryError('PRECONDITION_FAILED', operation, 'source', 'SOURCE_CHANGED')
            encoded = cast(str, payloads[0]['body']).encode()
            event = decode_media_event(encoded, 8192, occurrence_limit=2, text_limit=512)
            if hashlib.sha256(encoded).hexdigest() != member['payload_digest']: raise InvalidValue()
            interpretations: list[Value] = []
            for item in sequence(member['media']):
                if self._media is None: return MemoryError('CAPABILITY_UNAVAILABLE', operation, 'media', 'OWNER_MISSING')
                selected = record(item)
                values = await self._media.rows.read('interpretations_get', {'interpretation_id': selected['interpretation_id']})
                if not values: raise InvalidValue()
                value = decode_interpretation(cast(str, values[0]['body']).encode())
                if value['interpretation_id'] != selected['interpretation_id']: raise InvalidValue()
                interpretations.append(value)
            again = await self._memory.rows.read('review_member', {**args, 'ordinal': ordinal})
            if again != rows: return MemoryError('PRECONDITION_FAILED', operation, 'source', 'SOURCE_CHANGED')
            result = MappingProxyType({'member': member, 'event': event, 'interpretations': tuple(interpretations)})
            encode_content(result, SOURCE_MEMBER_RESULT_LIMIT)
            return Found(result)
        except OwnerFailure as failure:
            return MemoryError(failure.code, operation, failure.field, failure.reason, failure.cleanup_pending)
        except (InvalidValue, UnicodeError):
            return MemoryError('STORAGE_FAILED', operation, 'storage', 'INTEGRITY_FAILURE')

    def close(self) -> bool:
        """Revoke inspection without releasing the memory owner's storage lease."""
        self._closed = True; self._grants.clear()
        return self._active == 0
