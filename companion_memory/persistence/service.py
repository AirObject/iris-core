"""Bounded asynchronous coordination of one local SQLite database.

Trusted assembly owns lifecycle, schema and capability issuance. Each synchronous
SQLite connection has one worker owner; async caller deadlines do not cancel or
reuse that connection. Only this coordinator commits, after receipt and required
audits are complete. Every participant failure poisons the entire transaction.
Unknown completion faults the service and requires explicit result confirmation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import threading
from types import MappingProxyType
from typing import Literal, cast

from companion_memory.configuration import EffectiveSnapshot
from companion_memory.logging_service.audit_records import (
    AuditErr, AuditRecord, AuditRequirement, AuditStaged,
    audit_manifest, audit_record_value, create_audit_record, freeze_audit_event, validate_audit_record,
)
from ._codec import (
    assembly_value, decode_audit, decode_receipt, prepare_command,
    receipt_value, valid_identity,
)
from ._settings import Settings, read_settings
from .definitions import CommandDefinition, LocalCommand, RepositoryDefinition, StatementDefinition
from .resources import DatabaseResources
from .results import (
    CloseReport, Committed, ErrorCode, ErrorField, ExecutionResult, Failed, Found,
    Health, InitializationResult, InitializationUnconfirmed, Lifecycle, NotCommitted,
    NotFound, Operation, OperationIdentity, PersistenceError, ReadResult, Ready,
    Reason, Receipt, RecoveryHandle, Rejected, Staged, Unconfirmed,
)
from .schema import InvalidValue, Value, ValueTooLarge, encode_value, freeze_value, utc_text, valid_identifier

_APPLICATION_ID = 0x49524953
_FORMAT_VERSION = 1
_BASE_SCHEMA = (
    ("application_metadata", "CREATE TABLE application_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), database_id TEXT NOT NULL, format_version INTEGER NOT NULL, assembly BLOB NOT NULL)"),
    ("operation_receipts", "CREATE TABLE operation_receipts (owner_namespace TEXT NOT NULL, operation_kind TEXT NOT NULL, scope_id TEXT NOT NULL, operation_key TEXT NOT NULL, commit_id TEXT NOT NULL UNIQUE, receipt BLOB NOT NULL, PRIMARY KEY(owner_namespace, operation_kind, scope_id, operation_key))"),
    ("required_audit_events", "CREATE TABLE required_audit_events (commit_id TEXT PRIMARY KEY REFERENCES operation_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED, manifest BLOB NOT NULL)"),
    ("audit_records", "CREATE TABLE audit_records (commit_id TEXT NOT NULL REFERENCES operation_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED, event_slot TEXT NOT NULL, audit_id TEXT NOT NULL UNIQUE, record BLOB NOT NULL, PRIMARY KEY(commit_id, event_slot))"),
)


class _StorageFault(Exception):
    """Internal transport for one sanitized first failure, without an SQL payload."""

    def __init__(self, error: PersistenceError):
        self.error = error


class _Job:
    """One owned bounded slot, surviving caller timeout until resource completion."""

    def __init__(self, operation: Operation, deadline: float):
        self.operation: Operation = operation
        self.deadline = deadline
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.phase = "pending"
        self.owner: int | None = None
        self.result: object = None
        self.first_error: PersistenceError | None = None
        self.handle: RecoveryHandle | None = None
        self.connection: sqlite3.Connection | None = None
        self.uow: UnitOfWork | None = None
        self.absence_checked = False
        self.rollback_confirmed = False
        self.commit_confirmed = False
        self.cleanup_pending = False
        self.lock_spent = 0.0
        self.coordinator: AuditStorageBinding | None = None
        self.evidence: Committed | None = None
        self.application_identified = False
        self.target_bound = False


class UnitOfWork:
    """Opaque temporary participant authority; has no commit, SQL or connection API."""

    __slots__ = ("_service", "_job", "_definition", "_identity", "_commit_id", "_events", "_active", "_changed")

    def __new__(cls):
        raise TypeError("Unit-of-work authority is issued only by storage.")

    @classmethod
    def _create(cls, service: PersistenceService, job: _Job, definition: CommandDefinition,
                identity: OperationIdentity, commit_id: str, events: MappingProxyType[str, Value]) -> UnitOfWork:
        self = object.__new__(cls)
        self._service, self._job, self._definition = service, job, definition
        self._identity, self._commit_id, self._events = identity, commit_id, events
        self._active, self._changed = True, set()
        return self


class _SealedPort:
    """Prevent supported callers from mutating an already-bound capability field."""

    __slots__ = ()
    _service: PersistenceService

    def _service_issue(self, operation: Operation) -> PersistenceError | None:
        try:
            service = object.__getattribute__(self, "_service")
        except AttributeError:
            service = None
        if type(service) is not PersistenceService:
            return PersistenceError("ACCESS_DENIED", operation, "operation", "CAPABILITY_MISMATCH")
        return None

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("A bound capability is immutable.")
        object.__setattr__(self, name, value)


class OperationPort(_SealedPort):
    """Scope-bound complete command/recovery access without lifecycle authority."""

    __slots__ = ("_service", "_definition", "_scope")

    def __init__(self, service: PersistenceService, definition: CommandDefinition, scope: str):
        self._service, self._definition, self._scope = service, definition, scope

    async def execute(self, operation_key: object, local_command: object) -> ExecutionResult:
        """Execute once or recover an identical original receipt; never auto-replay.

        Keep the key and full command before calling. Only Committed confirms
        effects; an Unconfirmed result requires resolve_operation. Waiting is
        bounded even when the owned SQLite call remains blocked underneath.
        """
        if (issue := self._service_issue("execute")) is not None:
            return Rejected(issue)
        return await self._service._execute(self, operation_key, local_command)

    def recovery_handle(self, operation_key: object, local_command: object) -> RecoveryHandle | Rejected:
        """Reconstruct confirmation input from retained original input without I/O."""
        if (issue := self._service_issue("resolve_operation")) is not None:
            return Rejected(issue)
        prepared = self._service._prepare(self, operation_key, local_command, "resolve_operation")
        return prepared if isinstance(prepared, Rejected) else prepared[0]

    async def read_receipt(self, operation_key: object) -> ReadResult[Receipt]:
        """Read one validated receipt. NotFound is a snapshot observation only."""
        if (issue := self._service_issue("read_receipt")) is not None:
            return Failed(issue)
        return cast(ReadResult[Receipt], await self._service._read(self, operation_key, "read_receipt"))

    async def resolve_operation(self, recovery_handle: object) -> ExecutionResult:
        """Confirm a retained operation without invoking its business handler."""
        if (issue := self._service_issue("resolve_operation")) is not None:
            return Rejected(issue)
        return await self._service._resolve(self, recovery_handle)


class StatementPort(_SealedPort):
    """One statically bound typed module statement, with enforced scope and owner."""

    __slots__ = ("_service", "_repository", "_definition", "_scope")

    def __init__(self, service: PersistenceService, repository: RepositoryDefinition,
                 definition: StatementDefinition, scope: str):
        self._service, self._repository, self._definition, self._scope = service, repository, definition, scope

    def participate(self, uow: object, parameters: object) -> Staged[Value] | Failed:
        """Stage this module's typed work; every refusal poisons its active UoW."""
        if (issue := self._service_issue("participate")) is not None:
            return Failed(issue)
        return self._service._participate(self, uow, parameters)

    async def read_object(self, parameters: object) -> ReadResult[Value]:
        """Read a fixed typed query from one short committed snapshot, with no cursor."""
        if (issue := self._service_issue("read_object")) is not None:
            return Failed(issue)
        return cast(ReadResult[Value], await self._service._read(self, parameters, "read_object"))


class AuditStorageBinding(_SealedPort):
    """Restricted storage mechanisms issued to logging, never to ordinary modules.

    Write bindings contain one mandatory event capability; reader bindings are
    issued only by trusted developer-authorized assembly. A coordinator binding
    can verify the complete manifest but cannot run arbitrary module SQL.
    """

    __slots__ = ("_service", "_scope", "_requirement", "_reader", "_coordinator")

    def __init__(self, service: PersistenceService, scope: str, requirement: AuditRequirement | None,
                 *, reader: bool = False, coordinator: bool = False):
        self._service, self._scope, self._requirement = service, scope, requirement
        self._reader, self._coordinator = reader, coordinator

    def state_valid(self) -> bool:
        return self.is_issued() and self._service.get_health().lifecycle == "READY"

    def is_issued(self) -> bool:
        if type(self) is not AuditStorageBinding:
            return False
        try:
            service = object.__getattribute__(self, "_service")
            return (type(service) is PersistenceService and (service._ports.get(id(self)) is self
                    or (service._writer is not None and service._writer.coordinator is self)))
        except AttributeError:
            return False

    def uow_ended(self, uow: object) -> bool:
        """Recognize only an original issued UoW whose lifetime already ended."""
        if type(uow) is not UnitOfWork:
            return False
        try:
            job = object.__getattribute__(uow, "_job")
        except AttributeError:
            return False
        return (type(job) is _Job and job.uow is uow and uow._service is self._service and not uow._active)

    def matches_snapshot(self, snapshot: object) -> bool:
        return snapshot is self._service._snapshot

    def requirement(self) -> AuditRequirement | None:
        return self._requirement

    def permits(self, uow: object, *, coordinator: bool = False) -> bool:
        return (self.is_issued() and (self._coordinator if coordinator else self._requirement is not None)
                and self._service._valid_uow(uow, self._scope)
                and (coordinator or any(item is self._requirement for item in cast(UnitOfWork, uow)._definition.required_audits)))

    def permits_read(self) -> bool:
        return self.is_issued() and self._reader

    def poison(self, error: PersistenceError) -> None:
        self._service._poison(error)

    def stage(self, uow: UnitOfWork, event: MappingProxyType[str, Value]) -> AuditStaged | Failed:
        return self._service._stage_audit(self, uow, event)

    def required(self, uow: UnitOfWork) -> tuple[tuple[AuditRequirement, ...], tuple[AuditRecord, ...]] | Failed:
        return self._service._required_audits(self, uow)

    async def read(self, identity: object) -> Found[tuple[AuditRecord, ...]] | NotFound | Failed:
        return cast(Found[tuple[AuditRecord, ...]] | NotFound | Failed,
                    await self._service._read(self, identity, "read_receipt", audit=True))


class PersistenceService:
    """Trusted lifecycle owner; construction performs no filesystem or database I/O.

    Binding methods belong only to trusted startup assembly. It hands application
    callers restricted ports, after independently authenticating developer audit
    access. No production path, default tuning, identity store or migration is
    inferred. Reopening requires a new instance and the retained expected ID.
    """

    def __init__(self, repositories: tuple[RepositoryDefinition, ...], commands: tuple[CommandDefinition, ...]):
        self._repositories, self._commands = repositories, commands
        self._command_map = {(item.owner_namespace, item.operation_kind): item for item in commands}
        self._assembly = assembly_value(repositories, commands)
        self._schema = _BASE_SCHEMA + tuple((table.name, table.sql.strip().rstrip(";")) for repo in repositories for table in repo.tables)
        if (len({name for name, _ in self._schema}) != len(self._schema)
                or len(self._command_map) != len(commands)
                or len({item.owner_module for item in repositories}) != len(repositories)
                or any(not valid_identifier(item.owner_module) for item in repositories)
                or any(not valid_identifier(item.owner_namespace) or not valid_identifier(item.operation_kind)
                       or type(item.command_version) is not int or item.command_version < 1
                       or any(not any(repo is known for known in repositories) for repo in item.participants)
                       or len({event.event_slot for event in item.required_audits}) != len(item.required_audits)
                       or any(not valid_identifier(event.event_slot) or event.owner_module not in {repo.owner_module for repo in item.participants}
                              for event in item.required_audits) for item in commands)):
            raise ValueError("Trusted storage assembly is inconsistent.")
        self._lock = threading.RLock()
        self._lifecycle: Lifecycle = "NEW"
        self._reason: Reason | Literal["NONE"] = "NONE"
        self._resources: DatabaseResources | None = None
        self._settings: Settings | None = None
        self._snapshot: EffectiveSnapshot | None = None
        self._writer: _Job | None = None
        self._reads: set[_Job] = set()
        self._connections: set[sqlite3.Connection] = set()
        self._owner_fd: int | None = None
        self._file_identity: tuple[int, int] | None = None
        self._unresolved: set[OperationIdentity] = set()
        self._checkpoint_pending = False
        self._close_report: CloseReport | None = None
        self._close_job: _Job | None = None
        self._ports: dict[int, object] = {}

    def get_health(self) -> Health:
        """Observe only current in-memory ownership; never perform storage I/O."""
        with self._lock:
            pending = ((self._lifecycle in ("FAULTED", "CLOSING") and bool(self._writer or self._reads or self._connections or self._owner_fd is not None))
                       or any(job.cleanup_pending for job in (*self._reads, *((self._writer,) if self._writer else ()))))
            return Health(self._lifecycle, self._reason, int(self._writer is not None), len(self._reads),
                          pending, self._checkpoint_pending, len(self._unresolved))

    def bind_operation(self, definition: CommandDefinition, scope_id: str) -> OperationPort:
        """Issue a scope-bound port from an exact statically registered definition."""
        if not any(definition is item for item in self._commands) or not valid_identifier(scope_id):
            raise ValueError("A registered command and explicit valid scope are required.")
        port = OperationPort(self, definition, scope_id)
        self._ports[id(port)] = port
        return port

    def bind_statement(self, repository: RepositoryDefinition, definition: StatementDefinition, scope_id: str) -> StatementPort:
        """Issue one module statement capability; callers cannot choose SQL text."""
        if (not any(repository is item for item in self._repositories)
                or not any(definition is item for item in repository.statements) or not valid_identifier(scope_id)):
            raise ValueError("A registered module statement and explicit scope are required.")
        port = StatementPort(self, repository, definition, scope_id)
        self._ports[id(port)] = port
        return port

    def bind_audit_writer(self, requirement: AuditRequirement, scope_id: str) -> AuditStorageBinding:
        """Give a module only its registered mandatory audit-slot write capability."""
        if (not any(requirement is event for item in self._commands for event in item.required_audits)
                or not valid_identifier(scope_id)):
            raise ValueError("A registered event and explicit scope are required.")
        port = AuditStorageBinding(self, scope_id, requirement)
        self._ports[id(port)] = port
        return port

    def bind_audit_reader(self, scope_id: str) -> AuditStorageBinding:
        """Trusted assembly grants already-authorized developer access for one scope."""
        if not valid_identifier(scope_id):
            raise ValueError("An explicit valid audit-read scope is required.")
        port = AuditStorageBinding(self, scope_id, None, reader=True)
        self._ports[id(port)] = port
        return port

    def _error(self, operation: Operation, code: ErrorCode, reason: Reason,
               field: ErrorField = "resources", *, pending: bool = False) -> PersistenceError:
        return PersistenceError(code, operation, field, reason, pending)

    def _state_error(self, operation: Operation) -> PersistenceError | None:
        state = self._lifecycle
        if state in ("CLOSING", "CLOSED"):
            reason = "SERVICE_CLOSED"
        elif operation == "initialize":
            if state == "NEW":
                return None
            reason = "ALREADY_INITIALIZED" if state == "READY" else "SERVICE_FAULTED"
        elif state == "NEW":
            reason = "NOT_INITIALIZED"
        elif state == "FAULTED" and operation != "resolve_operation":
            reason = "SERVICE_FAULTED"
        else:
            return None
        return self._error(operation, "INVALID_STATE", reason, "state")

    def _fault(self, reason: Reason) -> None:
        with self._lock:
            if self._lifecycle not in ("CLOSING", "CLOSED"):
                self._lifecycle = "FAULTED"
            self._reason = reason

    def _expired(self, job: _Job) -> bool:
        assert self._resources is not None
        return job.cancelled.is_set() or self._resources.monotonic() >= job.deadline

    def _check_deadline(self, job: _Job) -> None:
        if self._expired(job):
            raise _StorageFault(self._error(job.operation, "DEADLINE_EXCEEDED", "OPERATION_DEADLINE"))

    def _record_failure(self, job: _Job, error: PersistenceError) -> PersistenceError:
        if job.first_error is None:
            job.first_error = replace(error, operation=job.operation)
        return job.first_error

    async def _run(self, job: _Job, work: Callable[[], object], *, writer: bool = False) -> object:
        loop = asyncio.get_running_loop()
        completion = loop.create_future()

        def notify() -> None:
            if not completion.done():
                completion.set_result(None)

        def worker() -> None:
            job.owner = threading.get_ident()
            try:
                if job.operation != "close":
                    self._check_deadline(job)
                job.result = work()
            except _StorageFault as failure:
                error = self._record_failure(job, failure.error)
                job.result = Failed(error)
            except BaseException as failure:
                # Allocation/process faults are returned to the waiting task,
                # never represented as a successful or expected domain result.
                job.result = failure
                self._fault("IO_FAILED")
            finally:
                with self._lock:
                    if writer and self._writer is job:
                        self._writer = None
                    self._reads.discard(job)
                    if job.operation == "close" and self._close_report is None and type(job.result) is CloseReport:
                        self._close_report = job.result
                    job.done.set()
                try:
                    loop.call_soon_threadsafe(notify)
                except RuntimeError:
                    pass  # The caller loop may end while owned cleanup continues.

        threading.Thread(target=worker, name="persistence-owner", daemon=True).start()
        assert self._resources is not None
        remaining = max(0.0, job.deadline - self._resources.monotonic())
        try:
            await asyncio.wait_for(asyncio.shield(completion), remaining)
        except (TimeoutError, asyncio.CancelledError) as interruption:
            with self._lock:
                if not job.done.is_set():
                    job.cancelled.set()
                    job.cleanup_pending = True
                    self._record_failure(job, self._error(job.operation, "DEADLINE_EXCEEDED", "OPERATION_DEADLINE", pending=True))
                    self._fault(job.first_error.reason if job.first_error else "OPERATION_DEADLINE")
                    if job.handle is not None and job.evidence is None:
                        self._unresolved.add(job.handle.identity)
                    if job.operation == "close" and self._close_report is None:
                        self._close_report = CloseReport("INCOMPLETE", self._error("close", "DEADLINE_EXCEEDED", "OPERATION_DEADLINE", pending=True))
                    if type(interruption) is asyncio.CancelledError:
                        raise
                    if job.evidence is not None:
                        return job.evidence
                    return None
        if isinstance(job.result, BaseException):
            raise job.result
        return job.result

    def _sql_error(self, job: _Job, failure: sqlite3.Error) -> PersistenceError:
        code = getattr(failure, "sqlite_errorcode", None)
        base = code & 255 if type(code) is int else None
        if self._expired(job):
            return self._error(job.operation, "DEADLINE_EXCEEDED", "OPERATION_DEADLINE")
        if base in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return self._error(job.operation, "RESOURCE_BUSY", "LOCK_DEADLINE")
        if base == sqlite3.SQLITE_CONSTRAINT:
            return self._error(job.operation, "TRANSACTION_FAILED", "CONSTRAINT_FAILED", "transaction")
        if (base == sqlite3.SQLITE_NOTADB and job.operation in ("initialize", "resolve_operation")
                and not job.application_identified):
            return self._error(job.operation, "FORMAT_UNSUPPORTED", "FOREIGN_DATABASE", "format")
        if base in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            return self._error(job.operation, "INTEGRITY_FAILURE", "DATA_INCONSISTENT", "transaction")
        reason: Reason = "READ_ONLY" if base == sqlite3.SQLITE_READONLY else "NO_SPACE" if base == sqlite3.SQLITE_FULL else "IO_FAILED"
        return self._error(job.operation, "STORAGE_UNAVAILABLE", reason)

    def _sql(self, job: _Job, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        self._check_deadline(job)
        assert job.connection is not None
        job.connection.execute("PRAGMA busy_timeout=0")
        return self._execute_sql(job, sql, parameters)

    def _execute_sql(self, job: _Job, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        """Share one lock budget while preserving the currently installed authorizer."""
        self._check_deadline(job)
        assert job.connection is not None and self._settings is not None and self._resources is not None
        while True:
            try:
                return job.connection.execute(sql, parameters)  # type: ignore[arg-type] -- only internal bound native SQL parameters reach SQLite.
            except sqlite3.Error as failure:
                code = getattr(failure, "sqlite_errorcode", None)
                base = code & 255 if type(code) is int else None
                lock_budget = max(0.0, self._settings.lock_wait_ms / 1000 - job.lock_spent)
                remaining = job.deadline - self._resources.monotonic()
                if (base not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        or sql == "COMMIT" or lock_budget <= 0 or remaining <= 0):
                    raise _StorageFault(self._sql_error(job, failure)) from None
            # Only explicit waiting after SQLITE_BUSY/LOCKED consumes lock budget.
            # Successful SQL/encoding time consumes the total deadline alone.
            started = self._resources.monotonic()
            job.cancelled.wait(min(0.005, lock_budget, remaining))
            job.lock_spent += max(0.0, self._resources.monotonic() - started)
            self._check_deadline(job)

    def _connect(self, job: _Job, *, readonly: bool = False) -> sqlite3.Connection:
        self._check_deadline(job)
        assert self._settings is not None and self._resources is not None
        path = Path(self._settings.database_file)
        metadata = path.stat(follow_symlinks=False)
        if self._file_identity != (metadata.st_dev, metadata.st_ino):
            raise _StorageFault(self._error(job.operation, "STORAGE_UNAVAILABLE", "RESOURCE_INVALID"))
        uri = path.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
        try:
            connection = self._resources.connect(uri, uri=True, timeout=0, isolation_level=None, check_same_thread=False)
        except MemoryError:
            raise
        except Exception:
            raise _StorageFault(self._error(job.operation, "STORAGE_UNAVAILABLE", "OPEN_FAILED" if job.operation == "initialize" else "IO_FAILED")) from None
        with self._lock:
            self._connections.add(connection)
        job.connection = connection
        current = path.stat(follow_symlinks=False)
        parent = path.parent.stat(follow_symlinks=False)
        assert self._owner_fd is not None
        anchor = os.fstat(self._owner_fd)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or current.st_uid != os.geteuid()
                or self._file_identity != (current.st_dev, current.st_ino)
                or (parent.st_dev, parent.st_ino) != (anchor.st_dev, anchor.st_ino)
                or str(path.resolve()) != str(path)):
            raise _StorageFault(self._error(job.operation, "STORAGE_UNAVAILABLE", "RESOURCE_INVALID"))
        connection.set_progress_handler(lambda: int(self._expired(job)), 100)
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        return connection

    def _configure(self, job: _Job, *, change_journal: bool = False) -> None:
        assert job.connection is not None and self._settings is not None
        expected = {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1, "read_uncommitted": 0,
                    "wal_autocheckpoint": self._settings.wal_checkpoint_pages}
        for name, value in expected.items():
            if name != "journal_mode" or change_journal:
                self._sql(job, "PRAGMA " + name + "=" + str(value))
            actual = self._sql(job, "PRAGMA " + name).fetchone()[0]
            if actual != value:
                if job.operation == "initialize":
                    raise _StorageFault(self._error(job.operation, "RUNTIME_UNSUPPORTED", "SQLITE_CAPABILITY_MISSING"))
                raise _StorageFault(self._error(job.operation, "STORAGE_UNAVAILABLE", "IO_FAILED"))

    def _release_connection(self, job: _Job) -> None:
        connection = job.connection
        if connection is None:
            return
        try:
            connection.set_authorizer(None)
            connection.set_progress_handler(None, 0)
            connection.close()
        except MemoryError:
            raise
        except Exception:
            job.cleanup_pending = True
            self._fault("RESOURCE_CLOSE_FAILED")
        else:
            with self._lock:
                self._connections.discard(connection)
            job.connection = None
            job.cleanup_pending = False

    def _checkpoint(self, job: _Job) -> None:
        """Attempt nonblocking WAL checkpoint and retain incomplete frame evidence."""
        try:
            busy, frames, checkpointed = self._sql(job, "PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            with self._lock:
                self._checkpoint_pending = bool(busy or (frames >= 0 and checkpointed < frames))
        except _StorageFault as fault:
            with self._lock:
                self._checkpoint_pending = True
            self._fault(fault.error.reason)
        except MemoryError:
            raise
        except Exception:
            with self._lock:
                self._checkpoint_pending = True
            self._fault("IO_FAILED")

    def _rollback(self, job: _Job) -> bool:
        connection = job.connection
        if connection is None:
            return not job.absence_checked
        try:
            connection.set_authorizer(None)
            connection.set_progress_handler(None, 0)
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            job.rollback_confirmed = not connection.in_transaction
            return job.rollback_confirmed
        except MemoryError:
            raise
        except Exception:
            self._fault("RECOVERY_UNAVAILABLE")
            return False

    def _own_target(self, job: _Job, mode: str) -> None:
        """Anchor an exclusive regular target in an owned, non-aliased directory.

        A lifetime advisory lock proves other supported service owners have
        ended before reopening. SQLite's own locks still arbitrate SQL access.
        No abnormal file or companion is removed to make initialization succeed.
        """
        assert self._settings is not None and self._resources is not None
        self._check_deadline(job)
        path = Path(self._settings.database_file)
        if self._resources.retained_identity_check(self._resources.expected_database_id, str(path)) is not True:
            raise _StorageFault(self._error("initialize", "ACCESS_DENIED", "CAPABILITY_MISMATCH"))
        if str(path.resolve()) != str(path):
            raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "RESOURCE_INVALID"))
        if os.path.lexists(path):
            if mode == "CREATE_NEW":
                raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "TARGET_EXISTS"))
            target = path.lstat()
            if not stat.S_ISREG(target.st_mode) or target.st_nlink != 1 or target.st_uid != os.geteuid():
                raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "RESOURCE_INVALID"))
        parent = path.parent.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022
                or not os.access(path.parent, os.W_OK | os.X_OK)):
            raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "RESOURCE_INVALID"))
        logging_directory = self._settings.logging_directory
        if logging_directory is not None:
            log = Path(logging_directory)
            if str(log.resolve()) != str(log) or path.parent == log or path.parent in log.parents or log in path.parent.parents:
                raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "RESOURCE_INVALID"))
        for suffix in ("-wal", "-shm", "-journal"):
            companion = Path(str(path) + suffix)
            if os.path.lexists(companion):
                if mode == "CREATE_NEW":
                    raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "TARGET_EXISTS"))
                info = companion.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                    raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "RESOURCE_INVALID"))
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _StorageFault(self._error("initialize", "RESOURCE_BUSY", "ADMISSION_BUSY")) from None
            self._owner_fd = directory_fd
            flags = os.O_RDWR | os.O_NOFOLLOW
            if mode == "CREATE_NEW":
                flags |= os.O_CREAT | os.O_EXCL
            try:
                owner_fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "TARGET_EXISTS")) from None
            except FileNotFoundError:
                raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "TARGET_MISSING")) from None
            metadata = os.fstat(owner_fd)
            os.close(owner_fd)
            current = path.stat(follow_symlinks=False)
            anchored_parent = os.fstat(directory_fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.geteuid()
                    or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
                    or (parent.st_dev, parent.st_ino) != (anchored_parent.st_dev, anchored_parent.st_ino)):
                raise _StorageFault(self._error("initialize", "STORAGE_UNAVAILABLE", "RESOURCE_INVALID"))
            self._file_identity = metadata.st_dev, metadata.st_ino
        finally:
            if self._owner_fd != directory_fd:
                os.close(directory_fd)

    def _release_target(self) -> bool:
        if self._owner_fd is None:
            return True
        try:
            os.close(self._owner_fd)
        except OSError:
            self._fault("RESOURCE_CLOSE_FAILED")
            return False
        self._owner_fd = None
        return True

    async def initialize(self, snapshot: object, resources: object, mode: object) -> InitializationResult:
        """Explicitly CREATE_NEW or OPEN_EXISTING a prebound retained database ID.

        Refuse unsupported format/identity/schema without repairs. Only complete
        validation produces Ready. Any possibly committed creation lacking final
        evidence returns the original retained identity as unconfirmed.
        """
        with self._lock:
            error = self._state_error("initialize")
            if error is not None:
                return Rejected(error)
            if type(resources) is not DatabaseResources:
                return Rejected(self._error("initialize", "ACCESS_DENIED", "CAPABILITY_MISMATCH"))
            if (not valid_identifier(resources.expected_database_id) or not callable(resources.retained_identity_check)
                    or type(mode) is not str or mode not in ("CREATE_NEW", "OPEN_EXISTING")):
                return Rejected(self._error("initialize", "INVALID_INPUT", "INVALID_SHAPE"))
            started = resources.monotonic()
            settings = read_settings(snapshot)
            if isinstance(settings, PersistenceError):
                return Rejected(settings)
            if self._writer is not None:
                return Rejected(self._error("initialize", "RESOURCE_BUSY", "ADMISSION_BUSY"))
            assert type(snapshot) is EffectiveSnapshot
            self._resources, self._settings, self._snapshot = resources, settings, snapshot
            job = _Job("initialize", started + settings.operation_timeout_ms / 1000)
            self._writer = job

        def initialize_owned() -> InitializationResult:
            failure: PersistenceError | None = None
            try:
                if sqlite3.sqlite_version_info < (3, 51, 3):
                    raise _StorageFault(self._error("initialize", "RUNTIME_UNSUPPORTED", "SQLITE_VERSION_UNSUPPORTED"))
                if sqlite3.threadsafety == 0:
                    raise _StorageFault(self._error("initialize", "RUNTIME_UNSUPPORTED", "SQLITE_CAPABILITY_MISSING"))
                self._own_target(job, mode)
                job.target_bound = True
                self._connect(job)
                if mode == "CREATE_NEW":
                    self._configure(job, change_journal=True)
                    self._sql(job, "BEGIN IMMEDIATE")
                    job.phase = "transaction"
                    self._sql(job, "PRAGMA application_id=" + str(_APPLICATION_ID))
                    for _, statement in self._schema:
                        self._sql(job, statement)
                    self._sql(job, "INSERT INTO application_metadata VALUES(1, ?, ?, ?)",
                              (resources.expected_database_id, _FORMAT_VERSION, self._assembly))
                    job.phase = "commit"
                    self._sql(job, "COMMIT")
                    job.commit_confirmed = True
                self._verify_database(job)
                self._configure(job)
                self._sql(job, "BEGIN")
                self._check_all_receipts(job)
                self._sql(job, "COMMIT")
                self._release_connection(job)
                if job.cleanup_pending:
                    raise _StorageFault(self._error("initialize", "CLEANUP_INCOMPLETE", "RESOURCE_CLOSE_FAILED", pending=True))
                self._check_deadline(job)
                with self._lock:
                    if self._lifecycle == "NEW":
                        self._lifecycle = "READY"
                return Ready(resources.expected_database_id, _FORMAT_VERSION)
            except _StorageFault as fault:
                failure = self._record_failure(job, fault.error)
            except MemoryError:
                raise
            except Exception:
                failure = self._record_failure(job, self._error("initialize", "STORAGE_UNAVAILABLE", "OPEN_FAILED"))
            finally:
                if job.connection is not None:
                    if job.phase != "commit" or job.commit_confirmed or job.connection.in_transaction:
                        self._rollback(job)
                    self._release_connection(job)
            assert failure is not None
            if not job.cleanup_pending:
                job.cleanup_pending = not self._release_target()
            failure = replace(failure, cleanup_pending=job.cleanup_pending)
            unknown = ((job.phase == "commit" and not job.commit_confirmed and not job.rollback_confirmed)
                       or (mode == "OPEN_EXISTING" and job.target_bound and failure.reason in (
                           "IO_FAILED", "OPEN_FAILED", "READ_ONLY", "NO_SPACE", "LOCK_DEADLINE",
                           "OPERATION_DEADLINE", "RESOURCE_CLOSE_FAILED", "RECOVERY_UNAVAILABLE")))
            if job.cleanup_pending or unknown:
                self._fault(failure.reason)
            if unknown:
                return InitializationUnconfirmed(resources.expected_database_id, failure)
            return Rejected(failure)

        result = await self._run(job, initialize_owned, writer=True)
        if result is None:
            assert job.first_error is not None
            return InitializationUnconfirmed(resources.expected_database_id, replace(job.first_error, cleanup_pending=True))
        if type(result) is Failed:
            return Rejected(result.error)
        return cast(InitializationResult, result)

    def _verify_database(self, job: _Job) -> None:
        assert self._resources is not None
        application_id = self._sql(job, "PRAGMA application_id").fetchone()[0]
        if application_id != _APPLICATION_ID:
            raise _StorageFault(self._error(job.operation, "FORMAT_UNSUPPORTED", "FOREIGN_DATABASE", "format"))
        job.application_identified = True
        tables = self._sql(job, "SELECT name FROM sqlite_schema WHERE type='table' AND name='application_metadata'").fetchall()
        if not tables:
            raise _StorageFault(self._error(job.operation, "FORMAT_UNSUPPORTED", "INITIALIZATION_INCOMPLETE", "format"))
        metadata_columns = self._sql(job, "PRAGMA table_info(application_metadata)").fetchall()
        if {row[1] for row in metadata_columns} != {"singleton", "database_id", "format_version", "assembly"}:
            raise _StorageFault(self._error(job.operation, "FORMAT_UNSUPPORTED", "INITIALIZATION_INCOMPLETE", "format"))
        rows = self._sql(job, "SELECT database_id, format_version, assembly FROM application_metadata WHERE singleton=1").fetchall()
        if len(rows) != 1 or not valid_identifier(rows[0][0]):
            raise _StorageFault(self._error(job.operation, "FORMAT_UNSUPPORTED", "INITIALIZATION_INCOMPLETE", "format"))
        identity, version, assembly = rows[0]
        if identity != self._resources.expected_database_id:
            raise _StorageFault(self._error(job.operation, "INTEGRITY_FAILURE", "DATABASE_ID_MISMATCH"))
        if type(version) is not int or version != _FORMAT_VERSION or assembly != self._assembly:
            raise _StorageFault(self._error(job.operation, "FORMAT_UNSUPPORTED", "SCHEMA_VERSION_UNSUPPORTED", "format"))
        actual = self._sql(job, "SELECT name, sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name").fetchall()
        if actual != sorted(self._schema):
            raise _StorageFault(self._error(job.operation, "INTEGRITY_FAILURE", "SCHEMA_MISMATCH", "format"))
        if self._sql(job, "PRAGMA foreign_key_check").fetchone() is not None:
            raise _StorageFault(self._error(job.operation, "INTEGRITY_FAILURE", "DATA_INCONSISTENT", "receipt"))
        if self._sql(job, "PRAGMA quick_check").fetchone() != ("ok",):
            raise _StorageFault(self._error(job.operation, "INTEGRITY_FAILURE", "DATA_INCONSISTENT", "transaction"))

    def _check_all_receipts(self, job: _Job) -> None:
        cursor = self._sql(job, "SELECT owner_namespace, operation_kind, scope_id, operation_key FROM operation_receipts")
        assert self._resources is not None
        while row := cursor.fetchone():
            self._check_deadline(job)
            self._receipt(job, OperationIdentity(self._resources.expected_database_id, *row))
        orphan = self._sql(job, "SELECT 1 FROM audit_records a LEFT JOIN operation_receipts r USING(commit_id) WHERE r.commit_id IS NULL LIMIT 1").fetchone()
        missing = self._sql(job, "SELECT 1 FROM required_audit_events a LEFT JOIN operation_receipts r USING(commit_id) WHERE r.commit_id IS NULL LIMIT 1").fetchone()
        if orphan or missing:
            raise _StorageFault(self._error(job.operation, "INTEGRITY_FAILURE", "DATA_INCONSISTENT", "receipt"))

    def _stored_audits(self, job: _Job, definition: CommandDefinition, receipt: Receipt, *, require_complete: bool = True) -> tuple[AuditRecord, ...]:
        manifest = self._sql(job, "SELECT length(manifest), CASE WHEN length(manifest)<=65536 THEN manifest END FROM required_audit_events WHERE commit_id=?", (receipt.commit_id,)).fetchone()
        expected = encode_value(audit_manifest(definition.required_audits), 65536)
        if manifest is None or manifest[0] > 65536 or manifest[1] != expected:
            raise InvalidValue()
        rows = self._sql(job, "SELECT event_slot, audit_id, length(record), CASE WHEN length(record)<=65536 THEN record END FROM audit_records WHERE commit_id=? ORDER BY event_slot LIMIT 257", (receipt.commit_id,)).fetchall()
        requirements = {item.event_slot: item for item in definition.required_audits}
        if len(rows) > 256 or (require_complete and {row[0] for row in rows} != set(requirements)):
            raise InvalidValue()
        records = []
        for slot, audit_id, size, encoded in rows:
            if slot not in requirements or size > 65536:
                raise InvalidValue()
            record = decode_audit(encoded)
            if record.audit_id != audit_id:
                raise InvalidValue()
            event = freeze_audit_event(requirements[slot], {
                "event_version": record.event_version, "actor_kind": record.actor_kind,
                "actor_ref": record.actor_ref, "reason_code": record.reason_code,
                "target_refs": record.target_refs, "change": record.change,
            })
            record = replace(record, target_refs=event["target_refs"], change=event["change"])
            if validate_audit_record(record, requirements[slot], receipt.identity, receipt.commit_id) != encoded:
                raise InvalidValue()
            records.append(record)
        return tuple(records)

    def _receipt(self, job: _Job, identity: OperationIdentity) -> Receipt | None:
        key = (identity.owner_namespace, identity.operation_kind, identity.scope_id, identity.operation_key)
        row = self._sql(job, "SELECT commit_id, length(receipt), CASE WHEN length(receipt)<=65536 THEN receipt END FROM operation_receipts WHERE owner_namespace=? AND operation_kind=? AND scope_id=? AND operation_key=?", key).fetchone()
        if row is None:
            orphan = self._sql(job, "SELECT 1 FROM audit_records a LEFT JOIN operation_receipts r USING(commit_id) WHERE r.commit_id IS NULL LIMIT 1").fetchone()
            orphan_manifest = self._sql(job, "SELECT 1 FROM required_audit_events a LEFT JOIN operation_receipts r USING(commit_id) WHERE r.commit_id IS NULL LIMIT 1").fetchone()
            if orphan or orphan_manifest:
                self._fault("DATA_INCONSISTENT")
                raise _StorageFault(self._error(job.operation, "INTEGRITY_FAILURE", "DATA_INCONSISTENT", "receipt"))
            return None
        try:
            definition = self._command_map[(identity.owner_namespace, identity.operation_kind)]
            if row[1] > 65536:
                raise InvalidValue()
            receipt = decode_receipt(row[2], definition)
            if receipt.identity != identity or receipt.commit_id != row[0]:
                raise InvalidValue()
            self._stored_audits(job, definition, receipt)
            return receipt
        except (InvalidValue, KeyError, TypeError):
            self._fault("DATA_INCONSISTENT")
            raise _StorageFault(self._error(job.operation, "INTEGRITY_FAILURE", "DATA_INCONSISTENT", "receipt")) from None

    def _bound_identity(self, port: OperationPort, key: object, operation: Operation) -> OperationIdentity | PersistenceError:
        if (type(port) is not OperationPort or self._ports.get(id(port)) is not port or port._service is not self
                or not any(port._definition is item for item in self._commands) or not valid_identifier(port._scope)):
            return self._error(operation, "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation")
        if not valid_identifier(key):
            return self._error(operation, "INVALID_INPUT", "INVALID_SHAPE", "query" if operation == "read_receipt" else "operation")
        assert self._resources is not None
        return OperationIdentity(self._resources.expected_database_id, port._definition.owner_namespace,
                                 port._definition.operation_kind, port._scope, cast(str, key))

    def _prepare(self, port: OperationPort, key: object, command: object, operation: Operation) -> tuple[RecoveryHandle, MappingProxyType[str, Value], MappingProxyType[str, Value]] | Rejected:
        error = self._state_error(operation)
        if error is not None:
            return Rejected(error)
        identity = self._bound_identity(port, key, operation)
        if isinstance(identity, PersistenceError):
            return Rejected(identity)
        if type(command) is not LocalCommand or type(command.version) is not int:
            return Rejected(self._error(operation, "INVALID_INPUT", "INVALID_SHAPE", "operation"))
        if command.version != port._definition.command_version:
            return Rejected(self._error(operation, "INVALID_INPUT", "UNSUPPORTED_COMMAND", "operation"))
        assert self._settings is not None
        try:
            return prepare_command(port._definition, identity, command, self._settings.command_max_bytes)
        except ValueTooLarge:
            return Rejected(self._error(operation, "INVALID_INPUT", "LIMIT_EXCEEDED", "operation"))
        except (InvalidValue, RecursionError):
            return Rejected(self._error(operation, "INVALID_INPUT", "INVALID_SHAPE", "operation"))

    def _valid_uow(self, uow: object, scope: str) -> bool:
        return (type(uow) is UnitOfWork and self._writer is not None and self._writer.uow is uow
                and uow._service is self and uow._active
                and uow._job is self._writer and uow._job.uow is uow and uow._job.owner == threading.get_ident()
                and uow._identity.scope_id == scope and uow._job.connection is not None
                and uow._job.connection.in_transaction)

    def _poison(self, error: PersistenceError) -> None:
        job = self._writer
        if job is not None and job.owner == threading.get_ident():
            self._record_failure(job, replace(error, operation="execute"))

    def _participant_failure(self, error: PersistenceError) -> Failed:
        self._poison(error)
        return Failed(error)

    def _statement(self, job: _Job, port: StatementPort, parameters: Value) -> Value:
        assert job.connection is not None and type(parameters) is MappingProxyType and self._settings is not None
        connection = job.connection
        owned = dict(parameters)
        owned["scope_id"] = port._scope
        tables = {table.name for table in port._repository.tables}

        def authorize(action: int, first: str | None, second: str | None, database: str | None, trigger: str | None) -> int:
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and first in tables:
                return sqlite3.SQLITE_OK
            if port._definition.writes and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and first in tables:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        self._check_deadline(job)
        # PRAGMA ownership remains infrastructure-only; configure before installing
        # the module authorizer, then run its single statically bound statement.
        connection.execute("PRAGMA busy_timeout=0")
        connection.set_authorizer(authorize)
        try:
            cursor = self._execute_sql(job, port._definition.sql, owned)
            rows: list[Value] = []
            size = 0
            while row := cursor.fetchone():
                self._check_deadline(job)
                names = tuple(column[0] for column in cursor.description)
                if len(set(names)) != len(names):
                    raise InvalidValue()
                frozen = freeze_value(port._definition.row_schema, dict(zip(names, row)))
                size += len(encode_value(frozen, self._settings.receipt_max_bytes))
                if size > self._settings.receipt_max_bytes:
                    raise ValueTooLarge()
                rows.append(frozen)
            return tuple(rows)
        except sqlite3.Error as failure:
            raise _StorageFault(self._sql_error(job, failure)) from None
        finally:
            connection.set_authorizer(None)

    def _participate(self, port: StatementPort, uow: object, parameters: object) -> Staged[Value] | Failed:
        error = self._state_error("participate")
        if error is not None:
            return self._participant_failure(error)
        if (type(port) is not StatementPort or self._ports.get(id(port)) is not port or port._service is not self or not self._valid_uow(uow, port._scope)
                or not any(port._repository is item for item in cast(UnitOfWork, uow)._definition.participants)):
            return self._participant_failure(self._error("participate", "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation"))
        assert type(uow) is UnitOfWork
        try:
            values = freeze_value(port._definition.parameters, parameters)
            assert self._settings is not None
            encode_value(values, self._settings.command_max_bytes)
            self._check_deadline(uow._job)
            if uow._job.first_error is not None:
                return Failed(replace(uow._job.first_error, operation="participate"))
            result = self._statement(uow._job, port, values)
            if port._definition.writes:
                uow._changed.add(port._repository.owner_module)
            return Staged(result)
        except ValueTooLarge:
            error = self._error("participate", "INVALID_INPUT", "LIMIT_EXCEEDED", "operation")
        except (InvalidValue, RecursionError):
            error = self._error("participate", "INVALID_INPUT", "INVALID_SHAPE", "operation")
        except _StorageFault as failure:
            error = replace(failure.error, operation="participate")
        except MemoryError:
            raise
        except Exception:
            error = self._error("participate", "STORAGE_UNAVAILABLE", "IO_FAILED")
        return self._participant_failure(error)

    def _stage_audit(self, binding: AuditStorageBinding, uow: UnitOfWork, event: MappingProxyType[str, Value]) -> AuditStaged | Failed:
        requirement = binding._requirement
        assert requirement is not None and self._settings is not None and self._resources is not None
        try:
            self._check_deadline(uow._job)
            if requirement.owner_module not in uow._changed:
                return Failed(self._error("participate", "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation"))
            record = create_audit_record(requirement, uow._identity, uow._commit_id, event,
                                         self._resources.new_id, self._resources.utc_now)
            encoded = encode_value(audit_record_value(record), self._settings.event_max_bytes)
            count = self._sql(uow._job, "SELECT count(*) FROM audit_records WHERE commit_id=?", (uow._commit_id,)).fetchone()[0]
            if count >= self._settings.events_per_operation:
                raise ValueTooLarge()
            if event != uow._events[requirement.event_slot]:
                return Failed(self._error("participate", "TRANSACTION_FAILED", "CONSTRAINT_FAILED", "audit"))
            if self._sql(uow._job, "SELECT 1 FROM audit_records WHERE commit_id=? AND event_slot=?", (uow._commit_id, requirement.event_slot)).fetchone():
                return Failed(self._error("participate", "TRANSACTION_FAILED", "CONSTRAINT_FAILED", "audit"))
            self._sql(uow._job, "INSERT INTO audit_records VALUES(?, ?, ?, ?)", (uow._commit_id, requirement.event_slot, record.audit_id, encoded))
            return AuditStaged(requirement.event_slot, record.audit_id)
        except ValueTooLarge:
            return Failed(self._error("participate", "INVALID_INPUT", "LIMIT_EXCEEDED", "audit"))
        except _StorageFault as failure:
            return Failed(replace(failure.error, operation="participate"))
        except MemoryError:
            raise
        except Exception:
            return Failed(self._error("participate", "STORAGE_UNAVAILABLE", "IO_FAILED"))

    def _required_audits(self, binding: AuditStorageBinding, uow: UnitOfWork) -> tuple[tuple[AuditRequirement, ...], tuple[AuditRecord, ...]] | Failed:
        try:
            # A temporary receipt carries associations only; no result is published
            # or stored until the complete mandatory set has been checked.
            receipt = Receipt(1, uow._identity, uow._definition.command_version, 1, "0" * 64,
                              uow._commit_id, "", uow._definition.result_schema_version, None)
            records = self._stored_audits(uow._job, uow._definition, receipt, require_complete=False)
            return uow._definition.required_audits, records
        except InvalidValue:
            self._fault("DATA_INCONSISTENT")
            return Failed(self._error("participate", "INTEGRITY_FAILURE", "DATA_INCONSISTENT", "audit"))
        except _StorageFault as failure:
            return Failed(replace(failure.error, operation="participate"))
        except MemoryError:
            raise
        except Exception:
            return Failed(self._error("participate", "STORAGE_UNAVAILABLE", "IO_FAILED"))

    async def _execute(self, port: OperationPort, key: object, command: object) -> ExecutionResult:
        started = self._resources.monotonic() if self._resources is not None else 0.0
        with self._lock:
            prepared = self._prepare(port, key, command, "execute")
            if isinstance(prepared, Rejected):
                return prepared
            handle, values, events = prepared
            assert self._resources is not None and self._settings is not None
            if self._writer is not None:
                return Rejected(self._error("execute", "RESOURCE_BUSY", "ADMISSION_BUSY"))
            job = _Job("execute", started + self._settings.operation_timeout_ms / 1000)
            job.handle = handle
            self._writer = job

        def execute_owned() -> ExecutionResult:
            committed: Committed | None = None
            failure: PersistenceError | None = None
            conflict = False
            try:
                self._connect(job)
                self._configure(job)
                self._sql(job, "BEGIN IMMEDIATE")
                job.phase = "transaction"
                existing = self._receipt(job, handle.identity)
                if existing is not None:
                    if (existing.fingerprint != handle.fingerprint or existing.command_version != handle.command_version
                            or existing.fingerprint_version != handle.fingerprint_version):
                        conflict = True
                        raise _StorageFault(self._error("execute", "IDEMPOTENCY_CONFLICT", "CONTENT_MISMATCH", "operation"))
                    committed = Committed(existing, "EXISTING")
                    with self._lock:
                        job.evidence = committed
                    self._rollback(job)
                    self._checkpoint(job)
                else:
                    job.absence_checked = True
                    assert self._resources is not None and self._settings is not None and self._snapshot is not None
                    try:
                        commit_id = self._resources.new_id()
                    except MemoryError:
                        raise
                    except Exception:
                        raise _StorageFault(self._error("execute", "TRANSACTION_FAILED", "RECEIPT_FAILED", "receipt")) from None
                    if not valid_identifier(commit_id):
                        raise _StorageFault(self._error("execute", "TRANSACTION_FAILED", "RECEIPT_FAILED", "receipt"))
                    uow = UnitOfWork._create(self, job, port._definition, handle.identity, cast(str, commit_id), events)
                    job.uow = uow
                    manifest = encode_value(audit_manifest(port._definition.required_audits), 65536)
                    try:
                        self._sql(job, "INSERT INTO required_audit_events VALUES(?, ?)", (commit_id, manifest))
                    except _StorageFault:
                        raise _StorageFault(self._error("execute", "TRANSACTION_FAILED", "AUDIT_FAILED", "audit")) from None
                    try:
                        result = port._definition.handler(uow, values)
                    except MemoryError:
                        raise
                    except Exception:
                        self._record_failure(job, self._error("execute", "TRANSACTION_FAILED", "PARTICIPANT_REJECTED", "transaction"))
                        result = None
                    self._check_deadline(job)
                    if job.first_error is not None:
                        raise _StorageFault(job.first_error)
                    from companion_memory.logging_service.audit import AuditAccess
                    job.coordinator = AuditStorageBinding(self, port._scope, None, coordinator=True)
                    audit = AuditAccess.for_coordinator(job.coordinator, self._settings.events_per_operation)
                    checked = audit.check_required_audits(uow)
                    if type(checked) is AuditErr:
                        assert job.first_error is not None
                        raise _StorageFault(job.first_error)
                    try:
                        frozen = freeze_value(port._definition.result_schema, result, owned=True)
                        receipt = Receipt(1, handle.identity, handle.command_version, 1, handle.fingerprint,
                                          cast(str, commit_id), utc_text(self._resources.utc_now()),
                                          port._definition.result_schema_version, frozen)
                        encoded = encode_value(receipt_value(receipt), self._settings.receipt_max_bytes)
                        self._sql(job, "INSERT INTO operation_receipts VALUES(?, ?, ?, ?, ?, ?)",
                                  (handle.identity.owner_namespace, handle.identity.operation_kind,
                                   handle.identity.scope_id, handle.identity.operation_key, commit_id, encoded))
                    except MemoryError:
                        raise
                    except Exception:
                        raise _StorageFault(self._error("execute", "TRANSACTION_FAILED", "RECEIPT_FAILED", "receipt")) from None
                    self._check_deadline(job)
                    job.phase = "commit"
                    self._sql(job, "COMMIT")
                    job.commit_confirmed = True
                    committed = Committed(receipt, "NEW")
                    with self._lock:
                        job.evidence = committed
                    self._checkpoint(job)
            except _StorageFault as fault:
                failure = self._record_failure(job, fault.error)
            except MemoryError:
                raise
            except Exception:
                failure = self._record_failure(job, self._error("execute", "STORAGE_UNAVAILABLE", "IO_FAILED"))
            finally:
                if job.uow is not None:
                    job.uow._active = False
                if committed is None:
                    # A raised COMMIT with no active transaction cannot tell
                    # rollback from lost commit confirmation. Preserve unknown.
                    if job.phase != "commit" or (job.connection is not None and job.connection.in_transaction):
                        self._rollback(job)
                self._release_connection(job)
            if committed is not None:
                with self._lock:
                    self._unresolved.discard(handle.identity)
                return committed
            assert failure is not None
            failure = replace(failure, cleanup_pending=job.cleanup_pending)
            if failure.code in ("STORAGE_UNAVAILABLE", "INTEGRITY_FAILURE"):
                self._fault(failure.reason)
            if conflict or not job.absence_checked:
                if job.phase != "commit":
                    return Rejected(failure)
            if job.rollback_confirmed:
                with self._lock:
                    self._unresolved.discard(handle.identity)
                return NotCommitted(failure)
            self._fault(failure.reason)
            with self._lock:
                self._unresolved.add(handle.identity)
            return Unconfirmed(handle, failure)

        result = await self._run(job, execute_owned, writer=True)
        if result is None:
            assert job.first_error is not None
            error = replace(job.first_error, cleanup_pending=True)
            if job.commit_confirmed and type(job.result) is Committed:
                return job.result
            if job.rollback_confirmed and job.absence_checked:
                return NotCommitted(error)
            return Unconfirmed(handle, error)
        if type(result) is Failed:
            return Rejected(result.error)
        return cast(ExecutionResult, result)

    async def _read(self, port: OperationPort | StatementPort | AuditStorageBinding, query: object,
                    operation: Operation, *, audit: bool = False) -> ReadResult[object]:
        started = self._resources.monotonic() if self._resources is not None else 0.0
        with self._lock:
            error = self._state_error(operation)
            if error is not None:
                return Failed(error)
            assert self._resources is not None and self._settings is not None
            identity: OperationIdentity | None = None
            parameters: Value = None
            if type(port) is OperationPort:
                checked = self._bound_identity(port, query, operation)
                if isinstance(checked, PersistenceError):
                    return Failed(checked)
                identity = checked
            elif type(port) is StatementPort:
                if self._ports.get(id(port)) is not port or port._service is not self or port._definition.writes:
                    return Failed(self._error(operation, "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation"))
                try:
                    parameters = freeze_value(port._definition.parameters, query)
                    encode_value(parameters, self._settings.command_max_bytes)
                except ValueTooLarge:
                    return Failed(self._error(operation, "INVALID_INPUT", "LIMIT_EXCEEDED", "query"))
                except (InvalidValue, RecursionError):
                    return Failed(self._error(operation, "INVALID_INPUT", "INVALID_SHAPE", "query"))
            elif type(port) is AuditStorageBinding:
                if not port.is_issued() or port._service is not self or not port._reader:
                    return Failed(self._error(operation, "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation"))
                if not valid_identity(query):
                    return Failed(self._error(operation, "INVALID_INPUT", "INVALID_SHAPE", "query"))
                identity = cast(OperationIdentity, query)
                if identity.scope_id != port._scope or identity.database_id != self._resources.expected_database_id:
                    return Failed(self._error(operation, "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation"))
            else:
                return Failed(self._error(operation, "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation"))
            if len(self._reads) >= self._settings.read_capacity:
                return Failed(self._error(operation, "RESOURCE_BUSY", "ADMISSION_BUSY"))
            job = _Job(operation, started + self._settings.operation_timeout_ms / 1000)
            self._reads.add(job)

        def read_owned() -> ReadResult[object]:
            result: ReadResult[object]
            try:
                self._connect(job, readonly=True)
                self._configure(job)
                self._sql(job, "BEGIN")
                if type(port) is StatementPort:
                    value = self._statement(job, port, parameters)
                    result = Found(value) if value else NotFound()
                else:
                    assert identity is not None
                    receipt = self._receipt(job, identity)
                    if receipt is None:
                        result = NotFound()
                    elif audit:
                        result = Found(self._stored_audits(job, self._command_map[(identity.owner_namespace, identity.operation_kind)], receipt))
                    else:
                        result = Found(receipt)
                self._sql(job, "COMMIT")
            except _StorageFault as fault:
                result = Failed(self._record_failure(job, fault.error))
            except MemoryError:
                raise
            except Exception:
                result = Failed(self._record_failure(job, self._error(operation, "STORAGE_UNAVAILABLE", "IO_FAILED")))
            finally:
                self._rollback(job)
                self._release_connection(job)
            if job.cleanup_pending:
                error = result.error if type(result) is Failed else self._error(operation, "CLEANUP_INCOMPLETE", "RESOURCE_CLOSE_FAILED")
                return Failed(replace(error, cleanup_pending=True))
            return result

        result = await self._run(job, read_owned)
        if result is None:
            assert job.first_error is not None
            return Failed(replace(job.first_error, cleanup_pending=True))
        return cast(ReadResult[object], result)

    async def _resolve(self, port: OperationPort, handle: object) -> ExecutionResult:
        with self._lock:
            error = self._state_error("resolve_operation")
            if error is not None:
                return Rejected(error)
            assert self._resources is not None and self._settings is not None
            if type(port) is not OperationPort or self._ports.get(id(port)) is not port or port._service is not self:
                return Rejected(self._error("resolve_operation", "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation"))
            if (type(handle) is not RecoveryHandle or not valid_identity(handle.identity)
                    or type(handle.command_version) is not int or type(handle.fingerprint_version) is not int
                    or type(handle.fingerprint) is not str or len(handle.fingerprint) != 64
                    or any(character not in "0123456789abcdef" for character in handle.fingerprint)):
                return Rejected(self._error("resolve_operation", "INVALID_INPUT", "INVALID_SHAPE", "operation"))
            if (handle.identity.database_id != self._resources.expected_database_id or handle.identity.scope_id != port._scope
                    or handle.identity.owner_namespace != port._definition.owner_namespace
                    or handle.identity.operation_kind != port._definition.operation_kind):
                return Rejected(self._error("resolve_operation", "ACCESS_DENIED", "CAPABILITY_MISMATCH", "operation"))
            if handle.command_version != port._definition.command_version or handle.fingerprint_version != 1:
                return Rejected(self._error("resolve_operation", "INVALID_INPUT", "UNSUPPORTED_COMMAND", "operation"))
            if self._writer is not None:
                return Unconfirmed(handle, self._error("resolve_operation", "RESULT_UNCONFIRMED", "RECOVERY_UNAVAILABLE", "transaction", pending=True))
            job = _Job("resolve_operation", self._resources.monotonic() + self._settings.operation_timeout_ms / 1000)
            job.handle = handle
            self._writer = job

        def resolve_owned() -> ExecutionResult:
            result: ExecutionResult
            try:
                self._connect(job)
                self._verify_database(job)
                self._configure(job)
                self._sql(job, "BEGIN IMMEDIATE")
                receipt = self._receipt(job, handle.identity)
                if receipt is None:
                    self._sql(job, "ROLLBACK")
                    job.rollback_confirmed = True
                    result = NotCommitted(None)
                elif (receipt.command_version != handle.command_version or receipt.fingerprint_version != handle.fingerprint_version
                      or receipt.fingerprint != handle.fingerprint):
                    result = Rejected(self._error("resolve_operation", "IDEMPOTENCY_CONFLICT", "CONTENT_MISMATCH", "operation"))
                else:
                    result = Committed(receipt, "EXISTING")
                    with self._lock:
                        job.evidence = result
                self._rollback(job)
                with self._lock:
                    self._unresolved.discard(handle.identity)
            except _StorageFault as fault:
                result = Unconfirmed(handle, self._record_failure(job, fault.error))
            except MemoryError:
                raise
            except Exception:
                result = Unconfirmed(handle, self._record_failure(job, self._error("resolve_operation", "STORAGE_UNAVAILABLE", "IO_FAILED")))
            finally:
                self._rollback(job)
                self._release_connection(job)
            if type(result) is Unconfirmed:
                self._fault(result.error.reason)
                with self._lock:
                    self._unresolved.add(handle.identity)
                return Unconfirmed(handle, replace(result.error, cleanup_pending=job.cleanup_pending))
            if job.cleanup_pending and type(result) is NotCommitted:
                return NotCommitted(self._error("resolve_operation", "CLEANUP_INCOMPLETE", "RESOURCE_CLOSE_FAILED", pending=True))
            return result

        result = await self._run(job, resolve_owned, writer=True)
        if result is None:
            assert job.first_error is not None
            return Unconfirmed(handle, replace(job.first_error, cleanup_pending=True))
        if type(result) is Failed:
            with self._lock:
                self._unresolved.add(handle.identity)
            return Unconfirmed(handle, result.error)
        return cast(ExecutionResult, result)

    async def close(self) -> CloseReport:
        """Stop admission and wait once within the configured total close deadline.

        Outstanding owners finish their own rollback/connection cleanup; no
        concurrent close is attempted. Repeated calls return the first frozen
        report, while get_health shows any later resource release.
        """
        owned: tuple[_Job, ...] = ()
        with self._lock:
            if self._close_report is not None:
                return self._close_report
            if self._resources is None or self._settings is None:
                self._lifecycle = "CLOSED"
                self._close_report = CloseReport("CLOSED", None)
                return self._close_report
            if self._close_job is not None:
                close_job = self._close_job
                already_closing = True
            else:
                close_job = _Job("close", self._resources.monotonic() + self._settings.close_timeout_ms / 1000)
                self._close_job = close_job
                self._lifecycle = "CLOSING"
                owned = tuple(self._reads) + ((self._writer,) if self._writer else ())
                for job in owned:
                    job.cancelled.set()
                already_closing = False

        if already_closing:
            while self._close_report is None:
                await asyncio.sleep(0.005)
            return self._close_report

        def close_owned() -> CloseReport:
            for job in owned:
                job.done.wait()
            failed = False
            for connection in tuple(self._connections):
                try:
                    connection.set_authorizer(None)
                    connection.set_progress_handler(None, 0)
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    connection.close()
                    with self._lock:
                        self._connections.discard(connection)
                except MemoryError:
                    raise
                except Exception:
                    failed = True
            if not self._connections:
                failed = not self._release_target() or failed
            with self._lock:
                self._lifecycle = "FAULTED" if failed else "CLOSED"
            if failed:
                return CloseReport("INCOMPLETE", self._error("close", "CLEANUP_INCOMPLETE", "RESOURCE_CLOSE_FAILED", pending=True))
            return CloseReport("CLOSED", None)

        result = await self._run(close_job, close_owned)
        with self._lock:
            if self._close_report is not None:
                return self._close_report
            if result is None:
                self._close_report = CloseReport("INCOMPLETE", self._error("close", "DEADLINE_EXCEEDED", "OPERATION_DEADLINE", pending=True))
            elif type(result) is Failed:
                self._close_report = CloseReport("INCOMPLETE", result.error)
            else:
                self._close_report = cast(CloseReport, result)
            return self._close_report
