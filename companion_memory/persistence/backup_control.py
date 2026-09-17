"""Durable original backup requests and verified completion under one owner.

The file copier cannot create an authoritative request or mark a database row
complete. Commands retain revisions, source authority and necessary audit facts;
closing this owner never discards an incomplete request.
"""
from __future__ import annotations
from hashlib import sha256
import time
from typing import cast
from . import (AuditFieldBinding, AuditResultBinding, Field, RecordSchema, ResultBoundCommandDefinition,
    ResultBoundCommand, UnitOfWork, PersistenceService)
from .daily_records import DailyRows, ID, UINT, DIGEST
from .daily_results import FACT, TARGETS, target
from .backup_records import backup_catalog, TABLES
from .owned_statements import OwnerFailure
from companion_memory.logging_service import AuditRequirement


class BackupControl:
    def __init__(self):
        self.catalog = backup_catalog()
        self.lease = None
        commands = []
        for kind, fields in (
            ('request_backup', (Field('source_authority', ID), Field('request_key', ID), Field('request_actor', ID))),
            ('complete_backup', (Field('expected_revision', UINT), Field('manifest_digest', DIGEST),
                Field('total_bytes', UINT), Field('file_count', UINT))),
            ('fail_backup', (Field('expected_revision', UINT), Field('failure', ID))),
            ('record_restore_activation', (Field('restore_id', ID), Field('manifest_digest', DIGEST),
                Field('source_authority', ID), Field('target_authority', ID), Field('target_binding', DIGEST))),
        ):
            requirement = AuditRequirement('backup', 'backup_changed', kind.upper(), 1, ('APPLY',), FACT)
            binding = AuditResultBinding('backup_changed', 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                AuditFieldBinding('change', 'RESULT', ('fact',))))
            def handle(uow, value, operation=kind):
                return self.handle(operation, uow, value)
            commands.append(ResultBoundCommandDefinition('backup', kind, 1, RecordSchema((Field('backup_id', ID),) + fields),
                1, RecordSchema((Field('backup_id', ID), Field('targets', TARGETS), Field('fact', FACT))),
                (self.catalog.definition,), (requirement,), handle, RecordSchema((Field('actor', ID),)), (binding,)))
        self.commands = tuple(commands)

    def bind(self, storage: PersistenceService, database: str, instance: str, authority: str):
        if self.lease is not None:
            raise ValueError('Backup owner is already bound.')
        self.storage, self.database, self.instance, self.authority = storage, database, instance, authority
        self.lease = storage.claim_module_owner(self.catalog.definition)
        if self.lease is None:
            raise ValueError('Backup owner cannot be acquired.')
        self.rows = DailyRows(self.catalog, TABLES, storage, database, instance, 'managed-bootstrap')

    def handle(self, kind: str, uow: UnitOfWork, value):
        if self.lease is None:
            raise OwnerFailure('INVALID_STATE', 'backup', 'NOT_READY')
        if kind == 'record_restore_activation':
            if value['target_authority'] != self.authority or value['source_authority'] == self.authority:
                raise OwnerFailure('PRECONDITION_FAILED', 'restore', 'AUTHORITY_CHANGED')
            old_restore = self.rows.get('restores', uow, value['restore_id'])
            if old_restore is not None:
                raise OwnerFailure('PRECONDITION_FAILED', 'restore', 'REVISION_CONFLICT')
            now = time.time_ns() // 1000
            self.rows.write('restores', uow, {'format_version': 1, 'object_id': value['restore_id'], 'revision': 1,
                'database_id': self.database, 'instance_id': self.instance, 'config_snapshot_id': 'managed-bootstrap',
                'created_at_us': now, 'updated_at_us': now, 'state': 'ACTIVE',
                **{key: value[key] for key in ('backup_id', 'manifest_digest', 'source_authority', 'target_authority', 'target_binding')}})
            targets = (target(value['restore_id'], 1),)
            return {'backup_id': value['backup_id'], 'targets': targets, 'fact': {'rows_changed': 1, 'targets': targets}}
        old = self.rows.get('backups', uow, value['backup_id'])
        now = time.time_ns() // 1000
        if kind == 'request_backup':
            if old is not None or value['source_authority'] != self.authority:
                raise OwnerFailure('PRECONDITION_FAILED', 'backup', 'AUTHORITY_CHANGED')
            record = {'format_version': 1, 'object_id': value['backup_id'], 'revision': 1,
                'database_id': self.database, 'instance_id': self.instance, 'config_snapshot_id': 'managed-bootstrap',
                'created_at_us': now, 'updated_at_us': now, 'source_authority': self.authority,
                'state': 'REQUESTED', 'manifest_digest': None, 'total_bytes': 0, 'file_count': 0, 'failure': None,
                'request_key': value['request_key'], 'request_actor': value['request_actor']}
        else:
            if old is None or old['revision'] != value['expected_revision'] or old['state'] != 'REQUESTED':
                raise OwnerFailure('PRECONDITION_FAILED', 'backup', 'REVISION_CONFLICT')
            outcome = ({'state': 'FAILED', 'failure': value['failure']} if kind == 'fail_backup' else
                {'state': 'COMPLETE', **{key: value[key] for key in ('manifest_digest', 'total_bytes', 'file_count')}})
            record = dict(old) | {'revision': cast(int, old['revision']) + 1,
                'updated_at_us': max(now, cast(int, old['updated_at_us'])), **outcome}
        saved = self.rows.write('backups', uow, record, cast(int, old['revision']) if old is not None else None)
        targets = (target(value['backup_id'], cast(int, saved['revision']), cast(int, old['revision']) if old is not None else None),)
        return {'backup_id': value['backup_id'], 'targets': targets, 'fact': {'rows_changed': 1, 'targets': targets}}

    async def request(self, key: str, actor: str):
        identity = 'backup-' + sha256((self.database + ':' + key).encode()).hexdigest()[:48]
        return await self.execute('request_backup', key, {'backup_id': identity, 'source_authority': self.authority,
            'request_key': key, 'request_actor': actor}, actor)

    async def execute(self, kind: str, key: str, values: dict, actor: str):
        definition = next(d for d in self.commands if d.operation_kind == kind)
        return await self.storage.bind_operation(definition, self.instance).execute(key,
            ResultBoundCommand(1, values, {'backup_changed': {'actor': actor}}))

    def close(self) -> bool:
        if self.lease is None:
            return True
        if self.lease.release():
            self.lease = None
            return True
        return False
