"""Exclusive local-volume ownership and durable identity before database creation.

Identity is atomically published and synced before SQLite is opened. A crashed
initializer may resume its original identity, but no unknown database is adopted.
The process lock is never removed or forcibly unlocked. Secret files are opened
relative to a separately verified read-only root and never included in backups.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import cast
from uuid import uuid4

from companion_memory.configuration.deployment import DeploymentSettings

DIRECTORIES = ('bootstrap', 'db', 'blobs', 'upload_staging', 'indexes',
               'logs', 'logs/runtime', 'logs/index', 'backups', 'backup_staging')
IDENTITY_FORMAT = 'MANAGED_RESOURCE_IDENTITY_V1'


def sync_directory(path: Path) -> None:
    """Sync a directory after atomic publication; errors retain uncertain status."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_record(path: Path, value: dict[str, object]) -> None:
    """Publish a bounded owner record without following destination symlinks."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if len(payload) > 65536:
        raise ValueError('Resource identity exceeds its carrier.')
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError('Resource identity alias rejected.')
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_record(path: Path) -> dict[str, object]:
    """Read one regular private record, rejecting aliases and oversized input."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Resource identity permissions differ.')
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('Resource identity exceeds its carrier.')
    value = json.loads(data)
    if type(value) is not dict:
        raise ValueError('Invalid resource identity.')
    return cast(dict[str, object], value)


class ManagedResources:
    """One physical owner of a verified volume, held until actual cleanup ends."""
    def __init__(self, settings: DeploymentSettings, assembly_digest: str, *, retired_for_restore: bool = False):
        self.settings = settings
        self.root = Path(settings.text('deployment.data_root'))
        self.secret_root = Path(settings.text('deployment.secret_root'))
        self.fd = -1
        self.identity: dict[str, object] = {}
        if os.geteuid() == 0:
            raise ValueError('The managed application requires a non-root account.')
        if len(assembly_digest) != 64 or any(c not in '0123456789abcdef' for c in assembly_digest):
            raise ValueError('A complete assembly digest is required.')
        self._directory(self.root, create=False)
        self._directory(self.secret_root, create=False, secret=True)
        self.check_space(0)
        self.fd = os.open(self.root / '.process.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('Invalid process ownership file.')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for name in DIRECTORIES:
                self._directory(self.root / name, create=True)
            identity_path = self.root / 'bootstrap/identity.json'
            if not identity_path.exists():
                if any((self.root / 'db').iterdir()):
                    raise ValueError('An unbound database cannot be adopted.')
                info = self.root.stat()
                atomic_record(identity_path, {
                    'format': IDENTITY_FORMAT, 'database_id': str(uuid4()), 'instance_id': str(uuid4()),
                    'wizard_id': str(uuid4()), 'root': str(self.root), 'root_device': info.st_dev,
                    'root_inode': info.st_ino, 'assembly_digest': assembly_digest,
                    'birth_device': info.st_dev, 'birth_inode': info.st_ino,
                    'authority_id': str(uuid4()), 'state': 'BOOTSTRAP',
                })
            self.identity = read_record(identity_path)
            expected_fields = {'format', 'database_id', 'instance_id', 'wizard_id', 'root', 'root_device',
                               'root_inode', 'birth_device', 'birth_inode', 'assembly_digest', 'authority_id', 'state'}
            info = self.root.stat()
            if (set(self.identity) != expected_fields or self.identity['format'] != IDENTITY_FORMAT
                    or self.identity['root'] != str(self.root) or self.identity['root_device'] != info.st_dev
                    or self.identity['root_inode'] != info.st_ino or self.identity['assembly_digest'] != assembly_digest
                    or self.identity['state'] not in ('BOOTSTRAP', 'INITIALIZING', 'ACTIVE', 'RETIRED')):
                raise ValueError('Retained startup identity differs.')
            from companion_memory.persistence.schema import valid_identifier
            if any(not valid_identifier(self.identity[k]) for k in ('database_id', 'instance_id', 'wizard_id', 'authority_id')):
                raise ValueError('Invalid retained startup identity.')
            if any(type(self.identity[key]) is not int or cast(int, self.identity[key]) < 0 for key in ('birth_device', 'birth_inode')):
                raise ValueError('Invalid retained birth resource identity.')
            if self.identity['state'] == 'RETIRED' and not retired_for_restore:
                raise ValueError('This resource no longer owns instance startup.')
            restore_path = self.root / 'bootstrap/restore-switch.json'
            if restore_path.exists() and self.identity['state'] != 'RETIRED':
                restored = read_record(restore_path)
                if (restored.get('format') != 'MANAGED_RESTORE_SWITCH_V1' or restored.get('state') != 'ACTIVE'
                        or restored.get('target_authority') != self.identity['authority_id']
                        or restored.get('target_device') != info.st_dev or restored.get('target_inode') != info.st_ino):
                    raise ValueError('Restore authority has not completed its switch.')
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _directory(path: Path, *, create: bool, secret: bool = False) -> None:
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Protected directory alias rejected.')
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Protected directory ownership or permissions differ.')
        if path.resolve(strict=True) != path:
            raise ValueError('Noncanonical protected directory.')
        if not secret and not os.access(path, os.W_OK | os.X_OK):
            raise ValueError('Protected directory is not writable.')
        if secret and not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise ValueError('Secrets require a read-only mount.')

    @property
    def database_id(self) -> str:
        return cast(str, self.identity['database_id'])

    @property
    def instance_id(self) -> str:
        return cast(str, self.identity['instance_id'])

    def check_space(self, additional: int) -> None:
        """Reserve host headroom before bounded preparation or backup allocation."""
        info = os.statvfs(self.root)
        if additional < 0 or info.f_bavail * info.f_frsize - additional < self.settings.integer('deployment.free_reserve_bytes'):
            raise ValueError('Insufficient free space for the protected reserve.')

    def retained_identity_check(self, database_id: str, path: str) -> bool:
        """Confirm the external expected identity without reading the database."""
        return (self.fd >= 0 and self.identity.get('state') != 'RETIRED' and self.identity == read_record(self.root / 'bootstrap/identity.json')
                and database_id == self.database_id and path == str(self.root / 'db/memory.sqlite3'))

    def read_secret(self, reference: str) -> bytes:
        """Read a single private basename, never arbitrary paths or symlinks."""
        if not reference or len(reference) > 128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in reference):
            raise ValueError('Invalid secret reference.')
        directory = os.open(self.secret_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(reference, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError('Invalid secret permissions.')
                value = stream.read(4097).rstrip(b'\n')
            if not 32 <= len(value) <= 4096:
                raise ValueError('Invalid secret length.')
            return value
        finally:
            os.close(directory)

    def protected_directories(self) -> dict[str, tuple[str, ...]]:
        """Enumerate actual protected roots, including same-database aliases."""
        db = (str(self.root / 'db'),)
        return {'media': (str(self.root / 'blobs'), str(self.root / 'upload_staging')),
                'database': db, 'audit': db, 'provider_usage': db,
                'backup': (str(self.root / 'backups'), str(self.root / 'backup_staging'))}

    def close(self) -> None:
        """Release this process's descriptor; the ownership file remains in place."""
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
