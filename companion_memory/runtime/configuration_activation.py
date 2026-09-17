"""Runtime-owned durable consumer acknowledgements for configuration decisions.

The configuration transaction may enlist this participant only while the real
runtime owner holds its lease. An acknowledgement describes a consumer that the
runtime has actually prepared and published; it never grants model permission.
"""
from __future__ import annotations
import time
from typing import cast

from companion_memory.configuration.activation_records import RUNTIME_TABLES, CONSUMERS
from companion_memory.persistence.daily_records import DailyRows, Record, identity
from companion_memory.persistence.daily_results import target
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence import UnitOfWork

class RuntimeConfigurationActivation:
    def __init__(self, catalog):
        self.catalog = catalog
        self.runtime = None
        self.admit = None

    def bind(self, runtime, admit):
        if self.runtime is not None or not runtime._bound or runtime._lease is None:
            raise ValueError('The actual runtime owner is required.')
        self.runtime, self.admit = runtime, admit
        stored = runtime.configuration
        self.rows = DailyRows(self.catalog, RUNTIME_TABLES, runtime.storage,
            stored.database_id, stored.scope_id, stored.snapshot_id)

    def check(self, uow: UnitOfWork):
        if self.runtime is None or self.admit is None or self.runtime._lease is None:
            raise OwnerFailure('INVALID_STATE', 'activation', 'NOT_READY')
        self.admit(uow)

    def row_id(self, activation: str, consumer: str) -> str:
        return identity('configuration-consumer', self.rows.database, self.rows.instance, activation, consumer)

    def decide(self, uow: UnitOfWork, activation: str, version: str):
        self.check(uow)
        now = time.time_ns() // 1000
        targets = []
        for consumer in CONSUMERS:
            object_id = self.row_id(activation, consumer)
            row = {'format_version': 1, 'object_id': object_id, 'revision': 1,
                'database_id': self.rows.database, 'instance_id': self.rows.instance,
                'config_snapshot_id': self.rows.snapshot, 'created_at_us': now, 'updated_at_us': now,
                'activation_id': activation, 'version_id': version, 'consumer': consumer,
                'state': 'PENDING', 'failure': None}
            self.rows.write('managed_consumers', uow, row)
            targets.append(target(object_id, 1))
        return {'rows_changed': len(targets), 'targets': tuple(targets)}

    def acknowledge(self, uow: UnitOfWork, activation: str, consumer: str, *, failure: str | None):
        self.check(uow)
        old = self.rows.get('managed_consumers', uow, self.row_id(activation, consumer))
        if old is None or old['state'] == 'BOUND':
            raise OwnerFailure('PRECONDITION_FAILED', 'activation', 'CONSUMER_CHANGED')
        revision = cast(int, old['revision'])
        self.rows.write('managed_consumers', uow, dict(old) | {'revision': revision + 1,
            'updated_at_us': max(time.time_ns() // 1000, cast(int, old['updated_at_us'])),
            'state': 'BOUND' if failure is None else 'FAILED', 'failure': failure}, revision)
        return {'rows_changed': 1, 'targets': (target(cast(str, old['object_id']), revision + 1, revision),)}

    def all_bound(self, uow: UnitOfWork, activation: str) -> bool:
        self.check(uow)
        return all((row := self.rows.get('managed_consumers', uow, self.row_id(activation, consumer))) is not None
            and row['state'] == 'BOUND' for consumer in CONSUMERS)

    async def status(self, activation: str) -> tuple[Record, ...]:
        values = []
        for consumer in CONSUMERS:
            row = await self.rows.read('managed_consumers', self.row_id(activation, consumer))
            if row is not None:
                values.append(row)
        return tuple(values)
