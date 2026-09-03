"""Vector projection domain rules (§22.2-22.4, ADR-0015).

Pure logic only: versioned embedding-input templates, the vector space
identity (model/dimension/metric/normalization/template/builder), surrogate
ID allocation bounds, vector output validation, deterministic manifest and
checksum material, and the stable Vector route degradation reason codes.
Persistence lives in the storage adapter; FAISS and NumPy types never cross
this boundary (vectors are sequences of floats).

The vector index is a projection (ADR-0001): every entry binds
``(resource_type, resource_id, resource_revision)`` and stores only
rebuildable metadata. The recall rehydrate re-reads canonical rows before
anything is returned — the index is never a source of truth.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from iris_memory_core.domain.errors import ConflictError, InvalidRequestError

#: Builder identity. Any change to templates, normalization, checksum
#: material or the build pipeline requires bumping these and building a new
#: generation (ADR-0015 §5): different spaces never share a generation.
VECTOR_BUILDER_VERSION = 1
VECTOR_TEXT_TEMPLATE_VERSION = 1
VECTOR_QUERY_TEMPLATE_VERSION = 1

#: Frozen metric/normalization pair (ADR-0015 §2): L2-normalized vectors +
#: inner product = cosine similarity. A generation with any other pair is
#: not loadable by this build.
VECTOR_METRIC = "cosine"
VECTOR_NORMALIZATION = "l2"

#: Resources whose canonical text enters the vector projection — the same
#: indexable set as FTS (ADR-0014 §1): observations stay behind the recent
#: window, artifacts are never indexed.
VECTOR_INDEXABLE_RESOURCE_TYPES = frozenset({"claim", "episode", "note"})

#: Surrogate IDs are server-assigned in [1, 2^62] (ADR-0015 §3): strictly
#: positive, far from the signed-int64 edges, never derived from a UUID.
SURROGATE_ID_MIN = 1
SURROGATE_ID_MAX = 1 << 62

#: L2-norm tolerance for provider output and startup probe verification.
NORMALIZATION_TOLERANCE = 1e-3

#: Default lag (unsettled applies + unincorporated delta rows per agent)
#: beyond which the vector route reports stale degradation (ADR-0015 §4).
VECTOR_STALENESS_LIMIT = 10_000

#: Default window an old generation must outlive (and lose all references)
#: before physical cleanup may remove it (ADR-0015 §5).
VECTOR_RETIREMENT_WINDOW_US = 24 * 3_600_000_000

#: Maximum characters of embedding input after template rendering (defense
#: in depth; the provider also enforces its own max_input_chars).
VECTOR_MAX_INPUT_CHARS = 24_000

#: Stable degradation reason codes for the vector route (ADR-0015 §7).
VECTOR_REASON_REBUILD_PENDING = "vector_rebuild_pending"
VECTOR_REASON_BUILDER_UNKNOWN = "vector_builder_unknown"
VECTOR_REASON_GENERATION_STALE = "vector_generation_stale"
VECTOR_REASON_INDEX_CORRUPT = "vector_index_corrupt"
VECTOR_REASON_UNAVAILABLE = "vector_unavailable"
VECTOR_REASON_AS_OF_UNSUPPORTED = "vector_as_of_unsupported"
VECTOR_REASON_SPACE_MISMATCH = "vector_space_mismatch"

#: Non-retryable reason codes: retrying the same request cannot help until
#: an operator rebuilds, reconfigures or disables something. A transient
#: provider outage (``vector_unavailable``) IS retryable.
VECTOR_NON_RETRYABLE_REASONS = frozenset(
    {
        VECTOR_REASON_BUILDER_UNKNOWN,
        VECTOR_REASON_AS_OF_UNSUPPORTED,
    }
)

#: Known builder versions the read path may trust; an unknown (future or
#: corrupted) stamp disables the vector route instead of serving results
#: from an untrusted space (ADR-0015 §5).
KNOWN_VECTOR_BUILDER_VERSIONS = frozenset({VECTOR_BUILDER_VERSION})
KNOWN_VECTOR_TEMPLATE_VERSIONS = frozenset({VECTOR_TEXT_TEMPLATE_VERSION})


class VectorDegradedError(Exception):
    """The vector route must degrade with a stable reason — never return
    untrustworthy results (ADR-0015 §6)."""

    def __init__(self, reason_code: str, *, retryable: bool | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        if retryable is None:
            retryable = reason_code not in VECTOR_NON_RETRYABLE_REASONS
        self.retryable = retryable


class EmbeddingProviderError(Exception):
    """Provider output failed validation (dimension/NaN/Inf/normalization/
    count mismatch) or the provider is unavailable (timeout/circuit open/
    rate limited). Carries a low-sensitivity reason code only — never the
    submitted text (ADR-0015 §2)."""

    def __init__(self, reason_code: str, *, retryable: bool = True) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class VectorSpaceConfig:
    """The identity of one vector space (ADR-0015 §5).

    Generations, delta rows and handles all carry this; any difference
    means a different space — indexes are never mixed.
    """

    model: str
    dimension: int
    metric: str = VECTOR_METRIC
    normalization: str = VECTOR_NORMALIZATION
    template_version: int = VECTOR_TEXT_TEMPLATE_VERSION
    builder_version: int = VECTOR_BUILDER_VERSION

    def __post_init__(self) -> None:
        if not self.model or len(self.model) > 128:
            raise InvalidRequestError("embedding model must be 1..128 characters")
        if self.dimension < 1 or self.dimension > 65_536:
            raise InvalidRequestError("embedding dimension must be within 1..65536")
        if self.metric != VECTOR_METRIC:
            raise InvalidRequestError(
                f"unsupported vector metric {self.metric!r}; this build serves {VECTOR_METRIC!r}"
            )
        if self.normalization != VECTOR_NORMALIZATION:
            raise InvalidRequestError(
                f"unsupported normalization {self.normalization!r}; "
                f"this build serves {VECTOR_NORMALIZATION!r}"
            )

    def as_manifest_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "dimension": self.dimension,
            "metric": self.metric,
            "normalization": self.normalization,
            "template_version": self.template_version,
            "builder_version": self.builder_version,
        }


def vector_space_matches(
    space: VectorSpaceConfig,
    *,
    model: str,
    dimension: int,
    metric: str,
    normalization: str,
    template_version: int,
    builder_version: int,
) -> bool:
    """Exact-match rule: any differing component means a different space."""
    return (
        space.model == model
        and space.dimension == dimension
        and space.metric == metric
        and space.normalization == normalization
        and space.template_version == template_version
        and space.builder_version == builder_version
    )


def builder_versions_trusted(builder_version: int, template_version: int) -> bool:
    return (
        builder_version in KNOWN_VECTOR_BUILDER_VERSIONS
        and template_version in KNOWN_VECTOR_TEMPLATE_VERSIONS
    )


def render_embedding_input(resource_type: str, **fields: str | None) -> str:
    """Versioned minimal embedding input per resource type (§22.2).

    The templates intentionally mirror the FTS text templates but live in
    their own version namespace: only the minimum canonical text needed for
    semantic matching is ever sent to a provider (ADR-0015 §2).
    """
    if resource_type == "claim":
        predicate = (fields.get("predicate") or "").strip()
        canonical_text = (fields.get("canonical_text") or "").strip()
        return f"{predicate}: {canonical_text}".strip()
    if resource_type == "episode":
        title = (fields.get("title") or "").strip()
        summary = (fields.get("summary") or "").strip()
        return f"{title}\n{summary}".strip()
    if resource_type == "note":
        title = (fields.get("title") or "").strip()
        body = (fields.get("body") or "").strip()
        return f"{title}\n{body}".strip()
    raise InvalidRequestError(f"resource type {resource_type!r} is not vector-indexable")


def render_query_input(topic: str, *, max_chars: int = VECTOR_MAX_INPUT_CHARS) -> str:
    """Versioned query-side input: NFKC + whitespace collapse + truncation."""
    normalized = unicodedata.normalize("NFKC", topic)
    normalized = " ".join(normalized.split())
    return normalized[:max_chars]


@dataclass(frozen=True, slots=True)
class VectorEntryInput:
    """One resource handed to the vector builder (already canonical)."""

    tenant_id: str
    resource_type: str
    resource_id: str
    resource_revision: int
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    content_hash: str
    occurred_us: int
    embedding_input: str

    def __post_init__(self) -> None:
        if self.resource_type not in VECTOR_INDEXABLE_RESOURCE_TYPES:
            raise InvalidRequestError(
                f"resource type {self.resource_type!r} is not vector-indexable"
            )
        if self.resource_revision < 1:
            raise InvalidRequestError("resource_revision must be >= 1")
        if not self.embedding_input.strip():
            raise InvalidRequestError("embedding input must not be empty")


@dataclass(frozen=True, slots=True)
class VectorIdMapRecord:
    """One UUID↔surrogate mapping row (ADR-0015 §3).

    ``incorporated_generation`` is the membership proof: the generation whose
    pointer-switch transaction stamped this row at this revision. ``None``
    means no published generation provably contains the row's current
    revision (freshly upserted, moved, or invalidated)."""

    tenant_id: str
    resource_type: str
    resource_id: str
    resource_revision: int
    surrogate_id: int
    agent_id: str
    model: str
    dimension: int
    content_hash: str
    status: str
    created_us: int
    invalidated_us: int | None = None
    incorporated_generation: str | None = None

    def __post_init__(self) -> None:
        validate_surrogate_id(self.surrogate_id)
        if self.status not in ("active", "invalid"):
            raise ConflictError(f"unknown vector id map status: {self.status!r}")
        if self.status == "invalid" and self.invalidated_us is None:
            raise ConflictError("invalid id map rows must carry invalidated_us")


@dataclass(frozen=True, slots=True)
class VectorGenerationRecord:
    """One immutable generation row (status ∈ {verified, retired})."""

    id: str
    tenant_id: str
    space: VectorSpaceConfig
    source_watermark: int
    tombstone_watermark: int
    vector_count: int
    content_checksum: str
    id_map_checksum: str
    index_checksum: str
    agent_watermarks_json: str
    status: str
    created_us: int
    verified_us: int
    retired_us: int | None = None

    def agent_watermarks(self) -> dict[str, int]:
        import json

        decoded = json.loads(self.agent_watermarks_json)
        return (
            {str(key): int(value) for key, value in decoded.items()}
            if isinstance(decoded, dict)
            else {}
        )


@dataclass(frozen=True, slots=True)
class VectorCurrentPointer:
    """Per-tenant current generation pointer with the fencing epoch."""

    tenant_id: str
    generation_id: str
    switch_epoch: int
    space: VectorSpaceConfig
    source_watermark: int
    tombstone_watermark: int
    switched_us: int


def validate_surrogate_id(surrogate_id: int) -> int:
    """Server-assigned surrogate IDs are legal signed int64 in [1, 2^62].

    A UUID truncated or hashed into an int64 would collide unprovably; the
    allocator hands out strictly increasing counters so collisions and reuse
    are structurally impossible (P7-IDMAP-01).
    """
    if isinstance(surrogate_id, bool) or not isinstance(surrogate_id, int):
        raise ConflictError("surrogate id must be an int")
    if not SURROGATE_ID_MIN <= surrogate_id <= SURROGATE_ID_MAX:
        raise ConflictError(f"surrogate id must be within [{SURROGATE_ID_MIN}, {SURROGATE_ID_MAX}]")
    return surrogate_id


def validate_vector(vector: Sequence[float], *, dimension: int) -> None:
    """Reject empty, wrong-dimension, non-finite and non-numeric output.

    Normalization is checked against the L2 norm ≈ 1 tolerance (the metric
    is cosine over L2-normalized vectors; ADR-0015 §2). Raises
    ``EmbeddingProviderError`` with a low-sensitivity reason.
    """
    if len(vector) != dimension:
        raise EmbeddingProviderError("embedding_dimension_mismatch", retryable=False)
    squared = 0.0
    for component in vector:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise EmbeddingProviderError("embedding_not_numeric", retryable=False)
        value = float(component)
        if math.isnan(value) or math.isinf(value):
            raise EmbeddingProviderError("embedding_not_finite", retryable=False)
        squared += value * value
    norm = math.sqrt(squared)
    if norm <= 0.0:
        raise EmbeddingProviderError("embedding_empty", retryable=False)
    if abs(norm - 1.0) > NORMALIZATION_TOLERANCE:
        raise EmbeddingProviderError("embedding_not_normalized", retryable=False)


def normalize_vector(vector: Sequence[float]) -> tuple[float, ...]:
    """Deterministic L2 normalization (checked, not trusted: callers still
    validate the result)."""
    validate_finite(vector)
    norm = math.sqrt(sum(float(v) * float(v) for v in vector))
    if norm <= 0.0:
        raise EmbeddingProviderError("embedding_empty", retryable=False)
    return tuple(float(v) / norm for v in vector)


def validate_finite(vector: Sequence[float]) -> None:
    for component in vector:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise EmbeddingProviderError("embedding_not_numeric", retryable=False)
        if math.isnan(float(component)) or math.isinf(float(component)):
            raise EmbeddingProviderError("embedding_not_finite", retryable=False)


#: Manifest field names frozen by ADR-0015 §5 — every generation directory
#: carries exactly these in ``manifest.json``.
MANIFEST_FIELDS: tuple[str, ...] = (
    "generation_id",
    "schema_version",
    "model",
    "dimension",
    "metric",
    "normalization",
    "template_version",
    "builder_version",
    "source_watermark",
    "tombstone_watermark",
    "vector_count",
    "content_hash",
    "id_map_hash",
    "index_hash",
    "agent_watermarks",
    "created_at",
)

#: The four files every complete generation directory contains.
GENERATION_FILES: tuple[str, ...] = (
    "manifest.json",
    "index.faiss",
    "id-map.snapshot",
    "checksums.txt",
)


def id_map_snapshot_line(entry: VectorEntryInput, surrogate_id: int) -> str:
    """One deterministic id-map snapshot line (surrogate-sorted on disk)."""
    return "\t".join(
        (
            str(validate_surrogate_id(surrogate_id)),
            entry.resource_type,
            entry.resource_id,
            str(entry.resource_revision),
            entry.content_hash,
        )
    )


def id_map_snapshot_hash(entries: Iterable[tuple[VectorEntryInput, int]]) -> str:
    """Deterministic digest over the sorted (surrogate, identity) set."""
    lines = sorted((id_map_snapshot_line(entry, surrogate) for entry, surrogate in entries))
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def generation_content_hash(entries: Iterable[tuple[VectorEntryInput, int]]) -> str:
    """Deterministic digest binding the logical content of a generation:
    surrogate, resource identity, revision and the CANONICAL content hash.
    Same canonical snapshot ⇒ same checksum; bound to canonical bytes, not
    to provider output (rebuilds with another provider yield the same
    content hash for the same snapshot)."""
    material = sorted(
        (
            str(surrogate),
            entry.resource_type,
            entry.resource_id,
            str(entry.resource_revision),
            entry.content_hash,
        )
        for entry, surrogate in entries
    )
    digest = hashlib.sha256()
    for row in material:
        digest.update("\x1f".join(row).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def file_checksums_material(checksums: Mapping[str, str]) -> str:
    """Deterministic ``checksums.txt`` body: sorted ``<sha256>  <name>``."""
    return "".join(f"{checksums[name]}  {name}\n" for name in sorted(checksums))


def embedding_input_digest(text: str) -> str:
    """Low-sensitivity digest for logs/metrics: never the text itself."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def vector_staleness_limit() -> int:
    """Lag beyond which the vector route reports stale degradation."""
    return VECTOR_STALENESS_LIMIT


__all__ = [
    "GENERATION_FILES",
    "KNOWN_VECTOR_BUILDER_VERSIONS",
    "KNOWN_VECTOR_TEMPLATE_VERSIONS",
    "MANIFEST_FIELDS",
    "NORMALIZATION_TOLERANCE",
    "SURROGATE_ID_MAX",
    "SURROGATE_ID_MIN",
    "VECTOR_BUILDER_VERSION",
    "VECTOR_INDEXABLE_RESOURCE_TYPES",
    "VECTOR_MAX_INPUT_CHARS",
    "VECTOR_METRIC",
    "VECTOR_NON_RETRYABLE_REASONS",
    "VECTOR_NORMALIZATION",
    "VECTOR_QUERY_TEMPLATE_VERSION",
    "VECTOR_REASON_AS_OF_UNSUPPORTED",
    "VECTOR_REASON_BUILDER_UNKNOWN",
    "VECTOR_REASON_GENERATION_STALE",
    "VECTOR_REASON_INDEX_CORRUPT",
    "VECTOR_REASON_REBUILD_PENDING",
    "VECTOR_REASON_SPACE_MISMATCH",
    "VECTOR_REASON_UNAVAILABLE",
    "VECTOR_RETIREMENT_WINDOW_US",
    "VECTOR_STALENESS_LIMIT",
    "VECTOR_TEXT_TEMPLATE_VERSION",
    "EmbeddingProviderError",
    "VectorCurrentPointer",
    "VectorDegradedError",
    "VectorEntryInput",
    "VectorGenerationRecord",
    "VectorIdMapRecord",
    "VectorSpaceConfig",
    "builder_versions_trusted",
    "embedding_input_digest",
    "file_checksums_material",
    "generation_content_hash",
    "id_map_snapshot_hash",
    "id_map_snapshot_line",
    "normalize_vector",
    "render_embedding_input",
    "render_query_input",
    "validate_surrogate_id",
    "validate_vector",
    "vector_space_matches",
    "vector_staleness_limit",
]
