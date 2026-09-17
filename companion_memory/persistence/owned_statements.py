"""Bind a statically declared owner's finite statement catalog.

This helper owns no business tables and exposes no SQL or dynamic query builder.
Domain participants retain their own schema validation and cross-owner protocols.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from .definitions import RepositoryDefinition, StatementDefinition
from .service import PersistenceService, UnitOfWork
from .results import Found, NotFound, Staged, Failed
from .schema import Value


@dataclass(frozen=True, slots=True)
class StatementCatalog:
    """Named fixed declarations, constructed only by the owning module."""
    definition: RepositoryDefinition
    statements: tuple[tuple[str, StatementDefinition], ...]


class OwnerFailure(Exception):
    """Fixed internal business cause; never attaches a submitted value or exception."""
    def __init__(self, code: str, field: str, reason: str, cleanup_pending: bool = False):
        super().__init__()
        self.code, self.field, self.reason, self.cleanup_pending = code, field, reason, cleanup_pending


class BoundStatements:
    """Scope-bound named ports available only to their data owner."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, scope: str):
        self._ports = {name: storage.bind_statement(catalog.definition, statement, scope)
                       for name, statement in catalog.statements}

    def stage(self, name: str, uow: UnitOfWork, parameters: object) -> tuple[MappingProxyType[str, Value], ...]:
        """Participate in the caller's transaction; failure poisons the UoW in storage."""
        result = self._ports[name].participate(uow, parameters)
        if type(result) is not Staged or type(result.value) is not tuple:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'WRITE_NOT_COMMITTED')
        return cast(tuple[MappingProxyType[str, Value], ...], result.value)

    async def read(self, name: str, parameters: object) -> tuple[MappingProxyType[str, Value], ...]:
        """Use one bounded snapshot; absence is never evidence permitting a replay."""
        result = await self._ports[name].read_object(parameters)
        if type(result) is NotFound:
            return ()
        if type(result) is not Found or type(result.value) is not tuple:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'READ_FAILED', type(result) is Failed and result.error.cleanup_pending)
        return cast(tuple[MappingProxyType[str, Value], ...], result.value)


@dataclass(slots=True)
class OwnerCauseSlot:
    """An active owner's first safe domain cause, with no retained request body."""
    users: int = 0
    cause: OwnerFailure | None = None


class OwnerCauses:
    """Synchronize finite invocation-owned slots with the SQLite writer thread."""
    def __init__(self):
        from threading import RLock
        self.lock = RLock()
        self.slots: dict[str, OwnerCauseSlot] = {}

    @staticmethod
    def identity(kind: str, values) -> str:
        import hashlib
        from .content_codec import encode_content
        return hashlib.sha256(encode_content((kind, values), 1048576)).hexdigest()

    def watch(self, kind, values):
        key = self.identity(kind, values)
        with self.lock:
            slot = self.slots.setdefault(key, OwnerCauseSlot()); slot.users += 1
            return key, slot

    def record(self, kind, values, cause: OwnerFailure):
        with self.lock:
            slot = self.slots.get(self.identity(kind, values))
            if slot is not None and slot.cause is None:
                slot.cause = OwnerFailure(cause.code, cause.field, cause.reason, cause.cleanup_pending)

    def release(self, key):
        with self.lock:
            slot = self.slots[key]; slot.users -= 1
            if not slot.users: del self.slots[key]

    async def execute_original(self,port,definition,key,command):
        """Preserve an owner's safe cause only for its proved uncommitted command.

        The watcher lives through this command's actual completion, including a
        writer still running after logical timeout. Known receipts and unknown
        commit states are never replaced by the handler's provisional failure.
        """
        from .results import NotCommitted
        from .schema import freeze_value
        from .completion import CompletionScope
        prior=await port.resolve_operation(port.recovery_handle(key,command))
        if type(prior) is not NotCommitted or prior.error is not None:return prior,None
        from .deadlines import check_deadline
        check_deadline()
        watched,slot=self.watch(definition.operation_kind,freeze_value(definition.input_schema,command.values,owned=True))
        with CompletionScope() as completion:
            try:
                outcome=await port.execute(key,command)
                return outcome,slot.cause if type(outcome) is NotCommitted else None
            finally:
                await completion.wait()
                self.release(watched)
