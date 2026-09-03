"""Profile projection domain rules (§13.5, ADR-0016 §2).

Pure logic only: the deterministic field model (sections, grouping keys,
conflict semantics), the stable degradation reason codes, and the
deterministic checksum material that binds a profile generation to its
persisted rows. Persistence lives in the storage adapter.

A profile is a projection (ADR-0001): every field binds canonical claim
ids + revisions and stores only rebuildable metadata. The only source is
the canonical CLAIM table — persona revisions/states and self-narrative
claims never enter an entity profile (ADR-0016 §2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from iris_memory_core.domain.errors import ConflictError

#: Builder identity. Any change to the section mapping, conflict semantics,
#: checksum material or the build pipeline requires bumping this and
#: building a new generation (ADR-0016 §2).
PROFILE_BUILDER_VERSION = 1

#: Known builder versions the read path may trust; an unknown (future or
#: corrupted) stamp disables the profile surface instead of serving an
#: untrusted projection.
KNOWN_PROFILE_BUILDER_VERSIONS = frozenset({PROFILE_BUILDER_VERSION})

#: The ``profile.apply`` payload version THIS build's rebuild provably covers
#: when settling the unleased backlog (future payload versions stay queued
#: for a build that understands them; mirrors JOB_PAYLOAD_VERSION, review
#: round 3).
PROFILE_APPLY_PAYLOAD_VERSION = 1

#: Default lag (unsettled ``profile.apply`` jobs for the requesting agent,
#: including ownerless ones) beyond which the profile surface reports stale.
PROFILE_STALENESS_LIMIT = 10_000

#: Default window a retired generation must outlive before physical cleanup.
PROFILE_RETIREMENT_WINDOW_US = 24 * 3_600_000_000

#: ``experience`` section admission: fact-category claims at or above this
#: importance become "important experiences" (ADR-0016 §2).
EXPERIENCE_IMPORTANCE_THRESHOLD = 0.5

#: ``goal`` section admission: fact-category claims whose predicate (lower,
#: first token stem) starts with one of these stems (ADR-0016 §2 — frozen
#: structural rule; no model inference).
GOAL_PREDICATE_STEMS: tuple[str, ...] = (
    "goal",
    "plan",
    "aim",
    "want",
    "aspire",
    "intend",
)

#: ``recent_change`` window, anchored on the SUBJECT's own latest recorded
#: claim (snapshot-anchored, never wall-clock: same snapshot ⇒ same fields).
RECENT_CHANGE_WINDOW_US = 7 * 86_400_000_000

#: Frozen profile sections. Entity/relationship subjects use the first
#: seven (the §13.5 partitions); space_group subjects use community/fact
#: plus the derived recent_change.
PROFILE_SECTION_IDENTITY = "identity"
PROFILE_SECTION_PREFERENCE = "preference"
PROFILE_SECTION_RELATIONSHIP = "relationship"
PROFILE_SECTION_EXPERIENCE = "experience"
PROFILE_SECTION_GOAL = "goal"
PROFILE_SECTION_RECENT_CHANGE = "recent_change"
PROFILE_SECTION_INTERACTION = "interaction"
PROFILE_SECTION_COMMUNITY = "community"
PROFILE_SECTION_FACT = "fact"

ENTITY_PROFILE_SECTIONS = frozenset(
    {
        PROFILE_SECTION_IDENTITY,
        PROFILE_SECTION_PREFERENCE,
        PROFILE_SECTION_RELATIONSHIP,
        PROFILE_SECTION_EXPERIENCE,
        PROFILE_SECTION_GOAL,
        PROFILE_SECTION_RECENT_CHANGE,
        PROFILE_SECTION_INTERACTION,
    }
)
GROUP_PROFILE_SECTIONS = frozenset(
    {PROFILE_SECTION_COMMUNITY, PROFILE_SECTION_FACT, PROFILE_SECTION_RECENT_CHANGE}
)

#: Field conflict states (ADR-0016 §2): conflicts are retained explicitly —
#: value_json lists every conflicting value; nothing is silently picked.
PROFILE_CONFLICT_SINGLE = "single"
PROFILE_CONFLICT_CONFLICT = "conflict"
PROFILE_CONFLICT_DISPUTED = "disputed"

#: Stable degradation reason codes for the profile surface (ADR-0016 §6).
PROFILE_REASON_REBUILD_PENDING = "profile_rebuild_pending"
PROFILE_REASON_BUILDER_UNKNOWN = "profile_builder_unknown"
PROFILE_REASON_GENERATION_STALE = "profile_generation_stale"
PROFILE_REASON_INDEX_CORRUPT = "profile_index_corrupt"
PROFILE_REASON_AS_OF_UNSUPPORTED = "profile_as_of_unsupported"

#: Non-retryable reasons: retrying the same request cannot help until an
#: operator rebuilds or upgrades.
PROFILE_NON_RETRYABLE_REASONS = frozenset(
    {PROFILE_REASON_BUILDER_UNKNOWN, PROFILE_REASON_AS_OF_UNSUPPORTED}
)

#: Claim statuses whose current revision participates in profile fields
#: (same visibility set as canonical current reads).
PROFILE_CLAIM_VISIBLE_STATUSES = ("active", "disputed")

#: The persona-isolation rule: self-narrative claims belong to the
#: persona/narrative pipeline (Phase 9) and NEVER enter an entity profile.
PROFILE_EXCLUDED_CATEGORIES = frozenset({"self_narrative"})

#: Maximum claims a single field may carry as sources (defense in depth for
#: pathological dedup storms; excess claims are dropped deterministically —
#: the field keeps the newest sources and is marked conflict when the
#: dropped claims disagreed).
MAX_FIELD_SOURCES = 32


class ProfileDegradedError(Exception):
    """The profile surface must degrade with a stable reason — never return
    untrustworthy results (ADR-0016 §2/§6)."""

    def __init__(self, reason_code: str, *, retryable: bool | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        if retryable is None:
            retryable = reason_code not in PROFILE_NON_RETRYABLE_REASONS
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ProfileSubjectKey:
    """One profile subject: entity, relationship (ordered entity pair) or
    space group (ADR-0016 §2)."""

    kind: str  # entity | relationship | space_group
    subject_id: str

    def __post_init__(self) -> None:
        if self.kind not in ("entity", "relationship", "space_group"):
            raise ConflictError(f"unknown profile subject kind: {self.kind!r}")
        if not self.subject_id:
            raise ConflictError("profile subject id must not be empty")

    @property
    def group_key(self) -> str:
        return f"{self.kind}:{self.subject_id}"


def relationship_subject_id(entity_a: str, entity_b: str) -> str:
    """Canonical ordered pair id for relationship subjects (min|max)."""
    lo, hi = sorted((entity_a, entity_b))
    return f"{lo}|{hi}"


@dataclass(frozen=True, slots=True)
class ProfileFieldSource:
    """One source claim reference: id + revision at build time."""

    claim_id: str
    revision: int
    value_hash: str = ""

    def as_ref(self) -> dict[str, object]:
        return {"claim_id": self.claim_id, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class ProfileFieldDraft:
    """The deterministic builder output for one field (pre-persistence)."""

    subject: ProfileSubjectKey
    section: str
    field: str
    group_key: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    privacy_labels: tuple[str, ...]
    value_json: str
    summary_text: str
    sources: tuple[ProfileFieldSource, ...]
    conflict_state: str
    freshness_us: int
    valid_from_us: int | None
    valid_until_us: int | None


@dataclass(frozen=True, slots=True)
class ProfileFieldRecord:
    """One persisted profile field row."""

    tenant_id: str
    generation_id: str
    subject_kind: str
    subject_id: str
    section: str
    field: str
    group_key: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    privacy_labels: tuple[str, ...]
    value_json: str
    summary_text: str
    sources: tuple[ProfileFieldSource, ...]
    conflict_state: str
    freshness_us: int
    valid_from_us: int | None
    valid_until_us: int | None
    created_us: int

    @property
    def subject(self) -> ProfileSubjectKey:
        return ProfileSubjectKey(self.subject_kind, self.subject_id)


@dataclass(frozen=True, slots=True)
class ProfileSubjectRecord:
    """Per-subject summary row: field count + deterministic digest."""

    tenant_id: str
    generation_id: str
    subject_kind: str
    subject_id: str
    field_count: int
    subject_checksum: str
    updated_us: int


@dataclass(frozen=True, slots=True)
class ProfileGenerationRecord:
    """One immutable profile generation row — the authoritative manifest."""

    id: str
    tenant_id: str
    builder_version: int
    source_watermark: int
    tombstone_watermark: int
    subject_count: int
    field_count: int
    content_checksum: str
    agent_watermarks_json: str
    status: str
    created_us: int
    verified_us: int
    retired_us: int | None = None

    def agent_watermarks(self) -> dict[str, int]:
        decoded = json.loads(self.agent_watermarks_json)
        return (
            {str(key): int(value) for key, value in decoded.items()}
            if isinstance(decoded, dict)
            else {}
        )


@dataclass(frozen=True, slots=True)
class ProfileCurrentPointer:
    tenant_id: str
    generation_id: str
    switch_epoch: int
    builder_version: int
    source_watermark: int
    tombstone_watermark: int
    switched_us: int


def profile_section_for_claim(
    *, category: str, predicate: str, importance: float, subject_kind: str
) -> str | None:
    """Deterministic (subject-kind, category) → section mapping (ADR-0016
    §2). ``None`` means the claim does not participate in profiles."""

    if category in PROFILE_EXCLUDED_CATEGORIES:
        return None
    if subject_kind == "space_group":
        if category == "community":
            return PROFILE_SECTION_COMMUNITY
        if category == "fact":
            return PROFILE_SECTION_FACT
        return None
    if category == "identity":
        return PROFILE_SECTION_IDENTITY
    if category == "preference":
        return PROFILE_SECTION_PREFERENCE
    if category == "relationship":
        return PROFILE_SECTION_RELATIONSHIP
    if category == "procedure":
        return PROFILE_SECTION_INTERACTION
    if category == "fact":
        stem = predicate.strip().lower().split("_", 1)[0].split(" ", 1)[0]
        if any(stem.startswith(prefix) for prefix in GOAL_PREDICATE_STEMS):
            return PROFILE_SECTION_GOAL
        if importance >= EXPERIENCE_IMPORTANCE_THRESHOLD:
            return PROFILE_SECTION_EXPERIENCE
        return None
    return None


def field_group_key(scope_key: str, privacy_labels: tuple[str, ...]) -> str:
    """Grouping key making cross-scope/privacy stitching inexpressible:
    the field PK includes it, and sources must share both components."""
    return hashlib.sha256(
        "\x1f".join((scope_key, *sorted(privacy_labels))).encode("utf-8")
    ).hexdigest()


def compute_field_conflict_state(
    sources: tuple[ProfileFieldSource, ...], *, any_disputed: bool
) -> str:
    """Conflicts are retained explicitly: multiple distinct value hashes ⇒
    conflict; any disputed status ⇒ disputed; otherwise single."""
    if any_disputed:
        return PROFILE_CONFLICT_DISPUTED
    value_hashes = {source.value_hash for source in sources}
    if len(sources) > 1 and len(value_hashes) > 1:
        return PROFILE_CONFLICT_CONFLICT
    return PROFILE_CONFLICT_SINGLE


def profile_field_digest(field: ProfileFieldDraft) -> str:
    """Deterministic digest of one field's logical content — same canonical
    snapshot ⇒ same digest (times and generation ids excluded)."""
    material = [
        field.subject.kind,
        field.subject.subject_id,
        field.section,
        field.field,
        field.group_key,
        field.agent_id,
        field.space_group_id or "",
        field.space_id or "",
        field.session_id or "",
        field.scope_key,
        *sorted(field.privacy_labels),
        field.value_json,
        field.conflict_state,
        str(field.freshness_us),
        str(field.valid_from_us or ""),
        str(field.valid_until_us or ""),
        *[f"{source.claim_id}:{source.revision}" for source in field.sources],
    ]
    digest = hashlib.sha256()
    digest.update("\x1f".join(material).encode("utf-8"))
    return digest.hexdigest()


def profile_field_record_digest(record: ProfileFieldRecord) -> str:
    """The same digest recomputed from a PERSISTED row — the verification
    path (build/verify/cleanup) re-derives checksums from the authoritative
    rows and compares them against the generation manifest (ADR-0016 §2)."""
    material = [
        record.subject_kind,
        record.subject_id,
        record.section,
        record.field,
        record.group_key,
        record.agent_id,
        record.space_group_id or "",
        record.space_id or "",
        record.session_id or "",
        record.scope_key,
        *sorted(record.privacy_labels),
        record.value_json,
        record.conflict_state,
        str(record.freshness_us),
        str(record.valid_from_us or ""),
        str(record.valid_until_us or ""),
        *[f"{source.claim_id}:{source.revision}" for source in record.sources],
    ]
    digest = hashlib.sha256()
    digest.update("\x1f".join(material).encode("utf-8"))
    return digest.hexdigest()


def subject_checksum_material(
    fields: tuple[ProfileFieldDraft, ...],
) -> tuple[int, str]:
    """Field count + digest over the sorted field digests of one subject."""
    digests = sorted(profile_field_digest(field) for field in fields)
    digest = hashlib.sha256()
    for entry in digests:
        digest.update(entry.encode("utf-8"))
        digest.update(b"\x1e")
    return len(fields), digest.hexdigest()


def generation_checksum_material(
    subjects: tuple[tuple[ProfileSubjectKey, int, str], ...],
) -> tuple[int, int, str]:
    """subject_count, field_count and the generation checksum over the
    sorted (subject key, field count, subject checksum) triples."""
    ordered = sorted(
        (key.group_key, field_count, checksum) for key, field_count, checksum in subjects
    )
    digest = hashlib.sha256()
    total_fields = 0
    for group_key, field_count, checksum in ordered:
        digest.update(f"{group_key}\x1f{field_count}\x1f{checksum}".encode())
        digest.update(b"\x1e")
        total_fields += field_count
    return len(ordered), total_fields, digest.hexdigest()


def render_field_value(
    values: tuple[tuple[str, str, str], ...],
) -> tuple[str, str]:
    """Render value_json + summary from (claim_id, value_text, status)
    triples. Conflicting values are ALL retained, oldest-first by claim id —
    never silently merged (ADR-0016 §2)."""
    ordered = sorted(values, key=lambda item: (item[1], item[0]))
    if len(ordered) == 1:
        value = json.dumps({"values": [ordered[0][1]]}, sort_keys=True, ensure_ascii=False)
        return value, ordered[0][1]
    payload = [
        {"claim_id": claim_id, "value": value_text} for claim_id, value_text, _status in ordered
    ]
    value = json.dumps({"values": payload}, sort_keys=True, ensure_ascii=False)
    summary = " | ".join(value_text for _claim, value_text, _status in ordered)
    return value, summary


__all__ = [
    "ENTITY_PROFILE_SECTIONS",
    "EXPERIENCE_IMPORTANCE_THRESHOLD",
    "GOAL_PREDICATE_STEMS",
    "GROUP_PROFILE_SECTIONS",
    "KNOWN_PROFILE_BUILDER_VERSIONS",
    "MAX_FIELD_SOURCES",
    "PROFILE_APPLY_PAYLOAD_VERSION",
    "PROFILE_BUILDER_VERSION",
    "PROFILE_CLAIM_VISIBLE_STATUSES",
    "PROFILE_CONFLICT_CONFLICT",
    "PROFILE_CONFLICT_DISPUTED",
    "PROFILE_CONFLICT_SINGLE",
    "PROFILE_EXCLUDED_CATEGORIES",
    "PROFILE_NON_RETRYABLE_REASONS",
    "PROFILE_REASON_AS_OF_UNSUPPORTED",
    "PROFILE_REASON_BUILDER_UNKNOWN",
    "PROFILE_REASON_GENERATION_STALE",
    "PROFILE_REASON_INDEX_CORRUPT",
    "PROFILE_REASON_REBUILD_PENDING",
    "PROFILE_RETIREMENT_WINDOW_US",
    "PROFILE_STALENESS_LIMIT",
    "RECENT_CHANGE_WINDOW_US",
    "ProfileCurrentPointer",
    "ProfileDegradedError",
    "ProfileFieldDraft",
    "ProfileFieldRecord",
    "ProfileFieldSource",
    "ProfileGenerationRecord",
    "ProfileSubjectKey",
    "ProfileSubjectRecord",
    "compute_field_conflict_state",
    "field_group_key",
    "generation_checksum_material",
    "profile_field_digest",
    "profile_field_record_digest",
    "profile_section_for_claim",
    "relationship_subject_id",
    "render_field_value",
    "subject_checksum_material",
]
