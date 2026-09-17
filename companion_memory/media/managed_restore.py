"""Audited native media relocation, retaining logical generations and receipts."""
from __future__ import annotations
import asyncio
from typing import cast, TYPE_CHECKING

from companion_memory.persistence import Field, RecordSchema, ResultBoundCommandDefinition, ResultBoundCommand, Committed, UnitOfWork
from companion_memory.persistence.daily_records import ID, UINT, identity
from companion_memory.persistence.daily_results import audits, FACT, TARGETS, INTENT, target
from companion_memory.persistence.owned_statements import OwnerFailure
from .restore_resources import MediaRestoreResources
if TYPE_CHECKING:
    from .service import MediaService


class ManagedMediaRestore:
    def __init__(self, owner: MediaService):
        self.owner = owner
        self.pending: tuple[str, dict] | None = None
        self.resources: MediaRestoreResources | None = None
        commands = []
        for table in ('roots', 'uploads', 'blobs'):
            required, bindings = audits('restore_media_' + table, ('media',))
            names = ('device', 'inode', 'staging_device', 'staging_inode', 'published_device', 'published_inode') if table == 'roots' else (
                ('staging_device', 'staging_inode') if table == 'uploads' else ('device', 'inode'))
            def handle(uow, values, kind=table):
                return self.handle(kind, uow, values)
            commands.append(ResultBoundCommandDefinition('media', 'restore_media_' + table, 1,
                RecordSchema((Field('object_id', ID), Field('switch_id', ID)) + tuple(Field(prefix + name, UINT)
                    for prefix in ('old_', 'new_') for name in names)), 1,
                RecordSchema((Field('targets', TARGETS), Field('facts', RecordSchema((Field('media', FACT),))))),
                owner.repositories, required, handle, INTENT, bindings))
        self.commands = tuple(commands)

    def handle(self, table: str, uow: UnitOfWork, values):
        owner = self.owner
        if (not owner._bound or owner._ready or owner._closing or self.resources is None
                or self.resources.resources.fd < 0 or self.pending != (table, dict(values))):
            raise OwnerFailure('ACCESS_DENIED', 'restore', 'BINDING_MISMATCH')
        key = {'roots': 'root_id', 'uploads': 'upload_id', 'blobs': 'blob_id'}[table]
        old = owner._get(table, uow, key, values['object_id'])
        original = {name[4:]: value for name, value in values.items() if name.startswith('old_')}
        if any(old[name] != value for name, value in original.items()):
            raise OwnerFailure('PRECONDITION_FAILED', 'restore', 'RESOURCE_IDENTITY_MISMATCH')
        changed = dict(old) | {name[4:]: value for name, value in values.items() if name.startswith('new_')}
        owner.rows.stage(table + '_update', uow, changed)
        revision = 1 if table == 'roots' else cast(int, old['writer_generation' if table == 'uploads' else 'generation'])
        # Media's existing physical-resource audit uses the retained generation;
        # remapping neither creates a new logical blob nor changes references.
        targets = (target(values['object_id'], revision),)
        return {'targets': targets, 'facts': {'media': {'rows_changed': 1, 'targets': targets}}}

    async def apply(self, resources: MediaRestoreResources, root_id: str) -> None:
        if type(resources) is not MediaRestoreResources or self.owner._ready or self.pending is not None:
            raise ValueError('Native unopened media restoration is required.')
        self.resources = resources
        owner = self.owner

        async def update(table: str, object_id: str, paths: dict):
            values: dict = {'object_id': object_id, 'switch_id': resources.switch_id}
            for prefix, path in paths.items():
                binding = resources.binding(path)
                if binding is None:
                    raise ValueError('Required media resource is absent from the verified restore.')
                values.update({outer + prefix + field: value for outer, pair in (('old_', binding[:2]), ('new_', binding[2:]))
                    for field, value in zip(('device', 'inode'), pair, strict=True)})
            self.pending = (table, values)
            try:
                definition = next(command for command in self.commands if command.operation_kind == 'restore_media_' + table)
                result = await owner.storage.bind_operation(definition, owner.instance_id).execute(
                    identity('restore-media', resources.switch_id, table, object_id),
                    ResultBoundCommand(1, values, {a.event_slot: {'actor': 'managed_restore'} for a in definition.required_audits}))
                if type(result) is not Committed:
                    raise OwnerFailure('STORAGE_FAILED', 'restore', 'ORIGINAL_RESULT_UNCONFIRMED', True)
            finally:
                self.pending = None

        await update('roots', root_id, {'': owner.root, 'staging_': owner.staging, 'published_': owner.published})
        for table, query, key in (('uploads', 'upload_page', 'upload_id'), ('blobs', 'recovery_page', 'blob_id')):
            after = ''
            while True:
                rows = await owner.rows.read(query, {'after': after, 'limit': owner.settings.integer('media.gc_page_size')})
                if not rows:
                    break
                for row in rows:
                    object_id = cast(str, row[key])
                    path = owner.staging / object_id if table == 'uploads' else owner.published / (object_id + '.' + str(row['generation']))
                    if resources.binding(path) is not None:
                        await update(table, object_id, {('staging_' if table == 'uploads' else ''): path})
                    elif table == 'blobs' and row['state'] == 'READY' or table == 'uploads' and row['state'] == 'SEALED':
                        raise ValueError('A retained media publication is missing from the restore.')
                after = cast(str, rows[-1][key])
        await asyncio.to_thread(resources.complete)
