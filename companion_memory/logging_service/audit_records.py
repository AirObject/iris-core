"""Same-transaction audit schema, immutable records and safe error protocol.

Trusted modules declare finite event schemas and mandatory slots. Event inputs
contain controlled identity/revision/change fields only; they are unrelated to
diagnostic levels, queues, messages or sinks. No audit text enters diagnostics.
"""

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from companion_memory.persistence.results import OperationIdentity
from companion_memory.persistence.schema import (
    Field, InvalidValue, RecordSchema, ScalarSchema, SequenceSchema, Value,
    encode_value, freeze_value, utc_text, valid_identifier,
)

type AuditOperation = Literal["bind_audit", "append_audit", "check_required_audits", "read_audit"]
type AuditCode = Literal["INVALID_INPUT", "ACCESS_DENIED", "INVALID_STATE", "CONFIGURATION_UNSUPPORTED", "AUDIT_CONFLICT", "AUDIT_INCOMPLETE", "AUDIT_FAILED", "INTEGRITY_FAILURE"]
type AuditField = Literal["state", "capability", "configuration", "event", "transaction", "query"]
type AuditReason = Literal[
    "AUDIT_INPUT_INVALID", "AUDIT_LIMIT_EXCEEDED", "AUDIT_ACCESS_DENIED", "AUDIT_STATE_INVALID",
    "AUDIT_CONFIGURATION_UNSUPPORTED", "AUDIT_EVENT_CONFLICT", "AUDIT_REQUIRED",
    "AUDIT_WRITE_FAILED", "AUDIT_READ_FAILED", "AUDIT_INCONSISTENT",
]


@dataclass(frozen=True, slots=True)
class AuditError:
    """Fixed cause without raw input, SQL, exceptions, paths or audit content."""

    code: AuditCode
    operation: AuditOperation
    field: AuditField
    reason: AuditReason
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class AuditErr:
    """Safe audit failure; the transaction coordinator decides commit evidence."""

    error: AuditError


@dataclass(frozen=True, slots=True)
class AuditRequirement:
    """One statically bound mandatory event with a finite structured change schema."""

    owner_module: str
    event_slot: str
    event_code: str
    event_version: int
    reason_codes: tuple[str, ...]
    change_schema: RecordSchema
    target_limit: int = 16

    def __post_init__(self) -> None:
        # Audit values intentionally remain narrower than repository text values.
        def permitted(schema: object) -> bool:
            if type(schema) is ScalarSchema:
                return schema.kind in ("boolean", "integer", "enum", "identifier")
            if type(schema) is SequenceSchema:
                return permitted(schema.item)
            if type(schema) is RecordSchema:
                return all(type(field) is Field and permitted(field.schema) for field in schema.fields)
            return False
        if not permitted(self.change_schema):
            raise ValueError("Audit changes require bounded non-text structured fields.")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Immutable historical event linked to the exact committed operation."""

    schema_version: int
    audit_id: str
    database_id: str
    commit_id: str
    operation_identity: OperationIdentity
    event_slot: str
    recorded_at: str
    owner_module: str
    event_code: str
    event_version: int
    actor_kind: str
    actor_ref: str
    reason_code: str
    target_refs: Value
    change: Value


@dataclass(frozen=True, slots=True)
class AuditStaged:
    """A slot was appended inside the transaction; persistence is unconfirmed."""

    event_slot: str
    audit_id: str


@dataclass(frozen=True, slots=True)
class AuditComplete:
    """Required events match inside the active transaction, before commit."""


@dataclass(frozen=True, slots=True)
class AuditFound:
    """One operation's complete immutable audit records in stable slot order."""

    records: tuple[AuditRecord, ...]


@dataclass(frozen=True, slots=True)
class AuditNotFound:
    """One committed read snapshot missed; this does not permit replay."""


def audit_event_schema(requirement: AuditRequirement) -> RecordSchema:
    """Construct the fixed event envelope around a trusted module change schema."""
    return RecordSchema((
        Field("event_version", ScalarSchema("integer", requirement.event_version, requirement.event_version)),
        Field("actor_kind", ScalarSchema("enum", choices=("SYSTEM", "OPERATOR"))),
        Field("actor_ref", ScalarSchema("identifier")),
        Field("reason_code", ScalarSchema("enum", choices=requirement.reason_codes)),
        Field("target_refs", SequenceSchema(RecordSchema((
            Field("object_id", ScalarSchema("identifier")),
            Field("previous_revision", ScalarSchema("integer"), nullable=True),
            Field("revision", ScalarSchema("integer")),
        )), 1, requirement.target_limit)),
        Field("change", requirement.change_schema),
    ))


def freeze_audit_event(requirement: AuditRequirement, event: object, *, owned: bool = False) -> MappingProxyType[str, Value]:
    """Reject all unknown fields and isolate the complete bounded event body."""
    return cast(MappingProxyType[str, Value], freeze_value(audit_event_schema(requirement), event, owned=owned))


def create_audit_record(requirement: AuditRequirement, identity: OperationIdentity, commit_id: str,
                        event: MappingProxyType[str, Value], new_id: Callable[[], object],
                        utc_now: Callable[[], object]) -> AuditRecord:
    """Logging owns audit IDs and record construction using injected sources.

    Storage supplies only its active UoW association and the complete previously
    validated event; this function cannot open a connection or commit a record.
    """
    audit_id = new_id()
    if not valid_identifier(audit_id):
        raise InvalidValue()
    return AuditRecord(
        1, cast(str, audit_id), identity.database_id, commit_id, identity,
        requirement.event_slot, utc_text(utc_now()), requirement.owner_module,
        requirement.event_code, requirement.event_version, cast(str, event["actor_kind"]),
        cast(str, event["actor_ref"]), cast(str, event["reason_code"]), event["target_refs"], event["change"],
    )


def audit_manifest(requirements: tuple[AuditRequirement, ...]) -> Value:
    """Protected mandatory-slot metadata stored beside each original receipt."""
    return tuple(MappingProxyType({
        "owner_module": item.owner_module, "event_slot": item.event_slot,
        "event_code": item.event_code, "event_version": item.event_version,
    }) for item in sorted(requirements, key=lambda item: item.event_slot))


def audit_record_value(record: AuditRecord) -> Value:
    """Encode only the owned audit fields, without a custom serialization hook."""
    identity = record.operation_identity
    return MappingProxyType({
        "schema_version": record.schema_version, "audit_id": record.audit_id,
        "database_id": record.database_id, "commit_id": record.commit_id,
        "operation_identity": MappingProxyType({
            "database_id": identity.database_id, "owner_namespace": identity.owner_namespace,
            "operation_kind": identity.operation_kind, "scope_id": identity.scope_id,
            "operation_key": identity.operation_key,
        }), "event_slot": record.event_slot, "recorded_at": record.recorded_at,
        "owner_module": record.owner_module, "event_code": record.event_code,
        "event_version": record.event_version, "actor_kind": record.actor_kind,
        "actor_ref": record.actor_ref, "reason_code": record.reason_code,
        "target_refs": record.target_refs, "change": record.change,
    })


def validate_audit_record(record: AuditRecord, requirement: AuditRequirement, identity: OperationIdentity, commit_id: str) -> bytes:
    """Check history against its declared mandatory slot and format size ceiling."""
    if (type(record) is not AuditRecord or type(record.schema_version) is not int or record.schema_version != 1
            or record.operation_identity != identity or record.database_id != identity.database_id
            or record.commit_id != commit_id or record.event_slot != requirement.event_slot
            or record.owner_module != requirement.owner_module or record.event_code != requirement.event_code
            or record.event_version != requirement.event_version or not valid_identifier(record.audit_id)):
        raise InvalidValue()
    freeze_audit_event(requirement, {
        "event_version": record.event_version, "actor_kind": record.actor_kind,
        "actor_ref": record.actor_ref, "reason_code": record.reason_code,
        "target_refs": record.target_refs, "change": record.change,
    }, owned=True)
    return encode_value(audit_record_value(record), 65536)
