"""Cognition-owned immutable configuration references for retained request material.

The reference is committed with the first material page or atomic material root.
It remains after body retirement, so old Provider requests can still be checked
against the complete original configuration without rewriting their birth ID.
"""
import time
from typing import cast
from companion_memory.configuration.execution_versions import ExecutionVersions, ExecutionVersion
from companion_memory.persistence import Field, RecordSchema, UnitOfWork
from companion_memory.persistence.daily_records import BASE, DailyTable, DailyRows, ID
from companion_memory.persistence.daily_results import target
from companion_memory.persistence.owned_statements import OwnerFailure

TABLE = DailyTable('material_configuration', (RecordSchema(BASE + (
    Field('context_id', ID), Field('owner_ref', ID), Field('version_id', ID))),), 4096, False)


class ManagedMaterialVersions:
    def __init__(self, rows: DailyRows, versions: ExecutionVersions):
        self.rows, self.versions = rows, versions

    def stage(self, uow: UnitOfWork, context_id: str, owner_ref: str):
        version = self.versions.current
        old = self.rows.get(TABLE.name, uow, context_id)
        if old is not None:
            if old['owner_ref'] != owner_ref or old['version_id'] != version.version_id:
                raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'BINDING_MISMATCH')
            return None
        now = time.time_ns() // 1000
        self.rows.write(TABLE.name, uow, {'format_version': 1, 'object_id': context_id, 'revision': 1,
            'database_id': self.rows.database, 'instance_id': self.rows.instance, 'config_snapshot_id': self.rows.snapshot,
            'created_at_us': now, 'updated_at_us': now, 'context_id': context_id, 'owner_ref': owner_ref,
            'version_id': version.version_id})
        return target(context_id, 1)

    async def existing(self, context_id: str, owner_ref: str) -> ExecutionVersion | None:
        row = await self.rows.read(TABLE.name, context_id)
        if row is None:
            return None
        if row['context_id'] != context_id or row['owner_ref'] != owner_ref:
            raise OwnerFailure('STORAGE_FAILED', 'configuration', 'BINDING_MISMATCH')
        return await self.versions.load(cast(str, row['version_id']))

    async def required(self, context_id: str, owner_ref: str) -> ExecutionVersion:
        version = await self.existing(context_id, owner_ref)
        if version is None:
            raise OwnerFailure('STORAGE_FAILED', 'configuration', 'WORK_VERSION_MISSING')
        return version
