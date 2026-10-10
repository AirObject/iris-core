"""Content-addressed attachments and bounded per-object image understanding.

Route-independent API: save_media -> add_message(media_ids=[...]). Uploads never
call a model. prepare_media runs after claiming a batch and before its snapshot.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import IMAGE_TOTAL_TIMEOUT, ModelError

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
GRACE_PERIOD = timedelta(days=1)
REFUSAL_TEXT = '敏感信息无法访问'
LABELS = {'image': '图片', 'audio': '音频', 'video': '视频'}
SOURCES = {'host': '宿主提供', 'system': '本系统理解', 'refused': '被拒绝', 'unprocessed': '未理解'}
# Raster images only. SVG/HTML and arbitrary application/* files are not accepted.
CONTENT_TYPES = {
    'image/png': 'image', 'image/jpeg': 'image', 'image/gif': 'image', 'image/webp': 'image',
    'audio/mpeg': 'audio', 'audio/wav': 'audio', 'audio/ogg': 'audio', 'audio/flac': 'audio',
    'video/mp4': 'video', 'video/webm': 'video',
}


class MediaError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__({
            'media_too_large': '媒体超过单文件大小上限',
            'unsupported_media_type': '媒体类型不支持或与文件头不符',
            'invalid_media': '媒体标识不存在或文件不可用',
            'invalid_media_list': 'media_ids 必须是不重复的媒体标识列表',
            'invalid_understanding': '理解文本必须是字符串，且不超过 32 KB',
            'unsafe_media_path': '媒体路径不可用',
        }.get(code, '媒体操作失败'))


def _now():
    return datetime.now(timezone.utc)


def _directory(store):
    path = store.path.parent / 'media'
    if path.is_symlink():
        raise MediaError('unsafe_media_path')
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _path(store, digest):
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise MediaError('unsafe_media_path')
    path = _directory(store) / digest
    if path.is_symlink():
        raise MediaError('unsafe_media_path')
    return path


def _matches(content_type, head):
    # MIME plus container signatures; decoding remains the provider's job. Never
    # execute, fetch URLs, decompress, or trust a client-supplied filename.
    checks = {
        'image/png': head.startswith(b'\x89PNG\r\n\x1a\n') and head[12:16] == b'IHDR',
        'image/jpeg': head.startswith(b'\xff\xd8\xff'),
        'image/gif': head.startswith((b'GIF87a', b'GIF89a')),
        'image/webp': head.startswith(b'RIFF') and head[8:12] == b'WEBP',
        'audio/mpeg': head.startswith(b'ID3') or (len(head) >= 2 and head[0] == 0xff and head[1] & 0xe0 == 0xe0),
        'audio/wav': head.startswith(b'RIFF') and head[8:12] == b'WAVE',
        'audio/ogg': head.startswith(b'OggS'),
        'audio/flac': head.startswith(b'fLaC'),
        'video/mp4': head[4:8] == b'ftyp' and any(brand in head[8:64] for brand in (b'isom', b'iso2', b'mp41', b'mp42', b'avc1', b'M4V ')),
        'video/webm': head.startswith(b'\x1aE\xdf\xa3') and b'webm' in head,
    }
    return checks.get(content_type, False)


def _projection(row):
    return {key: row[key] for key in ('id', 'sha256', 'content_type', 'size_bytes', 'kind',
            'understanding_source', 'understanding_text', 'completed_at', 'last_error')}


def get_media(store, media_id):
    with store.read() as conn:
        row = conn.execute('''SELECT o.*,f.sha256,f.content_type,f.size_bytes FROM media_objects o
            JOIN media_files f ON f.id=o.file_id WHERE o.id=?''', (media_id,)).fetchone()
    if row is None:
        raise MediaError('invalid_media')
    return _projection(row)


def read_media(store, media_id):
    """Read an existing original without creating directories or following links.

    Pin the media directory and file with descriptors so path replacement cannot
    redirect the read after validation. Never accept a client-supplied filename.
    """
    item = get_media(store, media_id)
    if (not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
            or not 0 < item['size_bytes'] <= DEFAULT_MAX_BYTES):
        raise MediaError('invalid_media')
    try:
        directory = os.open(store.path.parent / 'media', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(item['sha256'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != item['size_bytes']:
                raise MediaError('invalid_media')
            data = file.read(item['size_bytes'] + 1)
    except OSError:
        raise MediaError('invalid_media') from None
    if (len(data) != item['size_bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']
            or not _matches(item['content_type'], data[:512])):
        raise MediaError('invalid_media')
    return data, item['content_type']


def save_media(store, data, *, content_type, understanding_text=None, max_bytes=DEFAULT_MAX_BYTES, current=None):
    """Save bytes/a binary stream atomically; return a new object, reusing bytes.

    The AP line must enforce upload authentication/size while receiving the body
    too. This helper bounds reads to max_bytes+1 and takes no URLs or filenames.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError('max_bytes must be a positive integer')
    if not isinstance(content_type, str) or content_type not in CONTENT_TYPES:
        raise MediaError('unsupported_media_type')
    if understanding_text is not None and (not isinstance(understanding_text, str) or len(understanding_text.encode('utf-8')) > 32768):
        raise MediaError('invalid_understanding')
    host_text = understanding_text if understanding_text and understanding_text.strip() else None
    kind = CONTENT_TYPES[content_type]
    source = 'host' if host_text else 'unprocessed'
    text = host_text if host_text else f'[{LABELS[kind]}，未理解]'
    current = current or _now()
    stamp = current.isoformat()
    stream = io.BytesIO(data) if isinstance(data, bytes) else data
    directory = _directory(store)
    descriptor, temporary = tempfile.mkstemp(prefix='.upload-', dir=directory)
    size, head, digest = 0, b'', hashlib.sha256()
    try:
        with os.fdopen(descriptor, 'wb') as file:
            while True:
                chunk = stream.read(min(65536, max_bytes-size+1))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise MediaError('unsupported_media_type')
                size += len(chunk)
                if size > max_bytes:
                    raise MediaError('media_too_large')
                head = (head + chunk)[:512]
                digest.update(chunk)
                file.write(chunk)
            if not size or not _matches(content_type, head):
                raise MediaError('unsupported_media_type')
            file.flush()
            os.fsync(file.fileno())
        sha = digest.hexdigest()
        target = _path(store, sha)
        media_id = uuid.uuid4().hex
        # Only the rename/existence check and metadata commit hold the writer;
        # streaming, hashing and fsync above do not block message intake.
        with store.write() as conn:
            row = conn.execute('SELECT id,refused_at FROM media_files WHERE sha256=?', (sha,)).fetchone()
            if not target.exists():
                os.replace(temporary, target)
            elif not target.is_file():
                raise MediaError('unsafe_media_path')
            if row:
                fid = row[0]
                if row['refused_at'] and not host_text:
                    source, text = 'refused', REFUSAL_TEXT
                # A fresh upload of an orphan gets a fresh attachment window.
                conn.execute('UPDATE media_files SET unreferenced_at=? WHERE id=? AND unreferenced_at IS NOT NULL', (stamp, fid))
            else:
                fid = conn.execute('''INSERT INTO media_files(sha256,content_type,size_bytes,created_at,unreferenced_at)
                    VALUES(?,?,?,?,?)''', (sha, content_type, size, stamp, stamp)).lastrowid
            conn.execute('''INSERT INTO media_objects(id,file_id,kind,understanding_source,understanding_text,created_at,completed_at)
                VALUES(?,?,?,?,?,?,?)''', (media_id, fid, kind, source, text, stamp, stamp if source != 'unprocessed' else None))
    finally:
        Path(temporary).unlink(missing_ok=True)
    return get_media(store, media_id)


def save_host_media(store, data, *, entry_id, scope, actor, **fields):
    """Upload adapter: bind the new object to an authorized origin entry.

    The durable upload operation is the authorization grant, not a message
    reference. It must accompany this object when exporting/importing host media.
    It does not prevent ordinary orphan cleanup. An interrupted upload without
    its grant is inaccessible through HTTP and is collected after the grace day.
    """
    from .memory_ops import operation
    scope.require(entry_id)
    result = save_media(store, data, **fields)
    with store.write() as conn:
        operation(conn, 'media_uploaded', 'media', result['id'], {'entry_id': entry_id}, actor=actor)
    return result


def require_host_media(conn, media_ids, *, scope):
    """Check origin grants in the same transaction that attaches new messages."""
    for media_id in media_ids:
        row = conn.execute("""SELECT a.details_json FROM media_objects o
            JOIN admin_operations a ON a.object_type='media' AND a.object_id=o.id
            AND a.action='media_uploaded' WHERE o.id=? ORDER BY a.id LIMIT 1""", (media_id,)).fetchone()
        if row is None:
            raise MediaError('invalid_media')
        scope.require(json.loads(row[0])['entry_id'])


def attach_media(store, conn, message_id, media_ids):
    """Run inside the message insert transaction, including availability checks."""
    if not isinstance(media_ids, (list, tuple)) or any(not isinstance(mid, str) for mid in media_ids) or len(set(media_ids)) != len(media_ids):
        raise MediaError('invalid_media_list')
    for position, media_id in enumerate(media_ids):
        row = conn.execute('''SELECT f.sha256 FROM media_objects o JOIN media_files f ON f.id=o.file_id
            WHERE o.id=?''', (media_id,)).fetchone()
        if row is None or not _path(store, row[0]).is_file():
            raise MediaError('invalid_media')
        conn.execute('INSERT INTO message_media VALUES(?,?,?)', (message_id, position, media_id))


def message_media(conn, message_ids):
    result = {}
    if not message_ids:
        return result
    rows = conn.execute(f'''SELECT r.message_id,r.position,o.*,f.sha256,f.content_type,f.size_bytes
        FROM message_media r JOIN media_objects o ON o.id=r.media_id JOIN media_files f ON f.id=o.file_id
        WHERE r.message_id IN ({','.join('?' for _ in message_ids)}) ORDER BY r.message_id,r.position''', message_ids)
    for row in rows:
        result.setdefault(row['message_id'], []).append(_projection(row))
    return result


def material_content(content, media):
    """One message-sized data unit; caller applies the existing 1500-token cut."""
    if not media:
        return content
    rows = [{'序号': index, '类型': LABELS[item['kind']], '来源': SOURCES[item['understanding_source']],
             '被拒绝': item['understanding_source'] == 'refused', '理解文本': item['understanding_text']}
            for index, item in enumerate(media, 1)]
    return content + '\n媒体（数据）=' + json.dumps(rows, ensure_ascii=False, separators=(',', ':'))


def prepare_media(store, gateway, message_ids, *, batch_id=None, budget_seconds=IMAGE_TOTAL_TIMEOUT, clock=_now):
    """At most 120 s for the entire batch, including retries and admission.

    In-flight objects owned by another batch use their current placeholder; no
    extra wait is added. Leases expire after the budget on crash, and a stale
    worker cannot overwrite a new attempt. Refusals and completed attempts are
    not automatically repeated by later history/target/relearn passes.
    """
    if not gateway or not callable(getattr(gateway, 'image_understanding', None)):
        return
    config = gateway.configs.get('image_understanding')
    if not config or not config.model or not config.base_url:
        return
    monotonic = getattr(gateway, 'monotonic', time.monotonic)
    deadline = monotonic() + min(IMAGE_TOTAL_TIMEOUT, max(0, budget_seconds))
    with store.read() as conn:
        by_message = message_media(conn, message_ids)
    ordered = dict.fromkeys(item['id'] for mid in message_ids for item in by_message.get(mid, []))
    for media_id in ordered:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        token = uuid.uuid4().hex
        with store.write() as conn:
            row = conn.execute('''SELECT o.*,f.sha256,f.content_type,f.size_bytes,f.refused_at
                FROM media_objects o JOIN media_files f ON f.id=o.file_id WHERE o.id=?''', (media_id,)).fetchone()
            if row is None or row['kind'] != 'image' or row['understanding_source'] != 'unprocessed' or row['completed_at']:
                continue
            if row['refused_at']:
                conn.execute("UPDATE media_objects SET understanding_source='refused',understanding_text=?,completed_at=? WHERE id=?",
                             (REFUSAL_TEXT, clock().isoformat(), media_id))
                continue
            if row['attempt_token'] and datetime.fromisoformat(row['lease_until']) > clock():
                continue
            conn.execute('UPDATE media_objects SET attempt_token=?,lease_until=? WHERE id=?',
                         (token, (clock()+timedelta(seconds=remaining)).isoformat(), media_id))
        source, text, error, completed = 'unprocessed', '[图片，未理解]', None, True
        try:
            path = _path(store, row['sha256'])
            with path.open('rb') as file:
                data = file.read(row['size_bytes']+1)
            if len(data) != row['size_bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                raise MediaError('invalid_media')
            result = gateway.image_understanding(data, row['content_type'], batch_id=batch_id, _deadline=deadline)
            if monotonic() > deadline:
                raise ModelError('retryable', 'total timeout', reason='timeout')
            if not isinstance(result, str) or not result.strip():
                raise ModelError('invalid_output', 'invalid image description')
            source, text = 'system', result
        except ModelError as exc:
            if exc.category == 'content_rejection':
                source, text = 'refused', REFUSAL_TEXT
            # Store categories only; provider prose can contain input or secrets.
            error = exc.category
            completed = exc.category not in ('paused', 'queue_full', 'queue_timeout')
        except (OSError, ValueError):
            error = 'file_unavailable'
        with store.write() as conn:
            current = conn.execute('SELECT attempt_token,understanding_source FROM media_objects WHERE id=?', (media_id,)).fetchone()
            if current is None or current['attempt_token'] != token or current['understanding_source'] != 'unprocessed':
                continue
            if source == 'refused':
                conn.execute('UPDATE media_files SET refused_at=? WHERE id=?', (clock().isoformat(), row['file_id']))
            if conn.execute('SELECT refused_at FROM media_files WHERE id=?', (row['file_id'],)).fetchone()[0]:
                source, text, completed = 'refused', REFUSAL_TEXT, True
            conn.execute('''UPDATE media_objects SET understanding_source=?,understanding_text=?,last_error=?,
                completed_at=?,attempt_token=NULL,lease_until=NULL WHERE id=? AND attempt_token=?''',
                (source, text, error, clock().isoformat() if completed else None, media_id, token))


def files_for_message(conn, message_id):
    return [r[0] for r in conn.execute('''SELECT DISTINCT o.file_id FROM message_media r
        JOIN media_objects o ON o.id=r.media_id WHERE r.message_id=?''', (message_id,))]


def mark_unreferenced(conn, file_ids, current):
    # Maintenance supplies its execution clock; the trigger covers all other
    # deletion paths. Starting the grace period is atomic with message removal.
    for fid in file_ids:
        conn.execute('''UPDATE media_files SET unreferenced_at=? WHERE id=? AND NOT EXISTS(
            SELECT 1 FROM media_objects o JOIN message_media r ON r.media_id=o.id WHERE o.file_id=media_files.id)''',
            (current.isoformat(), fid))


def delete_unreferenced_file(store, conn, file_id, current):
    """Recheck under the same writer used for upload/attachment, then unlink.

    If interrupted after unlink, the orphan metadata is safe to retry. Intake
    verifies the file exists; no live message can acquire a missing attachment.
    """
    row = conn.execute('SELECT * FROM media_files WHERE id=?', (file_id,)).fetchone()
    if row is None or not row['unreferenced_at'] or current-datetime.fromisoformat(row['unreferenced_at']) < GRACE_PERIOD:
        return False
    if conn.execute('''SELECT 1 FROM media_objects o JOIN message_media r ON r.media_id=o.id
        WHERE o.file_id=? LIMIT 1''', (file_id,)).fetchone():
        return False
    _path(store, row['sha256']).unlink(missing_ok=True)
    conn.execute('DELETE FROM media_files WHERE id=?', (file_id,))
    return True


def cleanup_upload_orphans(store, current):
    """Reclaim interrupted temp/rename files after a day, never following links."""
    directory = store.path.parent / 'media'
    if not directory.exists() or directory.is_symlink():
        return {'removed': 0, 'failed': 0}
    result = {'removed': 0, 'failed': 0}
    cutoff = (current-GRACE_PERIOD).timestamp()
    for path in directory.iterdir():
        if not (re.fullmatch(r'[0-9a-f]{64}', path.name) or path.name.startswith('.upload-')):
            continue
        try:
            if path.is_symlink() or path.stat().st_mtime > cutoff:
                continue
            # Upload rename+registration takes this same writer. Recheck both
            # timestamp and DB membership so a concurrent save cannot lose bytes.
            with store.write() as conn:
                if conn.execute('SELECT 1 FROM media_files WHERE sha256=?', (path.name,)).fetchone():
                    continue
                if path.is_symlink() or not path.is_file() or path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
                result['removed'] += 1
        except FileNotFoundError:
            pass
        except OSError:
            result['failed'] += 1
    return result
