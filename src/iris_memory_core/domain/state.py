"""StateRecord domain rules: namespace policies and write validation (§9.2).

State is high-frequency, overwrite-oriented CURRENT truth: every canonical
write creates an immutable revision and atomically advances the current
pointer (ADR-0004). Whether history is retained — and how much — is a
Namespace Policy decision, not a storage accident. Coalescing applies only to
the derived projection job; a canonical revision is never skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from iris_memory_core.domain.hashing import content_hash
from iris_memory_core.domain.scope import Scope

#: Hard ceiling independent of any policy: the SQLite CHECK enforces the same
#: number so oversized values fail identically at both layers.
MAX_STATE_VALUE_BYTES = 262_144

MAX_NAMESPACE_LENGTH = 128
MAX_KEY_LENGTH = 256


class SourceAuthority(StrEnum):
    HOST = "host"
    PLATFORM = "platform"
    ADAPTER = "adapter"
    SYSTEM = "system"
    USER = "user"
    MODEL = "model"


ALL_SOURCE_AUTHORITIES = frozenset(item.value for item in SourceAuthority)


class ScopeRequirement(StrEnum):
    ANY = "any"
    AGENT = "agent"
    SPACE = "space"
    SESSION = "session"


class InvalidStateWriteError(ValueError):
    """Raised when a write violates its namespace policy."""


class UnknownNamespaceError(InvalidStateWriteError):
    """The namespace has no policy and no default fallback."""


@dataclass(frozen=True, slots=True)
class StateNamespacePolicy:
    namespace: str
    default_ttl_us: int
    max_ttl_us: int
    retain_history: bool
    max_history_revisions: int
    max_value_bytes: int
    allowed_source_authorities: frozenset[str]
    required_scope: ScopeRequirement

    def __post_init__(self) -> None:
        if self.default_ttl_us < 0 or self.max_ttl_us < 0:
            raise ValueError("ttl values must be non-negative (0 = no expiry)")
        if self.max_ttl_us and self.default_ttl_us > self.max_ttl_us:
            raise ValueError("default ttl must not exceed the max ttl")
        if self.max_history_revisions < 0:
            raise ValueError("max_history_revisions must be non-negative (0 = current only)")
        if self.max_value_bytes < 1 or self.max_value_bytes > MAX_STATE_VALUE_BYTES:
            raise ValueError(f"max_value_bytes must be within 1..{MAX_STATE_VALUE_BYTES}")
        if not self.allowed_source_authorities:
            raise ValueError("at least one source authority must be allowed")


#: Built-in namespace defaults (§9.2 examples: game map, OBS scene, online
#: status, current topic, device mode). Tenant rows in ``state_namespace_policies``
#: override these; unknown namespaces fall back to the conservative default.
BUILTIN_STATE_POLICIES: dict[str, StateNamespacePolicy] = {
    "runtime": StateNamespacePolicy(
        namespace="runtime",
        default_ttl_us=300_000_000,
        max_ttl_us=3_600_000_000,
        retain_history=False,
        max_history_revisions=0,
        max_value_bytes=8_192,
        allowed_source_authorities=frozenset({"host", "adapter", "system"}),
        required_scope=ScopeRequirement.ANY,
    ),
    "environment": StateNamespacePolicy(
        namespace="environment",
        default_ttl_us=3_600_000_000,
        max_ttl_us=86_400_000_000,
        retain_history=True,
        max_history_revisions=10,
        max_value_bytes=32_768,
        allowed_source_authorities=frozenset({"host", "platform", "adapter", "system"}),
        required_scope=ScopeRequirement.ANY,
    ),
    "topic": StateNamespacePolicy(
        namespace="topic",
        default_ttl_us=1_800_000_000,
        max_ttl_us=21_600_000_000,
        retain_history=True,
        max_history_revisions=5,
        max_value_bytes=8_192,
        allowed_source_authorities=frozenset({"host", "model", "system", "user"}),
        required_scope=ScopeRequirement.AGENT,
    ),
}

DEFAULT_STATE_POLICY = StateNamespacePolicy(
    namespace="",
    default_ttl_us=3_600_000_000,
    max_ttl_us=86_400_000_000,
    retain_history=False,
    max_history_revisions=0,
    max_value_bytes=32_768,
    allowed_source_authorities=ALL_SOURCE_AUTHORITIES,
    required_scope=ScopeRequirement.ANY,
)


def resolve_namespace_policy(
    namespace: str, override: StateNamespacePolicy | None
) -> StateNamespacePolicy:
    if not namespace or len(namespace) > MAX_NAMESPACE_LENGTH:
        raise InvalidStateWriteError(f"namespace must be 1..{MAX_NAMESPACE_LENGTH} characters")
    if override is not None:
        return override
    return BUILTIN_STATE_POLICIES.get(namespace, DEFAULT_STATE_POLICY)


def state_scope_key(scope: Scope) -> str:
    """Canonical NULL-free identity of one state scope (see recent_target_key)."""
    return "|".join(
        (
            scope.tenant_id,
            scope.agent_id or "",
            scope.space_group_id or "",
            scope.space_id or "",
            scope.session_id or "",
        )
    )


@dataclass(frozen=True, slots=True)
class StateRevision:
    """One immutable revision row (§9.2)."""

    id: str
    record_id: str
    tenant_id: str
    revision: int
    value_json: str
    source_ref: str | None
    source_authority: str
    observed_us: int
    expires_us: int | None
    coalesce_key: str | None
    created_us: int


@dataclass(frozen=True, slots=True)
class StateRecord:
    """The current-pointer row for one ``(scope, namespace, key)``."""

    id: str
    tenant_id: str
    agent_id: str | None
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    namespace: str
    key: str
    current_revision: int
    current_revision_id: str
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class StateEntry:
    """A list projection: the current record joined with its current value."""

    record: StateRecord
    value_json: str
    source_authority: str
    observed_us: int
    expires_us: int | None

    @property
    def value(self) -> dict[str, object]:
        decoded = json.loads(self.value_json)
        return decoded if isinstance(decoded, dict) else {}


@dataclass(frozen=True, slots=True)
class StateWriteDraft:
    """One validated PUT before persistence."""

    scope: Scope
    namespace: str
    key: str
    value_json: str
    source_authority: SourceAuthority
    observed_us: int
    expires_us: int | None
    source_ref: str | None
    coalesce_key: str | None
    expected_revision: int | None

    def fingerprint(self) -> str:
        return content_hash(
            {
                "scope": self.scope.as_dict(),
                "namespace": self.namespace,
                "key": self.key,
                "value": self.value_json,
                "authority": self.source_authority.value,
                "observed_us": self.observed_us,
                "expires_us": self.expires_us,
                "source_ref": self.source_ref,
            }
        )


def validate_state_write(
    policy: StateNamespacePolicy,
    scope: Scope,
    namespace: str,
    key: str,
    value: object,
    *,
    source_authority: str,
    observed_us: int,
    ttl_us: int | None,
    expires_us: int | None,
    source_ref: str | None = None,
    coalesce_key: str | None = None,
    expected_revision: int | None = None,
) -> StateWriteDraft:
    """Validate one PUT against its namespace policy; raises before any write."""
    if not key or len(key) > MAX_KEY_LENGTH:
        raise InvalidStateWriteError(f"key must be 1..{MAX_KEY_LENGTH} characters")
    try:
        authority = SourceAuthority(source_authority)
    except ValueError:
        raise InvalidStateWriteError(f"unknown source authority: {source_authority!r}") from None
    if authority.value not in policy.allowed_source_authorities:
        raise InvalidStateWriteError(
            f"source authority {authority.value} is not allowed for namespace "
            f"{policy.namespace or namespace!r}"
        )
    requirement = policy.required_scope
    if requirement is ScopeRequirement.AGENT and scope.agent_id is None:
        raise InvalidStateWriteError(f"namespace {namespace} requires an agent scope")
    if requirement is ScopeRequirement.SPACE and scope.space_id is None:
        raise InvalidStateWriteError(f"namespace {namespace} requires a space scope")
    if requirement is ScopeRequirement.SESSION and (
        scope.space_id is None or scope.session_id is None
    ):
        raise InvalidStateWriteError(f"namespace {namespace} requires a session scope")
    if not isinstance(value, dict):
        raise InvalidStateWriteError("value must be a JSON object")
    value_json = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(value_json.encode("utf-8")) > policy.max_value_bytes:
        raise InvalidStateWriteError(
            f"value exceeds the namespace byte ceiling ({policy.max_value_bytes})"
        )
    effective_expiry: int | None
    if expires_us is not None:
        if expires_us <= observed_us:
            raise InvalidStateWriteError("expires_us must be after observed_us")
        effective_expiry = expires_us
    elif ttl_us is not None:
        if ttl_us < 0:
            raise InvalidStateWriteError("ttl_us must be non-negative")
        effective_expiry = observed_us + ttl_us if ttl_us > 0 else None
    elif policy.default_ttl_us > 0:
        effective_expiry = observed_us + policy.default_ttl_us
    else:
        effective_expiry = None
    if effective_expiry is not None and policy.max_ttl_us > 0:
        capped = observed_us + policy.max_ttl_us
        effective_expiry = min(effective_expiry, capped)
    if coalesce_key is not None and (not coalesce_key or len(coalesce_key) > 256):
        raise InvalidStateWriteError("coalesce_key must be 1..256 characters")
    if expected_revision is not None and expected_revision < 1:
        raise InvalidStateWriteError("expected_revision must be a positive revision")
    return StateWriteDraft(
        scope=scope,
        namespace=namespace,
        key=key,
        value_json=value_json,
        source_authority=authority,
        observed_us=observed_us,
        expires_us=effective_expiry,
        source_ref=source_ref,
        coalesce_key=coalesce_key,
        expected_revision=expected_revision,
    )
