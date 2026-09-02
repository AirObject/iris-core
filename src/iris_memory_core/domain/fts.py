"""FTS projection domain rules (§22.1, ADR-0014 §1-2).

Pure logic only: versioned builder/tokenizer identity, index-text
templates and normalization, FTS5 query sanitization and the deterministic
generation checksum. Persistence lives in the storage adapter.

The FTS index is a projection (ADR-0001): every document binds
``(resource_type, resource_id, resource_revision)`` and stores only
rebuildable metadata. No code path may treat index content as fact — the
recall rehydrate re-reads Canonical rows by id+revision before anything is
returned.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from iris_memory_core.domain.errors import InvalidRequestError

#: Builder identity triple. Any change to normalization, templates,
#: stopwords, language handling or the tokenizer spelling requires bumping
#: these and rebuilding a shadow generation (ADR-0014 §1).
FTS_BUILDER_VERSION = 1
FTS_TOKENIZER_VERSION = 1
FTS_TEXT_TEMPLATE_VERSION = 1

#: Frozen builder configuration recorded on every generation. Stopwords are
#: applied at QUERY time only (never stripped from indexed text — phrase
# matching must not be distorted by builder-side stopword removal).
FTS_CONFIG: dict[str, object] = {
    "language": "generic",
    "normalization": ["nfkc_casefold", "collapse_whitespace", "strip_control"],
    "stopwords": "query-side:common-latin-v1",
    "stemming": "none",
    "tokenizer": "unicode61",
    "text_template_version": FTS_TEXT_TEMPLATE_VERSION,
}

#: Resources whose canonical text enters the FTS projection. Observations
#: stay behind the recent-context window; artifacts are never indexed
#: (external_ref is structurally never fetched, ADR-0013 §4).
FTS_INDEXABLE_RESOURCE_TYPES = frozenset({"claim", "episode", "note"})

#: Query-side stopwords (common Latin particles). A query made only of
#: stopwords yields no usable tokens and is rejected rather than broadened.
FTS_QUERY_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "and", "or", "to", "in", "on", "is", "are", "was", "were", "be"}
)

#: Maximum tokens accepted in one FTS query after sanitization.
FTS_MAX_QUERY_TOKENS = 16

#: Maximum characters of indexed text per document (defense in depth on top
#: of canonical text limits).
FTS_MAX_INDEX_TEXT_CHARS = 24_000

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[0-9a-z_\u00c0-\uffff]+")


class FtsQueryError(InvalidRequestError):
    """Raised when a user query cannot be converted to a safe FTS5 match."""

    code = "invalid_request"


@dataclass(frozen=True, slots=True)
class FtsDocumentInput:
    """One document handed to the projection builder (already canonical)."""

    tenant_id: str
    resource_type: str
    resource_id: str
    resource_revision: int
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    canonical_status: str
    privacy_labels: tuple[str, ...]
    subject_entity_id: str | None
    content_hash: str
    occurred_us: int
    valid_from_us: int | None
    valid_until_us: int | None
    raw_text: str

    def __post_init__(self) -> None:
        if self.resource_type not in FTS_INDEXABLE_RESOURCE_TYPES:
            raise InvalidRequestError(f"resource type {self.resource_type!r} is not FTS-indexable")
        if self.resource_revision < 1:
            raise InvalidRequestError("resource_revision must be >= 1")


@dataclass(frozen=True, slots=True)
class FtsGenerationRecord:
    """Immutable generation row (status ∈ {verified, retired})."""

    id: str
    tenant_id: str
    builder_version: int
    tokenizer_version: int
    config_json: str
    source_watermark: int
    tombstone_watermark: int
    document_count: int
    content_checksum: str
    status: str
    created_us: int
    verified_us: int


@dataclass(frozen=True, slots=True)
class FtsCurrentPointer:
    tenant_id: str
    generation_id: str
    builder_version: int
    tokenizer_version: int
    source_watermark: int
    tombstone_watermark: int
    switched_us: int


@dataclass(frozen=True, slots=True)
class FtsDocumentRecord:
    id: int
    tenant_id: str
    generation_id: str
    resource_type: str
    resource_id: str
    resource_revision: int
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    canonical_status: str
    privacy_labels: tuple[str, ...]
    subject_entity_id: str | None
    content_hash: str
    occurred_us: int
    valid_from_us: int | None
    valid_until_us: int | None
    index_text: str
    doc_status: str
    invalidated_us: int | None
    builder_version: int
    source_watermark: int
    tombstone_watermark: int
    created_us: int


def render_index_text(resource_type: str, **fields: str | None) -> str:
    """Versioned text template per resource type (§22.1: canonical text,
    controlled aliases and safe summaries only)."""
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
    raise InvalidRequestError(f"resource type {resource_type!r} is not FTS-indexable")


def normalize_index_text(text: str) -> str:
    """NFKC casefold + whitespace collapse + control strip (FTS_CONFIG)."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = _CONTROL_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    if len(normalized) > FTS_MAX_INDEX_TEXT_CHARS:
        normalized = normalized[:FTS_MAX_INDEX_TEXT_CHARS]
    return normalized


def fts_query_tokens(query: str) -> tuple[str, ...]:
    """Tokenize a user query the way the builder normalizes index text."""
    normalized = normalize_index_text(query)
    tokens = tuple(token for token in _TOKEN_RE.findall(normalized) if token)
    meaningful = tuple(token for token in tokens if token not in FTS_QUERY_STOPWORDS)
    return meaningful


def build_fts_query(query: str) -> str:
    """Convert free text to a safe FTS5 MATCH expression.

    Every token is double-quoted (no FTS5 syntax characters can survive),
    tokens are ANDed, and the count is capped. User input is NEVER spliced
    into MATCH syntax raw (ADR-0014 §8). Returns ``*``-free exact-token
    conjunctions; an empty/stopword-only query raises ``invalid_request``
    rather than broadening to a full scan.
    """
    tokens = fts_query_tokens(query)[:FTS_MAX_QUERY_TOKENS]
    if not tokens:
        raise FtsQueryError("query contains no searchable tokens")
    return " AND ".join(f'"{token}"' for token in tokens)


def generation_checksum(documents: Iterable[FtsDocumentInput | FtsDocumentRecord]) -> str:
    """Deterministic checksum over the sorted document identity set.

    Same canonical snapshot ⇒ same checksum; shadow verification and
    replays rely on this being order-independent and content-bound. Both
    build inputs and the persisted records read back for verification carry
    the checksum-relevant fields, so the function accepts either.
    """
    material = sorted(
        (
            doc.resource_type,
            doc.resource_id,
            str(doc.resource_revision),
            doc.content_hash,
            doc.scope_key,
            doc.canonical_status,
            ",".join(sorted(set(doc.privacy_labels))),
        )
        for doc in documents
    )
    digest = hashlib.sha256()
    for row in material:
        digest.update("\x1f".join(row).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


#: Known builder/tokenizer versions the read path may trust. A generation
#: stamped with an unknown (future or corrupted) version disables the FTS
#: route instead of returning untrustworthy results (ADR-0014 §1).
KNOWN_FTS_BUILDER_VERSIONS = frozenset({FTS_BUILDER_VERSION})
KNOWN_FTS_TOKENIZER_VERSIONS = frozenset({FTS_TOKENIZER_VERSION})


def builder_versions_trusted(builder_version: int, tokenizer_version: int) -> bool:
    return (
        builder_version in KNOWN_FTS_BUILDER_VERSIONS
        and tokenizer_version in KNOWN_FTS_TOKENIZER_VERSIONS
    )


def fts_staleness_limit() -> int:
    """Watermark lag beyond which the FTS route reports stale degradation."""
    return 10_000


__all__ = [
    "FTS_BUILDER_VERSION",
    "FTS_CONFIG",
    "FTS_INDEXABLE_RESOURCE_TYPES",
    "FTS_MAX_INDEX_TEXT_CHARS",
    "FTS_MAX_QUERY_TOKENS",
    "FTS_QUERY_STOPWORDS",
    "FTS_TEXT_TEMPLATE_VERSION",
    "FTS_TOKENIZER_VERSION",
    "FtsCurrentPointer",
    "FtsDocumentInput",
    "FtsDocumentRecord",
    "FtsGenerationRecord",
    "FtsQueryError",
    "build_fts_query",
    "builder_versions_trusted",
    "fts_query_tokens",
    "fts_staleness_limit",
    "generation_checksum",
    "normalize_index_text",
    "render_index_text",
]
