"""Bounded local file recovery after exclusive root ownership is established.

Only retained upload intents authorize publication. READY files are verified in
full before admission; damage never becomes a missing interpretation or a new
upload. Recovery sends no model requests and never resets an upload deadline.
"""
from __future__ import annotations
import os
import stat
import time
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Committed, Found, Value
from companion_memory.persistence.owned_statements import OwnerFailure
if TYPE_CHECKING:
    from .service import MediaService


async def recover_files(media: MediaService):
    """Verify one complete paged recovery within the configured total deadline."""
    from .service import MediaError, identity
    deadline = time.monotonic() + media.settings.integer('media.recovery_timeout_ms') / 1000
    def checkpoint():
        if time.monotonic() >= deadline:
            raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED', bool(media._jobs))
    if media._file_cursor[0] == 'reads':
        cursor = media._file_cursor[1]
        while True:
            checkpoint()
            reads = await media.rows.read('read_recovery_page', {'after': cursor, 'limit': media.settings.integer('media.gc_page_size')})
            if not reads: break
            for read in reads:
                checkpoint()
                result = await media._execute('release_media_read', 'release:' + cast(str, read['owner_id']),
                    {'read_id': read['owner_id'], 'occurrence_id': read['occurrence_id']})
                if type(result) is not Committed: return result
                cursor = cast(str, read['reference_id'])
                media._file_cursor = 'reads', cursor
        media._file_cursor = 'uploads', ''
    if media._file_cursor[0] == 'uploads':
        cursor = media._file_cursor[1]
        while True:
            checkpoint()
            rows = await media.rows.read('upload_page', {'after': cursor, 'limit': media.settings.integer('media.gc_page_size')})
            if not rows: break
            for upload in rows:
                checkpoint()
                uid = cast(str, upload['upload_id'])
                if upload['state'] == 'SEALED':
                    result = await recover_publication(media, upload)
                    if type(result) is not Committed: return result
                    upload = (await media.rows.read('uploads_get', {'upload_id': uid}))[0]
                if upload['state'] == 'READY':
                    await cleanup_staging(media, upload)
                elif upload['state'] == 'UPLOADING':
                    # Partial progress has no durability promise. The original
                    # intent remains queryable while a new owner requests full bytes.
                    result = await media._execute('require_media_reupload', identity('reupload', uid, upload['writer_generation']), {'upload_id': uid})
                    if type(result) is not Committed: return result
                cursor = uid
                media._file_cursor = 'uploads', cursor
        media._file_cursor = 'blobs', ''
    if media._file_cursor[0] == 'blobs':
        cursor = media._file_cursor[1]
        while True:
            checkpoint()
            rows = await media.rows.read('recovery_page', {'after': cursor, 'limit': media.settings.integer('media.gc_page_size')})
            if not rows: break
            for blob in rows:
                checkpoint(); bid = cast(str, blob['blob_id'])
                if blob['state'] == 'READY':
                    path = media.published / (bid + '.' + str(blob['generation']))
                    def verify():
                        observed = media._scan(path)
                        if observed != (blob['byte_count'], blob['sha256'], blob['device'], blob['inode']):
                            raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
                    await media.integrity.io(blob, 'recover:' + bid, verify)
                    counts = await media.rows.read('reference_count', {'blob_id': bid, 'generation': blob['generation']})
                    if counts[0]['count'] != blob['reference_count']:
                        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                elif blob['state'] == 'DELETE_PENDING':
                    result = await recover_retirement(media, blob)
                    if type(result) is not Committed: return result
                elif blob['state'] in ('PUBLISHING', 'FAULTED'):
                    return MediaError('FILE_FAILED', 'recover_media', 'media', 'CONTENT_CORRUPT')
                cursor = bid
                media._file_cursor = 'blobs', cursor
        media._file_cursor = 'complete', ''
    return Found(MappingProxyType({'state': 'FILES_VERIFIED', 'storage_execution': 'ACTUAL'}))


async def cleanup_staging(media: MediaService, upload: MappingProxyType[str, Value]) -> None:
    """Remove only the exact sealed temporary inode after READY confirmation."""
    uid = cast(str, upload['upload_id']); path = media.staging / uid
    def cleanup():
        try: meta = media.files.stat(path)
        except FileNotFoundError:
            media._sync_directory(media.staging)
            return
        if not stat.S_ISREG(meta.st_mode) or (meta.st_dev, meta.st_ino) != (upload['staging_device'], upload['staging_inode']):
            raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
        media.files.unlink(path); media._sync_directory(media.staging)
    await media._io(uid, cleanup)
    media._cleanup_pending.discard(uid); media._uploads.discard(uid); media._offsets.pop(uid, None); media._upload_deadlines.pop(uid, None)


async def recover_publication(media: MediaService, upload: MappingProxyType[str, Value]):
    """Finish only an originally sealed intent using its original READY key."""
    from .service import identity
    uid = cast(str, upload['upload_id']); stage = media.staging / uid
    destination = media.published / (cast(str, upload['blob_id']) + '.' + str(upload['generation']))
    def publish():
        try: original = media._scan(stage, True)
        except FileNotFoundError:
            original = media._scan(destination, True)
            if original != (upload['byte_count'], upload['sha256'], upload['staging_device'], upload['staging_inode']):
                raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
        else:
            if original != (upload['byte_count'], upload['sha256'], upload['staging_device'], upload['staging_inode']):
                raise OwnerFailure('FILE_FAILED', 'media', 'CONTENT_CORRUPT')
            try: media.files.link(stage, destination)
            except FileExistsError:
                target = media._scan(destination)
                if target[:2] != original[:2]: raise OwnerFailure('FILE_FAILED', 'media', 'HASH_COLLISION')
                first = media.files.open(stage, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    second = media.files.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        while part := os.read(first, media.settings.integer('media.upload_chunk_bytes')):
                            if os.read(second, len(part)) != part: raise OwnerFailure('FILE_FAILED', 'media', 'HASH_COLLISION')
                        if os.read(second, 1): raise OwnerFailure('FILE_FAILED', 'media', 'HASH_COLLISION')
                    finally: os.close(second)
                finally: os.close(first)
        media._sync_directory(media.published)
        meta = media.files.stat(destination)
        return meta.st_dev, meta.st_ino
    device, inode = cast(tuple[int, int], await media._io(uid, publish))
    return await media._execute('publish_media_upload', identity('ready', uid), {'upload_id': uid, 'device': device, 'inode': inode})


async def recover_retirement(media: MediaService, blob: MappingProxyType[str, Value]):
    """Finish a persisted zero-reference deletion for the exact old generation."""
    from .service import identity
    if blob['reference_count'] != 0 or blob['gc_operation'] is None:
        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    counts = await media.rows.read('reference_count', {'blob_id': blob['blob_id'], 'generation': blob['generation']})
    if counts[0]['count'] != 0: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    path = media.published / (cast(str, blob['blob_id']) + '.' + str(blob['generation']))
    def unlink():
        try: meta = media.files.stat(path)
        except FileNotFoundError: meta = None
        if meta is not None:
            if not stat.S_ISREG(meta.st_mode) or (meta.st_dev, meta.st_ino) != (blob['device'], blob['inode']):
                raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
            media.files.unlink(path)
        media._sync_directory(media.published)
    await media._io(cast(str, blob['gc_operation']), unlink)
    return await media._execute('delete_media_blob', identity('deleted', blob['gc_operation']),
        {'blob_id': blob['blob_id'], 'generation': blob['generation'], 'gc_operation': blob['gc_operation']})


async def maintain_uploads(media: MediaService):
    """Advance one bounded lifecycle page without stealing live or unconfirmed work."""
    from .service import identity
    pending = await media.integrity.flush()
    if pending is not None: return pending
    rows = await media.rows.read('upload_page', {'after': media._upload_cursor, 'limit': media.settings.integer('media.gc_page_size')})
    if not rows:
        media._upload_cursor = ''
        return None
    for upload in rows:
        uid = cast(str, upload['upload_id'])
        if uid in media._upload_owners or uid in media._jobs or media.storage.get_health().writes_in_flight:
            media._upload_cursor = uid
            continue
        if upload['state'] == 'SEALED':
            result = await recover_publication(media, upload)
            if type(result) is not Committed: return result
            upload = (await media.rows.read('uploads_get', {'upload_id': uid}))[0]
        if upload['state'] in ('UPLOADING', 'REUPLOAD_REQUIRED') and upload['staging_inode'] is None:
            def inspect():
                try: meta = media.files.stat(media.staging / uid)
                except FileNotFoundError: return None
                if not stat.S_ISREG(meta.st_mode): raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
                return meta.st_dev, meta.st_ino
            observed = await media._io(uid, inspect)
            if observed is not None:
                device, inode = cast(tuple[int, int], observed)
                result = await media._execute('bind_media_staging', identity('staging', uid, upload['writer_generation']),
                    {'upload_id': uid, 'writer_generation': upload['writer_generation'], 'staging_device': device, 'staging_inode': inode})
                if type(result) is not Committed: return result
                upload = (await media.rows.read('uploads_get', {'upload_id': uid}))[0]
        if upload['state'] in ('READY', 'UPLOADING', 'REUPLOAD_REQUIRED'):
            bindings = await media.rows.read('upload_bindings_get', {'upload_id': uid})
            now = time.time_ns() // 1000
            if not bindings and now >= media.upload_expires_at_us(upload):
                result = await media._execute('abandon_media_upload', identity('abandon_upload', uid),
                    {'upload_id': uid, 'now_us': media.upload_expires_at_us(upload)})
                if type(result) is not Committed: return result
                upload = (await media.rows.read('uploads_get', {'upload_id': uid}))[0]
        if upload['state'] in ('READY', 'ABANDONED'):
            try: await cleanup_staging(media, upload)
            except (OwnerFailure, OSError):
                media._cleanup_pending.add(uid)
                raise
        media._upload_cursor = uid
    return None
