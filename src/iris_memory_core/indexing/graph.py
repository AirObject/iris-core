"""Relation graph projection service (§13.3, §22.5, ADR-0016 §3-4).

Projects ONLY the frozen edge allowlist — canonical relations, verified
bindings and structurally-targeted relationship claims — into immutable
SQLite generations behind a fenced per-tenant pointer. Nicknames,
co-occurrence, vector similarity and model association never create
edges. Incremental applies maintain the current generation; a full
rebuild derives everything from canonical state deterministically; and
the read-path trust gate fails closed with a stable degraded reason so
the recall graph route falls back to canonical claim/relation routes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from iris_memory_core.application.ports import Clock, Transaction, UnitOfWork
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.graph import (
    GRAPH_APPLY_PAYLOAD_VERSION,
    GRAPH_BINDING_EDGE_TYPE,
    GRAPH_BUILDER_VERSION,
    GRAPH_CLAIM_EDGE_CATEGORY,
    GRAPH_EDGE_KIND_BINDING,
    GRAPH_EDGE_KIND_CLAIM,
    GRAPH_EDGE_KIND_RELATION,
    GRAPH_EDGE_VISIBLE_STATUSES,
    GRAPH_NODE_ENTITY,
    GRAPH_NODE_IDENTITY,
    GRAPH_REASON_BUILDER_UNKNOWN,
    GRAPH_REASON_GENERATION_STALE,
    GRAPH_REASON_INDEX_CORRUPT,
    GRAPH_REASON_REBUILD_PENDING,
    GRAPH_RETIREMENT_WINDOW_US,
    GRAPH_STALENESS_LIMIT,
    KNOWN_GRAPH_BUILDER_VERSIONS,
    GraphCurrentPointer,
    GraphDegradedError,
    GraphEdgeDraft,
    GraphEdgeRecord,
    GraphGenerationRecord,
    extract_claim_edge_target,
    graph_edge_record_digest,
    graph_generation_checksum,
    validate_edge_type,
)
from iris_memory_core.domain.identity import EntityState
from iris_memory_core.domain.memory import (
    CLAIM_CURRENT_VISIBLE_STATUSES,
    ClaimCurrent,
    ClaimRevision,
)
from iris_memory_core.domain.model import Entity
from iris_memory_core.indexing.profile import ProjectionMetrics


@dataclass(frozen=True, slots=True)
class GraphRebuildReport:
    generation_id: str
    node_count: int
    edge_count: int
    content_checksum: str
    source_watermark: int
    tombstone_watermark: int
    switched_from: str | None


#: Outbox kinds whose unsettled work can still change what the graph
#: projection SHOULD contain: the applies themselves plus every event kind
#: whose handler schedules a graph.apply. Verification compares against
#: canonical state only when this whole pipeline is quiescent — otherwise a
#: change merely sitting in the claim.changed stage would look like drift.
GRAPH_PIPELINE_KINDS = (
    "claim.changed",
    "graph.apply",
    "memory.invalidated",
    "relation.changed",
)


class GraphProjectionService:
    """Rebuild, apply, verify, cleanup and the trust gate for the relation
    graph projection of one deployment."""

    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        staleness_limit: int = GRAPH_STALENESS_LIMIT,
        retirement_window_us: int = GRAPH_RETIREMENT_WINDOW_US,
        metrics: ProjectionMetrics | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._staleness_limit = staleness_limit
        self._retirement_window_us = retirement_window_us
        self._metrics = metrics

    # -- projection state --------------------------------------------------------

    def projection_state(self) -> str:
        with self._uow.read() as tx:
            return tx.graph.projection_state()

    def pointer_info(self, tenant_id: str) -> dict[str, object]:
        with self._uow.read() as tx:
            info = tx.graph.pointer_info(tenant_id)
            pointer = tx.graph.pointer(tenant_id)
            if pointer is not None:
                generation = tx.graph.get_generation(pointer.generation_id)
                info["node_count"] = generation.node_count
                info["edge_count"] = generation.edge_count
            return info

    # -- deterministic edge derivation -----------------------------------------------

    def derive_all_edges(self, tx: Transaction, tenant_id: str) -> tuple[GraphEdgeDraft, ...]:
        """Every admitted edge derived from one canonical enumeration
        (deterministic; same snapshot ⇒ same edge set/checksum)."""
        drafts: list[GraphEdgeDraft] = []
        referenced_entities: set[str] = set()
        binding_rows = tx.identities.all_verified_bindings(tenant_id)
        identity_ids = tx.identities.external_identity_ids(tenant_id)
        for binding in binding_rows:
            referenced_entities.add(binding.entity_id)
        claim_pairs = tx.claims.all_current_claim_pairs(tenant_id)
        claim_inputs: list[tuple[ClaimCurrent, ClaimRevision]] = []
        for claim, revision in claim_pairs:
            if revision.category != GRAPH_CLAIM_EDGE_CATEGORY:
                continue
            target = extract_claim_edge_target(revision.value_json)
            if target and target != claim.subject_entity_id:
                claim_inputs.append((claim, revision))
                referenced_entities.add(claim.subject_entity_id)
                referenced_entities.add(target)
        relation_pairs = tx.relations.all_current_relation_pairs(tenant_id)
        for relation, _revision in relation_pairs:
            referenced_entities.add(relation.source_entity_id)
            referenced_entities.add(relation.target_entity_id)
        entities = tx.identities.entities_by_id(tenant_id, referenced_entities)
        for relation, relation_revision in relation_pairs:
            if relation.status not in GRAPH_EDGE_VISIBLE_STATUSES:
                continue
            if not _entity_admitted(entities, relation.source_entity_id):
                continue
            if not _entity_admitted(entities, relation.target_entity_id):
                continue
            drafts.append(
                GraphEdgeDraft(
                    edge_kind=GRAPH_EDGE_KIND_RELATION,
                    edge_type=validate_edge_type(relation.relation_type),
                    source_node_id=relation.source_entity_id,
                    source_node_kind=GRAPH_NODE_ENTITY,
                    target_node_id=relation.target_entity_id,
                    target_node_kind=GRAPH_NODE_ENTITY,
                    resource_type="relation",
                    resource_id=relation.id,
                    resource_revision=relation_revision.revision,
                    agent_id=relation.agent_id,
                    space_group_id=relation.space_group_id,
                    space_id=relation.space_id,
                    session_id=relation.session_id,
                    privacy_labels=tuple(relation_revision.privacy_labels),
                    status=relation.status,
                    confidence=relation.confidence,
                    importance=relation.importance,
                    valid_from_us=relation.valid_from_us,
                    valid_until_us=relation.valid_until_us,
                    content_hash=relation_revision.content_hash,
                )
            )
        for binding in binding_rows:
            if binding.external_identity_id not in identity_ids:
                continue
            if not _entity_admitted(entities, binding.entity_id):
                continue
            drafts.append(
                GraphEdgeDraft(
                    edge_kind=GRAPH_EDGE_KIND_BINDING,
                    edge_type=GRAPH_BINDING_EDGE_TYPE,
                    source_node_id=binding.external_identity_id,
                    source_node_kind=GRAPH_NODE_IDENTITY,
                    target_node_id=binding.entity_id,
                    target_node_kind=GRAPH_NODE_ENTITY,
                    resource_type="binding",
                    resource_id=binding.id,
                    resource_revision=binding.revision,
                    agent_id=None,
                    space_group_id=None,
                    space_id=None,
                    session_id=None,
                    privacy_labels=(),
                    status="active",
                    confidence=binding.confidence,
                    importance=1.0,
                    valid_from_us=binding.valid_from_us,
                    valid_until_us=binding.valid_until_us,
                    content_hash=binding.proof_digest,
                )
            )
        for claim, revision in claim_inputs:
            if claim.status not in CLAIM_CURRENT_VISIBLE_STATUSES:
                continue
            target = extract_claim_edge_target(revision.value_json)
            if not target:
                continue
            if not _entity_admitted(entities, claim.subject_entity_id):
                continue
            if not _entity_admitted(entities, target):
                continue
            drafts.append(
                GraphEdgeDraft(
                    edge_kind=GRAPH_EDGE_KIND_CLAIM,
                    edge_type=validate_edge_type(revision.predicate),
                    source_node_id=claim.subject_entity_id,
                    source_node_kind=GRAPH_NODE_ENTITY,
                    target_node_id=target,
                    target_node_kind=GRAPH_NODE_ENTITY,
                    resource_type="claim",
                    resource_id=claim.id,
                    resource_revision=revision.revision,
                    agent_id=claim.agent_id,
                    space_group_id=claim.space_group_id,
                    space_id=claim.space_id,
                    session_id=claim.session_id,
                    privacy_labels=tuple(revision.privacy_labels),
                    status=claim.status,
                    confidence=claim.confidence,
                    importance=claim.importance,
                    valid_from_us=claim.valid_from_us,
                    valid_until_us=claim.valid_until_us,
                    content_hash=revision.content_hash,
                )
            )
        drafts.sort(key=_edge_sort_key)
        return tuple(drafts)

    # -- rebuild ----------------------------------------------------------------------

    def rebuild_in_tx(self, tx: Transaction, tenant_id: str) -> GraphRebuildReport:
        """Full shadow rebuild inside the caller's fenced write transaction
        (pure SQLite work). Derive → checksum → insert rows → verify →
        fenced pointer CAS → retire the previous generation. Any failure
        rolls the whole transaction back; the previous generation keeps
        serving (ADR-0016 §3)."""
        previous = tx.graph.pointer(tenant_id)
        expected_epoch = tx.graph.current_epoch(tenant_id)
        drafts = self.derive_all_edges(tx, tenant_id)
        edge_count, node_count, content_checksum = graph_generation_checksum(drafts)
        agent_watermarks = tx.tenant_watermarks(tenant_id)
        tombstone_watermark = tx.tombstone_watermark()
        now_us = self._clock.now_us()
        generation = tx.graph.insert_generation(
            tenant_id=tenant_id,
            builder_version=GRAPH_BUILDER_VERSION,
            source_watermark=max(agent_watermarks.values(), default=0),
            tombstone_watermark=tombstone_watermark,
            node_count=node_count,
            edge_count=edge_count,
            content_checksum=content_checksum,
            agent_watermarks=agent_watermarks,
            now_us=now_us,
        )
        node_status = {
            entity_id: entity.state.value
            for entity_id, entity in _entities_for(tx, tenant_id, drafts).items()
        }
        tx.graph.insert_edges(
            tenant_id,
            generation.id,
            drafts,
            node_status=node_status,
            now_us=now_us,
        )
        # Verify the persisted rows reproduce the manifest exactly (counts
        # and recomputed checksum) BEFORE the pointer flips.
        if (
            tx.graph.node_count(tenant_id, generation.id) != node_count
            or tx.graph.edge_count(tenant_id, generation.id) != edge_count
        ):
            raise GraphDegradedError(GRAPH_REASON_INDEX_CORRUPT, retryable=False)
        tx.graph.recompute_generation_manifest(tenant_id, generation.id)
        stored = tx.graph.get_generation(generation.id)
        if stored.content_checksum != content_checksum:
            raise GraphDegradedError(GRAPH_REASON_INDEX_CORRUPT, retryable=False)
        tx.graph.switch_pointer(
            tenant_id=tenant_id,
            generation=stored,
            expected_epoch=expected_epoch,
            now_us=now_us,
        )
        # The publish snapshot covers every committed change by
        # construction: settle the unleased apply backlog this generation
        # provably incorporated, so the freshness frontier follows the
        # rebuild instead of waiting for redundant worker passes
        # (ADR-0016 §7; vector's delta-clear discipline). Only payloads at
        # or below THIS build's understood version settle — a future
        # payload version's semantics are not provably covered here.
        tx.outbox.settle_unleased_kind(
            tenant_id,
            "graph.apply",
            reason_code="covered_by_rebuild",
            now_us=now_us,
            payload_version=GRAPH_APPLY_PAYLOAD_VERSION,
        )
        return GraphRebuildReport(
            generation_id=generation.id,
            node_count=node_count,
            edge_count=edge_count,
            content_checksum=content_checksum,
            source_watermark=generation.source_watermark,
            tombstone_watermark=tombstone_watermark,
            switched_from=previous.generation_id if previous is not None else None,
        )

    def rebuild(self, tenant_id: str) -> GraphRebuildReport:
        """Admin path: one fenced write transaction + post-commit gauge."""
        with self._uow.write() as tx:
            report = self.rebuild_in_tx(tx, tenant_id)
        self.emit_generation(tenant_id)
        return report

    def emit_generation(self, tenant_id: str) -> None:
        if self._metrics is None:
            return
        try:
            with self._uow.read() as tx:
                pointer = tx.graph.pointer(tenant_id)
            if pointer is not None:
                self._metrics.index_generation("graph", pointer.switch_epoch)
        except Exception:
            pass

    # -- incremental apply ---------------------------------------------------------------

    def apply_change_in_tx(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        agent_id: str | None = None,
        source_watermark: int = 0,
    ) -> bool:
        """Re-derive one canonical resource's edges inside the CURRENT
        generation (idempotent; no generation ⇒ no-op). Deletion of the old
        edges, re-derivation, node pruning and manifest re-derivation land
        in the caller's fenced transaction (ADR-0016 §3/§7)."""
        if resource_type not in ("relation", "binding", "claim", "entity", "space_group"):
            return False
        pointer = tx.graph.pointer(tenant_id)
        if pointer is None:
            return False
        generation_id = pointer.generation_id
        if resource_type == "space_group":
            # Group membership changes no edge content; the enqueue is the
            # invalidation event boundary only (ADR-0016 §7).
            return False
        if resource_type == "entity":
            return self._apply_entity(
                tx,
                tenant_id,
                generation_id,
                resource_id,
                agent_id=agent_id,
                source_watermark=source_watermark,
            )
        tx.graph.delete_resource_edges(tenant_id, generation_id, resource_type, resource_id)
        drafts = self._derive_resource_edges(tx, tenant_id, resource_type, resource_id)
        if drafts:
            entities = _entities_for(tx, tenant_id, drafts)
            tx.graph.insert_edges(
                tenant_id,
                generation_id,
                drafts,
                node_status={
                    entity_id: entity.state.value for entity_id, entity in entities.items()
                },
                now_us=self._clock.now_us(),
            )
        tx.graph.prune_orphan_nodes(tenant_id, generation_id)
        tx.graph.recompute_generation_manifest(tenant_id, generation_id)
        self._bump(
            tx, tenant_id, generation_id, resource_type, resource_id, agent_id, source_watermark
        )
        return True

    def _apply_entity(
        self,
        tx: Transaction,
        tenant_id: str,
        generation_id: str,
        entity_id: str,
        *,
        agent_id: str | None,
        source_watermark: int,
    ) -> bool:
        """Entity-level re-derivation (redirect / tombstone invalidation):
        every edge touching the entity is deleted and re-derived from
        canonical state — a tombstoned or vanished entity yields zero
        edges, so deleted subjects cannot resurrect through the graph."""
        tx.graph.delete_entity_edges(tenant_id, generation_id, entity_id)
        drafts: list[GraphEdgeDraft] = []
        for relation_id in tx.relations.relations_for_entity(tenant_id, entity_id):
            drafts.extend(self._derive_resource_edges(tx, tenant_id, "relation", relation_id))
        for binding in tx.identities.verified_bindings_for_entity(tenant_id, entity_id):
            drafts.extend(self._derive_resource_edges(tx, tenant_id, "binding", binding.id))
        for claim in tx.claims.claims_for_subject(tenant_id, entity_id):
            drafts.extend(self._derive_resource_edges(tx, tenant_id, "claim", claim.id))
        for claim, _revision in tx.claims.claims_targeting_entity(tenant_id, entity_id):
            drafts.extend(self._derive_resource_edges(tx, tenant_id, "claim", claim.id))
        if drafts:
            entities = _entities_for(tx, tenant_id, tuple(drafts))
            tx.graph.insert_edges(
                tenant_id,
                generation_id,
                tuple(drafts),
                node_status={
                    entity_id_: entity.state.value for entity_id_, entity in entities.items()
                },
                now_us=self._clock.now_us(),
            )
        tx.graph.prune_orphan_nodes(tenant_id, generation_id)
        tx.graph.recompute_generation_manifest(tenant_id, generation_id)
        self._bump(tx, tenant_id, generation_id, "entity", entity_id, agent_id, source_watermark)
        return True

    def _bump(
        self,
        tx: Transaction,
        tenant_id: str,
        generation_id: str,
        resource_type: str,
        resource_id: str,
        agent_id: str | None,
        source_watermark: int,
    ) -> None:
        owner = agent_id
        if owner is None:
            owner = self.resource_agent(tx, tenant_id, resource_type, resource_id)
        if owner:
            tx.graph.bump_generation_watermark(tenant_id, generation_id, owner, source_watermark)

    def _derive_resource_edges(
        self, tx: Transaction, tenant_id: str, resource_type: str, resource_id: str
    ) -> tuple[GraphEdgeDraft, ...]:
        """Derive the current edge set of ONE canonical resource (empty when
        the resource is gone, invisible or no longer admissible)."""
        drafts: list[GraphEdgeDraft] = []
        referenced: set[str] = set()

        def _relation_drafts() -> list[GraphEdgeDraft]:
            try:
                relation = tx.relations.get(resource_id)
                revision = tx.relations.current_revision_row(relation.id)
            except NotFoundError:
                return []
            if relation.tenant_id != tenant_id or relation.status not in (
                GRAPH_EDGE_VISIBLE_STATUSES
            ):
                return []
            referenced.update((relation.source_entity_id, relation.target_entity_id))
            return [
                GraphEdgeDraft(
                    edge_kind=GRAPH_EDGE_KIND_RELATION,
                    edge_type=validate_edge_type(relation.relation_type),
                    source_node_id=relation.source_entity_id,
                    source_node_kind=GRAPH_NODE_ENTITY,
                    target_node_id=relation.target_entity_id,
                    target_node_kind=GRAPH_NODE_ENTITY,
                    resource_type="relation",
                    resource_id=relation.id,
                    resource_revision=revision.revision,
                    agent_id=relation.agent_id,
                    space_group_id=relation.space_group_id,
                    space_id=relation.space_id,
                    session_id=relation.session_id,
                    privacy_labels=tuple(revision.privacy_labels),
                    status=relation.status,
                    confidence=relation.confidence,
                    importance=relation.importance,
                    valid_from_us=relation.valid_from_us,
                    valid_until_us=relation.valid_until_us,
                    content_hash=revision.content_hash,
                )
            ]

        def _binding_drafts() -> list[GraphEdgeDraft]:
            try:
                binding = tx.get_binding(resource_id)
            except NotFoundError:
                return []
            if binding.tenant_id != tenant_id or binding.state.value != "verified":
                return []
            if tx.is_tombstoned(tenant_id, "binding", binding.id):
                return []
            if tx.is_tombstoned(tenant_id, "external_identity", binding.external_identity_id):
                return []
            referenced.add(binding.entity_id)
            return [
                GraphEdgeDraft(
                    edge_kind=GRAPH_EDGE_KIND_BINDING,
                    edge_type=GRAPH_BINDING_EDGE_TYPE,
                    source_node_id=binding.external_identity_id,
                    source_node_kind=GRAPH_NODE_IDENTITY,
                    target_node_id=binding.entity_id,
                    target_node_kind=GRAPH_NODE_ENTITY,
                    resource_type="binding",
                    resource_id=binding.id,
                    resource_revision=binding.revision,
                    agent_id=None,
                    space_group_id=None,
                    space_id=None,
                    session_id=None,
                    privacy_labels=(),
                    status="active",
                    confidence=binding.confidence,
                    importance=1.0,
                    valid_from_us=binding.valid_from_us,
                    valid_until_us=binding.valid_until_us,
                    content_hash=binding.proof_digest,
                )
            ]

        def _claim_drafts() -> list[GraphEdgeDraft]:
            try:
                claim = tx.claims.get(resource_id)
                revision = tx.claims.current_revision_row(claim.id)
            except NotFoundError:
                return []
            if claim.tenant_id != tenant_id or claim.status not in (CLAIM_CURRENT_VISIBLE_STATUSES):
                return []
            if revision.category != GRAPH_CLAIM_EDGE_CATEGORY:
                return []
            target = extract_claim_edge_target(revision.value_json)
            if not target or target == claim.subject_entity_id:
                return []
            referenced.update((claim.subject_entity_id, target))
            return [
                GraphEdgeDraft(
                    edge_kind=GRAPH_EDGE_KIND_CLAIM,
                    edge_type=validate_edge_type(revision.predicate),
                    source_node_id=claim.subject_entity_id,
                    source_node_kind=GRAPH_NODE_ENTITY,
                    target_node_id=target,
                    target_node_kind=GRAPH_NODE_ENTITY,
                    resource_type="claim",
                    resource_id=claim.id,
                    resource_revision=revision.revision,
                    agent_id=claim.agent_id,
                    space_group_id=claim.space_group_id,
                    space_id=claim.space_id,
                    session_id=claim.session_id,
                    privacy_labels=tuple(revision.privacy_labels),
                    status=claim.status,
                    confidence=claim.confidence,
                    importance=claim.importance,
                    valid_from_us=claim.valid_from_us,
                    valid_until_us=claim.valid_until_us,
                    content_hash=revision.content_hash,
                )
            ]

        if resource_type == "relation":
            drafts.extend(_relation_drafts())
        elif resource_type == "binding":
            drafts.extend(_binding_drafts())
        elif resource_type == "claim":
            drafts.extend(_claim_drafts())
        if not drafts:
            return ()
        entities = tx.identities.entities_by_id(tenant_id, referenced)
        identity_ids: set[str] | None = None
        admitted: list[GraphEdgeDraft] = []
        for draft in drafts:
            endpoints = [(draft.source_node_kind, draft.source_node_id)]
            if draft.target_node_kind == GRAPH_NODE_ENTITY:
                endpoints.append((draft.target_node_kind, draft.target_node_id))
            ok = True
            for kind, node_id in endpoints:
                if kind == GRAPH_NODE_ENTITY and not _entity_admitted(entities, node_id):
                    ok = False
                    break
            if not ok:
                continue
            if draft.edge_kind == GRAPH_EDGE_KIND_BINDING:
                if identity_ids is None:
                    identity_ids = tx.identities.external_identity_ids(tenant_id)
                if draft.source_node_id not in identity_ids:
                    continue
            admitted.append(draft)
        admitted.sort(key=_edge_sort_key)
        return tuple(admitted)

    @staticmethod
    def resource_agent(
        tx: Transaction, tenant_id: str, resource_type: str, resource_id: str
    ) -> str | None:
        """Best-effort owning agent of a graph resource (job attribution)."""
        try:
            if resource_type == "relation":
                return tx.relations.get(resource_id).agent_id
            if resource_type == "claim":
                return tx.claims.get(resource_id).agent_id
        except Exception:
            return None
        return None

    # -- trust gate ----------------------------------------------------------------------

    def trusted_generation_in_tx(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        minimum_watermark: int | None = None,
    ) -> tuple[GraphCurrentPointer, GraphGenerationRecord]:
        """Read-path trust gate (ADR-0016 §4). Structural checks only —
        pointer resolution, generation status/tenant, builder version,
        manifest counts vs authoritative rows, watermark monotonicity and
        the freshness frontier; full content checksum verification runs in
        rebuild/verify/cleanup."""
        state = tx.graph.projection_state()
        if state != "ready":
            raise GraphDegradedError(GRAPH_REASON_REBUILD_PENDING)
        pointer = tx.graph.pointer(tenant_id)
        if pointer is None:
            raise GraphDegradedError(GRAPH_REASON_REBUILD_PENDING)
        try:
            generation = tx.graph.get_generation(pointer.generation_id)
        except Exception:
            raise GraphDegradedError(GRAPH_REASON_INDEX_CORRUPT) from None
        if generation.status != "verified":
            raise GraphDegradedError(GRAPH_REASON_INDEX_CORRUPT)
        if generation.tenant_id != tenant_id:
            raise GraphDegradedError(GRAPH_REASON_INDEX_CORRUPT, retryable=False)
        if generation.builder_version not in KNOWN_GRAPH_BUILDER_VERSIONS:
            raise GraphDegradedError(GRAPH_REASON_BUILDER_UNKNOWN, retryable=False)
        stored_nodes = tx.graph.node_count(tenant_id, generation.id)
        stored_edges = tx.graph.edge_count(tenant_id, generation.id)
        if stored_nodes != generation.node_count or stored_edges != generation.edge_count:
            raise GraphDegradedError(GRAPH_REASON_INDEX_CORRUPT)
        if tx.tombstone_watermark() < pointer.tombstone_watermark:
            raise GraphDegradedError(GRAPH_REASON_INDEX_CORRUPT, retryable=False)
        backlog = tx.outbox.unsettled_job_count(tenant_id, agent_id, "graph.apply")
        backlog += tx.outbox.unsettled_null_agent_job_count(tenant_id, "graph.apply")
        if self._metrics is not None:
            self._metrics.index_lag("graph", backlog)
        if minimum_watermark is not None:
            agent_watermark = generation.agent_watermarks().get(agent_id)
            if agent_watermark is None:
                if backlog > 0:
                    raise GraphDegradedError(GRAPH_REASON_GENERATION_STALE)
            elif agent_watermark < minimum_watermark or backlog > 0:
                raise GraphDegradedError(GRAPH_REASON_GENERATION_STALE)
        elif backlog > self._staleness_limit:
            raise GraphDegradedError(GRAPH_REASON_GENERATION_STALE)
        return pointer, generation

    # -- verification and cleanup ------------------------------------------------------------

    def verify_in_tx(self, tx: Transaction, tenant_id: str) -> bool:
        """Full content verification: counts, recomputed checksum (from the
        persisted edge rows), zero dangling nodes, and — once the whole
        projection pipeline is quiescent (no unsettled apply or producer
        event, which by the freshness invariant means every committed
        change is incorporated) — exact equality with a fresh canonical
        derivation (missing, stale-referenced, revoked or tombstoned
        resources all diverge). Any mismatch fails CLOSED: the projection
        is marked pending_rebuild and the verdict is RETURNED, never
        raised — an escaping exception would roll the caller's worker
        transaction back together with the state write, leaving a corrupt
        generation marked ready (review round 3)."""
        pointer = tx.graph.pointer(tenant_id)
        if pointer is None:
            return True

        def _fail() -> bool:
            # Quarantine the actual generation as well as global readiness:
            # another tenant's successful rebuild cannot re-admit it.
            tx.graph.retire_generation(pointer.generation_id)
            tx.graph.set_projection_state("pending_rebuild")
            return False

        try:
            generation = tx.graph.get_generation(pointer.generation_id)
        except Exception:
            return _fail()
        if generation.status != "verified":
            return _fail()
        rows = tx.graph.all_edges(tenant_id, generation.id)
        stored_nodes = tx.graph.node_count(tenant_id, generation.id)
        if len(rows) != generation.edge_count:
            return _fail()
        digest = hashlib.sha256()
        nodes: set[tuple[str, str]] = set()
        for record in sorted(rows, key=graph_edge_record_digest):
            digest.update(graph_edge_record_digest(record).encode("utf-8"))
            digest.update(b"\x1e")
            nodes.add((record.source_node_kind, record.source_node_id))
            nodes.add((record.target_node_kind, record.target_node_id))
        for kind, node_id in sorted(nodes):
            digest.update(f"{kind}\x1f{node_id}".encode())
            digest.update(b"\x1e")
        if digest.hexdigest() != generation.content_checksum:
            return _fail()
        if stored_nodes != len(nodes):
            return _fail()
        # Content equality against canonical state runs only when the WHOLE
        # projection pipeline is quiescent (no unsettled apply and no
        # unsettled producer event): at that point every committed change is
        # incorporated, so any divergence is corruption. With work in flight
        # the projection is legitimately allowed to differ (the pending
        # applies reconcile it); only the structural checks above apply.
        backlog = sum(
            tx.outbox.unsettled_tenant_job_count(tenant_id, kind) for kind in GRAPH_PIPELINE_KINDS
        )
        if backlog == 0:
            drafts = self.derive_all_edges(tx, tenant_id)
            edge_count, node_count, content_checksum = graph_generation_checksum(drafts)
            if (edge_count, node_count, content_checksum) != (
                generation.edge_count,
                generation.node_count,
                generation.content_checksum,
            ):
                return _fail()
        return True

    @staticmethod
    def resource_still_admissible(tx: Transaction, tenant_id: str, edge: GraphEdgeRecord) -> bool:
        """The edge's canonical resource is still the one the edge binds:
        present, tenant-local, visible-status, at the SAME current revision,
        and untouched by the tombstone ledger. The traversal route runs this
        BEFORE any frontier expansion so a stale projection edge (correct /
        revoke / privacy change pending its apply) can neither become a
        candidate nor act as a traversal springboard (ADR-0016 §4, review
        round 3)."""
        try:
            if edge.resource_type == "relation":
                relation = tx.relations.get(edge.resource_id)
                return (
                    relation.tenant_id == tenant_id
                    and relation.status in GRAPH_EDGE_VISIBLE_STATUSES
                    and relation.current_revision == edge.resource_revision
                    and not tx.is_tombstoned(tenant_id, "relation", relation.id)
                )
            if edge.resource_type == "binding":
                binding = tx.get_binding(edge.resource_id)
                return (
                    binding.tenant_id == tenant_id
                    and binding.state.value == "verified"
                    and binding.revision == edge.resource_revision
                    and not tx.is_tombstoned(tenant_id, "binding", binding.id)
                    and not tx.is_tombstoned(
                        tenant_id, "external_identity", binding.external_identity_id
                    )
                )
            if edge.resource_type == "claim":
                claim = tx.claims.get(edge.resource_id)
                return (
                    claim.tenant_id == tenant_id
                    and claim.status in CLAIM_CURRENT_VISIBLE_STATUSES
                    and claim.current_revision == edge.resource_revision
                    and not tx.is_tombstoned(tenant_id, "claim", claim.id)
                )
        except Exception:
            return False
        return False

    def cleanup_in_tx(self, tx: Transaction, tenant_id: str) -> tuple[str, ...]:
        """Verify then delete retired generations beyond the rollback
        window (their node/edge rows go with them). A failed verification
        persists its pending_rebuild verdict in THIS transaction and skips
        deletion — the worker path commits the verdict instead of rolling
        it back with an exception (review round 3)."""
        if not self.verify_in_tx(tx, tenant_id):
            return ()
        return tx.graph.delete_retired_generations(
            tenant_id,
            keep=2,
            older_than_us=self._clock.now_us() - self._retirement_window_us,
        )


# -- helpers ----------------------------------------------------------------------


def _edge_sort_key(draft: GraphEdgeDraft) -> tuple[str, str, str]:
    return (draft.edge_kind, draft.edge_type, draft.resource_id)


def _entity_admitted(entities: dict[str, Entity], entity_id: str) -> bool:
    entity = entities.get(entity_id)
    if entity is None:
        return False
    return entity.state not in (EntityState.TOMBSTONED, EntityState.REDIRECTED)


def _entities_for(
    tx: Transaction, tenant_id: str, drafts: tuple[GraphEdgeDraft, ...]
) -> dict[str, Entity]:
    ids = {
        node_id
        for draft in drafts
        for kind, node_id in (
            (draft.source_node_kind, draft.source_node_id),
            (draft.target_node_kind, draft.target_node_id),
        )
        if kind == GRAPH_NODE_ENTITY
    }
    return tx.identities.entities_by_id(tenant_id, ids)


__all__ = ["GraphProjectionService", "GraphRebuildReport"]
