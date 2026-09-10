"""Two module-owned counters, exact audit intentions and disposable SQLite files.

The fixture writes a synthetic identity record before any database creation. Its
typed transfer handler validates revisions, mutates only each bound repository,
then stages both compulsory audit slots. Test-only hooks model explicit failures.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import unittest

from companion_memory.configuration import MetadataValue
from companion_memory.logging_service import AuditAccess, AuditBound, AuditErr, AuditRequirement, bind_audit
from companion_memory.persistence import (
    CommandDefinition, DatabaseResources, Field, LocalCommand, PersistenceService,
    Ready, RecordSchema, RepositoryDefinition, ScalarSchema, Staged, StatementDefinition,
    TableDefinition, UnitOfWork, Value,
)
from tests.configuration.persistence_support import persistence_snapshot

DATABASE_ID = "synthetic-database"
SCOPE = "sample_scope"
INTEGER = ScalarSchema("integer")
IDENTIFIER = ScalarSchema("identifier")
ROW_SCHEMA = RecordSchema((Field("units", INTEGER), Field("revision", INTEGER)))
CHANGE_SCHEMA = RecordSchema((Field("old_value", INTEGER), Field("new_value", INTEGER), Field("quantity", INTEGER)))


class Hooks:
    """Controlled test-only injection points around real SQLite calls and closes."""

    def __init__(self):
        self.before: Callable[[str], None] = lambda sql: None
        self.after: Callable[[str], None] = lambda sql: None
        self.before_close: Callable[[], None] = lambda: None

    def connect(self, database: str, **kwargs) -> sqlite3.Connection:
        hooks = self

        class HookConnection(sqlite3.Connection):
            def execute(self, sql: str, parameters=(), /):
                hooks.before(sql)
                cursor = super().execute(sql, parameters)
                hooks.after(sql)
                return cursor

            def close(self) -> None:
                hooks.before_close()
                super().close()

        return sqlite3.connect(database, factory=HookConnection, **kwargs)


def sqlite_fault(code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("Synthetic private storage failure.")
    error.sqlite_errorcode = code
    return error


class Fixture:
    """All persistent resources are under a caller-owned disposable directory."""

    def __init__(self, directory: Path, *, changes: dict[str, MetadataValue] | None = None, identity: str = DATABASE_ID):
        self.directory = directory.resolve()
        self.path = self.directory / "application.sqlite3"
        self.retained = self.directory / "retained-identity.json"
        if not self.retained.exists():
            self.retained.write_text(json.dumps({"identity": identity, "path": str(self.path)}))
        self.snapshot = persistence_snapshot(str(self.path), changes)
        self.hooks = Hooks()
        self.resources = DatabaseResources(identity, self.retention_check, connect=self.hooks.connect)
        self.source = self.repository("source")
        self.target = self.repository("target")
        self.source_audit = AuditRequirement("source", "source_changed", "COUNTER_CHANGED", 1, ("TRANSFER",), CHANGE_SCHEMA)
        self.target_audit = AuditRequirement("target", "target_changed", "COUNTER_CHANGED", 1, ("TRANSFER",), CHANGE_SCHEMA)
        self.local_hook: Callable[[str, UnitOfWork], None] = lambda point, uow: None
        self.skip_audit: str | None = None
        self.handler_calls = 0
        self.events: dict[str, object] = {}
        self.command_definition = CommandDefinition(
            "transfer", "transfer_units", 1,
            RecordSchema((Field("source_id", IDENTIFIER), Field("target_id", IDENTIFIER),
                          Field("quantity", ScalarSchema("integer", 1, 100)), Field("source_revision", INTEGER),
                          Field("target_revision", INTEGER), Field("policy", ScalarSchema("enum", choices=("STRICT",))))),
            1, RecordSchema((Field("source_units", INTEGER), Field("target_units", INTEGER),
                             Field("source_revision", INTEGER), Field("target_revision", INTEGER),
                             Field("status", ScalarSchema("enum", choices=("TRANSFERRED",))))),
            (self.source, self.target), (self.source_audit, self.target_audit), self.handler,
        )
        self.service = PersistenceService((self.source, self.target), (self.command_definition,))
        self.operation = self.service.bind_operation(self.command_definition, SCOPE)
        self.source_write = self.service.bind_statement(self.source, self.source.statements[0], SCOPE)
        self.target_write = self.service.bind_statement(self.target, self.target.statements[0], SCOPE)
        self.source_read = self.service.bind_statement(self.source, self.source.statements[1], SCOPE)
        self.target_read = self.service.bind_statement(self.target, self.target.statements[1], SCOPE)
        self.audits: dict[str, AuditAccess] = {}
        self.reader: AuditAccess | None = None

    def retention_check(self, identity: str, path: str) -> bool:
        return json.loads(self.retained.read_text()) == {"identity": identity, "path": path}

    @staticmethod
    def repository(owner: str) -> RepositoryDefinition:
        table = owner + "_counts"
        update = StatementDefinition(
            "UPDATE " + table + " SET units=units+:delta, revision=revision+1 WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:expected_revision RETURNING units, revision",
            RecordSchema((Field("object_id", IDENTIFIER), Field("delta", ScalarSchema("integer", -100, 100)), Field("expected_revision", INTEGER))),
            ROW_SCHEMA, True,
        )
        read = StatementDefinition("SELECT units, revision FROM " + table + " WHERE scope_id=:scope_id AND object_id=:object_id",
                                   RecordSchema((Field("object_id", IDENTIFIER),)), ROW_SCHEMA, False)
        return RepositoryDefinition(owner, 1, (TableDefinition(table,
            "CREATE TABLE " + table + " (scope_id TEXT NOT NULL, object_id TEXT NOT NULL, units INTEGER NOT NULL CHECK(units>=0), revision INTEGER NOT NULL CHECK(revision>=0), PRIMARY KEY(scope_id, object_id))"),), (update, read))

    async def initialize(self, mode: str = "CREATE_NEW"):
        result = await self.service.initialize(self.snapshot, self.resources, mode)
        if type(result) is Ready:
            for requirement in (self.source_audit, self.target_audit):
                binding = bind_audit(self.snapshot, self.service.bind_audit_writer(requirement, SCOPE))
                assert type(binding) is AuditBound
                self.audits[requirement.event_slot] = binding.value
            reader = bind_audit(self.snapshot, self.service.bind_audit_reader(SCOPE))
            assert type(reader) is AuditBound
            self.reader = reader.value
        return result

    def seed(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO source_counts VALUES(?, 'source-counter', 10, 0)", (SCOPE,))
            connection.execute("INSERT INTO target_counts VALUES(?, 'target-counter', 0, 0)", (SCOPE,))

    def command(self, quantity: int = 3, source_revision: int = 0, target_revision: int = 0,
                source_units: int = 10, target_units: int = 0) -> LocalCommand:
        values = {"source_id": "source-counter", "target_id": "target-counter", "quantity": quantity,
                  "source_revision": source_revision, "target_revision": target_revision, "policy": "STRICT"}
        events: dict[str, object] = {}
        for owner, old, new, revision in (("source", source_units, source_units - quantity, source_revision),
                                           ("target", target_units, target_units + quantity, target_revision)):
            events[owner + "_changed"] = {
                "event_version": 1, "actor_kind": "SYSTEM", "actor_ref": "synthetic-scheduler", "reason_code": "TRANSFER",
                "target_refs": [{"object_id": owner + "-counter", "previous_revision": revision, "revision": revision + 1}],
                "change": {"old_value": old, "new_value": new, "quantity": quantity},
            }
        self.events = events
        return LocalCommand(1, values, events)

    def handler(self, uow: UnitOfWork, values: MappingProxyType[str, Value]) -> object:
        self.handler_calls += 1
        quantity = cast(int, values["quantity"])
        changed = []
        for owner, port, delta in (("source", self.source_write, -quantity), ("target", self.target_write, quantity)):
            result = port.participate(uow, {"object_id": values[owner + "_id"], "delta": delta,
                                            "expected_revision": values[owner + "_revision"]})
            if type(result) is not Staged or type(result.value) is not tuple or len(result.value) != 1:
                raise ValueError("A synthetic counter revision did not match.")
            row = cast(Mapping[str, int], result.value[0])
            changed.append(row)
            self.local_hook("after_" + owner, uow)
        for slot, access in self.audits.items():
            if self.skip_audit != slot:
                result = access.append_audit(uow, self.events[slot])
                if type(result) is AuditErr:
                    raise ValueError("A required synthetic audit was rejected.")
        self.local_hook("after_audits", uow)
        return {"source_units": changed[0]["units"], "target_units": changed[1]["units"],
                "source_revision": changed[0]["revision"], "target_revision": changed[1]["revision"], "status": "TRANSFERRED"}

    def raw_counts(self) -> tuple[int, int, int, int]:
        with closing(sqlite3.connect(self.path)) as connection:
            return (
                connection.execute("SELECT units FROM source_counts").fetchone()[0],
                connection.execute("SELECT units FROM target_counts").fetchone()[0],
                connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0],
                connection.execute("SELECT count(*) FROM audit_records").fetchone()[0],
            )


class PersistenceTestCase(unittest.IsolatedAsyncioTestCase):
    """Own temporary database lifetimes and always close each service fixture."""

    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(prefix="iris-persistence-")
        self.directory = Path(self.temporary.name).resolve()
        self.fixture = Fixture(self.directory)
        self.assertIs(type(await self.fixture.initialize()), Ready)
        self.fixture.seed()

    async def asyncTearDown(self):
        self.fixture.hooks.before = lambda sql: None
        self.fixture.hooks.after = lambda sql: None
        self.fixture.hooks.before_close = lambda: None
        await self.fixture.service.close()
        self.temporary.cleanup()
