"""Relation graph projection domain rules (§13.3, §22.5, ADR-0016 §3-4).

Pure logic only: the frozen edge allowlist (canonical relations, verified
bindings, structurally-targeted relationship claims), the deterministic
edge identity and checksum material, the traversal budgets and the stable
degradation reason codes. Persistence lives in the storage adapter.

The graph is a projection (ADR-0001): every edge binds its canonical
(resource_type, resource_id, resource_revision). Nicknames, co-occurrence,
vector similarity and model association NEVER create edges (§13.3).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from iris_memory_core.domain.errors import ConflictError, InvalidRequestError

#: Builder identity. Any change to the edge allowlist, checksum material or
#: the build pipeline requires bumping this and building a new generation.
GRAPH_BUILDER_VERSION = 1

#: Known builder versions the read path may trust.
KNOWN_GRAPH_BUILDER_VERSIONS = frozenset({GRAPH_BUILDER_VERSION})

#: The ``graph.apply`` payload version THIS build's rebuild provably covers
#: when settling the unleased backlog (a future payload version's semantics
#: are unknown here — those jobs stay queued for a build that understands
#: them; mirrors JOB_PAYLOAD_VERSION, review round 3). Phase 10 moved both to
#: 2 together: the refs-only apply body is unchanged, so this build covers v1
#: and v2 alike (ADR-0019 §7).
GRAPH_APPLY_PAYLOAD_VERSION = 2

#: Default lag (unsettled ``graph.apply`` jobs for the requesting agent,
#: including ownerless ones) beyond which the graph route degrades stale.
GRAPH_STALENESS_LIMIT = 10_000

#: Default window a retired generation must outlive before physical cleanup.
GRAPH_RETIREMENT_WINDOW_US = 24 * 3_600_000_000

#: Edge kinds (ADR-0016 §3): the ONLY projections admitted into the graph.
GRAPH_EDGE_KIND_RELATION = "relation"
GRAPH_EDGE_KIND_BINDING = "binding"
GRAPH_EDGE_KIND_CLAIM = "claim"
GRAPH_EDGE_KINDS = frozenset(
    {GRAPH_EDGE_KIND_RELATION, GRAPH_EDGE_KIND_BINDING, GRAPH_EDGE_KIND_CLAIM}
)

#: The verified-binding edge type (binding edges always carry this type).
GRAPH_BINDING_EDGE_TYPE = "verified_binding"

#: Node kinds.
GRAPH_NODE_ENTITY = "entity"
GRAPH_NODE_IDENTITY = "external_identity"

#: Resource types a graph edge may bind.
GRAPH_EDGE_RESOURCE_TYPES = frozenset({"relation", "binding", "claim"})

#: Claim categories admitted for claim edges: relationship claims with a
#: STRUCTURAL target reference only (ADR-0016 §3).
GRAPH_CLAIM_EDGE_CATEGORY = "relationship"

#: Maximum edge-type length (same bound as relation_type).
MAX_EDGE_TYPE_CHARS = 200

#: Traversal budgets (ADR-0016 §4): server-enforced upper bounds for the
#: graph route. Malicious high-connectivity graphs cannot exceed any of
#: them regardless of the stored degree.
GRAPH_MAX_DEPTH = 2
GRAPH_MAX_FANOUT = 16
GRAPH_MAX_NODES = 64

#: Stable degradation reason codes for the graph route (ADR-0016 §6).
GRAPH_REASON_REBUILD_PENDING = "graph_rebuild_pending"
GRAPH_REASON_BUILDER_UNKNOWN = "graph_builder_unknown"
GRAPH_REASON_GENERATION_STALE = "graph_generation_stale"
GRAPH_REASON_INDEX_CORRUPT = "graph_index_corrupt"
GRAPH_REASON_AS_OF_UNSUPPORTED = "graph_as_of_unsupported"

#: Non-retryable reasons.
GRAPH_NON_RETRYABLE_REASONS = frozenset(
    {GRAPH_REASON_BUILDER_UNKNOWN, GRAPH_REASON_AS_OF_UNSUPPORTED}
)

#: Edge statuses admitted at build time (mirrors canonical visibility).
GRAPH_EDGE_VISIBLE_STATUSES = ("active", "disputed")


class GraphDegradedError(Exception):
    """The graph route must degrade with a stable reason — never return
    untrustworthy results (ADR-0016 §4/§6)."""

    def __init__(self, reason_code: str, *, retryable: bool | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        if retryable is None:
            retryable = reason_code not in GRAPH_NON_RETRYABLE_REASONS
        self.retryable = retryable


def extract_claim_edge_target(value_json: str) -> str | None:
    """Structural target extraction for claim edges (ADR-0016 §3).

    A relationship claim produces an edge ONLY when its value_json is an
    object carrying ``target_entity_id``. Text similarity, nicknames and
    model inference never produce a target — this function reads a
    structural field and nothing else."""
    try:
        value = json.loads(value_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    target = value.get("target_entity_id")
    if not isinstance(target, str) or not target:
        return None
    return target


def validate_edge_type(edge_type: str) -> str:
    if not edge_type or len(edge_type) > MAX_EDGE_TYPE_CHARS:
        raise InvalidRequestError(f"edge type must be 1..{MAX_EDGE_TYPE_CHARS} characters")
    return edge_type


@dataclass(frozen=True, slots=True)
class GraphEdgeDraft:
    """One deterministic edge the builder derives from a canonical row."""

    edge_kind: str
    edge_type: str
    source_node_id: str
    source_node_kind: str
    target_node_id: str
    target_node_kind: str
    resource_type: str
    resource_id: str
    resource_revision: int
    agent_id: str | None
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    privacy_labels: tuple[str, ...]
    status: str
    confidence: float
    importance: float
    valid_from_us: int | None
    valid_until_us: int | None
    content_hash: str

    def __post_init__(self) -> None:
        if self.edge_kind not in GRAPH_EDGE_KINDS:
            raise ConflictError(f"unknown graph edge kind: {self.edge_kind!r}")
        if self.resource_type not in GRAPH_EDGE_RESOURCE_TYPES:
            raise ConflictError(f"unknown graph edge resource: {self.resource_type!r}")
        if self.resource_revision < 1:
            raise ConflictError("graph edge resource_revision must be >= 1")
        if self.status not in GRAPH_EDGE_VISIBLE_STATUSES:
            raise ConflictError(f"unknown graph edge status: {self.status!r}")
        if self.source_node_kind not in ("entity", "external_identity") or (
            self.target_node_kind not in ("entity", "external_identity")
        ):
            raise ConflictError("unknown graph node kind")
        if not self.source_node_id or not self.target_node_id:
            raise ConflictError("graph edge endpoints must be non-empty")
        validate_edge_type(self.edge_type)


@dataclass(frozen=True, slots=True)
class GraphEdgeRecord:
    """One persisted graph edge row."""

    tenant_id: str
    generation_id: str
    edge_id: str
    edge_kind: str
    edge_type: str
    source_node_id: str
    source_node_kind: str
    target_node_id: str
    target_node_kind: str
    resource_type: str
    resource_id: str
    resource_revision: int
    agent_id: str | None
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    privacy_labels: tuple[str, ...]
    status: str
    confidence: float
    importance: float
    valid_from_us: int | None
    valid_until_us: int | None
    content_hash: str
    created_us: int


@dataclass(frozen=True, slots=True)
class GraphNodeRecord:
    tenant_id: str
    generation_id: str
    node_id: str
    node_kind: str
    node_status: str


@dataclass(frozen=True, slots=True)
class GraphGenerationRecord:
    """One immutable graph generation row — the authoritative manifest."""

    id: str
    tenant_id: str
    builder_version: int
    source_watermark: int
    tombstone_watermark: int
    node_count: int
    edge_count: int
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
class GraphCurrentPointer:
    tenant_id: str
    generation_id: str
    switch_epoch: int
    builder_version: int
    source_watermark: int
    tombstone_watermark: int
    switched_us: int


def graph_edge_id(edge: GraphEdgeDraft) -> str:
    """Deterministic edge identity — same canonical content ⇒ same id."""
    material = "\x1f".join(
        (
            edge.edge_kind,
            edge.edge_type,
            edge.source_node_kind,
            edge.source_node_id,
            edge.target_node_kind,
            edge.target_node_id,
            edge.resource_type,
            edge.resource_id,
            str(edge.resource_revision),
            edge.content_hash,
        )
    )
    return "gedge-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def graph_edge_digest(edge: GraphEdgeDraft) -> str:
    """Deterministic digest of one edge's logical content (revision-bound;
    generation id and build times excluded)."""
    material = [
        edge.edge_kind,
        edge.edge_type,
        edge.source_node_kind,
        edge.source_node_id,
        edge.target_node_kind,
        edge.target_node_id,
        edge.resource_type,
        edge.resource_id,
        str(edge.resource_revision),
        edge.agent_id or "",
        edge.space_group_id or "",
        edge.space_id or "",
        edge.session_id or "",
        *sorted(edge.privacy_labels),
        edge.status,
        str(round(edge.confidence, 6)),
        str(round(edge.importance, 6)),
        str(edge.valid_from_us or ""),
        str(edge.valid_until_us or ""),
        edge.content_hash,
    ]
    digest = hashlib.sha256()
    digest.update("\x1f".join(material).encode("utf-8"))
    return digest.hexdigest()


def graph_generation_checksum(edges: tuple[GraphEdgeDraft, ...]) -> tuple[int, int, str]:
    """edge_count, node_count and the deterministic content checksum over
    the sorted edge digests + the sorted referenced-node set."""
    digests = sorted(graph_edge_digest(edge) for edge in edges)
    nodes = set()
    for edge in edges:
        nodes.add((edge.source_node_kind, edge.source_node_id))
        nodes.add((edge.target_node_kind, edge.target_node_id))
    digest = hashlib.sha256()
    for entry in digests:
        digest.update(entry.encode("utf-8"))
        digest.update(b"\x1e")
    for kind, node_id in sorted(nodes):
        digest.update(f"{kind}\x1f{node_id}".encode())
        digest.update(b"\x1e")
    return len(edges), len(nodes), digest.hexdigest()


def graph_edge_record_digest(record: GraphEdgeRecord) -> str:
    """The same digest recomputed from a PERSISTED row — the verification
    path re-derives checksums from the authoritative rows (ADR-0016 §3)."""
    material = [
        record.edge_kind,
        record.edge_type,
        record.source_node_kind,
        record.source_node_id,
        record.target_node_kind,
        record.target_node_id,
        record.resource_type,
        record.resource_id,
        str(record.resource_revision),
        record.agent_id or "",
        record.space_group_id or "",
        record.space_id or "",
        record.session_id or "",
        *sorted(record.privacy_labels),
        record.status,
        str(round(record.confidence, 6)),
        str(round(record.importance, 6)),
        str(record.valid_from_us or ""),
        str(record.valid_until_us or ""),
        record.content_hash,
    ]
    digest = hashlib.sha256()
    digest.update("\x1f".join(material).encode("utf-8"))
    return digest.hexdigest()


__all__ = [
    "GRAPH_APPLY_PAYLOAD_VERSION",
    "GRAPH_BINDING_EDGE_TYPE",
    "GRAPH_BUILDER_VERSION",
    "GRAPH_CLAIM_EDGE_CATEGORY",
    "GRAPH_EDGE_KINDS",
    "GRAPH_EDGE_KIND_BINDING",
    "GRAPH_EDGE_KIND_CLAIM",
    "GRAPH_EDGE_KIND_RELATION",
    "GRAPH_EDGE_RESOURCE_TYPES",
    "GRAPH_EDGE_VISIBLE_STATUSES",
    "GRAPH_MAX_DEPTH",
    "GRAPH_MAX_FANOUT",
    "GRAPH_MAX_NODES",
    "GRAPH_NODE_ENTITY",
    "GRAPH_NODE_IDENTITY",
    "GRAPH_NON_RETRYABLE_REASONS",
    "GRAPH_REASON_AS_OF_UNSUPPORTED",
    "GRAPH_REASON_BUILDER_UNKNOWN",
    "GRAPH_REASON_GENERATION_STALE",
    "GRAPH_REASON_INDEX_CORRUPT",
    "GRAPH_REASON_REBUILD_PENDING",
    "GRAPH_RETIREMENT_WINDOW_US",
    "GRAPH_STALENESS_LIMIT",
    "KNOWN_GRAPH_BUILDER_VERSIONS",
    "MAX_EDGE_TYPE_CHARS",
    "GraphCurrentPointer",
    "GraphDegradedError",
    "GraphEdgeDraft",
    "GraphEdgeRecord",
    "GraphGenerationRecord",
    "GraphNodeRecord",
    "extract_claim_edge_target",
    "graph_edge_digest",
    "graph_edge_id",
    "graph_edge_record_digest",
    "graph_generation_checksum",
    "validate_edge_type",
]
