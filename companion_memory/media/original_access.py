"""Finite native original-byte inspection with persistent READ protection.

A read acquires protection before opening a file and releases it only after the
actual worker ends. Caller cancellation never cancels that ownership task. Safe
read results contain a bounded byte slice, never a filesystem path or hash.
"""
from __future__ import annotations
from companion_memory.persistence.completion import finish_owned
import asyncio
from dataclasses import dataclass, replace
import hashlib
import os
import stat
import uuid
from typing import TYPE_CHECKING, cast
from weakref import WeakValueDictionary
from companion_memory.persistence import Committed, NotCommitted, RecoveryHandle, ResultBoundCommand
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import valid_identifier, InvalidValue
if TYPE_CHECKING:
    from .service import MediaService


@dataclass(frozen=True, slots=True)
class OriginalChunk:
    """Bytes from one exact authorized occurrence and physical generation."""
    occurrence_id: str
    offset: int
    content: bytes
    eof: bool
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class OriginalInspection:
    """Finite occurrence scope issued independently of upload and memory reads."""
    _access: OriginalAccess
    _entry: str
    _occurrences: frozenset[str]

    def __init__(self): raise TypeError('Original inspection is issued by trusted assembly.')

    async def read_occurrence(self, occurrence_id: object):
        """Inspect one authorized occurrence and its exact selected understanding."""
        from .service import MediaError
        try: access = object.__getattribute__(self, '_access')
        except AttributeError: access = None
        if type(self) is not OriginalInspection or type(access) is not OriginalAccess:
            return MediaError('ACCESS_DENIED', 'read_occurrence', 'capability', 'BINDING_MISMATCH')
        return await access.read_occurrence(self, occurrence_id)


    async def read_original(self, occurrence_id: object, offset: object, length: object):
        """Read a bounded slice after native authority and actual generation checks."""
        from .service import MediaError
        try: access = object.__getattribute__(self, '_access')
        except AttributeError: access = None
        if type(self) is not OriginalInspection or type(access) is not OriginalAccess:
            return MediaError('ACCESS_DENIED', 'read_original', 'capability', 'BINDING_MISMATCH')
        return await access.read(self, occurrence_id, offset, length)


class OriginalAccess:
    """One media owner's bounded read tasks and unresolved cleanup obligations."""
    def __init__(self, media: MediaService):
        self.media = media
        self.grants: WeakValueDictionary[int, OriginalInspection] = WeakValueDictionary()
        self.jobs: dict[str, asyncio.Task] = {}
        self.pins: dict[str, tuple[str, str]] = {}
        self.closed = False

    def bind(self, entry_id: str, occurrence_ids: tuple[str, ...]) -> OriginalInspection:
        """Trusted review setup explicitly limits entry and occurrence identities."""
        if (self.closed or len(self.grants) >= self.media.settings.integer('media.gc_page_size') or not valid_identifier(entry_id) or type(occurrence_ids) is not tuple
                or not 1 <= len(occurrence_ids) <= 128 or any(not valid_identifier(v) for v in occurrence_ids)):
            raise ValueError('A finite original inspection grant is required.')
        grant = object.__new__(OriginalInspection)
        for key, value in (('_access', self), ('_entry', entry_id), ('_occurrences', frozenset(occurrence_ids))):
            object.__setattr__(grant, key, value)
        self.grants[id(grant)] = grant
        return grant

    async def read_occurrence(self, grant: OriginalInspection, occurrence_id: object):
        from types import MappingProxyType
        from companion_memory.persistence import Found, NotFound
        from .interpretations import decode_interpretation
        from .service import MediaError
        operation = 'read_occurrence'
        if self.grants.get(id(grant)) is not grant or type(occurrence_id) is not str or occurrence_id not in grant._occurrences:
            return MediaError('ACCESS_DENIED', operation, 'capability', 'OPERATION_NOT_GRANTED')
        if self.closed or not self.media._ready or self.media._closing:
            return MediaError('INVALID_STATE', operation, 'state', 'NOT_READY')
        if any(job.done() for rid, job in self.jobs.items() if rid in self.pins):
            await self.recover_cleanup()
        if len(self.jobs) >= self.media.settings.integer('media.read_concurrency'):
            return MediaError('RESOURCE_BUSY', operation, 'state', 'ADMISSION_FULL', True)
        rid = 'occurrence_read:' + uuid.uuid4().hex
        async def inspect():
            try:
                rows = await self.media.rows.read('inspect_occurrence', {'entry_id': grant._entry, 'occurrence_id': occurrence_id})
                if not rows: return NotFound()
                row = rows[0]
                if (row['interpretation_id'] is None) != (row['body'] is None):
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                understanding = None if row['body'] is None else decode_interpretation(cast(str, row['body']).encode())
                return Found(MappingProxyType({**{k: v for k, v in row.items() if k != 'body'}, 'interpretation': understanding}))
            except OwnerFailure as failure: return self.media._failure(operation, failure)
            except InvalidValue: return MediaError('STORAGE_FAILED', operation, 'storage', 'INTEGRITY_FAILURE')
        task = asyncio.create_task(finish_owned(inspect())); self.jobs[rid] = task
        def ended(job):
            if not job.cancelled(): job.exception()
            self.jobs.pop(rid, None)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((task,), timeout=self.media.settings.integer('media.operation_timeout_ms') / 1000)
        if not done: return MediaError('TIMEOUT', operation, 'state', 'DEADLINE_EXCEEDED', True)
        if self.closed or self.grants.get(id(grant)) is not grant:
            return MediaError('ACCESS_DENIED', operation, 'capability', 'OPERATION_NOT_GRANTED')
        return task.result()

    async def read(self, grant: OriginalInspection, occurrence_id: object, offset: object, length: object):
        from .service import MediaError
        if self.grants.get(id(grant)) is not grant or type(occurrence_id) is not str or occurrence_id not in grant._occurrences:
            return MediaError('ACCESS_DENIED', 'read_original', 'capability', 'OPERATION_NOT_GRANTED')
        if self.closed or not self.media._ready or self.media._closing:
            return MediaError('INVALID_STATE', 'read_original', 'state', 'NOT_READY')
        if (type(offset) is not int or not 0 <= offset <= self.media.settings.integer('media.blob_max_bytes')
                or type(length) is not int or not 1 <= length <= self.media.settings.integer('media.read_chunk_bytes')):
            return MediaError('INVALID_INPUT', 'read_original', 'input', 'LIMIT_EXCEEDED')
        if any(job.done() for rid, job in self.jobs.items() if rid in self.pins):
            await self.recover_cleanup()
        if len(self.jobs) >= self.media.settings.integer('media.read_concurrency'):
            return MediaError('RESOURCE_BUSY', 'read_original', 'state', 'ADMISSION_FULL', True)
        rid = 'read:' + uuid.uuid4().hex
        task = asyncio.create_task(finish_owned(self._run(rid, grant._entry, occurrence_id, offset, length)))
        self.jobs[rid] = task
        def ended(job):
            if not job.cancelled(): job.exception()
            if rid not in self.pins: self.jobs.pop(rid, None)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((task,), timeout=self.media.settings.integer('media.operation_timeout_ms') / 1000)
        if not done: return MediaError('TIMEOUT', 'read_original', 'state', 'DEADLINE_EXCEEDED', True)
        if self.closed or self.grants.get(id(grant)) is not grant:
            return MediaError('ACCESS_DENIED', 'read_original', 'capability', 'OPERATION_NOT_GRANTED')
        result = task.result()
        return replace(result, cleanup_pending=rid in self.pins) if type(result) is OriginalChunk else result

    async def _run(self, rid: str, entry_id: str, occurrence_id: str, offset: int, length: int):
        from .service import MediaError
        try:
            self.pins[rid] = entry_id, occurrence_id
            result = await self.media._execute('acquire_media_read', rid, {'read_id': rid, 'entry_id': entry_id, 'occurrence_id': occurrence_id})
            if type(result) is not Committed: return result
            rows = await self.media.rows.read('occurrences_get', {'occurrence_id': occurrence_id})
            if not rows: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            occurrence = rows[0]
            blobs = await self.media.rows.read('blobs_get', {'blob_id': occurrence['blob_id']})
            if not blobs: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            blob = blobs[0]
            path = self.media.published / (cast(str, blob['blob_id']) + '.' + str(blob['generation']))
            def read():
                fd = self.media.files.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    meta = os.fstat(fd)
                    if (not stat.S_ISREG(meta.st_mode) or (meta.st_dev, meta.st_ino, meta.st_size) !=
                            (blob['device'], blob['inode'], blob['byte_count']) or blob['generation'] != occurrence['generation']):
                        raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
                    digest = hashlib.sha256(); read_bytes = 0
                    while data := os.read(fd, self.media.settings.integer('media.read_chunk_bytes')):
                        read_bytes += len(data)
                        if read_bytes > meta.st_size:
                            raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
                        digest.update(data)
                    if read_bytes != meta.st_size:
                        raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
                    if digest.hexdigest() != blob['sha256']: raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
                    content = os.pread(fd, length, offset)
                    return OriginalChunk(occurrence_id, offset, content, offset + len(content) >= meta.st_size)
                finally: os.close(fd)
            value = await self.media.integrity.io(blob, rid, read)
            return value
        except (OwnerFailure, OSError) as failure:
            return self.media._failure('read_original', failure)
        finally:
            worker = self.media._jobs.get(rid)
            if worker is not None:
                try: await asyncio.shield(worker)
                except (OSError, OwnerFailure): pass
            if rid in self.pins:
                # This is an original-key local confirmation, never permission
                # inferred from a missed query or a caller's elapsed deadline.
                acquired = await self.media._execute('acquire_media_read', rid, {
                    'read_id': rid, 'entry_id': entry_id, 'occurrence_id': occurrence_id})
                if type(acquired) is not Committed:
                    port = self.media.operations['acquire_media_read']
                    command = ResultBoundCommand(1, {'read_id': rid, 'entry_id': entry_id, 'occurrence_id': occurrence_id},
                        {'media_changed': {'actor': 'media_owner'}})
                    handle = port.recovery_handle(rid, command)
                    if type(handle) is RecoveryHandle:
                        confirmed = await port.resolve_operation(handle)
                        if type(confirmed) is NotCommitted and (confirmed.error is None or not confirmed.error.cleanup_pending):
                            self.pins.pop(rid, None)
                else:
                    released = await self.media._execute('release_media_read', 'release:' + rid, {'read_id': rid, 'occurrence_id': occurrence_id})
                    if type(released) is Committed: self.pins.pop(rid, None)

    async def recover_cleanup(self) -> bool:
        """Resolve pins only after their original file reader and logical task ended."""
        for rid, (entry, occurrence) in tuple(self.pins.items()):
            job = self.jobs.get(rid)
            if job is not None and not job.done(): continue
            if rid in self.media._jobs: continue
            port = self.media.operations['acquire_media_read']
            command = ResultBoundCommand(1, {'read_id': rid, 'entry_id': entry, 'occurrence_id': occurrence}, {'media_changed': {'actor': 'media_owner'}})
            handle = port.recovery_handle(rid, command)
            if type(handle) is not RecoveryHandle: continue
            original = await port.resolve_operation(handle)
            if type(original) is NotCommitted and (original.error is None or not original.error.cleanup_pending):
                self.pins.pop(rid, None); self.jobs.pop(rid, None)
            elif type(original) is Committed:
                released = await self.media._execute('release_media_read', 'release:' + rid, {'read_id': rid, 'occurrence_id': occurrence})
                if type(released) is Committed: self.pins.pop(rid, None); self.jobs.pop(rid, None)
        return not self.pins

    async def close(self) -> bool:
        """Revoke new reads but keep ownership until every real read and pin ends."""
        self.closed = True; self.grants.clear()
        tasks = tuple(job for job in self.jobs.values() if not job.done())
        if tasks: await asyncio.wait(tasks, timeout=self.media.remaining_request())
        await self.recover_cleanup()
        return not self.jobs and not self.pins
