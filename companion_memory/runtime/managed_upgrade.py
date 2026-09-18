"""Explicit stopped-volume upgrade of the sole admitted managed predecessor.

Prepare an isolated database from a complete consistent backup. An fsynced
intent and retired startup authority fence both code versions during switching.
The original database, all business files, and backup remain recoverable; no
opening path performs an implicit migration or interprets a foreign assembly.
"""
from __future__ import annotations
from collections.abc import Callable
from contextlib import closing
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sqlite3
from typing import cast
from companion_memory.configuration.deployment import DeploymentSettings
from companion_memory.persistence._codec import assembly_value
from companion_memory.persistence.managed_backup import ConsistentBackup, file_digest
from .managed_bootstrap import ManagedBootstrap
from .managed_resources import ManagedResources, atomic_record, read_record, sync_directory


class CommunicationUpgrade:
    """One retained resource lease; only exact additive physical schemas qualify."""
    def __init__(self, settings: DeploymentSettings):
        self.old = ManagedBootstrap(settings, communication_format=False)
        self.new = ManagedBootstrap(settings, communication_format=True)
        if self.old.assembly_digest != '6c23faee15efaa705935c8d8c81e5b0372cb9e97a1a6d3829e72334bbb322d1a':
            raise ValueError('Committed predecessor declaration changed; upgrade is not qualified.')
        root = Path(settings.text('deployment.data_root'))
        if not (root / 'bootstrap/identity.json').is_file(): raise ValueError('Existing managed instance required.')
        identity = read_record(root / 'bootstrap/identity.json')
        if identity['assembly_digest'] not in (self.old.assembly_digest, self.new.assembly_digest):
            raise ValueError('Only the current committed managed predecessor is supported.')
        self.resources = ManagedResources(settings, cast(str, identity['assembly_digest']), retired_for_restore=True)
        self.root = root
        self.intent_path = root / 'bootstrap/communication-upgrade.json'

    def close(self) -> None:
        self.resources.close()

    def seal_database(self, database: Path) -> None:
        """Checkpoint every committed WAL frame while retaining the volume lease.

        A main-file hash is authoritative only after SQLite has recovered and
        closed its journals. Never unlink a journal to make a candidate match.
        Failure leaves the original database and journals available for recovery.
        """
        if self.resources.fd < 0 or self.resources.identity != read_record(self.root / 'bootstrap/identity.json'):
            raise ValueError('Upgrade requires the retained exclusive resource lease.')
        with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=0)) as db:
            db.execute('PRAGMA synchronous=FULL')
            result = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if result is None or result[0] != 0:
                raise ValueError('Upgrade database WAL remains busy.')
        if any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise ValueError('Upgrade database retains journals; no file switch is permitted.')
        with database.open('rb') as stream:
            os.fsync(stream.fileno())
        sync_directory(database.parent)

    @staticmethod
    def metadata(bootstrap: ManagedBootstrap) -> bytes:
        return assembly_value(bootstrap.assembly.repositories, bootstrap.assembly.commands, assembly_format=bootstrap.storage_format)

    def check(self, database: Path, bootstrap: ManagedBootstrap) -> None:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('SELECT database_id,assembly FROM application_metadata WHERE singleton=1').fetchall() != [(self.resources.database_id, self.metadata(bootstrap))]:
                raise ValueError('Exact predecessor or target assembly required.')
            if db.execute("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name").fetchall() != sorted(bootstrap.assembly.storage._schema):
                raise ValueError('Physical schema differs from its declared assembly.')
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('Upgrade database failed integrity checks.')

    def verify_predecessor(self) -> None:
        """Validate retained receipt/history encodings through the native reader."""
        import asyncio
        from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
        from companion_memory.persistence import DatabaseResources, Ready
        async def verify():
            storage = ManagedBootstrap(self.resources.settings, communication_format=False).assembly.storage
            snapshot = bootstrap_snapshot(self.resources.settings, self.resources.protected_directories())
            try:
                result = await storage.initialize(snapshot,
                    DatabaseResources(self.resources.database_id, self.resources.retained_identity_check), 'OPEN_EXISTING')
                if type(result) is not Ready:
                    raise ValueError('The committed predecessor failed native storage recovery.')
            finally:
                await storage.close()
                if storage.get_health().lifecycle != 'CLOSED':
                    raise ValueError('Predecessor verification retains unfinished storage work.')
        asyncio.run(verify())

    def prepare(self, key: str, *, checkpoint: Callable[[str], None] = lambda point: None):
        from companion_memory.persistence.schema import valid_identifier
        if not valid_identifier(key): raise ValueError('Valid original upgrade key required.')
        if self.intent_path.exists():
            retained = read_record(self.intent_path)
            if retained['state'] == 'ABORTED' and retained['operation_key'] != key:
                history = self.root / 'bootstrap/upgrade-history'
                history.mkdir(mode=0o700, exist_ok=True)
                atomic_record(history / (sha256(str(retained['operation_key']).encode()).hexdigest() + '.json'), retained)
                self.intent_path.unlink();sync_directory(self.intent_path.parent)
                return self.prepare(key, checkpoint=checkpoint)
            if retained['operation_key'] != key: raise ValueError('Another original upgrade owns this volume.')
            if retained['state'] != 'PREPARING': return retained
        r = self.resources
        if r.identity['assembly_digest'] != self.old.assembly_digest or r.identity['state'] == 'RETIRED':
            raise ValueError('Source is not the active committed predecessor.')
        database = self.root / 'db/memory.sqlite3'
        self.check(database, self.old)
        self.verify_predecessor()
        # Exact old command encodings remain part of the new assembly. Physical
        # extension may append only: no old table, index, receipt or body rewrite.
        old_schema = dict(self.old.assembly.storage._schema)
        new_schema = dict(self.new.assembly.storage._schema)
        if any(new_schema.get(name) != sql for name, sql in old_schema.items()):
            raise ValueError('This upgrade would change an existing physical schema.')
        from companion_memory.persistence._codec import command_descriptor
        old_commands = {(d.owner_namespace, d.operation_kind): command_descriptor(d) for d in self.old.assembly.commands}
        new_commands = {(d.owner_namespace, d.operation_kind): command_descriptor(d) for d in self.new.assembly.commands}
        if any(new_commands.get(name) != value for name, value in old_commands.items()):
            raise ValueError('An original command or confirmation interpretation changed.')
        uid = 'communication-' + sha256(key.encode()).hexdigest()[:32]
        # Freeze the source before making any backup. Retrying a key must never
        # pair an older backup with a newer source fingerprint.
        with closing(sqlite3.connect(database)) as db:
            if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                raise ValueError('Source WAL remains busy.')
        staging = self.root / 'backup_staging' / (uid + '-upgrade')
        staging.mkdir(mode=0o700, exist_ok=True)
        target = staging / 'prepared.sqlite3'
        preparing: dict[str, object] = {'format': 'MANAGED_COMMUNICATION_UPGRADE_V1', 'operation_key': key,
            'state': 'PREPARING', 'backup_id': uid, 'source_sha256': file_digest(database)[1]}
        if self.intent_path.exists():
            retained = read_record(self.intent_path)
            if retained != preparing: raise ValueError('Source changed during isolated preparation; abort the original preparation explicitly.')
        else: atomic_record(self.intent_path, preparing)
        copier = ConsistentBackup(r, lambda: r.fd >= 0)
        backup = copier.create(uid)
        copier.verify(backup)
        checkpoint('after_backup')
        if target.exists():
            # Never delete an interrupted candidate. Only this original intent
            # may quarantine its private candidate and prepare from its backup.
            from uuid import uuid4
            os.rename(target, staging / ('interrupted-' + uuid4().hex + '.sqlite3'))
            for suffix in ('-wal', '-shm'):
                sidecar = Path(str(target) + suffix)
                if sidecar.exists(): os.rename(sidecar, staging / ('interrupted-' + uuid4().hex + suffix))
            sync_directory(staging)
        shutil.copyfile(backup / 'files/db/memory.sqlite3', target)
        target.chmod(0o600)
        with closing(sqlite3.connect(target)) as db:
            db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            for name, sql in self.new.assembly.storage._schema:
                if name not in old_schema: db.execute(sql)
            db.execute('UPDATE application_metadata SET assembly=? WHERE singleton=1', (self.metadata(self.new),))
            db.commit()
            if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0: raise ValueError('Prepared WAL remains busy.')
        self.check(target, self.new)
        # SQLite closes and checkpoints before fingerprints; no other writer can
        # own the volume lease. Preserve a retained original file at the switch.
        with closing(sqlite3.connect(database)) as db:
            if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0: raise ValueError('Source WAL remains busy.')
        for path in (target, database):
            with path.open('rb') as stream: os.fsync(stream.fileno())
        intent = {'format': 'MANAGED_COMMUNICATION_UPGRADE_V1', 'operation_key': key, 'upgrade_id': uid,
            'state': 'PREPARED', 'old_assembly': self.old.assembly_digest, 'new_assembly': self.new.assembly_digest,
            'database_id': r.database_id, 'instance_id': r.instance_id, 'source_authority': r.identity['authority_id'],
            'source_identity': r.identity, 'source_sha256': file_digest(database)[1], 'target_sha256': file_digest(target)[1],
            'backup_id': uid, 'prepared': target.relative_to(self.root).as_posix(),
            'original': (staging / 'original.sqlite3').relative_to(self.root).as_posix(), 'startup_sends': 0}
        sync_directory(staging)
        atomic_record(self.intent_path, intent)
        checkpoint('after_prepare')
        return intent

    def activate(self, key: str, *, checkpoint: Callable[[str], None] = lambda point: None):
        intent = read_record(self.intent_path)
        if (intent.get('format') != 'MANAGED_COMMUNICATION_UPGRADE_V1' or intent['operation_key'] != key
                or intent['database_id'] != self.resources.database_id or intent['instance_id'] != self.resources.instance_id
                or intent['old_assembly'] != self.old.assembly_digest or intent['new_assembly'] != self.new.assembly_digest):
            raise ValueError('Original upgrade identity or exact build changed.')
        database = self.root / 'db/memory.sqlite3'
        if intent['state'] not in ('PREPARED', 'FENCING', 'ACTIVE'):
            raise ValueError('Complete the original isolated preparation before activation.')
        if intent['state'] == 'ACTIVE':
            self.check(database, self.new)
            return intent
        target = self.root / cast(str, intent['prepared'])
        original = self.root / cast(str, intent['original'])
        old_identity = cast(dict[str, object], intent['source_identity'])
        if intent['state'] == 'PREPARED':
            self.check(database, self.old);self.check(target, self.new)
            self.seal_database(database)
            self.seal_database(target)
            if file_digest(database)[1] != intent['source_sha256'] or file_digest(target)[1] != intent['target_sha256']:
                raise ValueError('Source changed since preparation; upgrade must not discard new work.')
            checkpoint('before_fence')
        # Retire using the predecessor's existing startup rule before changing
        # the database. Old code need not understand the new intent file.
        atomic_record(self.root / 'bootstrap/identity.json', old_identity | {'state': 'RETIRED'})
        self.resources.identity = old_identity | {'state': 'RETIRED'}
        atomic_record(self.intent_path, intent | {'state': 'FENCING'})
        checkpoint('after_fence')
        if not original.exists():
            self.seal_database(database)
            if file_digest(database)[1] != intent['source_sha256']: raise ValueError('Original source differs.')
            os.rename(database, original);sync_directory(database.parent);sync_directory(original.parent)
        if target.exists():
            self.seal_database(target)
            if database.exists() or file_digest(target)[1] != intent['target_sha256']: raise ValueError('Ambiguous target switch.')
            os.rename(target, database);sync_directory(database.parent);sync_directory(target.parent)
        if file_digest(database)[1] != intent['target_sha256'] or file_digest(original)[1] != intent['source_sha256']:
            raise ValueError('Retained switch resources differ.')
        self.check(database, self.new)
        checkpoint('after_database')
        active_identity = old_identity | {'assembly_digest': self.new.assembly_digest}
        atomic_record(self.root / 'bootstrap/identity.json', active_identity)
        self.resources.identity = active_identity
        checkpoint('after_identity')
        active = intent | {'state': 'ACTIVE'}
        atomic_record(self.intent_path, active)
        checkpoint('after_active')
        return active


    def abort(self, key: str):
        """Cancel only an undecided preparation; retain every backup and candidate."""
        intent = read_record(self.intent_path)
        if intent.get('operation_key') != key or intent.get('state') not in ('PREPARING', 'PREPARED', 'ABORTED'):
            raise ValueError('A fenced or committed upgrade cannot be rolled back by abort.')
        if self.resources.identity['assembly_digest'] != self.old.assembly_digest or self.resources.identity['state'] == 'RETIRED':
            raise ValueError('The original source no longer owns startup.')
        aborted = intent | {'state': 'ABORTED'}
        atomic_record(self.intent_path, aborted)
        return aborted
