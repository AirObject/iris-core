"""Read exact protected original bytes for the actual Provider media bridge.

A persistent PROCESSING edge must already exist. Only the owning media service
reads its physical file; a native Provider authority consumes the finite result.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import os
import stat
from typing import TYPE_CHECKING, cast
from companion_memory.persistence.completion import CompletionScope
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import valid_identifier
if TYPE_CHECKING:
    from .service import MediaService, MediaError


@dataclass(frozen=True, slots=True)
class ProcessingBytes:
    """Owned immutable bytes and stable protected artifact attribution."""
    content: bytes
    artifact_id: str
    modality: str


@dataclass(frozen=True, slots=True)
class ProcessingRead:
    """Logical read result and its operation-local resource completion notification."""
    result: ProcessingBytes | MediaError
    completion: CompletionScope


async def read_processing_bytes(media: MediaService, work_id: object, occurrence_id: object) -> ProcessingBytes | MediaError:
    """Verify actual occurrence, generation, reference edge and complete bytes."""
    from .service import MediaError, identity
    if not media._ready or media._closing or not valid_identifier(work_id) or not valid_identifier(occurrence_id):
        return MediaError('ACCESS_DENIED', 'authorize_stored_media', 'capability', 'BINDING_MISMATCH')
    try:
        occurrences = await media.rows.read('occurrences_get', {'occurrence_id': occurrence_id})
        if not occurrences: raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        occurrence = occurrences[0]; bid = cast(str, occurrence['blob_id']); generation = cast(int, occurrence['generation'])
        edge = identity('media_ref', 'PROCESSING', cast(str, work_id), bid, generation, cast(str, occurrence_id))
        refs = await media.rows.read('references_get', {'reference_id': edge})
        if not refs or refs[0]['owner_id'] != work_id: raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        blobs = await media.rows.read('blobs_get', {'blob_id': bid})
        if not blobs or blobs[0]['state'] != 'READY' or blobs[0]['generation'] != generation:
            raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_NOT_READY')
        blob = blobs[0]; path = media.published / (bid + '.' + str(generation))
        def read():
            fd = media.files.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                meta = os.fstat(fd)
                if (not stat.S_ISREG(meta.st_mode) or (meta.st_dev, meta.st_ino, meta.st_size) !=
                        (blob['device'], blob['inode'], blob['byte_count'])):
                    raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
                result = bytearray()
                while part := os.read(fd, media.settings.integer('media.read_chunk_bytes')):
                    if len(result) + len(part) > media.settings.integer('media.blob_max_bytes'):
                        raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
                    result.extend(part)
                if len(result) != blob['byte_count'] or hashlib.sha256(result).hexdigest() != blob['sha256']:
                    raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
                return bytes(result)
            finally: os.close(fd)
        data = cast(bytes, await media.integrity.io(blob, 'processing:' + cast(str, work_id), read))
        if await media.rows.read('references_get', {'reference_id': edge}) != refs:
            raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
        return ProcessingBytes(data, identity('media_artifact', media.configuration.database_id, cast(str, work_id), bid, generation), cast(str, occurrence['modality']))
    except (OwnerFailure, OSError) as failure: return media._failure('authorize_stored_media', failure)
