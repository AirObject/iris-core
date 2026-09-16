"""Real immutable file publication, occurrence binding and generation-safe GC.

File I/O runs in a finite executor outside SQLite transactions. Upload intent,
sealed bytes, READY binding and DELETE_PENDING are separately confirmed facts.
Native finite entry grants confer no path/hash access. Timed-out file workers
retain their actual slot and block reuse until they have ended.
"""
from __future__ import annotations
from companion_memory.persistence.completion import CompletionScope, finish_owned, retain_completion, start_owned
import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import time
from types import MappingProxyType
from typing import cast
from weakref import WeakValueDictionary
from companion_memory.configuration.content_persistence import StoredContentConfiguration
from companion_memory.configuration.text_persistence import StoredTextConfiguration,stored_text_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration,stored_semantic_configuration_issue
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.persistence import (
    AuditFieldBinding, AuditResultBinding, BoundedTextSchema, Committed, Field, Found,
    NotCommitted, PersistenceService, RecordSchema, RecoveryHandle, Rejected,
    ResultBoundCommand, ResultBoundCommandDefinition, SequenceSchema, UnitOfWork,
    Unconfirmed, Value, Receipt,
)
from companion_memory.persistence.schema import InvalidValue, ScalarSchema, valid_identifier, freeze_value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import BoundStatements, OwnerFailure, OwnerCauses
from companion_memory.logging_service import AuditRequirement
from companion_memory.memory.formats import ID, INT, REVISION, enum, record, sequence
from .storage import media_catalog
from .physical_paths import PhysicalDirectories
from .health import MediaHealth
from .processing_bytes import ProcessingRead
from .interpretations import isolate_interpretation, decode_interpretation


def identity(kind: str, *parts: Value) -> str:
    """Typed stable identifiers derived only from retained protocol constituents."""
    return kind + ':' + hashlib.sha256(encode_content(tuple(parts), 4096)).hexdigest()


@dataclass(frozen=True, slots=True)
class MediaError:
    """Safe media failure; no path, hash, raw byte or underlying exception escapes."""
    code: str
    operation: str
    field: str
    reason: str
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class MediaUnconfirmed:
    """Original local operation remains unconfirmed independently of cleanup."""
    handle: RecoveryHandle
    error: MediaError


@dataclass(frozen=True, slots=True)
class VolatileProgress:
    """Only the current temporary writer's progress, with no restart durability claim."""
    upload_id: str
    offset: int
    state: str = 'VOLATILE_PROGRESS'


MediaUploadResult = Committed | Found[Receipt] | MediaError | MediaUnconfirmed | VolatileProgress


@dataclass(frozen=True, slots=True)
class MediaResources:
    """Expected root identity is retained independently before initialization."""
    root_id: str
    retained_identity_check: Callable[[str, str, str], bool]


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class MediaUploadPort:
    """One entry's upload capability; IDs and metadata cannot widen its scope."""
    _service: MediaService
    _entry_id: str

    def __init__(self): raise TypeError('Media authority is issued by trusted assembly.')

    def _native(self):
        if type(self) is not MediaUploadPort: return None
        try: owner = object.__getattribute__(self, '_service')
        except AttributeError: return None
        return owner if type(owner) is MediaService else None

    async def begin_upload(self, key: object, modality: object) -> MediaUploadResult:
        """Commit the original upload intent before creating its temporary file."""
        owner = MediaUploadPort._native(self)
        if owner is None: return MediaError('ACCESS_DENIED', 'begin_upload', 'capability', 'BINDING_MISMATCH')
        return await owner.bounded_upload(self, 'begin_upload', (key, modality))

    async def append_upload(self, upload_id: object, offset: object, data: object) -> MediaUploadResult:
        """Append a bounded contiguous block; duplicate blocks require exact bytes."""
        owner = MediaUploadPort._native(self)
        if owner is None: return MediaError('ACCESS_DENIED', 'append_upload', 'capability', 'BINDING_MISMATCH')
        return await owner.bounded_upload(self, 'append_upload', (upload_id, offset, data))

    async def finish_upload(self, upload_id: object) -> MediaUploadResult:
        """Seal, publish without overwrite, sync and confirm the READY binding."""
        owner = MediaUploadPort._native(self)
        if owner is None: return MediaError('ACCESS_DENIED', 'finish_upload', 'capability', 'BINDING_MISMATCH')
        return await owner.bounded_upload(self, 'finish_upload', (upload_id,))

    async def resolve_upload(self, key: object, modality: object) -> MediaUploadResult:
        """Read the original intent receipt; this does not upload or understand bytes."""
        owner = MediaUploadPort._native(self)
        if owner is None: return MediaError('ACCESS_DENIED', 'resolve_upload', 'capability', 'BINDING_MISMATCH')
        return await owner.bounded_upload(self, 'resolve_upload', (key, modality))


class MediaService:
    """Static media owner and lifecycle service, using exclusively configured limits."""
    def __init__(self,*,daily_format:bool=False):
        self.daily_format = daily_format
        self.catalog = media_catalog(daily_format=daily_format); self.repositories = (self.catalog.definition,)
        self.files: PhysicalDirectories
        from .lifecycle import MediaLifecycle
        self.lifecycle = MediaLifecycle(self)
        self._processing_reads: set[CompletionScope] = set()
        self._physical_fault = False
        self._bound = False; self._ready = False; self._closing = False
        self._root_fd: int | None = None; self._owner_fd: int | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._jobs: dict[str, asyncio.Future] = {}; self._uploads: set[str] = set()
        self._grants: WeakValueDictionary[int, MediaUploadPort] = WeakValueDictionary()
        self._publishing = False
        self._upload_owners: set[str] = set()
        self._publication_futures: set[asyncio.Future] = set()
        self._publication_context: ContextVar[bool] = ContextVar('media_publication_context', default=False)
        self._initialization: asyncio.Task | None = None
        self._gc_job: asyncio.Task | None = None
        self._closing_task: asyncio.Task | None = None
        self._initialization_identity: tuple[str, str] | None = None
        self._retained_resources: MediaResources | None = None
        self._file_cursor: tuple[str, str] = ('reads', '')
        self._offsets: dict[str, int] = {}
        self._upload_deadlines: dict[str, float] = {}
        self._cleanup_pending: set[str] = set()
        self._upload_cursor = ''
        self._command_jobs: set[asyncio.Task] = set()
        self._command_admission = asyncio.Lock()
        self._causes = OwnerCauses()
        self._upload_jobs: set[asyncio.Task] = set()
        self._request_deadline: ContextVar[float | None] = ContextVar('media_request_deadline', default=None)
        from .original_access import OriginalAccess
        self.originals = OriginalAccess(self)
        from .work_transactions import MediaWorkTransactions
        self.work = MediaWorkTransactions(self)
        from .integrity import MediaIntegrity
        self.integrity = MediaIntegrity(self)
        self._definitions()

    def _definitions(self) -> None:
        change = RecordSchema((Field('resource_id', ID), Field('generation', INT), Field('state', ID),
            Field('byte_count', INT), Field('reference_changes', INT)))
        targets = SequenceSchema(RecordSchema((Field('object_id', ID), Field('previous_revision', INT, nullable=True), Field('revision', INT))), 1, 16)
        result = RecordSchema((Field('upload_id', ID, nullable=True), Field('blob_id', ID, nullable=True), Field('generation', INT),
            Field('state', ID), Field('byte_count', INT), Field('targets', targets), Field('change', change)))
        operations = {
            'bind_media_staging': (Field('upload_id', ID), Field('writer_generation', REVISION), Field('staging_device', INT), Field('staging_inode', INT)),
            'block_media_integrity': (Field('blob_id', ID), Field('generation', REVISION), Field('reason', enum('CONTENT_MISSING', 'CONTENT_CORRUPT', 'RESOURCE_IDENTITY_MISMATCH'))),
            'resume_media_upload': (Field('upload_id', ID), Field('previous_generation', REVISION)),
            'require_media_reupload': (Field('upload_id', ID),),
            'initialize_media_root': (Field('root_id', ID), Field('device', INT), Field('inode', INT), Field('staging_device', INT), Field('staging_inode', INT), Field('published_device', INT), Field('published_inode', INT)),
            'begin_media_upload': (Field('upload_id', ID), Field('entry_id', ID), Field('modality', enum('IMAGE', 'AUDIO', 'VIDEO')), Field('started_at_us', INT)),
            'seal_media_upload': (Field('upload_id', ID), Field('blob_id', ID), Field('sha256', ID), Field('byte_count', REVISION), Field('generation', REVISION),
                Field('staging_device', INT), Field('staging_inode', INT)),
            'publish_media_upload': (Field('upload_id', ID), Field('device', INT), Field('inode', INT)),
            'retire_media_blob': (Field('blob_id', ID), Field('generation', REVISION), Field('references_revision', REVISION), Field('gc_operation', ID)),
            'delete_media_blob': (Field('blob_id', ID), Field('generation', REVISION), Field('gc_operation', ID)),
            'acquire_media_read': (Field('read_id', ID), Field('entry_id', ID), Field('occurrence_id', ID)),
            'release_media_read': (Field('read_id', ID), Field('occurrence_id', ID)),
            'abandon_media_upload': (Field('upload_id', ID), Field('now_us', INT)),
        }
        commands = []
        for name, fields in operations.items():
            requirement = AuditRequirement('media', 'media_changed', name.upper(), 1, ('APPLY',), change)
            def handler(uow, values, operation=name):
                try: return self._handle(operation, uow, values)
                except OwnerFailure as failure:
                    self._causes.record(operation, values, failure)
                    raise
            commands.append(ResultBoundCommandDefinition('media', name, 1, RecordSchema(fields), 1, result, self.repositories,
                (requirement,), handler, RecordSchema((Field('actor', ID),)), (AuditResultBinding('media_changed', 1, (
                    AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                    AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                    AuditFieldBinding('change', 'RESULT', ('change',)),)),)))
        self.commands = tuple(commands)

    def daily_reference_targets(self,uow:UnitOfWork,blob_ids:tuple[str,...]):
        """Read actual bounded blob reference revisions under the media owner."""
        from companion_memory.persistence.daily_results import target
        if type(self.configuration) is not StoredDailyConfiguration or not 1<=len(blob_ids)<=8:raise InvalidValue()
        return tuple(target(bid,cast(int,self._blob(uow,bid)['references_revision'])) for bid in blob_ids)

    def bind(self, storage: PersistenceService, configuration: StoredContentConfiguration | StoredTextConfiguration | StoredSemanticConfiguration | StoredDailyConfiguration, instance_id: str) -> None:
        """Bind once to actual persistent configuration and an exclusive owner lease."""
        if self._bound or (type(configuration) is not StoredContentConfiguration and stored_text_configuration_issue(configuration) is not None
                and stored_semantic_configuration_issue(configuration) is not None and stored_daily_configuration_issue(configuration) is not None): raise ValueError('Native unbound media configuration required.')
        if type(configuration) is StoredDailyConfiguration and configuration.scope_id!=instance_id:raise ValueError('Daily media scope differs.')
        self.storage, self.configuration, self.instance_id = storage, configuration, instance_id
        self.settings = configuration.candidate.content
        from .work_rows import MediaWorkRows
        self.rows = MediaWorkRows(self.catalog, storage, instance_id)
        self._lease = storage.claim_module_owner(self.catalog.definition)
        if self._lease is None or self._lease.database_id != configuration.database_id:
            raise ValueError('Media database owner is unavailable.')
        self.operations = {d.operation_kind: storage.bind_operation(d, instance_id) for d in self.commands}
        self.root = Path(cast(str, self.settings.value('media.root_directory')))
        self.staging = Path(cast(str, self.settings.value('media.staging_directory')))
        self.published = self.root / 'published'
        self._executor = ThreadPoolExecutor(max_workers=self.settings.integer('media.file_worker_capacity'), thread_name_prefix='media-files')
        self._bound = True

    def _get(self, name: str, uow: UnitOfWork, key: str, value: Value) -> MappingProxyType[str, Value]:
        rows = self.rows.stage(name + '_get', uow, {key: value})
        if not rows: raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_NOT_READY')
        return rows[0]

    def _report(self, resource: str, state: str, *, upload: Value = None, blob: Value = None, generation: int = 0, size: int = 0, refs: int = 0):
        return {'upload_id': upload, 'blob_id': blob, 'generation': generation, 'state': state, 'byte_count': size,
            'targets': [{'object_id': resource, 'previous_revision': None, 'revision': generation}],
            'change': {'resource_id': resource, 'generation': generation, 'state': state, 'byte_count': size, 'reference_changes': refs}}

    def _invalidate_directories(self) -> None:
        """A lost physical directory identity fences all media use for this owner."""
        self._physical_fault = True
        self._ready = False
        self.integrity.notify()

    def _blob(self, uow: UnitOfWork, bid: str, *, check_integrity: bool = True) -> MappingProxyType[str, Value]:
        if self._physical_fault and check_integrity: raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
        row = self._get('blobs', uow, 'blob_id', bid)
        if check_integrity: self.integrity.check(uow, row)
        actual = self.rows.stage('reference_count', uow, {'blob_id': bid, 'generation': row['generation']})[0]['count']
        if actual != row['reference_count']: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return row

    def _reference(self, uow: UnitOfWork, bid: str, generation: int, kind: str, owner: str, occurrence: str | None, acquire: bool, now_us: int) -> None:
        row = self._blob(uow, bid, check_integrity=acquire)
        if row['state'] != 'READY' or row['generation'] != generation:
            raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_RETIRING' if row['state'] == 'DELETE_PENDING' else 'BLOB_NOT_READY')
        if row['references_revision'] == 2**63 - 1: raise InvalidValue()
        rid = identity('media_ref', kind, owner, bid, generation, occurrence)
        if acquire:
            self.rows.stage('references_insert', uow, {'reference_id': rid, 'blob_id': bid, 'generation': generation,
                'owner_kind': kind, 'owner_id': owner, 'occurrence_id': occurrence})
        elif len(self.rows.stage('references_delete', uow, {'reference_id': rid})) != 1:
            raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
        count = cast(int, row['reference_count']) + (1 if acquire else -1)
        if count < 0: raise InvalidValue()
        self.rows.stage('blobs_update', uow, {**row, 'references_revision': cast(int, row['references_revision']) + 1,
            'reference_count': count, 'unreferenced_at_us': now_us if count == 0 else None})

    def _handle(self, name: str, uow: UnitOfWork, v: MappingProxyType[str, Value]):
        if not self._bound: raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        if name == 'bind_media_staging':
            upload = self._get('uploads', uow, 'upload_id', v['upload_id'])
            if upload['state'] not in ('UPLOADING', 'REUPLOAD_REQUIRED') or upload['writer_generation'] != v['writer_generation']:
                raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'WORK_FENCED')
            self.rows.stage('uploads_update', uow, {**upload, **v})
            return self._report(cast(str, v['upload_id']), 'UPLOADING', upload=v['upload_id'])
        if name == 'block_media_integrity':
            blob = self._get('blobs', uow, 'blob_id', v['blob_id'])
            if blob['generation'] != v['generation']: raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_NOT_READY')
            self.rows.stage('integrity_insert', uow, dict(v))
            return self._report(cast(str, v['blob_id']), 'INTEGRITY_BLOCKED', blob=v['blob_id'], generation=cast(int, v['generation']))
        if name == 'resume_media_upload':
            upload = self._get('uploads', uow, 'upload_id', v['upload_id'])
            if upload['state'] != 'REUPLOAD_REQUIRED' or upload['writer_generation'] != v['previous_generation'] or upload['writer_generation'] == 2**63 - 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'WORK_FENCED')
            self.rows.stage('uploads_update', uow, {**upload, 'state': 'UPLOADING', 'writer_generation': cast(int, upload['writer_generation']) + 1})
            return self._report(cast(str, v['upload_id']), 'UPLOADING', upload=v['upload_id'])
        if name == 'require_media_reupload':
            upload = self._get('uploads', uow, 'upload_id', v['upload_id'])
            if upload['state'] != 'UPLOADING': raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'UPLOAD_EXPIRED')
            self.rows.stage('uploads_update', uow, {**upload, 'state': 'REUPLOAD_REQUIRED'})
            return self._report(cast(str, v['upload_id']), 'REUPLOAD_REQUIRED', upload=v['upload_id'])
        if name == 'initialize_media_root':
            self.rows.stage('roots_insert', uow, {**v, 'database_id': self.configuration.database_id})
            return self._report(cast(str, v['root_id']), 'READY')
        if name in ('acquire_media_read', 'release_media_read'):
            occurrence = self._get('occurrences', uow, 'occurrence_id', v['occurrence_id'])
            acquire = name == 'acquire_media_read'
            if acquire and (occurrence['entry_id'] != v['entry_id'] or occurrence['state'] != 'ATTACHED'):
                raise OwnerFailure('ACCESS_DENIED', 'media', 'BINDING_MISMATCH')
            self._reference(uow, cast(str, occurrence['blob_id']), cast(int, occurrence['generation']),
                'READ', cast(str, v['read_id']), cast(str, v['occurrence_id']), acquire, time.time_ns() // 1000)
            return self._report(cast(str, v['read_id']), 'READ_ACQUIRED' if acquire else 'READ_RELEASED', refs=1)
        if name == 'begin_media_upload':
            if len(self._uploads) > self.settings.integer('media.upload_concurrency'): raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL')
            self.rows.stage('uploads_insert', uow, {**v, 'state': 'UPLOADING', 'blob_id': None, 'generation': None, 'byte_count': 0,
                'sha256': None, 'staging_device': None, 'staging_inode': None, 'writer_generation': 1, 'ready_at_us': None})
            return self._report(cast(str, v['upload_id']), 'UPLOADING', upload=v['upload_id'])
        if name == 'seal_media_upload':
            upload = self._get('uploads', uow, 'upload_id', v['upload_id'])
            if upload['state'] != 'UPLOADING' or cast(int, v['byte_count']) > self.settings.integer('media.blob_max_bytes'):
                raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'OFFSET_MISMATCH')
            blobs = self.rows.stage('blobs_get', uow, {'blob_id': v['blob_id']})
            if blobs:
                blob = self._blob(uow, cast(str, v['blob_id']))
                if blob['sha256'] != v['sha256'] or blob['byte_count'] != v['byte_count']:
                    raise OwnerFailure('FILE_FAILED', 'media', 'HASH_COLLISION')
                if blob['state'] == 'READY':
                    if blob['generation'] != v['generation']: raise InvalidValue()
                elif blob['state'] == 'DELETED' and cast(int, blob['generation']) + 1 == v['generation']:
                    self.rows.stage('blobs_update', uow, {**blob, 'generation': v['generation'], 'state': 'PUBLISHING', 'device': None, 'inode': None,
                        'references_revision': cast(int, blob['references_revision']) + 1, 'unreferenced_at_us': None, 'gc_operation': None})
                else: raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_RETIRING')
            else:
                if v['generation'] != 1: raise InvalidValue()
                self.rows.stage('blobs_insert', uow, {'blob_id': v['blob_id'], 'sha256': v['sha256'], 'byte_count': v['byte_count'],
                    'declared_modality': upload['modality'], 'generation': 1, 'state': 'PUBLISHING', 'device': None, 'inode': None,
                    'references_revision': 1, 'reference_count': 0, 'unreferenced_at_us': None, 'gc_operation': None})
            self.rows.stage('uploads_update', uow, {**upload, **v, 'state': 'SEALED'})
            return self._report(cast(str, v['upload_id']), 'SEALED', upload=v['upload_id'], blob=v['blob_id'], generation=cast(int, v['generation']), size=cast(int, v['byte_count']))
        if name == 'publish_media_upload':
            upload = self._get('uploads', uow, 'upload_id', v['upload_id']); bid = cast(str, upload['blob_id'])
            if upload['state'] != 'SEALED': raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'OFFSET_MISMATCH')
            blob = self._blob(uow, bid)
            if blob['generation'] != upload['generation'] or blob['state'] not in ('PUBLISHING', 'READY'):
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_RETIRING')
            if blob['state'] == 'READY' and (blob['device'], blob['inode']) != (v['device'], v['inode']):
                raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
            self.rows.stage('blobs_update', uow, {**blob, 'state': 'READY', 'device': v['device'], 'inode': v['inode']})
            self._reference(uow, bid, cast(int, blob['generation']), 'UPLOAD', cast(str, v['upload_id']), None, True, cast(int, upload['started_at_us']))
            self.rows.stage('uploads_update', uow, {**upload, 'state': 'READY', 'ready_at_us': time.time_ns() // 1000})
            return self._report(cast(str, v['upload_id']), 'READY', upload=v['upload_id'], blob=bid, generation=cast(int, blob['generation']), size=cast(int, blob['byte_count']), refs=1)
        if name == 'abandon_media_upload':
            upload = self._get('uploads', uow, 'upload_id', v['upload_id'])
            if (upload['state'] not in ('READY', 'UPLOADING', 'REUPLOAD_REQUIRED')
                    or self.rows.stage('upload_bindings_get', uow, {'upload_id': v['upload_id']})
                    or cast(int, v['now_us']) < self.upload_expires_at_us(upload)):
                raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'UPLOAD_EXPIRED')
            refs = int(upload['state'] == 'READY')
            if refs:
                self._reference(uow, cast(str, upload['blob_id']), cast(int, upload['generation']), 'UPLOAD', cast(str, v['upload_id']), None, False, cast(int, v['now_us']))
            self.rows.stage('uploads_update', uow, {**upload, 'state': 'ABANDONED'})
            return self._report(cast(str, v['upload_id']), 'ABANDONED', upload=v['upload_id'], refs=refs)
        blob = self._blob(uow, cast(str, v['blob_id']))
        if blob['generation'] != v['generation']: raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_RETIRING')
        if name == 'retire_media_blob':
            v = MappingProxyType({**v, 'now_us': time.time_ns() // 1000})
            if blob['state'] != 'READY' or blob['references_revision'] != v['references_revision'] or blob['reference_count'] != 0 or blob['unreferenced_at_us'] is None:
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
            if cast(int, v['now_us']) - cast(int, blob['unreferenced_at_us']) < self.settings.integer('media.gc_unreferenced_grace_ms') * 1000:
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
            state = 'DELETE_PENDING'
        else:
            if blob['state'] != 'DELETE_PENDING' or blob['gc_operation'] != v['gc_operation'] or blob['reference_count']:
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
            state = 'DELETED'
        self.rows.stage('blobs_update', uow, {**blob, 'state': state, 'gc_operation': v['gc_operation']})
        return self._report(cast(str, v['blob_id']), state, blob=v['blob_id'], generation=cast(int, v['generation']), size=cast(int, blob['byte_count']))

    def upload_retention_ms(self, upload: MappingProxyType[str, Value]) -> int:
        """READY binding retention is independent of the incomplete upload deadline."""
        return self.settings.integer('media.unbound_upload_retention_ms' if upload['state'] == 'READY' else 'media.upload_total_timeout_ms')

    def upload_expires_at_us(self, upload: MappingProxyType[str, Value]) -> int:
        """Bind incomplete upload time to begin and unbound READY retention to publication."""
        since = upload['ready_at_us'] if upload['state'] == 'READY' else upload['started_at_us']
        if type(since) is not int: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return since + self.upload_retention_ms(upload) * 1000

    def remaining_request(self) -> float:
        deadline = self._request_deadline.get()
        return self.settings.integer('media.operation_timeout_ms') / 1000 if deadline is None else max(0.0, deadline - time.monotonic())

    async def bounded_upload(self, port: MediaUploadPort, operation: str, arguments: tuple[object, ...]) -> MediaUploadResult:
        issue = self._authorized(port, operation)
        if issue is not None: return issue
        if len(self._upload_jobs) >= self.settings.integer('media.file_worker_capacity'):
            return MediaError('RESOURCE_BUSY', operation, 'state', 'ADMISSION_FULL', True)
        upload_identity = (identity('upload', self.configuration.database_id, port._entry_id, arguments[0])
            if operation in ('begin_upload', 'resolve_upload') and type(arguments[0]) is str else arguments[0])
        if type(upload_identity) is not str: return MediaError('INVALID_INPUT', operation, 'input', 'INVALID_SHAPE')
        if operation == 'resolve_upload': upload_identity = 'resolve:' + upload_identity
        if upload_identity in self._upload_owners or upload_identity in self._jobs:
            return MediaError('RESOURCE_BUSY', operation, 'state', 'OWNER_ACTIVE', True)
        self._upload_owners.add(upload_identity)
        deadline = time.monotonic() + self.settings.integer('media.operation_timeout_ms') / 1000
        completion: asyncio.Future[MediaUploadResult] = asyncio.get_running_loop().create_future()
        completion.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        async def owned():
            token = self._request_deadline.set(deadline)
            own_completion = CompletionScope()
            own_completion.__enter__()
            try:
                result = await getattr(self, operation)(port, *arguments)
                if upload_identity not in self._jobs and not own_completion.pending:
                    self._upload_owners.discard(upload_identity)
                completion.set_result(result)
                return result
            except BaseException as failure:
                if not completion.done(): completion.set_exception(failure)
                raise
            finally:
                self._request_deadline.reset(token)
                worker = self._jobs.get(upload_identity)
                if worker is not None:
                    try: await asyncio.shield(worker)
                    except (OwnerFailure, OSError): pass
                own_completion.__exit__()
                await own_completion.wait()
                self._upload_owners.discard(upload_identity)
        task = asyncio.create_task(owned()); self._upload_jobs.add(task)
        def ended(job):
            if not job.cancelled(): job.exception()
            self._upload_jobs.discard(job)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((completion,), timeout=max(0.0, deadline - time.monotonic()))
        if not done: return MediaError('TIMEOUT', operation, 'state', 'DEADLINE_EXCEEDED', True)
        return completion.result()

    async def _execute(self, name: str, key: str, values: object) -> Committed | MediaError | MediaUnconfirmed:
        command = ResultBoundCommand(1, values, {'media_changed': {'actor': 'media_owner'}})
        port = self.operations[name]; handle = port.recovery_handle(key, command)
        if type(handle) is not RecoveryHandle: return MediaError('INVALID_INPUT', name, 'input', 'INVALID_SHAPE')
        if len(self._command_jobs) >= self.settings.integer('media.file_worker_capacity'):
            return MediaError('RESOURCE_BUSY', name, 'state', 'ADMISSION_FULL', True)
        definition = next(d for d in self.commands if d.operation_kind == name)
        owned = freeze_value(definition.input_schema, values)
        cause_key, slot = self._causes.watch(name, owned)
        async def execute():
            # The fixed task budget also bounds this fair local admission queue.
            # A caller timeout leaves its original task/identity occupied until
            # admission ends; no second writer is created for the same task.
            async with self._command_admission:
                if not self.remaining_request(): return MediaError('TIMEOUT', name, 'state', 'DEADLINE_EXCEEDED')
                result = await port.resolve_operation(handle)
                if type(result) is NotCommitted:
                    if not self.remaining_request(): return MediaError('TIMEOUT', name, 'state', 'DEADLINE_EXCEEDED')
                    result = await port.execute(key, command)
                    while (type(result) is Rejected and result.error.code == 'RESOURCE_BUSY'
                            and result.error.reason == 'ADMISSION_BUSY' and self.remaining_request()):
                        await asyncio.sleep(min(0.01, self.remaining_request()))
                        if not self.remaining_request(): break
                        result = await port.execute(key, command)
                return result
        deadline = time.monotonic() + self.remaining_request()
        async def owned_execute():
            token = self._request_deadline.set(deadline)
            try: return await execute()
            finally: self._request_deadline.reset(token)
        task, outcome = start_owned(owned_execute()); self._command_jobs.add(task)
        retain_completion(task)
        def ended(job):
            if not job.cancelled(): job.exception()
            self._command_jobs.discard(job); self._causes.release(cause_key)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((outcome,), timeout=self.remaining_request())
        if not done: return MediaUnconfirmed(handle, MediaError('STORAGE_FAILED', name, 'storage', 'COMMIT_UNCONFIRMED', True))
        result = outcome.result()
        if type(result) is MediaError: return result
        if type(result) is Committed: return result
        if type(result) is Unconfirmed:
            return MediaUnconfirmed(handle, MediaError('STORAGE_FAILED', name, 'storage', 'COMMIT_UNCONFIRMED', result.error.cleanup_pending))
        if type(result) is NotCommitted and slot.cause is not None:
            cause = slot.cause
            return MediaError(cause.code, name, cause.field, cause.reason, cause.cleanup_pending)
        return MediaError('STORAGE_FAILED', name, 'storage', 'WRITE_NOT_COMMITTED', getattr(getattr(result, 'error', None), 'cleanup_pending', False))

    async def _io(self, key: str, work: Callable[[], object]):
        remaining = self.remaining_request()
        if not remaining: raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED')
        if key in self._jobs or len(self._jobs) >= self.settings.integer('media.file_worker_capacity'):
            raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', key in self._jobs)
        assert self._executor is not None
        future = asyncio.get_running_loop().run_in_executor(self._executor, work)
        retain_completion(future)
        self._jobs[key] = future
        if self._publication_context.get(): self._publication_futures.add(future)
        def ended(job):
            if not job.cancelled(): job.exception()
            if self._jobs.get(key) is job: self._jobs.pop(key)
            self._publication_futures.discard(job)
        future.add_done_callback(ended)
        done, _ = await asyncio.wait((future,), timeout=min(remaining, self.settings.integer('media.io_timeout_ms') / 1000))
        if not done: raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED', True)
        return future.result()

    async def initialize(self, resources: MediaResources, mode: str, *, continuation=None):
        """Join one retained root acquisition/recovery without releasing old pins twice."""
        if not self._bound or self._closing or type(resources) is not MediaResources:
            return MediaError('INVALID_STATE', 'initialize_media', 'state', 'NOT_READY')
        if mode not in ('CREATE_NEW', 'OPEN_EXISTING') or not valid_identifier(resources.root_id):
            return MediaError('INVALID_INPUT', 'initialize_media', 'input', 'INVALID_SHAPE')
        if continuation is not None:
            from companion_memory.runtime.daily_initialization import DailyInitialization
            if type(continuation) is not DailyInitialization or mode!='CREATE_NEW' or not continuation.permits_media_continuation(self.configuration):
                return MediaError('ACCESS_DENIED','initialize_media','capability','BINDING_MISMATCH')
        if self._physical_fault: return MediaError('FILE_FAILED', 'initialize_media', 'media', 'RESOURCE_IDENTITY_MISMATCH')
        identity_key = resources.root_id, mode
        if self._initialization_identity is not None and self._initialization_identity != identity_key:
            return MediaError('ACCESS_DENIED', 'initialize_media', 'capability', 'BINDING_MISMATCH')
        if not resources.retained_identity_check(resources.root_id, self.configuration.database_id, str(self.root)):
            return MediaError('ACCESS_DENIED', 'initialize_media', 'capability', 'BINDING_MISMATCH')
        if self._ready: return Found(MappingProxyType({'state': 'READY', 'storage_execution': 'ACTUAL'}))
        self._initialization_identity = identity_key
        self._retained_resources = resources
        if self._initialization is None or self._initialization.done():
            self._initialization = asyncio.create_task(self._initialize(resources, mode,continue_empty=continuation is not None))
        done, _ = await asyncio.wait((self._initialization,), timeout=self.settings.integer('media.recovery_timeout_ms') / 1000)
        if not done: return MediaError('TIMEOUT', 'initialize_media', 'state', 'DEADLINE_EXCEEDED', True)
        return self._initialization.result()

    async def recover_media(self):
        """Advance only the retained local initialization, with no model or new identity."""
        if self._retained_resources is None or self._initialization_identity is None:
            return MediaError('INVALID_STATE', 'recover_media', 'state', 'NOT_READY')
        if self._ready: return await self.collect_unreferenced()
        return await self.initialize(self._retained_resources, self._initialization_identity[1])

    async def _initialize(self, resources: MediaResources, mode: str, *, continue_empty:bool=False):
        """Acquire the configured physical root and confirm its retained identity."""
        operation = 'initialize_media'
        if not self._bound or type(resources) is not MediaResources or not valid_identifier(resources.root_id) or not resources.retained_identity_check(resources.root_id, self.configuration.database_id, str(self.root)):
            return MediaError('ACCESS_DENIED', operation, 'capability', 'BINDING_MISMATCH')
        if mode not in ('CREATE_NEW', 'OPEN_EXISTING'):
            return MediaError('INVALID_INPUT', operation, 'input', 'INVALID_SHAPE')
        def acquire():
            if self._root_fd is not None and self._owner_fd is not None:
                self.files.validate()
                meta = os.fstat(self._root_fd)
                return meta.st_dev, meta.st_ino
            if mode == 'CREATE_NEW': self.root.mkdir(mode=0o700, exist_ok=True)
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            owner = -1
            try:
                owner = os.open('.owner', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                root = os.fstat(root_fd)
                if mode == 'CREATE_NEW':
                    allowed = {'.owner', self.staging.relative_to(self.root).parts[0]}
                    if continue_empty:allowed.add('published')
                    if any(name not in allowed for name in os.listdir(root_fd)):
                        raise OSError(errno.EEXIST, 'Media root is not empty.')
                    stage_fd = PhysicalDirectories.open_child(root_fd, self.staging.relative_to(self.root), True)
                    try:
                        if os.listdir(stage_fd): raise OSError(errno.EEXIST, 'Media staging is not empty.')
                    finally: os.close(stage_fd)
                    try:os.mkdir('published', 0o700, dir_fd=root_fd)
                    except FileExistsError:
                        if not continue_empty:raise
                    if continue_empty:
                        published_fd=os.open('published',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
                        try:
                            if os.listdir(published_fd):raise OSError(errno.EEXIST,'Unfinished media publication directory contains content.')
                        finally:os.close(published_fd)
                self.files = PhysicalDirectories(self.root, root_fd, (self.staging, self.published), owner, self._invalidate_directories)
                self._root_fd, self._owner_fd = root_fd, owner
                return root.st_dev, root.st_ino
            except BaseException:
                if owner >= 0: os.close(owner)
                os.close(root_fd); raise
        try:
            device, inode = cast(tuple[int, int], await self._io('root', acquire))
            children = {prefix + '_' + key: getattr(os.fstat(self.files.fds[path]), 'st_' + ('dev' if key == 'device' else 'ino'))
                for prefix, path in (('staging', self.staging), ('published', self.published)) for key in ('device', 'inode')}
            if mode == 'CREATE_NEW':
                result = await self._execute('initialize_media_root', 'media-root', {'root_id': resources.root_id, 'device': device, 'inode': inode, **children})
                if type(result) is not Committed: return result
            else:
                rows = await self.rows.read('roots_get', {'root_id': resources.root_id})
                if not rows or (rows[0]['device'], rows[0]['inode'], rows[0]['database_id']) != (device, inode, self.configuration.database_id) or any(rows[0][key] != value for key, value in children.items()):
                    raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
            if mode != 'CREATE_NEW':
                from .recovery import recover_files
                recovered = await recover_files(self)
                if type(recovered) is not Found: return recovered
            self._ready = True
            self.lifecycle.start()
            return Found(MappingProxyType({'state': 'READY', 'storage_execution': 'ACTUAL'}))
        except (OwnerFailure, OSError) as failure:
            return self._failure(operation, failure)

    async def observation(self):
        """Read only bounded aggregate file and cleanup state through this owner."""
        if not self._ready or self._closing:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        health=self.get_health()
        rows=await self.rows.read('observe_gc',{})
        return MappingProxyType({'storage':rows[0],'file_workers':health.file_workers,
            'cleanup_pending':health.cleanup_pending,'processing_suspects':health.processing_suspects,
            'collection_in_flight':health.collection_in_flight})

    def bind_upload(self, entry_id: str) -> MediaUploadPort:
        """Trusted ingress setup grants one exact entry; no caller path is accepted."""
        if not self._ready or self._closing or len(self._grants) >= self.settings.integer('media.gc_page_size') or not valid_identifier(entry_id): raise ValueError('Media upload binding is unavailable.')
        port = object.__new__(MediaUploadPort)
        object.__setattr__(port, '_service', self); object.__setattr__(port, '_entry_id', entry_id)
        self._grants[id(port)] = port
        return port

    def _authorized(self, port: object, operation: str) -> MediaError | None:
        if type(port) is not MediaUploadPort or self._grants.get(id(port)) is not port:
            return MediaError('ACCESS_DENIED', operation, 'capability', 'BINDING_MISMATCH')
        if not self._ready or self._closing: return MediaError('INVALID_STATE', operation, 'state', 'SERVICE_CLOSED' if self._closing else 'NOT_READY')
        return None

    def _failure(self, operation: str, failure: OwnerFailure | OSError) -> MediaError:
        if isinstance(failure, OwnerFailure):
            return MediaError(failure.code, operation, failure.field, failure.reason, failure.cleanup_pending)
        reason = {errno.ENOSPC: 'NO_SPACE', errno.EROFS: 'READ_ONLY', errno.ENOENT: 'CONTENT_MISSING', errno.ELOOP: 'RESOURCE_IDENTITY_MISMATCH'}.get(failure.errno if failure.errno is not None else -1, 'IO_FAILED')
        return MediaError('FILE_FAILED', operation, 'media', reason)

    async def begin_upload(self, port: object, key: object, modality: object):
        operation = 'begin_upload'; error = self._authorized(port, operation)
        if error: return error
        assert type(port) is MediaUploadPort
        if not valid_identifier(key) or type(modality) is not str or modality not in ('IMAGE', 'AUDIO', 'VIDEO'):
            return MediaError('INVALID_INPUT', operation, 'upload', 'INVALID_SHAPE')
        uid = identity('upload', self.configuration.database_id, port._entry_id, cast(str, key))
        try:
            existing = await self.rows.read('uploads_get', {'upload_id': uid})
            if existing:
                row = existing[0]
                if row['entry_id'] != port._entry_id or row['modality'] != modality:
                    return MediaError('IDEMPOTENCY_CONFLICT', operation, 'input', 'CONTENT_MISMATCH')
                if row['state'] == 'REUPLOAD_REQUIRED':
                    return MediaError('PRECONDITION_FAILED', operation, 'upload', 'REUPLOAD_REQUIRED')
                return await self._upload_receipt('begin_media_upload', uid, operation, as_commit=True)
            if uid in self._uploads: return MediaError('RESOURCE_BUSY', operation, 'state', 'OWNER_ACTIVE', True)
            if len(self._uploads) >= self.settings.integer('media.upload_concurrency'):
                return MediaError('RESOURCE_BUSY', operation, 'state', 'ADMISSION_FULL')
            self._uploads.add(uid)
            started = time.time_ns() // 1000
            self.limit_upload_request(uid, started)
            result = await self._execute('begin_media_upload', uid, {'upload_id': uid, 'entry_id': port._entry_id, 'modality': modality, 'started_at_us': started})
            if type(result) is not Committed:
                if type(result) is not MediaUnconfirmed:
                    self._uploads.discard(uid); self._upload_deadlines.pop(uid, None)
                return result
            def create():
                fd = self.files.open(self.staging / uid, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try: meta = os.fstat(fd); return meta.st_dev, meta.st_ino
                finally: os.close(fd)
            dev, ino = cast(tuple[int, int], await self._io(uid, create)); self._offsets[uid] = 0
            bound = await self._execute('bind_media_staging', identity('staging', uid, 1),
                {'upload_id': uid, 'writer_generation': 1, 'staging_device': dev, 'staging_inode': ino})
            return result if type(bound) is Committed else bound
        except (OwnerFailure, OSError) as failure: return self._failure(operation, failure)

    async def _upload(self, port: MediaUploadPort, uid: object) -> MappingProxyType[str, Value]:
        if not valid_identifier(uid): raise OwnerFailure('INVALID_INPUT', 'upload', 'INVALID_IDENTIFIER')
        rows = await self.rows.read('uploads_get', {'upload_id': uid})
        if not rows or rows[0]['entry_id'] != port._entry_id:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        return rows[0]

    def limit_upload_request(self, upload_id: str, started_at_us: int) -> None:
        """Cap this operation by the original total period, including actual I/O."""
        now = time.time_ns() // 1000
        remaining = self.settings.integer('media.upload_total_timeout_ms') / 1000 - (now - started_at_us) / 1000000
        if now < started_at_us or remaining <= 0:
            raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED')
        deadline = time.monotonic() + remaining
        previous = self._upload_deadlines.get(upload_id)
        if previous is not None: deadline = min(deadline, previous)
        # Only active upload ownership retains a monotonic timer. Recovered
        # unclaimed metadata does not grow a second unbounded cache.
        if upload_id in self._uploads: self._upload_deadlines[upload_id] = deadline
        current = self._request_deadline.get()
        self._request_deadline.set(deadline if current is None else min(current, deadline))

    async def append_upload(self, port: object, upload_id: object, offset: object, data: object):
        operation = 'append_upload'; error = self._authorized(port, operation)
        if error: return error
        assert type(port) is MediaUploadPort
        if type(offset) is not int or offset < 0 or type(data) is not bytes or not 1 <= len(data) <= self.settings.integer('media.upload_chunk_bytes') or offset + len(data) > self.settings.integer('media.blob_max_bytes'):
            return MediaError('INVALID_INPUT', operation, 'upload', 'LIMIT_EXCEEDED')
        try:
            upload = await self._upload(port, upload_id); uid = cast(str, upload_id)
            if upload['state'] not in ('UPLOADING', 'REUPLOAD_REQUIRED'): raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'OFFSET_MISMATCH')
            self.limit_upload_request(uid, cast(int, upload['started_at_us']))
            if time.time_ns() // 1000 - cast(int, upload['started_at_us']) >= self.settings.integer('media.upload_total_timeout_ms') * 1000:
                raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED')
            if upload['state'] == 'REUPLOAD_REQUIRED':
                if offset != 0: raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'REUPLOAD_REQUIRED')
                if uid in self._jobs or len(self._uploads) >= self.settings.integer('media.upload_concurrency'):
                    raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', uid in self._jobs)
                resumed = await self._execute('resume_media_upload', identity('resume_upload', uid, upload['writer_generation']),
                    {'upload_id': uid, 'previous_generation': upload['writer_generation']})
                if type(resumed) is not Committed: return resumed
                self._uploads.add(uid)
                def reset():
                    fd = self.files.open(self.staging / uid, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    try:
                        meta = os.fstat(fd)
                        if not stat.S_ISREG(meta.st_mode) or (upload['staging_inode'] is not None and (meta.st_dev, meta.st_ino) != (upload['staging_device'], upload['staging_inode'])):
                            raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
                        os.ftruncate(fd, 0)
                        return meta.st_dev, meta.st_ino
                    finally: os.close(fd)
                dev, ino = cast(tuple[int, int], await self._io(uid, reset)); self._offsets[uid] = 0
                generation = cast(int, upload['writer_generation']) + 1
                bound = await self._execute('bind_media_staging', identity('staging', uid, generation),
                    {'upload_id': uid, 'writer_generation': generation, 'staging_device': dev, 'staging_inode': ino})
                if type(bound) is not Committed: return bound
                upload = await self._upload(port, uid)
            if uid not in self._offsets: raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'REUPLOAD_REQUIRED')
            def append():
                fd = self.files.open(self.staging / uid, os.O_RDWR | os.O_NOFOLLOW)
                try:
                    meta = os.fstat(fd)
                    if not stat.S_ISREG(meta.st_mode) or (meta.st_dev, meta.st_ino) != (upload['staging_device'], upload['staging_inode']):
                        raise OwnerFailure('FILE_FAILED', 'upload', 'RESOURCE_IDENTITY_MISMATCH')
                    size = meta.st_size
                    if offset < size:
                        if offset + len(data) > size or os.pread(fd, len(data), offset) != data:
                            raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'OFFSET_MISMATCH')
                    elif offset == size:
                        written = 0
                        while written < len(data):
                            n = os.pwrite(fd, data[written:], offset + written)
                            if n <= 0: raise OSError(errno.EIO, 'Write failed.')
                            written += n
                    else: raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'OFFSET_MISMATCH')
                    return os.fstat(fd).st_size
                finally: os.close(fd)
            current = cast(int, await self._io(uid, append)); self._offsets[uid] = current
            return VolatileProgress(uid, current)
        except (OwnerFailure, OSError) as failure: return self._failure(operation, failure)

    def _scan(self, path: Path, sync: bool = False) -> tuple[int, str, int, int]:
        fd = self.files.open(path, (os.O_RDWR if sync else os.O_RDONLY) | os.O_NOFOLLOW)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or not 1 <= meta.st_size <= self.settings.integer('media.blob_max_bytes'):
                raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
            checksum = hashlib.sha256(); size = 0
            while data := os.read(fd, self.settings.integer('media.upload_chunk_bytes')):
                checksum.update(data); size += len(data)
                if size > self.settings.integer('media.blob_max_bytes'): raise OwnerFailure('INVALID_INPUT', 'media', 'LIMIT_EXCEEDED')
            if sync: self._synchronize(fd)
            if size != meta.st_size: raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
            return size, checksum.hexdigest(), meta.st_dev, meta.st_ino
        finally: os.close(fd)

    @staticmethod
    def _synchronize(fd: int) -> None:
        try: os.fsync(fd)
        except OSError as failure:
            if failure.errno in (errno.ENOSPC, errno.EROFS): raise
            raise OwnerFailure('FILE_FAILED', 'media', 'SYNC_FAILED') from failure

    def _sync_directory(self, directory: Path) -> None:
        self._synchronize(self.files.directory(directory))

    async def finish_upload(self, port: object, upload_id: object):
        operation = 'finish_upload'; error = self._authorized(port, operation)
        if error: return error
        assert type(port) is MediaUploadPort
        if self._publishing or self._publication_futures: return MediaError('RESOURCE_BUSY', operation, 'state', 'OWNER_ACTIVE')
        self._publishing = True
        publication = self._publication_context.set(True)
        try:
            upload = await self._upload(port, upload_id); uid = cast(str, upload_id)
            if upload['state'] in ('READY', 'ABANDONED'):
                original = await self._upload_receipt('publish_media_upload', identity('ready', uid), operation, as_commit=True)
                if type(original) is Committed:
                    from .recovery import cleanup_staging
                    try: await cleanup_staging(self, upload)
                    except (OwnerFailure, OSError): self._cleanup_pending.add(uid)
                return original
            if upload['state'] == 'UPLOADING':
                self.limit_upload_request(uid, cast(int, upload['started_at_us']))
                if time.time_ns() // 1000 - cast(int, upload['started_at_us']) >= self.settings.integer('media.upload_total_timeout_ms') * 1000:
                    raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED')
                size, checksum, dev, ino = cast(tuple[int, str, int, int], await self._io(uid, lambda: self._scan(self.staging / uid, True)))
                bid = identity('blob', checksum, size)
                blobs = await self.rows.read('blobs_get', {'blob_id': bid})
                generation = 1 if not blobs else cast(int, blobs[0]['generation']) + int(blobs[0]['state'] == 'DELETED')
                sealed = await self._execute('seal_media_upload', identity('seal', uid), {'upload_id': uid, 'blob_id': bid, 'sha256': checksum,
                    'byte_count': size, 'generation': generation, 'staging_device': dev, 'staging_inode': ino})
                if type(sealed) is not Committed: return sealed
                upload = await self._upload(port, uid)
            if upload['state'] != 'SEALED': raise OwnerFailure('PRECONDITION_FAILED', 'upload', 'UPLOAD_EXPIRED')
            bid = cast(str, upload['blob_id']); generation = cast(int, upload['generation'])
            destination = self.published / (bid + '.' + str(generation))
            def publish():
                stage = self.staging / uid
                size, checksum, dev, ino = self._scan(stage, True)
                if (size, checksum, dev, ino) != (upload['byte_count'], upload['sha256'], upload['staging_device'], upload['staging_inode']):
                    raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
                try: self.files.link(stage, destination)
                except FileExistsError:
                    other_size, other_hash, _, _ = self._scan(destination)
                    if (other_size, other_hash) != (size, checksum): raise OwnerFailure('FILE_FAILED', 'media', 'HASH_COLLISION')
                    a = self.files.open(stage, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        b = self.files.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
                        try:
                            while block := os.read(a, self.settings.integer('media.upload_chunk_bytes')):
                                if block != os.read(b, len(block)): raise OwnerFailure('FILE_FAILED', 'media', 'HASH_COLLISION')
                            if os.read(b, 1): raise OwnerFailure('FILE_FAILED', 'media', 'HASH_COLLISION')
                        finally: os.close(b)
                    finally: os.close(a)
                self._sync_directory(self.published)
                meta = self.files.stat(destination)
                return meta.st_dev, meta.st_ino
            dev, ino = cast(tuple[int, int], await self._io(uid, publish))
            result = await self._execute('publish_media_upload', identity('ready', uid), {'upload_id': uid, 'device': dev, 'inode': ino})
            if type(result) is Committed:
                from .recovery import cleanup_staging
                try: await cleanup_staging(self, upload)
                except (OwnerFailure, OSError): self._cleanup_pending.add(uid)
            return result
        except (OwnerFailure, OSError) as failure: return self._failure(operation, failure)
        finally:
            self._publication_context.reset(publication)
            self._publishing = False

    async def _upload_receipt(self, kind: str, key: str, operation: str, *, as_commit: bool):
        result = await self.operations[kind].read_receipt(key)
        if type(result) is Found: return Committed(result.value, 'EXISTING') if as_commit else result
        from companion_memory.persistence import NotFound
        reason = 'INTEGRITY_FAILURE' if type(result) is NotFound else 'READ_FAILED'
        return MediaError('STORAGE_FAILED', operation, 'storage', reason, getattr(getattr(result, 'error', None), 'cleanup_pending', False))

    async def resolve_upload(self, port: object, key: object, modality: object):
        operation = 'resolve_upload'; error = self._authorized(port, operation)
        if error: return error
        assert type(port) is MediaUploadPort
        if not valid_identifier(key) or type(modality) is not str or modality not in ('IMAGE', 'AUDIO', 'VIDEO'):
            return MediaError('INVALID_INPUT', operation, 'input', 'INVALID_SHAPE')
        uid = identity('upload', self.configuration.database_id, port._entry_id, cast(str, key))
        try:
            upload = await self._upload(port, uid)
            if upload['modality'] != modality: return MediaError('IDEMPOTENCY_CONFLICT', operation, 'input', 'CONTENT_MISMATCH')
            if upload['blob_id'] is not None and upload['state'] in ('READY', 'ABANDONED'): return await self._upload_receipt('publish_media_upload', identity('ready', uid), operation, as_commit=False)
            return await self._upload_receipt('begin_media_upload', uid, operation, as_commit=False)
        except OwnerFailure as failure: return self._failure(operation, failure)

    def acceptance_fact(self, uow: UnitOfWork, entry_id: str, message_id: str) -> MappingProxyType[str, Value]:
        """Report the actual newly attached occurrences without granting protection."""
        occurrences = self.rows.stage('event_occurrences', uow, {'message_id': message_id})
        if any(row['entry_id'] != entry_id or row['state'] != 'ATTACHED' for row in occurrences):
            raise OwnerFailure('ACCESS_DENIED', 'media', 'BINDING_MISMATCH')
        reports = tuple(row for row in occurrences if row['interpretation_id'] is not None)
        return MappingProxyType({'provenance': 'EXTERNAL_REPORT' if reports else 'NONE', 'guard_created': False, 'reused': False,
            'counts': tuple(MappingProxyType({'name': key, 'count': count}) for key, count in (('occurrences_attached', len(occurrences)), ('external_reports', len(reports)))),
            'references': tuple(MappingProxyType({'name': 'occurrence', 'object_id': row['occurrence_id'], 'revision': row['selection_revision']}) for row in occurrences)})

    def external_interpretation(self, upload: MappingProxyType[str, Value], media: MappingProxyType[str, Value],
            message_id: str, interpretation_id: str) -> MappingProxyType[str, Value]:
        """Construct the same exact external record before and inside acceptance."""
        report = record(media['interpretation'])
        return isolate_interpretation({'interpretation_version': 1, 'interpretation_id': interpretation_id, 'blob_id': upload['blob_id'],
                    'generation': upload['generation'], 'task': 'TRANSCRIBE' if media['modality'] == 'AUDIO' else 'DESCRIBE', 'modality': media['modality'],
                    'origin': 'EXTERNAL', 'status': report['status'], 'text': report['text'], 'coverage': report['coverage'], 'source_ref': report['source_ref'],
                    'interpretation_fingerprint': None, 'prompt_revision': None, 'scope_kind': 'EVENT', 'scope_id': self.instance_id,
                    'event_id': message_id, 'provider_request_id': None, 'created_at_us': upload['started_at_us'],
                    'failure_reason': 'EXTERNAL_FAILURE' if report['status'] == 'FAILED' else None},
                    text_limit=self.settings.integer('media.interpretation_text_max_bytes'), record_limit=self.settings.integer('media.interpretation_record_max_bytes'))

    async def validate_event_reports(self, entry_id: str, message_id: str, event: MappingProxyType[str, Value]) -> None:
        """Read only authorized upload metadata and encode full reports before writes."""
        for item in sequence(event['media']):
            medium = record(item)
            report = medium['interpretation']
            if report is None or record(report)['status'] == 'MISSING': continue
            rows = await self.rows.read('uploads_get', {'upload_id': medium['reference_id']})
            if not rows or rows[0]['entry_id'] != entry_id:
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
            self.external_interpretation(rows[0], medium, message_id, identity('interpretation', medium['occurrence_id'], 'EXTERNAL'))

    def retain_event(self, uow: UnitOfWork, entry_id: str, message_id: str, event: MappingProxyType[str, Value]) -> None:
        """Consume authorized READY upload protections into independent event occurrences."""
        for ordinal, item in enumerate(sequence(event['media'])):
            media = record(item); uid = cast(str, media['reference_id']); occurrence = cast(str, media['occurrence_id'])
            if occurrence != identity('occurrence', message_id, ordinal): raise OwnerFailure('ACCESS_DENIED', 'media', 'BINDING_MISMATCH')
            upload = self._get('uploads', uow, 'upload_id', uid)
            if (upload['entry_id'], upload['modality'], upload['state']) != (entry_id, media['modality'], 'READY'):
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_NOT_READY')
            bindings = self.rows.stage('upload_bindings_get', uow, {'upload_id': uid})
            if bindings and bindings[0]['message_id'] != message_id:
                raise OwnerFailure('ACCESS_DENIED', 'media', 'BINDING_MISMATCH')
            if not bindings:
                self.rows.stage('upload_bindings_insert', uow, {'upload_id': uid, 'message_id': message_id})
            bid = cast(str, upload['blob_id']); generation = cast(int, upload['generation'])
            self._reference(uow, bid, generation, 'EVENT', message_id, occurrence, True, cast(int, upload['started_at_us']))
            # One binding may occur twice within the same event; the second
            # occurrence acquires its own edge but does not remove UPLOAD twice.
            rid = identity('media_ref', 'UPLOAD', uid, bid, generation, None)
            if self.rows.stage('references_get', uow, {'reference_id': rid}):
                self._reference(uow, bid, generation, 'UPLOAD', uid, None, False, cast(int, upload['started_at_us']))
            interpretation_id = None
            report = media['interpretation']
            if report is not None and record(report)['status'] != 'MISSING':
                report = record(report); interpretation_id = identity('interpretation', occurrence, 'EXTERNAL')
                interpretation = self.external_interpretation(upload, media, message_id, interpretation_id)
                self.rows.stage('interpretations_insert', uow, {'interpretation_id': interpretation_id, 'blob_id': bid, 'body': encode_content(interpretation, 2048).decode()})
            self.rows.stage('occurrences_insert', uow, {'occurrence_id': occurrence, 'message_id': message_id, 'media_index': ordinal, 'entry_id': entry_id,
                'blob_id': bid, 'generation': generation, 'modality': media['modality'], 'upload_id': uid, 'state': 'ATTACHED',
                'interpretation_id': interpretation_id, 'selection_revision': 1})
            if interpretation_id is not None:
                self.rows.stage('selections_insert', uow, {'occurrence_id': occurrence, 'selection_revision': 1,
                    'interpretation_id': interpretation_id, 'generation': generation, 'selection_kind': 'ORIGINAL'})

    def retain_consumer(self, uow: UnitOfWork, kind: str, owner_id: str, members: tuple[MappingProxyType[str, Value], ...]) -> None:
        """Acquire a preparation, batch or candidate's precise selected media refs."""
        if kind not in ('PREPARATION', 'BATCH', 'CANDIDATE'): raise InvalidValue()
        for member in members:
            for item in sequence(member['media']):
                selected = record(item)
                self._reference(uow, cast(str, selected['blob_id']), cast(int, selected['generation']), kind, owner_id,
                    cast(str, selected['occurrence_id']), True, cast(int, member['received_at_us']))
                if selected['interpretation_id'] is not None:
                    self.rows.stage('interpretation_holders_insert', uow, {'holder_id': identity('consumer_interpretation', kind, owner_id, selected['occurrence_id']),
                        'interpretation_id': selected['interpretation_id'], 'owner_kind': kind, 'owner_id': owner_id})

    def pin_preparation_versions(self, uow: UnitOfWork, preparation_id: str, members: tuple[MappingProxyType[str, Value], ...]) -> int:
        """Replace only actual changed preparation selections under the enclosing writer."""
        changed = 0
        for member in members:
            for item in sequence(member['media']):
                selected = record(item)
                if selected['interpretation_id'] is None: raise InvalidValue()
                holder = identity('consumer_interpretation', 'PREPARATION', preparation_id, selected['occurrence_id'])
                before = self.rows.stage('interpretation_holders_get', uow, {'holder_id': holder})
                if before and before[0]['interpretation_id'] == selected['interpretation_id']: continue
                if before: self.rows.stage('interpretation_holders_delete', uow, {'holder_id': holder})
                self.rows.stage('interpretation_holders_insert', uow, {'holder_id': holder, 'interpretation_id': selected['interpretation_id'],
                    'owner_kind': 'PREPARATION', 'owner_id': preparation_id})
                changed += 1
        return changed

    def observe_consumer_release(self, uow: UnitOfWork, kind: str, owner_id: str,
                                 members: tuple[MappingProxyType[str, Value], ...]) -> MappingProxyType[str, Value]:
        """Capture exact consumer edges and physical revisions before release planning."""
        from .release import MEDIA_RELEASE
        from companion_memory.memory.formats import isolate
        if kind not in ('PREPARATION', 'BATCH', 'CANDIDATE'): raise InvalidValue()
        blobs = {}; references = []; versions = []
        for member in members:
            for item in sequence(member['media']):
                selected = record(item); bid = cast(str, selected['blob_id'])
                blob = self._blob(uow, bid)
                if blob['generation'] != selected['generation'] or blob['state'] != 'READY':
                    raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
                blobs[bid] = MappingProxyType({k: blob[k] for k in ('blob_id', 'generation', 'references_revision', 'reference_count')})
                rid = identity('media_ref', kind, owner_id, bid, selected['generation'], selected['occurrence_id'])
                edge = self.rows.stage('references_get', uow, {'reference_id': rid})
                if not edge or edge[0]['owner_kind'] != kind or edge[0]['owner_id'] != owner_id:
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                references.append(rid)
                holder = identity('consumer_interpretation', kind, owner_id, selected['occurrence_id'])
                retained = self.rows.stage('interpretation_holders_get', uow, {'holder_id': holder})
                if retained:
                    if retained[0]['owner_kind'] != kind or retained[0]['owner_id'] != owner_id:
                        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                    versions.append(holder)
        return isolate(MEDIA_RELEASE, {'blobs': tuple(blobs[k] for k in sorted(blobs)),
            'blob_references': tuple(sorted(references)), 'interpretation_references': tuple(sorted(versions))}, 8192)

    def release_consumer(self, uow: UnitOfWork, kind: str, owner_id: str, members: tuple[MappingProxyType[str, Value], ...]) -> None:
        """Release only the named consumer; EVENT and PROCESSING are independent."""
        if kind not in ('PREPARATION', 'BATCH', 'CANDIDATE'): raise InvalidValue()
        for member in members:
            for item in sequence(member['media']):
                selected = record(item)
                self._reference(uow, cast(str, selected['blob_id']), cast(int, selected['generation']), kind, owner_id,
                    cast(str, selected['occurrence_id']), False, time.time_ns() // 1000)
                holder = identity('consumer_interpretation', kind, owner_id, selected['occurrence_id'])
                if self.rows.stage('interpretation_holders_get', uow, {'holder_id': holder}):
                    self.rows.stage('interpretation_holders_delete', uow, {'holder_id': holder})

    def preparation_selections(self, uow: UnitOfWork, entry_id: str, message_id: str) -> tuple[MappingProxyType[str, Value], ...]:
        """Retain unresolved occurrences explicitly while reserving a FIFO window."""
        result = []
        for occurrence in self.rows.stage('event_occurrences', uow, {'message_id': message_id}):
            if occurrence['entry_id'] != entry_id or occurrence['state'] != 'ATTACHED':
                raise OwnerFailure('ACCESS_DENIED', 'media', 'BINDING_MISMATCH')
            blob = self._blob(uow, cast(str, occurrence['blob_id']))
            if blob['state'] != 'READY' or blob['generation'] != occurrence['generation']:
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_NOT_READY')
            result.append(MappingProxyType({k: occurrence[k] for k in (
                'occurrence_id', 'interpretation_id', 'blob_id', 'generation', 'selection_revision')}))
        return tuple(result)

    def current_selections(self, uow: UnitOfWork, entry_id: str, message_id: str) -> tuple[MappingProxyType[str, Value], ...]:
        """Read this event's current completed immutable selection versions."""
        selections = []
        for occurrence in self.rows.stage('event_occurrences', uow, {'message_id': message_id}):
            if occurrence['entry_id'] != entry_id or occurrence['state'] != 'ATTACHED':
                raise OwnerFailure('ACCESS_DENIED', 'media', 'BINDING_MISMATCH')
            if occurrence['interpretation_id'] is None:
                raise OwnerFailure('PRECONDITION_FAILED', 'interpretation', 'SELECTION_CHANGED')
            selected = decode_interpretation(cast(str, self._get('interpretations', uow, 'interpretation_id', occurrence['interpretation_id'])['body']).encode())
            if selected['origin'] == 'INTERNAL':
                protected = self.work.reusable(uow, occurrence, cast(str, selected['scope_id']),
                    cast(str, selected['interpretation_fingerprint']), cast(str, selected['prompt_revision']))
                if protected is not None and protected[1] == 'PROTECTED_REFUSAL' and protected[0]['interpretation_id'] != occurrence['interpretation_id']:
                    self.work.select(uow, occurrence, cast(str, protected[0]['interpretation_id']), 'PROTECTED_REFUSAL')
                    occurrence = self._get('occurrences', uow, 'occurrence_id', occurrence['occurrence_id'])
            selections.append(MappingProxyType({k: occurrence[k] for k in (
                'occurrence_id', 'interpretation_id', 'blob_id', 'generation', 'selection_revision')}))
        self.verify_selections(uow, entry_id, message_id, tuple(selections))
        return tuple(selections)

    def verify_selections(self, uow: UnitOfWork, entry_id: str, message_id: str, selections: tuple[MappingProxyType[str, Value], ...]) -> tuple[MappingProxyType[str, Value], ...]:
        """Validate immutable selected understanding and the protected physical generation."""
        result = []
        for selection in selections:
            occurrence = self._get('occurrences', uow, 'occurrence_id', selection['occurrence_id'])
            if (occurrence['entry_id'] != entry_id or occurrence['message_id'] != message_id or occurrence['state'] != 'ATTACHED'
                    or any(occurrence[k] != selection[k] for k in ('blob_id', 'generation'))):
                raise OwnerFailure('ACCESS_DENIED', 'media', 'BINDING_MISMATCH')
            versions = self.rows.stage('selections_get', uow, {'occurrence_id': selection['occurrence_id'], 'selection_revision': selection['selection_revision']})
            if not versions or versions[0]['interpretation_id'] != selection['interpretation_id'] or versions[0]['generation'] != selection['generation']:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            blob = self._blob(uow, cast(str, selection['blob_id']))
            if blob['state'] != 'READY' or blob['generation'] != selection['generation'] or not blob['reference_count']:
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_NOT_READY')
            interpretation = self._get('interpretations', uow, 'interpretation_id', selection['interpretation_id'])
            value = decode_interpretation(cast(str, interpretation['body']).encode())
            if (value['blob_id'] != selection['blob_id'] or cast(int, value['generation']) > cast(int, selection['generation'])
                    or value['modality'] != occurrence['modality']):
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            if value['origin'] == 'EXTERNAL' and (value['event_id'] != message_id or value['generation'] != selection['generation']):
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            if value['generation'] != selection['generation']:
                expected_kind = 'PROTECTED_REFUSAL' if value['status'] == 'REFUSED' else 'CONTENT_REUSE'
                if (value['origin'] != 'INTERNAL' or value['status'] not in ('COMPLETE', 'EMPTY', 'REFUSED')
                        or versions[0]['selection_kind'] != expected_kind
                        or value['status'] != 'REFUSED' and value['scope_kind'] != 'CONTENT'):
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            result.append(value)
        return tuple(result)

    async def read_daily_interpretation(self,batch_id:str,selected:MappingProxyType[str,Value],member:MappingProxyType[str,Value]):
        """Read one complete immutable understanding protected by the actual batch."""
        versions=await self.rows.read('interpretations_get',{'interpretation_id':selected['interpretation_id']})
        holder_id=identity('consumer_interpretation','BATCH',batch_id,selected['occurrence_id'])
        holders=await self.rows.read('interpretation_holders_get',{'holder_id':holder_id})
        selections=await self.rows.read('selections_get',{'occurrence_id':selected['occurrence_id'],'selection_revision':selected['selection_revision']})
        if (len(versions)!=1 or len(holders)!=1 or len(selections)!=1 or holders[0]['owner_kind']!='BATCH' or holders[0]['owner_id']!=batch_id
                or holders[0]['interpretation_id']!=selected['interpretation_id'] or selections[0]['interpretation_id']!=selected['interpretation_id']):raise InvalidValue()
        value=decode_interpretation(cast(str,versions[0]['body']).encode())
        if value['blob_id']!=selected['blob_id'] or cast(int,value['generation'])>cast(int,selected['generation']):raise InvalidValue()
        if value['origin']=='EXTERNAL' and (value['generation']!=selected['generation'] or value['event_id']!=member['message_id']):raise InvalidValue()
        if value['generation']!=selected['generation'] and value['status']!='REFUSED':raise InvalidValue()
        return value

    async def read_retained_interpretations(self,source_id:str,member:MappingProxyType[str,Value]):
        """Read exact source-held versions for bounded formal-source recovery."""
        if not self._bound or self._closing or not valid_identifier(source_id):raise InvalidValue()
        selections=tuple(record(v) for v in sequence(member['media']))
        if len(selections)>self.settings.integer('media.event_occurrence_limit'):raise InvalidValue()
        values=[]
        for selected in selections:
            versions=await self.rows.read('interpretations_get',{'interpretation_id':selected['interpretation_id']})
            holders=await self.rows.read('interpretation_holders_get',{'holder_id':identity('interpretation_ref',source_id,selected['occurrence_id'])})
            if len(versions)!=1 or len(holders)!=1 or holders[0]['interpretation_id']!=selected['interpretation_id']:raise InvalidValue()
            value=decode_interpretation(cast(str,versions[0]['body']).encode())
            if value['interpretation_id']!=selected['interpretation_id'] or value['blob_id']!=selected['blob_id']:raise InvalidValue()
            values.append(value)
        return tuple(values)

    def retain_source(self, uow: UnitOfWork, source_id: str, entry_id: str, members: tuple[MappingProxyType[str, Value], ...]) -> None:
        """Acquire source blob/version protection before runtime consumers release."""
        for member in members:
            selections = tuple(record(s) for s in sequence(member['media']))
            self.verify_selections(uow, entry_id, cast(str, member['message_id']), selections)
            for selected in selections:
                occurrence = cast(str, selected['occurrence_id'])
                self._reference(uow, cast(str, selected['blob_id']), cast(int, selected['generation']), 'SOURCE', source_id, occurrence, True, cast(int, member['received_at_us']))
                self.rows.stage('interpretation_holders_insert', uow, {'holder_id': identity('interpretation_ref', source_id, occurrence),
                    'interpretation_id': selected['interpretation_id'], 'owner_kind': 'SOURCE', 'owner_id': source_id})

    def observe_source_release(self, uow: UnitOfWork, source_id: str,
            members: tuple[MappingProxyType[str, Value], ...], deleted_payloads: frozenset[str]) -> MappingProxyType[str, Value]:
        """Snapshot the actual bounded source release closure without mutations."""
        from .release import observe_source_release
        return observe_source_release(self, uow, source_id, members, deleted_payloads)

    def release_source(self, uow: UnitOfWork, source_id: str,
            members: tuple[MappingProxyType[str, Value], ...], now_us: int) -> tuple[int, int]:
        """Participate in an already verified fixed source retirement plan."""
        from .release import release_source
        return release_source(self, uow, source_id, members, now_us)

    def release_event(self, uow: UnitOfWork, message_id: str) -> None:
        """Release event edges only when ingress releases the actual final payload."""
        for occurrence in self.rows.stage('event_occurrences', uow, {'message_id': message_id}):
            if occurrence['state'] != 'ATTACHED': raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
            self._reference(uow, cast(str, occurrence['blob_id']), cast(int, occurrence['generation']), 'EVENT', message_id,
                cast(str, occurrence['occurrence_id']), False, time.time_ns() // 1000)
            self.rows.stage('occurrences_update', uow, {**occurrence, 'state': 'RELEASED', 'interpretation_id': None})

    async def collect_unreferenced(self, after: str = ''):
        """Retire and unlink one configured bounded page, preserving generation fences."""
        operation = 'collect_unreferenced'
        if not self._ready or self._closing: return MediaError('INVALID_STATE', operation, 'state', 'NOT_READY')
        if self._publishing or self._publication_futures or self._gc_job is not None:
            return MediaError('RESOURCE_BUSY', operation, 'state', 'OWNER_ACTIVE', True)
        if type(after) is not str or (after and not valid_identifier(after)):
            return MediaError('INVALID_INPUT', operation, 'input', 'INVALID_SHAPE')
        deadline = time.monotonic() + self.settings.integer('media.operation_timeout_ms') / 1000
        async def owned():
            token = self._request_deadline.set(deadline)
            try: return await self._collect_unreferenced(after)
            finally:
                self._request_deadline.reset(token)
        task = asyncio.create_task(finish_owned(owned())); self._gc_job = task
        def ended(job):
            if not job.cancelled(): job.exception()
            if self._gc_job is job: self._gc_job = None
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((task,), timeout=max(0.0, deadline - time.monotonic()))
        if not done: return MediaError('TIMEOUT', operation, 'state', 'DEADLINE_EXCEEDED', True)
        return task.result()

    async def _collect_unreferenced(self, after: str):
        operation = 'collect_unreferenced'
        self._publishing = True; deleted = 0
        publication = self._publication_context.set(True)
        try:
            await self._io('lifecycle_directories', self.files.validate)
            from .recovery import maintain_uploads
            maintained = await maintain_uploads(self)
            if maintained is not None: return maintained
            rows = await self.rows.read('gc_page', {'after': after, 'limit': self.settings.integer('media.gc_page_size')})
            for blob in rows:
                if not self.remaining_request(): return MediaError('TIMEOUT', operation, 'state', 'DEADLINE_EXCEEDED')
                bid = cast(str, blob['blob_id']); generation = cast(int, blob['generation'])
                gc = cast(str, blob['gc_operation'] or identity('gc', bid, generation, blob['references_revision']))
                if blob['state'] == 'READY':
                    if blob['unreferenced_at_us'] is not None and time.time_ns() // 1000 - cast(int, blob['unreferenced_at_us']) < self.settings.integer('media.gc_unreferenced_grace_ms') * 1000: continue
                    result = await self._execute('retire_media_blob', identity('retire', gc), {'blob_id': bid, 'generation': generation,
                        'references_revision': blob['references_revision'], 'gc_operation': gc})
                    if type(result) is MediaError and result.reason == 'OWNERSHIP_CHANGED': continue
                    if type(result) is not Committed: return result
                path = self.published / (bid + '.' + str(generation))
                def unlink():
                    try: meta = self.files.stat(path)
                    except FileNotFoundError: meta = None
                    if meta is not None:
                        if not stat.S_ISREG(meta.st_mode) or (meta.st_dev, meta.st_ino) != (blob['device'], blob['inode']):
                            raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
                        self.files.unlink(path)
                    self._sync_directory(self.published)
                await self._io(gc, unlink)
                result = await self._execute('delete_media_blob', identity('deleted', gc), {'blob_id': bid, 'generation': generation, 'gc_operation': gc})
                if type(result) is not Committed: return result
                deleted += 1
            return Found(MappingProxyType({'deleted': deleted, 'after': rows[-1]['blob_id'] if rows else after}))
        except (OwnerFailure, OSError) as failure: return self._failure(operation, failure)
        finally:
            self._publication_context.reset(publication)
            self._publishing = False

    def get_health(self) -> MediaHealth:
        """Return safe owner-local completion and maintenance facts without I/O."""
        from .health import MediaHealth
        return MediaHealth(self._ready and not self._closing, self._gc_job is not None, len(self._jobs),
            bool(self._cleanup_pending or self.integrity.pending or self.originals.pins or self._jobs or self._command_jobs or self._processing_reads or (self._closing and self._owner_fd is not None)),
            self.lifecycle.suspects, self.lifecycle.observed_at_us)

    def matches_provider(self, storage: PersistenceService, database_id: str | None, scope: str) -> bool:
        """Trusted bridge verifies actual owner binding without acquiring byte access."""
        return self._bound and self.storage is storage and self.configuration.database_id == database_id and self.instance_id == scope

    async def read_processing(self, work_id: object, occurrence_id: object) -> ProcessingRead:
        """Return protected bytes plus this read's restricted actual-end notification."""
        from .processing_bytes import read_processing_bytes, ProcessingRead
        completion = CompletionScope()
        if len(self._processing_reads) >= self.settings.integer('media.processing_concurrency'):
            return ProcessingRead(MediaError('RESOURCE_BUSY', 'authorize_stored_media', 'state', 'ADMISSION_FULL'), completion)
        self._processing_reads.add(completion)
        try:
            with completion:
                result = await read_processing_bytes(self, work_id, occurrence_id)
            return ProcessingRead(result, completion)
        finally:
            completion.when_ended(lambda: self._processing_reads.discard(completion))

    def bind_original_inspection(self, entry_id: str, occurrence_ids: tuple[str, ...]):
        """Issue independent finite original review authority from trusted setup."""
        return self.originals.bind(entry_id, occurrence_ids)

    def stop_admission(self) -> None:
        """Stop uploads and periodic work while retaining legal cleanup and roots."""
        self._closing = True; self._grants.clear(); self.lifecycle.stop.set()

    async def close(self) -> bool:
        """Retain root and executor ownership while any file or database work survives."""
        self.stop_admission()
        if not self._bound: return True
        deadline = time.monotonic() + self.settings.integer('media.close_timeout_ms') / 1000
        if self._closing_task is None or self._closing_task.done():
            async def owned():
                token = self._request_deadline.set(deadline)
                try: return await self._close_owned()
                finally: self._request_deadline.reset(token)
            self._closing_task = asyncio.create_task(owned())
            self._closing_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        done, _ = await asyncio.wait((self._closing_task,), timeout=max(0.0, deadline - time.monotonic()))
        return self._closing_task.result() if done else False

    async def _close_owned(self) -> bool:
        if self.lifecycle.task is not None and not self.lifecycle.task.done():
            await asyncio.wait((self.lifecycle.task,), timeout=self.remaining_request())
            if not self.lifecycle.task.done(): return False
        if self._processing_reads: return False
        if self._initialization is not None and not self._initialization.done():
            await asyncio.wait((self._initialization,), timeout=self.remaining_request())
            if not self._initialization.done(): return False
        if self._gc_job is not None:
            await asyncio.wait((self._gc_job,), timeout=self.remaining_request())
            if self._gc_job is not None and not self._gc_job.done(): return False
        if not await self.originals.close(): return False
        if self._upload_jobs:
            await asyncio.wait(tuple(self._upload_jobs), timeout=self.remaining_request())
        if self._upload_jobs: return False
        if self._command_jobs:
            await asyncio.wait(tuple(self._command_jobs), timeout=self.remaining_request())
        if self._command_jobs: return False
        if self._jobs:
            await asyncio.wait(tuple(self._jobs.values()), timeout=self.remaining_request())
        if self._jobs: return False
        if self._bound:
            assert self._lease is not None
            if not self._lease.release(): return False
        if self._executor: self._executor.shutdown(wait=False, cancel_futures=False)
        if hasattr(self, 'files'): self.files.close()
        if self._owner_fd is not None: os.close(self._owner_fd); self._owner_fd = None
        if self._root_fd is not None: os.close(self._root_fd); self._root_fd = None
        return True
