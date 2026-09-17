"""Runtime-owned immutable work-to-configuration references.

These references are created in the original work transaction and retained with
that work. They supplement its birth snapshot rather than rewriting a source,
object, Provider request, receipt or business initialization identity.
"""
from __future__ import annotations
import time
from typing import cast

from companion_memory.configuration.activation_records import RUNTIME_TABLES
from companion_memory.configuration.execution_versions import ExecutionVersions, ExecutionVersion
from companion_memory.persistence.daily_records import DailyRows, identity
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence import UnitOfWork


class ManagedWorkConfiguration:
    def __init__(self, runtime, versions: ExecutionVersions):
        self.runtime, self.versions = runtime, versions
        self.fenced = False
        catalog = next(c for c in runtime.catalogs if c.definition.owner_module == 'runtime')
        stored = runtime.configuration
        self.rows = DailyRows(catalog, RUNTIME_TABLES, runtime.storage, stored.database_id, stored.scope_id, stored.snapshot_id)

    def key(self, kind: str, work_id: str) -> str:
        return identity('work-configuration', self.rows.database, self.rows.instance, kind, work_id)

    def freeze(self, uow: UnitOfWork, kind: str, work_id: str):
        version = self.versions.current
        if self.fenced:
            raise OwnerFailure('RESOURCE_BUSY', 'configuration', 'ACTIVATION_PENDING')
        uow.require_commit_permission(lambda: not self.fenced and version is self.versions.active)
        key = self.key(kind, work_id)
        old = self.rows.get('managed_work_configuration', uow, key)
        if old is not None:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'WORK_ALREADY_FROZEN')
        now = time.time_ns() // 1000
        self.rows.write('managed_work_configuration', uow, {'format_version': 1, 'object_id': key, 'revision': 1,
            'database_id': self.rows.database, 'instance_id': self.rows.instance,
            'config_snapshot_id': self.rows.snapshot, 'created_at_us': now, 'updated_at_us': now,
            'work_kind': kind, 'work_id': work_id, 'version_id': version.version_id})

    async def load(self, kind: str, work_id: str) -> ExecutionVersion:
        row = await self.rows.read('managed_work_configuration', self.key(kind, work_id))
        if row is None or row['work_kind'] != kind or row['work_id'] != work_id:
            raise OwnerFailure('STORAGE_FAILED', 'configuration', 'WORK_VERSION_MISSING')
        return await self.versions.load(cast(str, row['version_id']))

    async def command_version(self, kind: str, values: dict) -> ExecutionVersion:
        if kind.removesuffix('_with_media') == 'select_content_preparation':
            return self.versions.active
        batch = values.get('batch_id')
        preparation = values.get('preparation_id')
        plan = values.get('plan_id')
        if type(preparation) is not str and type(plan) is str and kind.startswith('dispose_preparation'):
            plans = await self.runtime.rows.read('disposal_get', {'plan_id': plan})
            if plans:
                preparation = plans[0]['preparation_id']
        if type(batch) is not str and type(preparation) is str:
            rows = await self.runtime.rows.read('preparations_get', {'preparation_id': preparation})
            if rows:
                batch = rows[0]['batch_id']
        if type(batch) is str:
            return await self.load('BATCH', batch)
        return self.versions.current
