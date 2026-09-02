"""Long-term memory domain rules: Episode, Claim, Evidence, Relation, Artifact (§13).

Pure logic only: status machines, bi-temporal visibility, dedup identity,
procedure-claim safety and artifact locator defense. Persistence lives in the
storage adapter; nothing here imports a framework.

Bi-temporal contract (§13.2, §19):

- Valid time (``valid_from_us``/``valid_until_us``) is the business validity
  window the caller declares; ``None`` means unbounded on that side.
- System time lives on immutable claim revisions: ``recorded_at_us`` is set
  when the revision becomes current; ``superseded_at_us`` is stamped exactly
  once by the transaction that installs the successor revision. Revision
  CONTENT never changes — the single write-once system-time end stamp is part
  of the append-only discipline (ADR-0004), not an in-place edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from iris_memory_core.domain.errors import InvalidRequestError
from iris_memory_core.domain.hashing import content_hash, request_fingerprint

MAX_CLAIM_PREDICATE_CHARS = 200
MAX_CLAIM_CANONICAL_TEXT_CHARS = 20_000
MAX_CLAIM_VALUE_JSON_BYTES = 65_536
MAX_EPISODE_SUMMARY_CHARS = 20_000
MAX_EPISODE_TITLE_CHARS = 500
MAX_EVIDENCE_SPAN_CHARS = 4_000
MAX_RELATION_TYPE_CHARS = 200
MAX_INLINE_ARTIFACT_BYTES = 262_144  # 256 KiB: inline stays a row, not a blob file
MAX_LOCAL_ARTIFACT_BYTES = 64 * 1024 * 1024  # 64 MiB per local blob
MAX_ARTIFACT_MEDIA_TYPE_CHARS = 255


class InvalidClaimError(InvalidRequestError):
    """Raised when a claim violates the §13.2 contract."""


class InvalidEvidenceError(InvalidRequestError):
    """Raised when evidence fails the strict source-ref admission rules."""

    code = "evidence_invalid"


class EvidenceRequiredError(InvalidRequestError):
    """An active claim must keep at least one currently valid evidence row."""

    code = "evidence_required"


class SubjectAmbiguousError(InvalidRequestError):
    """Subject omission outside the contracted self/current-actor scenario."""

    code = "subject_ambiguous"


class UnsafeProcedureClaimError(InvalidRequestError):
    """Procedure claims carry declarative data, never executable content."""

    code = "unsafe_procedure_claim"


class ArtifactInvalidError(InvalidRequestError):
    """Artifact admission failure (locator, size, media type or hash)."""

    code = "artifact_invalid"


class ClaimCategory(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    FACT = "fact"
    COMMUNITY = "community"
    PROCEDURE = "procedure"
    SELF_NARRATIVE = "self_narrative"


ALL_CLAIM_CATEGORIES = frozenset(item.value for item in ClaimCategory)


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    TOMBSTONED = "tombstoned"


#: §13.2 lifecycle. ``superseded``/``retracted``/``expired``/``archived`` are
#: end states for the current pointer (content stays queryable as history);
#: ``tombstoned`` is reachable ONLY through the Forget ledger (ADR-0005),
#: never through a status transition. ``disputed`` can resolve back to
#: ``active`` when the conflict is settled by a correcting revision.
CLAIM_TRANSITIONS: dict[str, frozenset[str]] = {
    ClaimStatus.ACTIVE.value: frozenset(
        {
            ClaimStatus.DISPUTED.value,
            ClaimStatus.SUPERSEDED.value,
            ClaimStatus.RETRACTED.value,
            ClaimStatus.EXPIRED.value,
            ClaimStatus.ARCHIVED.value,
        }
    ),
    ClaimStatus.DISPUTED.value: frozenset(
        {
            ClaimStatus.ACTIVE.value,
            ClaimStatus.SUPERSEDED.value,
            ClaimStatus.RETRACTED.value,
            ClaimStatus.ARCHIVED.value,
        }
    ),
    ClaimStatus.ARCHIVED.value: frozenset({ClaimStatus.ACTIVE.value, ClaimStatus.RETRACTED.value}),
    ClaimStatus.SUPERSEDED.value: frozenset(),
    ClaimStatus.RETRACTED.value: frozenset(),
    ClaimStatus.EXPIRED.value: frozenset(),
    ClaimStatus.TOMBSTONED.value: frozenset(),
}

#: Statuses whose current revision participates in canonical reads (search,
#: recall rehydrate, promotion). Archived content exists for history only.
CLAIM_CURRENT_VISIBLE_STATUSES = (ClaimStatus.ACTIVE.value, ClaimStatus.DISPUTED.value)


class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CORRECTS = "corrects"


ALL_EVIDENCE_RELATIONS = frozenset(item.value for item in EvidenceRelation)

#: Resource types admissible as a claim evidence SourceRef. Each type has a
#: canonical validator in the application layer; admission is decided by the
#: referenced resource's own scope/privacy/status/tombstone state.
EVIDENCE_SOURCE_TYPES = frozenset({"observation", "artifact", "episode", "claim", "note"})


class SourceAuthority(StrEnum):
    """§13.2 source authority ladder; higher rank outranks model inference."""

    AGENT_INFERENCE = "agent_inference"
    EXTRACTED = "extracted"
    USER_STATEMENT = "user_statement"
    PLATFORM_VERIFIED = "platform_verified"
    ADMIN_CONFIRMED = "admin_confirmed"
    EXPLICIT_CORRECTION = "explicit_correction"


SOURCE_AUTHORITY_RANK: dict[SourceAuthority, int] = {
    SourceAuthority.AGENT_INFERENCE: 10,
    SourceAuthority.EXTRACTED: 20,
    SourceAuthority.USER_STATEMENT: 30,
    SourceAuthority.PLATFORM_VERIFIED: 40,
    SourceAuthority.ADMIN_CONFIRMED: 50,
    SourceAuthority.EXPLICIT_CORRECTION: 60,
}


class EpisodeStatus(StrEnum):
    OPEN = "open"
    SEALED = "sealed"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    TOMBSTONED = "tombstoned"


EPISODE_TRANSITIONS: dict[str, frozenset[str]] = {
    EpisodeStatus.OPEN.value: frozenset(
        {EpisodeStatus.SEALED.value, EpisodeStatus.SUPERSEDED.value, EpisodeStatus.ARCHIVED.value}
    ),
    EpisodeStatus.SEALED.value: frozenset(
        {EpisodeStatus.SUPERSEDED.value, EpisodeStatus.ARCHIVED.value}
    ),
    EpisodeStatus.ARCHIVED.value: frozenset({EpisodeStatus.OPEN.value}),
    EpisodeStatus.SUPERSEDED.value: frozenset(),
    EpisodeStatus.TOMBSTONED.value: frozenset(),
}


class RelationStatus(StrEnum):
    ACTIVE = "active"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    ARCHIVED = "archived"
    TOMBSTONED = "tombstoned"


RELATION_TRANSITIONS: dict[str, frozenset[str]] = {
    RelationStatus.ACTIVE.value: frozenset(
        {
            RelationStatus.DISPUTED.value,
            RelationStatus.SUPERSEDED.value,
            RelationStatus.RETRACTED.value,
            RelationStatus.ARCHIVED.value,
        }
    ),
    RelationStatus.DISPUTED.value: frozenset(
        {
            RelationStatus.ACTIVE.value,
            RelationStatus.SUPERSEDED.value,
            RelationStatus.RETRACTED.value,
            RelationStatus.ARCHIVED.value,
        }
    ),
    RelationStatus.ARCHIVED.value: frozenset({RelationStatus.ACTIVE.value}),
    RelationStatus.SUPERSEDED.value: frozenset(),
    RelationStatus.RETRACTED.value: frozenset(),
    RelationStatus.TOMBSTONED.value: frozenset(),
}


class ArtifactStorageKind(StrEnum):
    INLINE = "inline"
    LOCAL_BLOB = "local_blob"
    EXTERNAL_REF = "external_ref"


class ArtifactStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    TOMBSTONED = "tombstoned"


ARTIFACT_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    ArtifactStatus.ACTIVE.value: frozenset({ArtifactStatus.ARCHIVED.value}),
    ArtifactStatus.ARCHIVED.value: frozenset({ArtifactStatus.ACTIVE.value}),
    ArtifactStatus.TOMBSTONED.value: frozenset(),
}

#: Media types admitted by default. The allowlist is deliberately narrow for
#: Phase 5; widening is a policy decision recorded in the Phase 5 ADR.
DEFAULT_ALLOWED_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/json",
        "image/png",
        "image/jpeg",
        "image/webp",
        "audio/ogg",
        "application/octet-stream",
    }
)


def validate_claim_transition(current: str, target: str) -> None:
    if current not in CLAIM_TRANSITIONS:
        raise InvalidClaimError(f"unknown claim status: {current!r}")
    if target not in CLAIM_TRANSITIONS:
        raise InvalidClaimError(f"unknown claim status: {target!r}")
    if target not in CLAIM_TRANSITIONS[current]:
        raise InvalidClaimError(f"claim cannot transition from {current!r} to {target!r}")


def validate_episode_transition(current: str, target: str) -> None:
    if current not in EPISODE_TRANSITIONS:
        raise InvalidClaimError(f"unknown episode status: {current!r}")
    if target not in EPISODE_TRANSITIONS:
        raise InvalidClaimError(f"unknown episode status: {target!r}")
    if target not in EPISODE_TRANSITIONS[current]:
        raise InvalidClaimError(f"episode cannot transition from {current!r} to {target!r}")


def validate_relation_transition(current: str, target: str) -> None:
    if current not in RELATION_TRANSITIONS:
        raise InvalidClaimError(f"unknown relation status: {current!r}")
    if target not in RELATION_TRANSITIONS:
        raise InvalidClaimError(f"unknown relation status: {target!r}")
    if target not in RELATION_TRANSITIONS[current]:
        raise InvalidClaimError(f"relation cannot transition from {current!r} to {target!r}")


def validate_score(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise InvalidClaimError(f"{name} must be within [0, 1]")


def validate_claim_value(
    *,
    predicate: str,
    value_json: str,
    canonical_text: str,
    category: str,
    confidence: float,
    importance: float,
    accessibility: float,
    valid_from_us: int | None,
    valid_until_us: int | None,
) -> None:
    if not predicate or len(predicate) > MAX_CLAIM_PREDICATE_CHARS:
        raise InvalidClaimError(f"predicate must be 1..{MAX_CLAIM_PREDICATE_CHARS} characters")
    if category not in ALL_CLAIM_CATEGORIES:
        raise InvalidClaimError(f"unknown claim category: {category!r}")
    if len(value_json) > MAX_CLAIM_VALUE_JSON_BYTES:
        raise InvalidClaimError(f"value_json must be at most {MAX_CLAIM_VALUE_JSON_BYTES} bytes")
    if not canonical_text or len(canonical_text) > MAX_CLAIM_CANONICAL_TEXT_CHARS:
        raise InvalidClaimError(
            f"canonical_text must be 1..{MAX_CLAIM_CANONICAL_TEXT_CHARS} characters"
        )
    validate_score("confidence", confidence)
    validate_score("importance", importance)
    validate_score("accessibility", accessibility)
    if valid_from_us is not None and valid_until_us is not None and valid_until_us <= valid_from_us:
        raise InvalidClaimError("valid_until_us must be strictly after valid_from_us")


#: Keys that make a procedure value executable rather than declarative. The
#: guard is structural: presence of any of these keys rejects the write.
_PROCEDURE_FORBIDDEN_KEYS = frozenset(
    {
        "exec",
        "execute",
        "code",
        "script",
        "shell",
        "bash",
        "sh",
        "command",
        "cmd",
        "sql",
        "query",
        "eval",
        "python",
        "javascript",
        "binary",
        "url_fetch",
        "download",
    }
)

#: Statements that look like runnable shell/SQL regardless of the key they
#: sit under. Prefix match keeps the check deterministic and reviewable.
_UNSAFE_VALUE_PREFIXES = (
    "#!",
    "select ",
    "insert ",
    "update ",
    "delete from ",
    "drop ",
    "create table ",
    "alter table ",
    "rm -",
    "curl ",
    "wget ",
)

_PROCEDURE_ALLOWED_TOP_KEYS = frozenset(
    {"name", "description", "steps", "preferences", "tool", "notes"}
)
_PROCEDURE_STEP_KEYS = frozenset({"action", "description", "params", "ordinal"})


def _procedure_value_is_safe(value: object) -> None:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if len(lowered) < 512:
            for prefix in _UNSAFE_VALUE_PREFIXES:
                if lowered.startswith(prefix):
                    raise UnsafeProcedureClaimError(
                        f"procedure claim value contains executable-looking content "
                        f"(prefix {prefix.strip()!r})"
                    )
        return
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _procedure_value_is_safe(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UnsafeProcedureClaimError("procedure claim keys must be strings")
            if key.lower() in _PROCEDURE_FORBIDDEN_KEYS:
                raise UnsafeProcedureClaimError(f"procedure claim forbids executable key {key!r}")
            _procedure_value_is_safe(item)
        return
    raise UnsafeProcedureClaimError(
        f"procedure claim values must be JSON primitives, lists or objects "
        f"(got {type(value).__name__})"
    )


def validate_procedure_value(value: object) -> None:
    """Procedure claims store declarative steps/preferences only (§13.4).

    Rejected: executable keys (shell/sql/code/...), byte sequences, and any
    string that opens like a runnable shell or SQL statement. Hosts interpret
    declared steps under their own tool policy — Core never stores the code.
    """
    if not isinstance(value, dict):
        raise UnsafeProcedureClaimError("procedure claim value must be a JSON object")
    unknown = set(value) - _PROCEDURE_ALLOWED_TOP_KEYS
    if unknown:
        raise UnsafeProcedureClaimError(
            f"procedure claim top-level keys must be within {sorted(_PROCEDURE_ALLOWED_TOP_KEYS)}"
        )
    if "steps" in value:
        steps = value["steps"]
        if not isinstance(steps, list) or not steps:
            raise UnsafeProcedureClaimError("procedure steps must be a non-empty list")
        for step in steps:
            if not isinstance(step, dict):
                raise UnsafeProcedureClaimError("each procedure step must be an object")
            unknown_step = set(step) - _PROCEDURE_STEP_KEYS
            if unknown_step:
                raise UnsafeProcedureClaimError(
                    f"procedure step keys must be within {sorted(_PROCEDURE_STEP_KEYS)}"
                )
            if not isinstance(step.get("action"), str) or not step["action"]:
                raise UnsafeProcedureClaimError("each procedure step needs an action string")
    _procedure_value_is_safe(value)


def claim_value_hash(*, predicate: str, value_json: str, canonical_text: str) -> str:
    return content_hash(
        {"predicate": predicate, "value_json": value_json, "canonical_text": canonical_text}
    )


def claim_dedup_key(
    *,
    tenant_id: str,
    agent_id: str,
    subject_entity_id: str,
    predicate: str,
    value_hash: str,
    scope_key: str,
) -> str:
    """Exact dedup identity (§13.2): tenant, agent, subject, predicate,
    canonical value hash and scope. Text/vector similarity can only propose
    candidates — never merge claims that differ in any component."""
    return content_hash(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "subject_entity_id": subject_entity_id,
            "predicate": predicate,
            "value_hash": value_hash,
            "scope_key": scope_key,
        }
    )


def memory_scope_key(
    tenant_id: str,
    agent_id: str,
    space_group_id: str | None,
    space_id: str | None,
    session_id: str | None,
) -> str:
    """Canonical NULL-free scope identity (same grammar as note_scope_key)."""
    return "|".join((tenant_id, agent_id, space_group_id or "", space_id or "", session_id or ""))


# -- evidence -----------------------------------------------------------------


def validate_evidence_spec(
    *,
    source_type: str,
    source_id: str,
    relation: str,
    source_authority: str,
    evidence_span: str | None,
) -> None:
    if source_type not in EVIDENCE_SOURCE_TYPES:
        raise InvalidEvidenceError(f"evidence source type not allowed: {source_type!r}")
    if not isinstance(source_id, str) or not source_id:
        raise InvalidEvidenceError("evidence source_id must be a non-empty string")
    if relation not in ALL_EVIDENCE_RELATIONS:
        raise InvalidEvidenceError(f"unknown evidence relation: {relation!r}")
    try:
        SOURCE_AUTHORITY_RANK[SourceAuthority(source_authority)]
    except ValueError:
        raise InvalidEvidenceError(f"unknown source authority: {source_authority!r}") from None
    if evidence_span is not None and len(evidence_span) > MAX_EVIDENCE_SPAN_CHARS:
        raise InvalidEvidenceError(
            f"evidence_span must be at most {MAX_EVIDENCE_SPAN_CHARS} characters"
        )


def authority_allows_correction(current: str, proposed: str) -> bool:
    """§13.2: model inference and text similarity never overwrite a higher
    authority fact. A correction must rank at or above the claim's current
    source authority; lower ranks may only propose a dispute."""
    return (
        SOURCE_AUTHORITY_RANK[SourceAuthority(proposed)]
        >= SOURCE_AUTHORITY_RANK[SourceAuthority(current)]
    )


# -- bi-temporal visibility -----------------------------------------------------


def revision_current_at(
    *,
    recorded_at_us: int,
    superseded_at_us: int | None,
    as_of_us: int,
) -> bool:
    """System-time interval semantics: recorded_at <= as_of < superseded_at."""
    if recorded_at_us > as_of_us:
        return False
    return superseded_at_us is None or superseded_at_us > as_of_us


def valid_at(
    *,
    valid_from_us: int | None,
    valid_until_us: int | None,
    at_us: int,
) -> bool:
    """Valid-time interval semantics with unbounded ends."""
    before_window = valid_from_us is not None and valid_from_us > at_us
    after_window = valid_until_us is not None and valid_until_us <= at_us
    return not (before_window or after_window)


@dataclass(frozen=True, slots=True)
class ClaimCurrent:
    """Current-pointer row; full content lives in claim revisions."""

    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    subject_entity_id: str
    predicate: str
    category: str
    status: str
    confidence: float
    importance: float
    accessibility: float
    source_authority: str
    valid_from_us: int | None
    valid_until_us: int | None
    evidence_count: int
    dedup_key: str
    history_available_from_us: int
    recorded_at_us: int
    superseded_at_us: int | None
    extractor_version: str | None
    current_revision: int
    current_revision_id: str
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class ClaimRevision:
    """One immutable claim revision; ``superseded_at_us`` is stamped once by
    the successor transaction (bi-temporal system-time end)."""

    id: str
    claim_id: str
    tenant_id: str
    revision: int
    subject_entity_id: str
    predicate: str
    value_json: str
    canonical_text: str
    category: str
    privacy_labels: tuple[str, ...]
    source_refs: tuple[dict[str, object], ...]
    status: str
    confidence: float
    importance: float
    accessibility: float
    source_authority: str
    valid_from_us: int | None
    valid_until_us: int | None
    recorded_at_us: int
    superseded_at_us: int | None
    extractor_version: str | None
    content_hash: str
    created_us: int
    created_by: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Append-only evidence row; ``invalidated_us`` is write-once."""

    id: str
    claim_id: str
    tenant_id: str
    source_type: str
    source_id: str
    source_revision: int | None
    relation: str
    source_authority: str
    evidence_span: str | None
    recorded_at_us: int
    invalidated_us: int | None
    created_by: str


@dataclass(frozen=True, slots=True)
class EpisodeCurrent:
    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    title: str
    status: str
    importance: float
    started_at_us: int | None
    ended_at_us: int | None
    extractor_version: str | None
    current_revision: int
    current_revision_id: str
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class EpisodeRevision:
    id: str
    episode_id: str
    tenant_id: str
    revision: int
    title: str
    summary: str
    participant_entity_ids: tuple[str, ...]
    observation_refs: tuple[dict[str, object], ...]
    privacy_labels: tuple[str, ...]
    source_refs: tuple[dict[str, object], ...]
    status: str
    importance: float
    valence: float | None
    arousal: float | None
    started_at_us: int | None
    ended_at_us: int | None
    extractor_version: str | None
    content_hash: str
    created_us: int
    created_by: str


@dataclass(frozen=True, slots=True)
class RelationCurrent:
    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    source_entity_id: str
    relation_type: str
    target_entity_id: str
    status: str
    confidence: float
    importance: float
    accessibility: float
    valid_from_us: int | None
    valid_until_us: int | None
    evidence_count: int
    current_revision: int
    current_revision_id: str
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class RelationRevision:
    id: str
    relation_id: str
    tenant_id: str
    revision: int
    source_entity_id: str
    relation_type: str
    target_entity_id: str
    privacy_labels: tuple[str, ...]
    evidence_refs: tuple[dict[str, object], ...]
    status: str
    confidence: float
    importance: float
    accessibility: float
    valid_from_us: int | None
    valid_until_us: int | None
    superseded_at_us: int | None
    content_hash: str
    created_us: int
    created_by: str


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    media_type: str
    storage_kind: str
    locator: str
    content_hash: str
    size_bytes: int
    privacy_labels: tuple[str, ...]
    source_ref: dict[str, object] | None
    status: str
    refcount: int
    created_us: int
    updated_us: int


# -- artifact locator defense ---------------------------------------------------

#: Locator alphabet for server-generated local blob paths: shard dir + id.
_SAFE_LOCATOR_RE = re.compile(
    r"^[0-9a-f]{2}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_EXTERNAL_URL_RE = re.compile(r"^https?://[^\s:@/]+([:/][^\s]*)?$")


class ArtifactLocatorError(ArtifactInvalidError):
    """Locator normalization failure (traversal, absolute path, bad URL)."""


def validate_media_type(
    media_type: str, allowed: frozenset[str] = DEFAULT_ALLOWED_MEDIA_TYPES
) -> None:
    if not media_type or len(media_type) > MAX_ARTIFACT_MEDIA_TYPE_CHARS:
        raise ArtifactInvalidError("media_type must be 1..255 characters")
    if "/" not in media_type:
        raise ArtifactInvalidError("media_type must be a type/subtype pair")
    if allowed is not None and media_type not in allowed:
        raise ArtifactInvalidError(f"media type not allowed: {media_type!r}")


def normalize_local_locator(locator: str) -> str:
    """Validate a server-owned relative locator under the artifact root.

    Defense (§13.6, §29.1): no absolute paths, no ``..`` or ``.`` segments, no
    backslashes, no drive letters, no symlink components — the normalized
    string must exactly match ``shard/<uuid>``. Client-supplied paths are
    never accepted; the server always derives the locator from the id.
    """
    if not isinstance(locator, str) or not locator:
        raise ArtifactLocatorError("locator must be a non-empty string")
    if "\\" in locator or "\x00" in locator:
        raise ArtifactLocatorError("locator must not contain backslashes or NUL bytes")
    if locator.startswith("/") or locator.startswith("~"):
        raise ArtifactLocatorError("locator must be relative to the artifact root")
    if re.match(r"^[A-Za-z]:", locator):
        raise ArtifactLocatorError("locator must not be a drive path")
    parts = locator.split("/")
    if any(part in ("", ".", "..") for part in parts) or len(parts) != 2:
        raise ArtifactLocatorError("locator must be exactly shard/<artifact-id>")
    if not _SAFE_LOCATOR_RE.match(locator):
        raise ArtifactLocatorError("locator must match shard/<uuid> (server-generated)")
    return locator


def normalize_external_url(url: str) -> str:
    """External references are stored as data only — never fetched (§13.6)."""
    if not isinstance(url, str) or not url:
        raise ArtifactLocatorError("external url must be a non-empty string")
    if len(url) > 2048:
        raise ArtifactLocatorError("external url must be at most 2048 characters")
    if url.startswith("/") or "\\" in url or " " in url:
        raise ArtifactLocatorError("external url must be an absolute http(s) URL")
    # Reject embedded userinfo credentials: a stored reference must not
    # smuggle basic auth around as data.
    after_scheme = url.split("://", 1)[-1]
    if "@" in after_scheme.split("/", 1)[0]:
        raise ArtifactLocatorError("external url must not embed credentials")
    if not _EXTERNAL_URL_RE.match(url):
        raise ArtifactLocatorError("external url must be an absolute http(s) URL")
    return url


def artifact_shard(artifact_id: str) -> str:
    """Deterministic two-hex-char shard for the blob directory."""
    return artifact_id[:2]


def validate_artifact_admission(
    *,
    storage_kind: str,
    size_bytes: int,
    media_type: str,
    content_hash: str,
) -> None:
    if storage_kind == ArtifactStorageKind.INLINE.value:
        if size_bytes > MAX_INLINE_ARTIFACT_BYTES:
            raise ArtifactInvalidError(
                f"inline artifacts must be at most {MAX_INLINE_ARTIFACT_BYTES} bytes"
            )
    elif storage_kind == ArtifactStorageKind.LOCAL_BLOB.value:
        if size_bytes > MAX_LOCAL_ARTIFACT_BYTES:
            raise ArtifactInvalidError(
                f"local blobs must be at most {MAX_LOCAL_ARTIFACT_BYTES} bytes"
            )
    elif storage_kind != ArtifactStorageKind.EXTERNAL_REF.value:
        raise ArtifactInvalidError(f"unknown storage kind: {storage_kind!r}")
    if size_bytes < 0:
        raise ArtifactInvalidError("size_bytes must not be negative")
    validate_media_type(media_type)
    if not re.match(r"^[0-9a-f]{64}$", content_hash or ""):
        raise ArtifactInvalidError("content_hash must be a sha-256 hex digest")


# -- fingerprints ---------------------------------------------------------------


def remember_fingerprint(
    *,
    agent_id: str,
    subject_entity_id: str,
    predicate: str,
    value_json: str,
    canonical_text: str,
    category: str,
    privacy_labels: tuple[str, ...],
    source_refs: tuple[dict[str, object], ...],
    evidence: tuple[dict[str, object], ...],
    scope: dict[str, str | None],
    confidence: float,
    importance: float,
    accessibility: float,
    source_authority: str,
    valid_from_us: int | None,
    valid_until_us: int | None,
    extractor_version: str | None,
) -> str:
    return request_fingerprint(
        "claim:remember",
        {
            "agent_id": agent_id,
            "subject_entity_id": subject_entity_id,
            "predicate": predicate,
            "value_json": value_json,
            "canonical_text": canonical_text,
            "category": category,
            "privacy_labels": list(privacy_labels),
            "source_refs": [dict(ref) for ref in source_refs],
            "evidence": [dict(item) for item in evidence],
            "scope": scope,
            "confidence": confidence,
            "importance": importance,
            "accessibility": accessibility,
            "source_authority": source_authority,
            "valid_from_us": valid_from_us,
            "valid_until_us": valid_until_us,
            "extractor_version": extractor_version,
        },
    )


def correct_fingerprint(
    *,
    claim_id: str,
    expected_revision: int,
    mode: str,
    value_json: str,
    canonical_text: str,
    evidence: tuple[dict[str, object], ...],
    reason: str,
) -> str:
    return request_fingerprint(
        "claim:correct",
        {
            "claim_id": claim_id,
            "expected_revision": expected_revision,
            "mode": mode,
            "value_json": value_json,
            "canonical_text": canonical_text,
            "evidence": [dict(item) for item in evidence],
            "reason": reason,
        },
    )


__all__ = [
    "ALL_CLAIM_CATEGORIES",
    "ALL_EVIDENCE_RELATIONS",
    "ARTIFACT_STATUS_TRANSITIONS",
    "CLAIM_CURRENT_VISIBLE_STATUSES",
    "CLAIM_TRANSITIONS",
    "DEFAULT_ALLOWED_MEDIA_TYPES",
    "EPISODE_TRANSITIONS",
    "EVIDENCE_SOURCE_TYPES",
    "MAX_CLAIM_CANONICAL_TEXT_CHARS",
    "MAX_CLAIM_PREDICATE_CHARS",
    "MAX_CLAIM_VALUE_JSON_BYTES",
    "MAX_EPISODE_SUMMARY_CHARS",
    "MAX_EPISODE_TITLE_CHARS",
    "MAX_EVIDENCE_SPAN_CHARS",
    "MAX_INLINE_ARTIFACT_BYTES",
    "MAX_LOCAL_ARTIFACT_BYTES",
    "MAX_RELATION_TYPE_CHARS",
    "RELATION_TRANSITIONS",
    "SOURCE_AUTHORITY_RANK",
    "ArtifactInvalidError",
    "ArtifactLocatorError",
    "ArtifactRecord",
    "ArtifactStatus",
    "ArtifactStorageKind",
    "ClaimCategory",
    "ClaimCurrent",
    "ClaimRevision",
    "ClaimStatus",
    "EpisodeCurrent",
    "EpisodeRevision",
    "EpisodeStatus",
    "EvidenceRecord",
    "EvidenceRelation",
    "EvidenceRequiredError",
    "InvalidClaimError",
    "InvalidEvidenceError",
    "RelationCurrent",
    "RelationRevision",
    "RelationStatus",
    "SourceAuthority",
    "SubjectAmbiguousError",
    "UnsafeProcedureClaimError",
    "artifact_shard",
    "authority_allows_correction",
    "claim_dedup_key",
    "claim_value_hash",
    "correct_fingerprint",
    "memory_scope_key",
    "normalize_external_url",
    "normalize_local_locator",
    "remember_fingerprint",
    "revision_current_at",
    "valid_at",
    "validate_artifact_admission",
    "validate_claim_transition",
    "validate_claim_value",
    "validate_episode_transition",
    "validate_evidence_spec",
    "validate_media_type",
    "validate_procedure_value",
    "validate_relation_transition",
    "validate_score",
]
