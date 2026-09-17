"""Stopped-volume restore verification and durable transfer of startup authority.

Both source and isolated target locks stay held throughout the switch. Retiring
the source precedes target activation; interruption can leave neither active,
but never grants two copies startup authority. The original database is retained.
"""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import cast
from uuid import uuid4
from collections.abc import Callable

from companion_memory.configuration.deployment import DeploymentSettings
from companion_memory.persistence.managed_backup import ConsistentBackup, file_digest, sync_tree, private_parents
from .managed_resources import ManagedResources, atomic_record, read_record, sync_directory


class RestoreSwitch:
    """Local deployment port; no network route accepts arbitrary volume paths."""
    def __init__(self, settings: DeploymentSettings, assembly_digest: str):
        self.source = ManagedResources(settings, assembly_digest, retired_for_restore=True)

    def close(self) -> None:
        self.source.close()

    def prepare(self, backup_id: str, target: Path) -> dict[str, object]:
        if self.source.identity['state'] == 'RETIRED':
            raise ValueError('A retired source cannot create another restore.')
        from companion_memory.persistence.schema import valid_identifier
        if not valid_identifier(backup_id) or '/' in backup_id or backup_id in ('.', '..'):
            raise ValueError('Invalid backup identity.')
        copier = ConsistentBackup(self.source, lambda: True)
        backup = self.source.root / 'backups' / backup_id
        manifest = copier.restore_isolated(backup, target)
        switch_id = str(uuid4())
        preserved = cast(dict[str, str], read_record(target / 'restore-intent.json')['preserved_files'])
        for name in ('restore-manifest.json', 'restore-switch.json', 'restore-media.json', 'restore-outgoing.json'):
            original = target / 'bootstrap' / name
            if original.exists():
                retained = target / 'bootstrap/restore-history' / switch_id / name
                private_parents(retained.parent, target)
                os.rename(original, retained)
                preserved['bootstrap/' + name] = retained.relative_to(target).as_posix()
        # Keep the original signed-by-digest file declaration on the isolated
        # volume so native owners can validate remapping after a later restart.
        source = backup / 'manifest.json'
        destination = target / 'bootstrap/restore-manifest.json'
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as incoming:
            with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as outgoing:
                while block := incoming.read(1048576):
                    outgoing.write(block)
                outgoing.flush();os.fsync(outgoing.fileno())
        manifest_digest = file_digest(destination)[1]
        info = target.stat()
        intent = {'format': 'MANAGED_RESTORE_SWITCH_V1', 'switch_id': switch_id, 'backup_id': backup_id,
            'source_authority': manifest['source_authority'], 'target_authority': str(uuid4()),
            'database_id': manifest['database_id'], 'instance_id': manifest['instance_id'],
            'manifest_digest': manifest_digest, 'target_device': info.st_dev, 'target_inode': info.st_ino,
            'runtime_root': self.source.identity['root'], 'state': 'PREPARED', 'startup_sends': 0, 'preserved_files': preserved}
        atomic_record(target / 'bootstrap/restore-switch.json', intent)
        sync_tree(target)
        sync_directory(target.parent)
        return intent

    def activate(self, target: Path, switch_id: str, *, checkpoint: Callable[[str], None] = lambda point: None) -> dict[str, object]:
        """Reconfirm the same switch after interruption; never infer from names."""
        ManagedResources._directory(target, create=False)
        fd = os.open(target / '.process.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_nlink != 1 or meta.st_mode & 0o077:
                raise ValueError('Invalid isolated target lock.')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            intent = read_record(target / 'bootstrap/restore-switch.json')
            if intent.get('format') != 'MANAGED_RESTORE_SWITCH_V1' or intent.get('switch_id') != switch_id:
                raise ValueError('Restore switch identity differs.')
            meta = target.stat()
            if (intent['target_device'], intent['target_inode']) != (meta.st_dev, meta.st_ino):
                raise ValueError('Isolated restore resource changed.')
            source = self.source
            if (intent['source_authority'] != source.identity['authority_id'] or intent['database_id'] != source.database_id
                    or intent['instance_id'] != source.instance_id or intent['runtime_root'] != source.identity['root']):
                raise ValueError('Restore source identity differs.')
            path = source.root / 'bootstrap/restore-outgoing.json'
            if path.exists():
                retained = read_record(path)
                if {k: v for k, v in retained.items() if k != 'state'} != {k: v for k, v in intent.items() if k != 'state'}:
                    raise ValueError('Another restore owns this source.')
            elif source.identity['state'] == 'RETIRED':
                raise ValueError('Retired authority has no matching switch.')
            identity = read_record(target / 'bootstrap/identity.json')
            expected_identity = source.identity | {'root': intent['runtime_root'], 'root_device': meta.st_dev,
                'root_inode': meta.st_ino, 'authority_id': intent['target_authority'], 'state': 'ACTIVE'}
            if intent['state'] == 'ACTIVE':
                if source.identity['state'] != 'RETIRED' or identity != expected_identity:
                    raise ValueError('Completed restore authority differs.')
                return intent
            # Before authority changes, verify every target byte against the
            # original complete snapshot. A partially opened target is rejected.
            manifest = restore_manifest(target, intent)
            preserved = cast(dict[str, str], intent['preserved_files'])
            for item in cast(list[dict[str, object]], manifest['files']):
                name = cast(str, item['path'])
                if name == 'bootstrap/identity.json' and identity == expected_identity:
                    continue
                if file_digest(target / preserved.get(name, name)) != (item['bytes'], item['sha256']):
                    raise ValueError('Restore target content changed before activation.')
            checkpoint('before_source_fence')
            atomic_record(path, intent | {'state': 'FENCING'})
            atomic_record(source.root / 'bootstrap/identity.json', source.identity | {'state': 'RETIRED'})
            source.identity = source.identity | {'state': 'RETIRED'}
            checkpoint('after_source_fence')
            if identity['authority_id'] not in (intent['source_authority'], intent['target_authority']):
                raise ValueError('Restore target already belongs to another authority.')
            atomic_record(target / 'bootstrap/identity.json', expected_identity)
            checkpoint('after_target_identity')
            active = intent | {'state': 'ACTIVE'}
            atomic_record(target / 'bootstrap/restore-switch.json', active)
            atomic_record(path, active)
            checkpoint('after_target_active')
            return active
        finally:
            os.close(fd)


def restore_manifest(root: Path, intent: dict[str, object]) -> dict[str, object]:
    """Read only a bounded manifest tied to the durable verified switch."""
    path = root / 'bootstrap/restore-manifest.json'
    size, digest = file_digest(path)
    if size > 4194304 or digest != intent['manifest_digest']:
        raise ValueError('Restore manifest differs from its switch.')
    value = json.loads(path.read_bytes())
    if type(value) is not dict or value.get('database_id') != intent['database_id'] or value.get('source_authority') != intent['source_authority']:
        raise ValueError('Restore manifest identity differs.')
    return value
