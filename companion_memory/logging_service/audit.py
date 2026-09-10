"""Restricted same-transaction audit access, independent of runtime diagnostics.

Trusted assembly binds ready storage and checked configuration. Module writers
hold one fixed event slot, developers get scope-bound point reads, and only the
transaction coordinator checks completion. Every mandatory write/check failure
poisons its active transaction; staged records never mean committed success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from companion_memory.configuration import EffectiveSnapshot, PresentValue, persistence_snapshot_issue
from companion_memory.persistence.results import Failed, Found, NotFound, PersistenceError
from companion_memory.persistence.schema import InvalidValue
from .audit_records import (
    AuditCode, AuditComplete, AuditErr, AuditError, AuditField, AuditFound, AuditNotFound,
    AuditOperation, AuditReason, AuditStaged, freeze_audit_event,
)

if TYPE_CHECKING:
    from companion_memory.persistence.service import AuditStorageBinding


@dataclass(frozen=True, slots=True)
class AuditBound:
    """A ready restricted audit capability, with no diagnostic service created."""

    value: AuditAccess


class AuditAccess:
    """Storage-bound audit capability. Knowing an operation key grants no access."""

    __slots__ = ("_binding", "_event_limit")

    def __init__(self, binding: AuditStorageBinding, event_limit: int):
        from companion_memory.persistence.service import AuditStorageBinding
        if (type(binding) is not AuditStorageBinding or not binding.is_issued()
                or type(event_limit) is not int or not 1 <= event_limit <= 256):
            raise TypeError("Audit access requires an issued binding and a checked limit.")
        self._binding, self._event_limit = binding, event_limit

    @classmethod
    def for_coordinator(cls, binding: AuditStorageBinding, event_limit: int) -> AuditAccess:
        """Build a verifier from storage's issued active coordinator capability."""
        return cls(binding, event_limit)

    def _failure(self, operation: AuditOperation, code: AuditCode, reason: AuditReason,
                 field: AuditField, *, pending: bool = False) -> AuditErr:
        result = AuditErr(AuditError(code, operation, field, reason, pending))
        if operation in ("append_audit", "check_required_audits"):
            self._binding.poison(PersistenceError(
                "TRANSACTION_FAILED", "execute", "audit", "AUDIT_REQUIRED" if reason == "AUDIT_REQUIRED" else "AUDIT_FAILED", pending,
            ))
        return result

    def _storage_failure(self, operation: AuditOperation, failure: Failed) -> AuditErr:
        error = failure.error
        if error.code == "ACCESS_DENIED":
            return self._failure(operation, "ACCESS_DENIED", "AUDIT_ACCESS_DENIED", "capability", pending=error.cleanup_pending)
        if error.code == "INVALID_STATE":
            return self._failure(operation, "INVALID_STATE", "AUDIT_STATE_INVALID", "state", pending=error.cleanup_pending)
        if error.code == "INTEGRITY_FAILURE":
            return self._failure(operation, "INTEGRITY_FAILURE", "AUDIT_INCONSISTENT",
                                 "query" if operation == "read_audit" else "transaction", pending=error.cleanup_pending)
        if operation == "append_audit" and error.reason == "LIMIT_EXCEEDED":
            return self._failure(operation, "INVALID_INPUT", "AUDIT_LIMIT_EXCEEDED", "event", pending=error.cleanup_pending)
        if operation == "append_audit" and error.reason == "CONSTRAINT_FAILED" and error.field == "audit":
            return self._failure(operation, "AUDIT_CONFLICT", "AUDIT_EVENT_CONFLICT", "event", pending=error.cleanup_pending)
        if operation == "read_audit" and error.code == "INVALID_INPUT":
            return self._failure(operation, "INVALID_INPUT", "AUDIT_INPUT_INVALID", "query", pending=error.cleanup_pending)
        return self._failure(operation, "AUDIT_FAILED", "AUDIT_READ_FAILED" if operation == "read_audit" else "AUDIT_WRITE_FAILED",
                             "query" if operation == "read_audit" else "transaction", pending=error.cleanup_pending)

    def append_audit(self, uow: object, event: object) -> AuditStaged | AuditErr:
        """Validate exact event fields, then append its bound slot in the same UoW.

        Event data must match the frozen command intention and follow a legal
        module change. Duplicate, unknown or invalid events fail the entire UoW.
        Returned IDs/slots are staged associations, not a persistence receipt.
        """
        if not self._binding.state_valid():
            return self._failure("append_audit", "INVALID_STATE", "AUDIT_STATE_INVALID", "state")
        if self._binding.uow_ended(uow):
            return self._failure("append_audit", "INVALID_STATE", "AUDIT_STATE_INVALID", "state")
        if not self._binding.permits(uow):
            return self._failure("append_audit", "ACCESS_DENIED", "AUDIT_ACCESS_DENIED", "capability")
        requirement = self._binding.requirement()
        assert requirement is not None
        try:
            owned = freeze_audit_event(requirement, event)
        except (InvalidValue, RecursionError):
            return self._failure("append_audit", "INVALID_INPUT", "AUDIT_INPUT_INVALID", "event")
        result = self._binding.stage(uow, owned)  # type: ignore[arg-type] -- permits checked the exact active unit-of-work capability.
        return self._storage_failure("append_audit", result) if isinstance(result, Failed) else result

    def check_required_audits(self, uow: object) -> AuditComplete | AuditErr:
        """Coordinator-only check of persisted mandatory metadata and every slot."""
        if not self._binding.state_valid():
            return self._failure("check_required_audits", "INVALID_STATE", "AUDIT_STATE_INVALID", "state")
        if self._binding.uow_ended(uow):
            return self._failure("check_required_audits", "INVALID_STATE", "AUDIT_STATE_INVALID", "state")
        if not self._binding.permits(uow, coordinator=True):
            return self._failure("check_required_audits", "ACCESS_DENIED", "AUDIT_ACCESS_DENIED", "capability")
        result = self._binding.required(uow)  # type: ignore[arg-type] -- permits checked the exact active unit-of-work capability.
        if isinstance(result, Failed):
            return self._storage_failure("check_required_audits", result)
        required, records = result
        if len(records) > self._event_limit:
            return self._failure("check_required_audits", "INVALID_INPUT", "AUDIT_LIMIT_EXCEEDED", "event")
        expected = {item.event_slot for item in required}
        actual = {item.event_slot for item in records}
        if actual - expected or len(actual) != len(records):
            return self._failure("check_required_audits", "AUDIT_CONFLICT", "AUDIT_EVENT_CONFLICT", "event")
        if expected != actual:
            return self._failure("check_required_audits", "AUDIT_INCOMPLETE", "AUDIT_REQUIRED", "transaction")
        return AuditComplete()

    async def read_audit(self, operation_identity: object) -> AuditFound | AuditNotFound | AuditErr:
        """Read one complete historical operation for a developer-bound scope.

        Permission is checked before the query, and every row is linked to a
        validated receipt and manifest. No broad listing, SQL, pagination or
        content forwarding into agent context/diagnostics is offered.
        """
        if not self._binding.state_valid():
            return self._failure("read_audit", "INVALID_STATE", "AUDIT_STATE_INVALID", "state")
        if not self._binding.permits_read():
            return self._failure("read_audit", "ACCESS_DENIED", "AUDIT_ACCESS_DENIED", "capability")
        result = await self._binding.read(operation_identity)
        if type(result) is Failed:
            return self._storage_failure("read_audit", result)
        if type(result) is Found:
            return AuditFound(result.value)
        assert type(result) is NotFound
        return AuditNotFound()


def bind_audit(snapshot: object, storage_binding: object) -> AuditBound | AuditErr:
    """Bind exact issued ready storage and applicable audit configuration in memory.

    Capability validity precedes readiness and snapshot checks. No I/O, logger,
    schema registration, role-string authorization or implicit resolution occurs.
    """
    from companion_memory.persistence.service import AuditStorageBinding

    def failed(code: AuditCode, reason: AuditReason, field: AuditField) -> AuditErr:
        return AuditErr(AuditError(code, "bind_audit", field, reason))

    if type(storage_binding) is not AuditStorageBinding or not storage_binding.is_issued():
        return failed("ACCESS_DENIED", "AUDIT_ACCESS_DENIED", "capability")
    if not storage_binding.state_valid():
        return failed("INVALID_STATE", "AUDIT_STATE_INVALID", "state")
    if persistence_snapshot_issue(snapshot, audit_only=True) is not None or not storage_binding.matches_snapshot(snapshot):
        return failed("CONFIGURATION_UNSUPPORTED", "AUDIT_CONFIGURATION_UNSUPPORTED", "configuration")
    assert type(snapshot) is EffectiveSnapshot
    values = {entry.definition.key: entry.state.value for entry in snapshot.list_entries() if type(entry.state) is PresentValue}
    limit = values["audit.events_per_operation"]
    assert type(limit) is int
    return AuditBound(AuditAccess(storage_binding, limit))
