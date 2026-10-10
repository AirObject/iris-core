"""Private, versioned snapshots and offline, whole-directory restoration.

Export never borrows the Store writer for SQLite's backup loop. Import stages
and validates everything before one atomic directory exchange under the service
and configuration leases. No archive content or credential is logged.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime
from importlib.resources import files
from pathlib import Path, PurePosixPath

from .configuration import CONFIG_LOCK_WAIT_SECONDS, RuntimeConfig
from .db import Store, dumps, now
from .memory_ops import operation
from .process_lock import StoreLease, LeaseBusyError

FORMAT_VERSION = 1
MANIFEST_LIMIT = 8 * 1024 * 1024
CHUNK = 1024 * 1024


class BackupError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _invalid():
    return BackupError('invalid_backup', '备份清单、文件完整性或数据库校验失败。')


def migration_versions():
    return sorted(p.name for p in files('iris').joinpath('migrations').iterdir() if p.name.endswith('.sql'))


def _versions(conn):
    return [row[0] for row in conn.execute('SELECT version FROM schema_migrations ORDER BY version')]


def _check_versions(versions):
    known = migration_versions()
    if not versions or versions != known[:len(versions)]:
        raise BackupError('unsupported_migration', '备份的迁移版本比当前程序新或迁移历史不兼容，请使用匹配的新版程序。')


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _invalid()
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw, object_pairs_hook=unique)


def _sync_file(path):
    with path.open('rb') as file:
        os.fsync(file.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_file(path):
    return open(path, 'xb', opener=lambda name, flags: os.open(name, flags, 0o600))


def _safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or any(ord(c) < 32 for c in name):
        raise _invalid()
    parts = name.split('/')
    if any(p in ('', '.', '..') for p in parts) or PurePosixPath(name).is_absolute():
        raise _invalid()
    if name not in ('iris.db', 'secrets.json') and (parts[0] != 'media' or len(parts) < 2):
        raise _invalid()


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _media_inventory(directory):
    if directory.is_symlink():
        raise BackupError('media_changed', '媒体目录不能包含符号链接或特殊文件。')
    if not directory.exists():
        return None
    inventory = {}
    def visit(parent):
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise BackupError('media_changed', '媒体目录不能包含符号链接或特殊文件。')
                if entry.is_dir(follow_symlinks=False):
                    visit(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    name = Path(entry.path).relative_to(directory.parent).as_posix()
                    _safe_name(name)
                    inventory[name] = _identity(entry.stat(follow_symlinks=False))
                else:
                    raise BackupError('media_changed', '媒体目录不能包含符号链接或特殊文件。')
    visit(directory)
    return inventory


def _copy_media(source, destination, expected):
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as incoming, _private_file(destination) as outgoing:
        if _identity(os.fstat(incoming.fileno())) != expected:
            raise BackupError('media_changed', '导出期间媒体发生变化，请重试。')
        shutil.copyfileobj(incoming, outgoing, CHUNK)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        if _identity(os.fstat(incoming.fileno())) != expected:
            raise BackupError('media_changed', '导出期间媒体发生变化，请重试。')


def _copy_database(source, destination):
    with _private_file(destination):
        pass
    target = sqlite3.connect(destination)
    try:
        source.backup(target, pages=256, sleep=.01)
        target.execute('PRAGMA journal_mode=DELETE')
    finally:
        target.close()


def _models(conn):
    row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='models'").fetchone()
    return _json(row[0]) if row else {}


def _without_secret_references(path):
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        models = _models(conn)
        changed = False
        for config in models.values():
            if 'api_key_ref' in config:
                del config['api_key_ref']
                changed = True
        if changed:
            conn.execute("UPDATE runtime_settings SET value_json=? WHERE key='models'", (dumps(models),))


def _check_database(path, versions):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise _invalid()
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None or _versions(conn) != versions:
            raise _invalid()
    finally:
        conn.close()


def _metadata(manifest, size, *, purpose='manual'):
    return {key: manifest[key] for key in ('backup_id', 'format_version', 'migration_version', 'created_at',
                                          'includes_model_secrets', 'media_included')} | {
        'file_count': len(manifest['files']), 'size': size, 'purpose': purpose}


def export_archive(store, output, *, include_secrets=False, actor='local_cli', runtime=None,
                   purpose='manual', _configuration_locked=False, _audit_store=None):
    """Publish a complete 0600 ZIP without replacing an existing output file.

    A pinned WAL read transaction defines the database instant. Media must stay
    unchanged across that instant and copying; a changed inventory aborts export.
    The configuration lease spans only snapshot pinning and optional key capture.
    """
    output = Path(output).absolute()
    runtime = runtime or RuntimeConfig(store)
    directory = store.path.parent
    published = False
    try:
        if type(include_secrets) is not bool or output.exists() or output.is_symlink():
            raise BackupError('output_exists', '输出文件已存在或导出选项无效，请选择新的文件名。')
        if output.resolve().is_relative_to((directory / 'media').resolve()):
            raise BackupError('invalid_output', '备份文件不能写入媒体目录。')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.iris-export-', dir=output.parent) as work:
            stage = Path(work)
            inventory = _media_inventory(directory / 'media')
            with store.read() as source:
                lock = contextlib.nullcontext() if _configuration_locked else runtime.locked(timeout=CONFIG_LOCK_WAIT_SECONDS)
                with lock:
                    # BEGIN alone does not pin a SQLite snapshot; this SELECT does.
                    versions = _versions(source)
                    _check_versions(versions)
                    stamp = now()
                    if include_secrets:
                        keys = runtime._secrets()
                        references = {row['api_key_ref'] for row in _models(source).values() if row.get('api_key_ref')}
                        if not references <= keys.keys():
                            raise BackupError('missing_model_secrets', '模型密钥引用缺失，无法导出含密钥的完整备份。')
                        with _private_file(stage / 'secrets.json') as file:
                            file.write(dumps({'format_version': 1, 'keys': {key: keys[key] for key in sorted(references)}}).encode())
                _copy_database(source, stage / 'iris.db')
            if not include_secrets:
                _without_secret_references(stage / 'iris.db')
            _check_database(stage / 'iris.db', versions)
            for name, identity in (inventory or {}).items():
                _copy_media(directory / name, stage / name, identity)
            if inventory != _media_inventory(directory / 'media'):
                raise BackupError('media_changed', '导出期间媒体发生变化，请重试。')
            manifest = {'format': 'iris-backup', 'format_version': FORMAT_VERSION, 'backup_id': uuid.uuid4().hex,
                        'created_at': stamp, 'migration_version': versions[-1], 'migrations': versions,
                        'includes_model_secrets': include_secrets, 'media_included': inventory is not None, 'files': {}}
            names = ['iris.db', *sorted(inventory or {})] + (['secrets.json'] if include_secrets else [])
            archive_path = stage / 'archive.zip'
            with _private_file(archive_path) as file, zipfile.ZipFile(file, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for name in names:
                    digest = hashlib.sha256()
                    size = 0
                    with (stage / name).open('rb') as incoming, archive.open(name, 'w', force_zip64=True) as member:
                        while data := incoming.read(CHUNK):
                            member.write(data)
                            digest.update(data)
                            size += len(data)
                    manifest['files'][name] = {'sha256': digest.hexdigest(), 'size': size}
                raw_manifest = dumps(manifest).encode('utf-8')
                if len(raw_manifest) > MANIFEST_LIMIT:
                    raise BackupError('manifest_too_large', '备份清单超过 8 MiB，请减少媒体文件数后重试。')
                archive.writestr('manifest.json', raw_manifest)
            _sync_file(archive_path)
            result = _metadata(manifest, archive_path.stat().st_size, purpose=purpose)
            # link is atomic and refuses replacement, including a racing symlink.
            os.link(archive_path, output)
            published = True
            _sync_directory(output.parent)
            with (_audit_store or store).write() as conn:
                operation(conn, 'backup_export', 'backup', manifest['backup_id'], result, actor=actor)
            return result
    except BackupError:
        if published:
            output.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        if published:
            output.unlink(missing_ok=True)
        raise BackupError('export_failed', '备份导出失败，请检查可用空间、数据完整性、媒体变化或配置锁后重试。') from None


def recent_exports(store, *, limit=30):
    with store.read() as conn:
        rows = conn.execute("""SELECT actor,details_json FROM admin_operations WHERE action='backup_export'
            ORDER BY id DESC LIMIT ?""", (max(1, min(limit, 100)),)).fetchall()
    fields = ('backup_id', 'format_version', 'migration_version', 'created_at', 'includes_model_secrets',
              'media_included', 'file_count', 'size', 'purpose')
    return [{'actor': row['actor'], **{key: value for key, value in _json(row['details_json']).items() if key in fields}}
            for row in rows]


def _read_manifest(archive):
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(set(names)) != len(names) or names.count('manifest.json') != 1:
        raise _invalid()
    if archive.getinfo('manifest.json').file_size > MANIFEST_LIMIT:
        raise _invalid()
    value = _json(archive.read('manifest.json'))
    if not isinstance(value, dict) or value.get('format') != 'iris-backup' or type(value.get('format_version')) is not int or value['format_version'] != FORMAT_VERSION:
        raise BackupError('unsupported_format', '备份格式版本不受支持。')
    versions = value.get('migrations')
    if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
        raise _invalid()
    _check_versions(versions)
    if value.get('migration_version') != versions[-1]:
        raise _invalid()
    if not isinstance(value.get('backup_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', value['backup_id']):
        raise _invalid()
    if not isinstance(value.get('created_at'), str) or datetime.fromisoformat(value['created_at']).tzinfo is None:
        raise _invalid()
    if any(type(value.get(field)) is not bool for field in ('includes_model_secrets', 'media_included')):
        raise _invalid()
    members = value.get('files')
    if not isinstance(members, dict) or 'iris.db' not in members or set(names) != {'manifest.json', *members}:
        raise _invalid()
    if ('secrets.json' in members) != value['includes_model_secrets']:
        raise _invalid()
    for info in infos:
        mode = stat.S_IFMT(info.external_attr >> 16)
        if info.is_dir() or mode not in (0, stat.S_IFREG) or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _invalid()
        if info.filename == 'manifest.json':
            continue
        _safe_name(info.filename)
        if info.filename.startswith('media/') and not value['media_included']:
            raise _invalid()
        detail = members[info.filename]
        if not isinstance(detail, dict) or type(detail.get('size')) is not int or detail['size'] != info.file_size:
            raise _invalid()
        if not isinstance(detail.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', detail['sha256']):
            raise _invalid()
    return value


def _unpack(archive_path, stage):
    with zipfile.ZipFile(archive_path) as archive:
        manifest = _read_manifest(archive)
        for name, detail in manifest['files'].items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(name) as source, _private_file(target) as output:
                while data := source.read(CHUNK):
                    size += len(data)
                    if size > detail['size']:
                        raise _invalid()
                    output.write(data)
                    digest.update(data)
                output.flush()
                os.fsync(output.fileno())
            if size != detail['size'] or digest.hexdigest() != detail['sha256']:
                raise _invalid()
    _check_database(stage / 'iris.db', manifest['migrations'])
    if manifest['includes_model_secrets']:
        # Validate the optional file without returning or logging its contents.
        with contextlib.closing(sqlite3.connect(stage / 'iris.db')) as conn:
            refs = {row['api_key_ref'] for row in _models(conn).values() if row.get('api_key_ref')}
        secrets_data = _json((stage / 'secrets.json').read_bytes())
        if not isinstance(secrets_data, dict) or secrets_data.get('format_version') != 1 or not isinstance(secrets_data.get('keys'), dict):
            raise _invalid()
        keys = secrets_data['keys']
        if not refs <= keys.keys() or not all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items()):
            raise _invalid()
    else:
        _without_secret_references(stage / 'iris.db')
    if manifest['media_included']:
        (stage / 'media').mkdir(exist_ok=True)
    return manifest


def _exchange_function():
    # Python has no portable atomic exchange of two nonempty directories. Both
    # supported local platforms expose it; never fall back to a partial restore.
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin' and hasattr(library, 'renameatx_np'):
        function, at_current = library.renameatx_np, -2
    elif sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
        function, at_current = library.renameat2, -100
    else:
        raise BackupError('atomic_import_unavailable', '此平台不支持原子目录替换；请在 macOS 或 Linux 上导入。')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    return function, at_current


def _exchange_directories(left, right):
    function, at_current = _exchange_function()
    if function(at_current, os.fsencode(left), at_current, os.fsencode(right), 2) != 0:
        raise OSError(ctypes.get_errno(), 'atomic directory exchange failed')


def _preserve_unmanaged(directory, stage, database_name):
    managed = {database_name, database_name + '-wal', database_name + '-shm', database_name + '-journal',
               'media', 'secrets.json'}
    for source in directory.iterdir():
        if source.name in managed:
            continue
        destination = stage / source.name
        if destination.exists() or destination.is_symlink():
            raise BackupError('data_directory_conflict', '数据目录包含与导入文件冲突的其他文件。')
        if source.is_symlink():
            destination.symlink_to(os.readlink(source), target_is_directory=False)
        elif source.is_dir():
            shutil.copytree(source, destination, copy_function=os.link, symlinks=True)
        else:
            # In particular, preserve the exact held lock inodes on both sides of
            # the exchange. A starting service cannot acquire a fresh lock file.
            os.link(source, destination)


class _OfflineDatabase:
    """Read an existing destination without startup migrations or initialization.

    Used only while the import owns both leases. Its pre-import export record is
    written into the staged database, alongside the successful import record.
    """
    def __init__(self, path):
        self.path = path

    @contextlib.contextmanager
    def read(self):
        conn = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
        try:
            conn.execute('BEGIN')
            yield conn
        finally:
            conn.close()


def import_archive(archive, database, *, confirm_overwrite=False, backup_include_secrets=False):
    """Offline restore; validation/migration failures never publish staged data.

    Pre-import archives live beside the data directory, survive replacement, and
    exclude model secrets unless the operator explicitly opts in.
    """
    database = Path(database).resolve()
    directory = database.parent
    archive = Path(archive).resolve()
    stage = None
    committed = False
    try:
        if directory == directory.parent or database.name in ('secrets.json', 'media') or database.name.endswith('.owner.lock'):
            raise BackupError('invalid_data_directory', '请选择独立的 Iris 数据目录。')
        _exchange_function()
        directory.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.iris-import-', dir=directory.parent))
        manifest = _unpack(archive, stage)
        if database.name != 'iris.db':
            (stage / 'iris.db').rename(stage / database.name)
        staged_database = stage / database.name
        # Migrate the private copy, never the live destination before validation.
        staged_store = Store(staged_database)
        staged_store.close()
        with StoreLease(database), StoreLease(directory / '.configuration', timeout=CONFIG_LOCK_WAIT_SECONDS):
            if database.is_symlink() or (directory / 'media').is_symlink() or (directory / 'secrets.json').is_symlink():
                raise BackupError('unsafe_data_directory', '数据文件与媒体目录不能是符号链接。')
            existing = [p for p in directory.iterdir() if not p.name.endswith('.owner.lock')]
            if existing and not confirm_overwrite:
                raise BackupError('confirmation_required', '覆盖已有数据须显式传入 --confirm-overwrite；覆盖前会自动备份。')
            previous = None
            previous_id = None
            staged_store = Store(staged_database)
            try:
                if existing:
                    if not database.is_file():
                        raise BackupError('incomplete_destination', '现有目录没有可备份的数据库，请改用全新的数据目录。')
                    previous = directory.with_name(directory.name + '.pre-import-' + uuid.uuid4().hex + '.zip')
                    info = export_archive(_OfflineDatabase(database), previous, include_secrets=backup_include_secrets,
                                          purpose='pre_import', _configuration_locked=True, _audit_store=staged_store)
                    previous_id = info['backup_id']
                result = {'backup_id': manifest['backup_id'], 'migration_version': migration_versions()[-1],
                          'includes_model_secrets': manifest['includes_model_secrets'], 'previous_backup_id': previous_id}
                with staged_store.write() as conn:
                    operation(conn, 'backup_import', 'backup', manifest['backup_id'], result, actor='local_cli')
            finally:
                staged_store.close()
            _preserve_unmanaged(directory, stage, database.name)
            _sync_file(staged_database)
            for parent, _, _ in os.walk(stage, followlinks=False):
                _sync_directory(parent)
            _exchange_directories(stage, directory)
            committed = True
            _sync_directory(directory.parent)
            return {**result, 'previous_backup': str(previous) if previous else None}
    except BackupError:
        raise
    except LeaseBusyError:
        raise BackupError('service_running', '请先停止服务及其他离线或配置命令，再导入备份。') from None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, zipfile.BadZipFile, RuntimeError):
        if committed:
            raise BackupError('import_sync_failed', '数据已完整替换，但目录同步失败；请检查磁盘，覆盖前备份仍在数据目录旁。') from None
        raise _invalid() from None
    finally:
        if stage is not None:
            # After exchange this is the old data directory. Hard-linked lock
            # files remain in the new directory; open descriptors keep ownership.
            shutil.rmtree(stage, ignore_errors=True)
