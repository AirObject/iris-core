"""FTS5 projection application service (§22.1, ADR-0014 §1-2).

The FTS index is a rebuildable projection (ADR-0001): the shadow rebuild
collects canonical live revisions inside ONE write transaction, records the
observed watermarks, verifies count/checksum/sample queries, and flips the
per-tenant current pointer in that same transaction. Any failure leaves the
previous verified generation in service. Incremental maintenance consumes
the existing refs-only change events; tombstones logically invalidate
documents immediately and physical cleanup runs asynchronously.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.domain.errors import ConflictError, NotFoundError
from iris_memory_core.domain.fts import (
    FTS_BUILDER_VERSION,
    FTS_CONFIG,
    FTS_INDEXABLE_RESOURCE_TYPES,
    FTS_TOKENIZER_VERSION,
    FtsDocumentInput,
    FtsDocumentRecord,
    builder_versions_trusted,
    fts_staleness_limit,
    generation_checksum,
    render_index_text,
)
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.memory import CLAIM_CURRENT_VISIBLE_STATUSES
from iris_memory_core.domain.note import NoteStatus

#: Live note statuses whose text enters the index (terminal/promoted notes
#: are historical only; tombstoned rows are excluded everywhere).
FTS_NOTE_STATUSES = (
    NoteStatus.INBOX.value,
    NoteStatus.PINNED.value,
    NoteStatus.SNOOZED.value,
)

#: Stable degradation reason codes for the FTS route (ADR-0014 §10).
FTS_REASON_REBUILD_PENDING = "fts_rebuild_pending"
FTS_REASON_BUILDER_UNKNOWN = "fts_builder_unknown"
FTS_REASON_GENERATION_STALE = "fts_generation_stale"
FTS_REASON_INDEX_CORRUPT = "fts_index_corrupt"
FTS_REASON_UNAVAILABLE = "fts_unavailable"


class FtsDegradedError(Exception):
    """The FTS route must degrade with a stable reason — never return
    untrustworthy results (ADR-0014 §1)."""

    def __init__(self, reason_code: str, *, retryable: bool = True) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class FtsRebuildReport:
    generation_id: str
    document_count: int
    content_checksum: str
    source_watermark: int
    tombstone_watermark: int
    switched_from: str | None


@dataclass(frozen=True, slots=True)
class FtsSearchHit:
    document: FtsDocumentRecord
    relevance: float


def _claim_document(claim: Any, revision: Any) -> FtsDocumentInput:
    return FtsDocumentInput(
        tenant_id=claim.tenant_id,
        resource_type="claim",
        resource_id=claim.id,
        resource_revision=revision.revision,
        agent_id=claim.agent_id,
        space_group_id=claim.space_group_id,
        space_id=claim.space_id,
        session_id=claim.session_id,
        scope_key=claim.scope_key,
        canonical_status=revision.status,
        privacy_labels=revision.privacy_labels,
        subject_entity_id=revision.subject_entity_id,
        content_hash=revision.content_hash,
        occurred_us=revision.recorded_at_us,
        valid_from_us=claim.valid_from_us,
        valid_until_us=claim.valid_until_us,
        raw_text=render_index_text(
            "claim",
            predicate=revision.predicate,
            canonical_text=revision.canonical_text,
        ),
    )


def _episode_document(episode: Any, revision: Any) -> FtsDocumentInput:
    return FtsDocumentInput(
        tenant_id=episode.tenant_id,
        resource_type="episode",
        resource_id=episode.id,
        resource_revision=revision.revision,
        agent_id=episode.agent_id,
        space_group_id=episode.space_group_id,
        space_id=episode.space_id,
        session_id=episode.session_id,
        scope_key=episode.scope_key,
        canonical_status=revision.status,
        privacy_labels=revision.privacy_labels,
        subject_entity_id=None,
        content_hash=revision.content_hash,
        occurred_us=revision.created_us,
        valid_from_us=None,
        valid_until_us=None,
        raw_text=render_index_text("episode", title=revision.title, summary=revision.summary),
    )


def _note_document(note: Any, revision: Any) -> FtsDocumentInput:
    return FtsDocumentInput(
        tenant_id=note.tenant_id,
        resource_type="note",
        resource_id=note.id,
        resource_revision=revision.revision,
        agent_id=note.agent_id,
        space_group_id=note.space_group_id,
        space_id=note.space_id,
        session_id=note.session_id,
        scope_key=note.scope_key,
        canonical_status=revision.status,
        privacy_labels=revision.privacy_labels,
        subject_entity_id=None,
        content_hash=revision.content_hash,
        occurred_us=revision.created_us,
        valid_from_us=None,
        valid_until_us=None,
        raw_text=render_index_text("note", title=revision.title, body=revision.body),
    )


def collect_fts_documents(
    tx: Transaction, tenant_id: str, *, batch: int = 200
) -> list[FtsDocumentInput]:
    """Enumerate every live indexable resource of the tenant (current
    revisions only, tombstone-free by construction of the canonical
    surfaces). Runs inside the caller's transaction so the rebuild sees one
    consistent snapshot. Every resource family paginates by keyset — a
    rebuild must never silently truncate: a truncated-but-"verified"
    generation is the worst possible outcome (ADR-0014 §1).
    """
    documents: list[FtsDocumentInput] = []
    agents = sorted(
        set(tx.retention.claim_agents(tenant_id))
        | set(tx.retention.episode_agents(tenant_id))
        | set(tx.retention.note_agents(tenant_id))
    )
    for agent_id in agents:
        # Claims: keyset pagination over the maintenance-scope enumeration.
        cursor_updated: int | None = None
        cursor_id: str | None = None
        while True:
            page = tx.claims.search_page(
                tenant_id=tenant_id,
                agent_id=agent_id,
                space_group_id=None,
                space_id=None,
                session_id=None,
                statuses=list(CLAIM_CURRENT_VISIBLE_STATUSES),
                cursor_updated_us=cursor_updated,
                cursor_id=cursor_id,
                limit=batch,
                scope_mode="maintenance",
            )
            if not page:
                break
            for claim, revision in page:
                cursor_updated = claim.updated_us
                cursor_id = claim.id
                documents.append(_claim_document(claim, revision))
            if len(page) < batch:
                break
        # Episodes: keyset pagination over (updated_us DESC, id DESC).
        episode_cursor_updated: int | None = None
        episode_cursor_id: str | None = None
        while True:
            episode_page = tx.episodes.list_episodes(
                tenant_id,
                agent_id,
                statuses=("open", "sealed"),
                limit=batch,
                cursor_updated_us=episode_cursor_updated,
                cursor_id=episode_cursor_id,
            )
            if not episode_page:
                break
            for episode in episode_page:
                episode_cursor_updated = episode.updated_us
                episode_cursor_id = episode.id
                episode_revision = tx.episodes.current_revision_row(episode.id)
                documents.append(_episode_document(episode, episode_revision))
            if len(episode_page) < batch:
                break
        # Notes: keyset pagination over (created_us ASC, id ASC).
        note_cursor_created: int | None = None
        note_cursor_id: str | None = None
        while True:
            note_page = tx.notes.list_notes(
                tenant_id,
                agent_id,
                statuses=FTS_NOTE_STATUSES,
                limit=batch,
                cursor_created_us=note_cursor_created,
                cursor_id=note_cursor_id,
            )
            if not note_page:
                break
            for note in note_page:
                note_cursor_created = note.created_us
                note_cursor_id = note.id
                note_revision = tx.notes.current_revision_row(note.id)
                documents.append(_note_document(note, note_revision))
            if len(note_page) < batch:
                break
    documents.sort(key=lambda doc: (doc.resource_type, doc.resource_id))
    return documents


class FtsProjectionService:
    """Shadow rebuild, atomic switch, incremental apply and trusted search."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    # -- capability ----------------------------------------------------------

    def capability_available(self) -> bool:
        with self._uow.read() as tx:
            return tx.fts.ensure_index()

    def projection_state(self) -> str:
        with self._uow.read() as tx:
            return tx.fts.projection_state()

    # -- rebuild ---------------------------------------------------------------

    def rebuild(self, tenant_id: str, *, actor: str = "admin") -> FtsRebuildReport:
        """Full shadow rebuild with verification and atomic pointer switch.

        The whole build (collection, generation insert, document upserts,
        count/checksum/sample verification, pointer flip) runs in ONE write
        transaction under the writer gate: either the new generation is
        verified and current, or nothing changed and the previous verified
        generation keeps serving.
        """
        del actor  # audited by the caller (admin plane)
        with self._uow.write() as tx:
            return self.rebuild_in_tx(tx, tenant_id)

    def rebuild_in_tx(self, tx: Transaction, tenant_id: str) -> FtsRebuildReport:
        """Transaction-bound rebuild core (handler/admin share one body)."""
        if not tx.fts.ensure_index():
            raise FtsDegradedError(FTS_REASON_UNAVAILABLE, retryable=False)
        documents = collect_fts_documents(tx, tenant_id)
        checksum = generation_checksum(documents)
        # The rebuild snapshot is transaction-consistent: it covers every
        # committed change of each agent up to that agent's current seq. The
        # per-agent watermarks feed the generation's reporting metadata and
        # each document row's source_watermark; the TRUST GATE does not use
        # them (it counts the outbox backlog, which a full rebuild drains by
        # construction).
        agent_ids = sorted(
            set(tx.retention.claim_agents(tenant_id))
            | set(tx.retention.episode_agents(tenant_id))
            | set(tx.retention.note_agents(tenant_id))
        )
        agent_watermarks: dict[str, int] = {}
        source_watermark = 0
        for agent_id in agent_ids:
            state = tx.watermark(tenant_id, agent_id)
            agent_seq = state.current_seq if state is not None else 0
            agent_watermarks[agent_id] = agent_seq
            source_watermark = max(source_watermark, agent_seq)
        tombstone_watermark = tx.tombstone_watermark()
        previous = tx.fts.pointer(tenant_id)
        generation = tx.fts.insert_generation(
            tenant_id=tenant_id,
            builder_version=FTS_BUILDER_VERSION,
            tokenizer_version=FTS_TOKENIZER_VERSION,
            config_json=canonical_json(FTS_CONFIG),
            source_watermark=source_watermark,
            tombstone_watermark=tombstone_watermark,
            document_count=len(documents),
            content_checksum=checksum,
        )
        for document in documents:
            tx.fts.upsert_document(
                generation_id=generation.id,
                document=document,
                source_watermark=agent_watermarks.get(document.agent_id, source_watermark),
                tombstone_watermark=tombstone_watermark,
                builder_version=FTS_BUILDER_VERSION,
            )
        self._verify_generation(tx, generation, documents)
        tx.fts.switch_pointer(tenant_id=tenant_id, generation=generation)
        return FtsRebuildReport(
            generation_id=generation.id,
            document_count=len(documents),
            content_checksum=checksum,
            source_watermark=source_watermark,
            tombstone_watermark=tombstone_watermark,
            switched_from=previous.generation_id if previous is not None else None,
        )

    @staticmethod
    def _verify_generation(
        tx: Transaction, generation: Any, documents: list[FtsDocumentInput]
    ) -> None:
        """Count + recomputed checksum + sample queries before the switch.

        The checksum is recomputed from the PERSISTED rows of the generation,
        not from the in-memory build list: this is what proves the documents
        that will serve queries are exactly the ones that were collected.
        """
        count = tx.fts.count_documents(generation.id)
        if count != len(documents):
            raise ConflictError("fts generation verification failed: document count mismatch")
        stored_checksum = generation_checksum(list(tx.fts.documents_for_generation(generation.id)))
        if stored_checksum != generation.content_checksum:
            raise ConflictError("fts generation verification failed: checksum mismatch")
        probes = documents[:3] or []
        for probe in probes:
            token = probe.raw_text.casefold().split(":", 1)[-1].strip().split()
            term = next((part for part in token if len(part) >= 4), None)
            if term is None:
                continue
            hits = tx.fts.sample_query(generation.id, f'"{term}"', limit=1)
            if not hits:
                raise ConflictError("fts generation verification failed: sample query miss")

    # -- incremental apply -------------------------------------------------------

    def apply_change_in_tx(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> bool:
        """Upsert or invalidate one resource inside the current generation.

        Returns True when a document row changed. No current generation ⇒
        no-op (nothing has been built yet; the rebuild owns initial load).
        The staleness trust gate is derived from the outbox backlog — the
        completion CAS of THIS job is what shrinks it — so there is no
        applied-watermark bookkeeping here to get wrong.
        """
        if resource_type not in FTS_INDEXABLE_RESOURCE_TYPES:
            return False
        pointer = tx.fts.pointer(tenant_id)
        if pointer is None:
            return False

        if tx.is_tombstoned(tenant_id, resource_type, resource_id):
            changed = tx.fts.invalidate_document(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            return changed > 0
        document, agent_id = self._canonical_document(tx, tenant_id, resource_type, resource_id)
        if document is None:
            # Missing or no longer live: logically invalidate any stale doc.
            changed = tx.fts.invalidate_document(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            del agent_id
            return changed > 0
        existing = tx.fts.document_for_resource(tenant_id, resource_type, resource_id)
        if (
            existing is not None
            and existing.generation_id == pointer.generation_id
            and existing.resource_revision == document.resource_revision
            and existing.doc_status == "active"
        ):
            return False
        watermark_state = tx.watermark(tenant_id, document.agent_id)
        source_watermark = watermark_state.current_seq if watermark_state is not None else 0
        tx.fts.upsert_document(
            generation_id=pointer.generation_id,
            document=document,
            source_watermark=source_watermark,
            tombstone_watermark=tx.tombstone_watermark(),
            builder_version=pointer.builder_version,
        )
        return True

    @staticmethod
    def resource_agent(
        tx: Transaction, tenant_id: str, resource_type: str, resource_id: str
    ) -> str | None:
        """Best-effort owning agent of an FTS-indexable resource (used to
        attribute projection jobs to the right agent even when the
        triggering event carried none)."""
        stored = tx.fts.document_for_resource(tenant_id, resource_type, resource_id)
        if stored is not None:
            return stored.agent_id
        document, agent_id = FtsProjectionService._canonical_document(
            tx, tenant_id, resource_type, resource_id
        )
        del document
        return agent_id

    @staticmethod
    def _canonical_document(
        tx: Transaction, tenant_id: str, resource_type: str, resource_id: str
    ) -> tuple[FtsDocumentInput | None, str | None]:
        try:
            if resource_type == "claim":
                claim = tx.claims.get(resource_id)
                if claim.tenant_id != tenant_id:
                    raise NotFoundError("claim belongs to another tenant")
                if claim.status not in CLAIM_CURRENT_VISIBLE_STATUSES:
                    return None, claim.agent_id
                claim_revision = tx.claims.current_revision_row(claim.id)
                return _claim_document(claim, claim_revision), claim.agent_id
            if resource_type == "episode":
                episode = tx.episodes.get(resource_id)
                if episode.tenant_id != tenant_id:
                    raise NotFoundError("episode belongs to another tenant")
                if episode.status not in ("open", "sealed"):
                    return None, episode.agent_id
                episode_revision = tx.episodes.current_revision_row(episode.id)
                return _episode_document(episode, episode_revision), episode.agent_id
            if resource_type == "note":
                note = tx.notes.get(resource_id)
                if note.tenant_id != tenant_id:
                    raise NotFoundError("note belongs to another tenant")
                if note.status not in FTS_NOTE_STATUSES:
                    return None, note.agent_id
                note_revision = tx.notes.current_revision_row(note.id)
                return _note_document(note, note_revision), note.agent_id
        except NotFoundError:
            return None, None
        raise NotFoundError(f"resource type {resource_type!r} is not FTS-indexable")

    # -- cleanup -----------------------------------------------------------------

    def cleanup_in_tx(self, tx: Transaction, tenant_id: str) -> tuple[int, int]:
        """Physical cleanup: invalid documents and old retired generations."""
        documents_deleted = tx.fts.delete_invalid_documents(tenant_id)
        generations_deleted = tx.fts.delete_retired_generations(tenant_id)
        return documents_deleted, generations_deleted

    # -- trusted search -------------------------------------------------------------

    def search_in_tx(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        match_expression: str,
        space_group_id: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        valid_at_us: int | None = None,
        limit: int = 50,
    ) -> list[FtsSearchHit]:
        """Trusted FTS search: run the read-path trust gate, then match.

        Raises :class:`FtsDegradedError` with a stable reason whenever the
        projection is absent, stale beyond policy, unverified or corrupted —
        the caller degrades the route; untrustworthy results never escape.
        """
        if not tx.fts.ensure_index():
            raise FtsDegradedError(FTS_REASON_UNAVAILABLE, retryable=False)
        state = tx.fts.projection_state()
        if state != "ready":
            raise FtsDegradedError(FTS_REASON_REBUILD_PENDING)
        pointer = tx.fts.pointer(tenant_id)
        if pointer is None:
            raise FtsDegradedError(FTS_REASON_REBUILD_PENDING)
        if not builder_versions_trusted(pointer.builder_version, pointer.tokenizer_version):
            raise FtsDegradedError(FTS_REASON_BUILDER_UNKNOWN, retryable=False)
        generation = tx.fts.get_generation(pointer.generation_id)
        if generation.status != "verified":
            raise FtsDegradedError(FTS_REASON_INDEX_CORRUPT)
        if generation.tenant_id != tenant_id:
            raise FtsDegradedError(FTS_REASON_INDEX_CORRUPT, retryable=False)
        # Staleness trust gate: the REQUESTING agent's UNSETTLED fts.apply
        # backlog, counted from the live queue state. Every indexable change
        # enqueues exactly one coalesced apply job, and a job counts as
        # consumed only when its fenced completion CAS commits together with
        # the projection write — so the count is the continuous consumption
        # frontier by construction. Watermark arithmetic would be wrong in
        # both directions: a single out-of-order apply can MAX-jump a
        # per-agent applied watermark over still-pending holes (false
        # fresh), and non-indexable traffic advances the live seq without
        # ever enqueueing an apply (permanent false stale).
        # A committed change already counts while its producer event is
        # waiting to enqueue apply. Ownerless erasures affect this tenant.
        backlog = sum(
            tx.outbox.unsettled_job_count(tenant_id, agent_id, kind)
            + tx.outbox.unsettled_null_agent_job_count(tenant_id, kind)
            for kind in (
                "claim.changed",
                "episode.changed",
                "note.changed",
                "memory.invalidated",
                "fts.apply",
            )
        )
        if backlog > fts_staleness_limit():
            raise FtsDegradedError(FTS_REASON_GENERATION_STALE)
        if tx.tombstone_watermark() < pointer.tombstone_watermark:
            raise FtsDegradedError(FTS_REASON_INDEX_CORRUPT, retryable=False)
        try:
            results = tx.fts.search(
                tenant_id=tenant_id,
                agent_id=agent_id,
                generation_id=pointer.generation_id,
                match_expression=match_expression,
                space_group_id=space_group_id,
                space_id=space_id,
                session_id=session_id,
                valid_at_us=valid_at_us,
                limit=limit,
            )
        except Exception as error:
            if isinstance(error, (ConflictError, NotFoundError)):
                raise
            raise FtsDegradedError(FTS_REASON_INDEX_CORRUPT) from error
        return [
            FtsSearchHit(document=document, relevance=relevance) for document, relevance in results
        ]

    def pointer_info(self, tenant_id: str) -> dict[str, object]:
        """Pointer summary for capabilities/health (no content)."""
        with self._uow.read() as tx:
            state = tx.fts.projection_state()
            pointer = tx.fts.pointer(tenant_id)
            if pointer is None:
                return {"state": state, "generation": None}
            return {
                "state": state,
                "generation": pointer.generation_id,
                "builder_version": pointer.builder_version,
                "tokenizer_version": pointer.tokenizer_version,
                "source_watermark": pointer.source_watermark,
                "tombstone_watermark": pointer.tombstone_watermark,
            }


__all__ = [
    "FTS_NOTE_STATUSES",
    "FTS_REASON_BUILDER_UNKNOWN",
    "FTS_REASON_GENERATION_STALE",
    "FTS_REASON_INDEX_CORRUPT",
    "FTS_REASON_REBUILD_PENDING",
    "FTS_REASON_UNAVAILABLE",
    "FtsDegradedError",
    "FtsProjectionService",
    "FtsRebuildReport",
    "FtsSearchHit",
    "collect_fts_documents",
]
