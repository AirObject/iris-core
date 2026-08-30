"""Domain records for tenants, agents, spaces and identity aggregates.

Plain immutable dataclasses shared by application services and storage
adapters; persistence details never leak back into these types.
"""

from __future__ import annotations

import json
from dataclasses import MISSING, dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints

from iris_memory_core.domain.identity import (
    BindingMethod,
    BindingState,
    EntityKind,
    EntityState,
    FieldAuthority,
)

# Timestamps cross layers as Unix microseconds (§4.2); RFC 3339 strings are a
# projection concern owned by the future API layer.


@dataclass(frozen=True, slots=True)
class Tenant:
    id: str
    status: str
    created_us: int


@dataclass(frozen=True, slots=True)
class PersonaRevision:
    id: str
    tenant_id: str
    agent_id: str
    revision: int
    core: str
    traits: str
    narrative: str
    content_hash: str
    status: str
    source: str
    created_us: int


@dataclass(frozen=True, slots=True)
class Agent:
    id: str
    tenant_id: str
    display_name: str
    status: str
    persona_current_revision_id: str | None
    created_us: int


@dataclass(frozen=True, slots=True)
class SpaceGroup:
    id: str
    tenant_id: str
    name: str
    description: str
    primary_space_id: str | None
    status: str
    revision: int
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class Space:
    id: str
    tenant_id: str
    agent_id: str | None
    space_group_id: str | None
    kind: str
    status: str
    revision: int
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    tenant_id: str
    space_id: str
    status: str
    started_us: int
    ended_us: int | None


@dataclass(frozen=True, slots=True)
class SpaceGroupBinding:
    id: str
    tenant_id: str
    space_id: str
    space_group_id: str
    bound_us: int
    unbound_us: int | None
    bound_by: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    tenant_id: str
    kind: EntityKind
    state: EntityState
    display_name: str
    privacy_labels: tuple[str, ...]
    revision: int
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    id: str
    tenant_id: str
    provider: str
    realm: str
    external_id: str
    entity_id: str | None
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class Binding:
    id: str
    tenant_id: str
    external_identity_id: str
    entity_id: str
    state: BindingState
    method: BindingMethod
    confidence: float
    proof_digest: str
    valid_from_us: int
    valid_until_us: int | None
    revision: int
    created_us: int
    updated_us: int
    created_by: str
    confirmed_by: str | None
    revoked_by: str | None


@dataclass(frozen=True, slots=True)
class EntityRedirect:
    id: str
    tenant_id: str
    from_entity_id: str
    to_entity_id: str
    reason_code: str
    created_by: str
    created_us: int


@dataclass(frozen=True, slots=True)
class IdentityAttribute:
    id: str
    tenant_id: str
    entity_id: str
    field: str
    value: str
    authority: FieldAuthority
    source_ref: str
    effective_us: int
    recorded_us: int
    superseded_us: int | None
    status: str


@dataclass(frozen=True, slots=True)
class AttributeWrite:
    """Outcome of a field-level authority merge (§6.3)."""

    current: IdentityAttribute
    outcome: str  # MergeOutcome value: supersede | ignored | coexist
    changed: bool


@dataclass(frozen=True, slots=True)
class Tombstone:
    id: str
    tenant_id: str
    resource_type: str
    resource_id: str
    reason_code: str
    deleted_by: str
    created_us: int
    tombstone_seq: int


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    tenant_id: str
    actor: str
    action: str
    resource_type: str
    resource_id: str
    revision: int | None
    reason_code: str
    details: dict[str, object]
    created_us: int


@dataclass(frozen=True, slots=True)
class WatermarkState:
    tenant_id: str
    agent_id: str
    current_seq: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    tenant_id: str
    app_instance_id: str
    operation: str
    idempotency_key: str
    request_fingerprint: str
    status: str
    response_code: str | None
    response_body: str | None
    resource_refs: tuple[str, ...]
    transaction_ref: str | None
    owner_token: str | None
    created_us: int
    expires_us: int


@dataclass(frozen=True, slots=True)
class ResourceLink:
    id: str
    tenant_id: str
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    relation: str
    created_us: int


#: Minimal locked bootstrap persona written atomically with Agent creation
#: (ADR-0008). Content is deliberately tiny and stable; Phase 9 expands the
#: same revision/pointer contract without touching these ids or hashes.
BOOTSTRAP_PERSONA_SOURCE = "bootstrap"
BOOTSTRAP_PERSONA_STATUS = "published"
BOOTSTRAP_PERSONA_CORE = '{"language":"und","name_placeholder":true}'
BOOTSTRAP_PERSONA_TRAITS = "[]"
BOOTSTRAP_PERSONA_NARRATIVE = ""
BOOTSTRAP_PERSONA_REVISION = 1


@dataclass(frozen=True, slots=True)
class IdempotentResult:
    """Outcome of an idempotent execution; replayed results keep the first body."""

    code: str
    body: str
    resource_refs: tuple[str, ...]
    replayed: bool


# -- idempotent replay snapshots ------------------------------------------------
#
# Replay must return the FIRST outcome (§20.5), not a re-read of the current
# aggregate. Services serialize the resulting record into the idempotency
# outcome body and restore it on replay; these helpers do that generically for
# the frozen dataclasses above.


def record_snapshot(record: Any) -> dict[str, Any]:
    """Convert one domain record into a JSON-safe dict (enums to values)."""
    if record is None:
        return {}
    out: dict[str, Any] = {}
    for field in fields(record):
        value = getattr(record, field.name)
        if isinstance(value, Enum):
            out[field.name] = value.value
        elif isinstance(value, tuple):
            out[field.name] = [item.value if isinstance(item, Enum) else item for item in value]
        elif is_dataclass(value) and not isinstance(value, type):
            out[field.name] = record_snapshot(value)
        else:
            out[field.name] = value
    return out


#: Idempotent-replay snapshots carry a format version so future shape changes
#: can migrate old outcome bodies instead of misreading them.
SNAPSHOT_FORMAT_VERSION = 1


def snapshot_json(record: Any) -> str:
    return json.dumps(
        {"snapshot_version": SNAPSHOT_FORMAT_VERSION, "record": record_snapshot(record)},
        ensure_ascii=False,
        sort_keys=True,
    )


def _coerce(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if isinstance(annotation, type) and is_dataclass(annotation):
        return record_restore(annotation, value)
    origin = get_origin(annotation)
    if origin is tuple:
        item_type = get_args(annotation)[0] if get_args(annotation) else str
        return tuple(_coerce(item_type, item) for item in value)
    return value


def record_restore(record_type: type[Any], data: object) -> Any:
    """Rebuild a domain record from a :func:`snapshot_json` body.

    Forward-compatible by contract: unknown keys (fields added after the
    snapshot was taken) are ignored, and missing keys fall back to the field's
    declared default when one exists. Because of this, NEW RECORD FIELDS MUST
    SHIP WITH DEFAULTS or old idempotency outcomes stop replaying. A missing
    key without a default raises ``KeyError`` naming the field. Enveloped
    snapshots must use the exact supported format version; unknown versions
    are rejected instead of being silently decoded with v1 semantics.
    """
    if not isinstance(data, dict):
        raise ValueError("snapshot must be a JSON object")
    if "record" in data or "snapshot_version" in data:
        version = data.get("snapshot_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != SNAPSHOT_FORMAT_VERSION
        ):
            raise ValueError(
                f"unsupported idempotency snapshot version {version!r}; "
                f"expected {SNAPSHOT_FORMAT_VERSION}"
            )
        payload = data.get("record")
        if not isinstance(payload, dict):
            raise ValueError("idempotency snapshot record must be a JSON object")
        data = payload
    hints = get_type_hints(record_type)
    kwargs: dict[str, Any] = {}
    for field in fields(record_type):
        if field.name in data:
            kwargs[field.name] = _coerce(hints[field.name], data[field.name])
        elif field.default is not MISSING or field.default_factory is not MISSING:
            kwargs[field.name] = (
                field.default_factory() if field.default_factory is not MISSING else field.default
            )
        else:
            raise KeyError(
                f"snapshot for {record_type.__name__} lacks required field {field.name!r}; "
                "new record fields must ship with defaults to keep old outcomes replayable"
            )
    return record_type(**kwargs)
