"""Application ports for memory; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol

from iris_memory_core.domain.memory import (
    ArtifactRecord,
    ClaimCurrent,
    ClaimRevision,
    EpisodeCurrent,
    EpisodeRevision,
    EvidenceRecord,
    RelationCurrent,
    RelationRevision,
)


class EpisodeSurface(Protocol):
    """Repository surface for episode current rows and immutable revisions."""

    def get(self, episode_id: str) -> EpisodeCurrent: ...
    def get_revision(self, revision_id: str) -> EpisodeRevision: ...
    def current_revision_row(self, episode_id: str) -> EpisodeRevision: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        title: str,
        status: str,
        importance: float,
        started_at_us: int | None,
        ended_at_us: int | None,
        extractor_version: str | None,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        episode_id: str,
        tenant_id: str,
        revision: int,
        title: str,
        summary: str,
        participant_entity_ids: tuple[str, ...],
        observation_refs: tuple[dict[str, object], ...],
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        importance: float,
        valence: float | None,
        arousal: float | None,
        started_at_us: int | None,
        ended_at_us: int | None,
        extractor_version: str | None,
        content_hash: str,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, episode_id: str, revision_id: str) -> int: ...
    def advance_pointer(
        self,
        episode_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        importance: float | None = None,
    ) -> int: ...
    def raise_pointer_mismatch(self, episode_id: str, expected: int) -> None: ...
    def history(self, episode_id: str, *, limit: int = 100) -> Sequence[EpisodeRevision]: ...
    def list_episodes(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: Sequence[str] = ("open", "sealed"),
        limit: int = 100,
        cursor_updated_us: int | None = None,
        cursor_id: str | None = None,
    ) -> Sequence[EpisodeCurrent]: ...
    def episodes_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]: ...
    def episodes_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]: ...
    def episodes_with_participant(
        self, tenant_id: str, subject_entity_id: str, *, agent_id: str | None = None
    ) -> tuple[str, ...]: ...
    def erase_content(self, episode_id: str, *, now_us: int) -> None: ...


class ClaimSurface(Protocol):
    """Repository surface for claims, revisions and evidence."""

    def get(self, claim_id: str) -> ClaimCurrent: ...
    def get_revision(self, revision_id: str) -> ClaimRevision: ...
    def current_revision_row(self, claim_id: str) -> ClaimRevision: ...
    def find_live_by_dedup_key(self, tenant_id: str, dedup_key: str) -> ClaimCurrent | None: ...
    def revision_current_at(self, claim_id: str, as_of_us: int) -> ClaimRevision | None: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        subject_entity_id: str,
        predicate: str,
        category: str,
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        source_authority: str,
        valid_from_us: int | None,
        valid_until_us: int | None,
        evidence_count: int,
        dedup_key: str,
        recorded_at_us: int,
        extractor_version: str | None,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        claim_id: str,
        tenant_id: str,
        revision: int,
        subject_entity_id: str,
        predicate: str,
        value_json: str,
        canonical_text: str,
        category: str,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        source_authority: str,
        valid_from_us: int | None,
        valid_until_us: int | None,
        recorded_at_us: int,
        extractor_version: str | None,
        content_hash: str,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, claim_id: str, revision_id: str) -> int: ...
    def stamp_revision_superseded(self, revision_id: str, *, superseded_at_us: int) -> int: ...
    def advance_pointer(
        self,
        claim_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        confidence: float | None = None,
        importance: float | None = None,
        accessibility: float | None = None,
        source_authority: str | None = None,
        valid_from_us: int | None = None,
        valid_from_set: bool = False,
        valid_until_us: int | None = None,
        valid_until_set: bool = False,
        superseded_at_us: int | None = None,
        superseded_at_set: bool = False,
        evidence_count: int | None = None,
    ) -> int: ...
    def raise_pointer_mismatch(self, claim_id: str, expected: int) -> None: ...
    def insert_evidence(
        self,
        *,
        claim_id: str,
        tenant_id: str,
        source_type: str,
        source_id: str,
        source_revision: int | None,
        relation: str,
        source_authority: str,
        evidence_span: str | None,
        created_by: str,
        recorded_at_us: int | None = None,
    ) -> tuple[EvidenceRecord, bool]: ...
    def evidence_for_claim(self, claim_id: str) -> Sequence[EvidenceRecord]: ...
    def invalidate_evidence_for_source(
        self, tenant_id: str, source_type: str, source_id: str, *, now_us: int
    ) -> int: ...
    def valid_evidence_count(self, claim_id: str) -> int: ...
    def recount_evidence(self, claim_id: str) -> int: ...
    def all_current_claim_pairs(
        self,
        tenant_id: str,
        *,
        statuses: Sequence[str] = ...,
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]: ...

    def claims_for_group(
        self, tenant_id: str, space_group_id: str, *, categories: Sequence[str]
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]: ...

    def claims_targeting_entity(
        self, tenant_id: str, entity_id: str
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]: ...

    def claims_for_subject_predicate(
        self, tenant_id: str, agent_id: str, subject_entity_id: str, predicate: str | None
    ) -> Sequence[ClaimCurrent]: ...
    def claims_for_subject(
        self, tenant_id: str, subject_entity_id: str, *, agent_id: str | None = None
    ) -> Sequence[ClaimCurrent]: ...
    def claims_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> Sequence[ClaimCurrent]: ...
    def claims_for_space(self, tenant_id: str, space_id: str) -> Sequence[ClaimCurrent]: ...
    def claims_citing_source(
        self, tenant_id: str, source_type: str, source_id: str
    ) -> Sequence[ClaimCurrent]: ...
    def erase_content(self, claim_id: str, *, now_us: int) -> None: ...
    def set_history_available_from(self, claim_id: str, available_from_us: int) -> None: ...
    def prune_revisions(self, claim_id: str, *, keep: int) -> int: ...
    def earliest_kept_recorded_at(self, claim_id: str) -> int: ...
    def history(self, claim_id: str, *, limit: int = 100) -> Sequence[ClaimRevision]: ...
    def search_page(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        statuses: Sequence[str],
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        category: str | None = None,
        valid_at_us: int | None = None,
        as_of_us: int | None = None,
        exclude_ids: Collection[str] = (),
        cursor_updated_us: int | None = None,
        cursor_id: str | None = None,
        limit: int = 100,
        scope_mode: str = "request",
    ) -> Sequence[tuple[ClaimCurrent, ClaimRevision]]: ...


class RelationSurface(Protocol):
    """Repository surface for canonical relations."""

    def get(self, relation_id: str) -> RelationCurrent: ...

    def all_current_relation_pairs(
        self,
        tenant_id: str,
        *,
        statuses: Sequence[str] = ...,
    ) -> tuple[tuple[RelationCurrent, RelationRevision], ...]: ...
    def get_revision(self, revision_id: str) -> RelationRevision: ...
    def current_revision_row(self, relation_id: str) -> RelationRevision: ...
    def find_live(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        space_group_id: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        valid_from_us: int | None = None,
        valid_until_us: int | None = None,
    ) -> RelationCurrent | None: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        valid_from_us: int | None,
        valid_until_us: int | None,
        evidence_count: int,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        relation_id: str,
        tenant_id: str,
        revision: int,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        privacy_labels: tuple[str, ...],
        evidence_refs: tuple[dict[str, object], ...],
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        valid_from_us: int | None,
        valid_until_us: int | None,
        content_hash: str,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, relation_id: str, revision_id: str) -> int: ...
    def stamp_revision_superseded(self, revision_id: str, *, superseded_at_us: int) -> int: ...
    def advance_pointer(
        self,
        relation_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        source_entity_id: str | None = None,
        relation_type: str | None = None,
        target_entity_id: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        accessibility: float | None = None,
        valid_from_us: int | None = None,
        valid_from_set: bool = False,
        valid_until_us: int | None = None,
        valid_until_set: bool = False,
        evidence_count: int | None = None,
    ) -> int: ...
    def raise_pointer_mismatch(self, relation_id: str, expected: int) -> None: ...
    def history(self, relation_id: str, *, limit: int = 100) -> Sequence[RelationRevision]: ...
    def relations_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]: ...
    def relations_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]: ...
    def relations_for_entity(
        self, tenant_id: str, entity_id: str, *, agent_id: str | None = None
    ) -> tuple[str, ...]: ...
    def erase_content(self, relation_id: str, *, now_us: int) -> None: ...
    def insert_evidence(
        self,
        *,
        relation_id: str,
        tenant_id: str,
        source_type: str,
        source_id: str,
        source_revision: int | None,
        relation: str,
        source_authority: str,
        evidence_span: str | None,
        created_by: str,
        recorded_at_us: int | None = None,
    ) -> bool: ...
    def evidence_for_relation(
        self, relation_id: str, *, only_valid: bool = False, limit: int | None = None
    ) -> Sequence[EvidenceRecord]: ...
    def invalidate_evidence_for_source(
        self, tenant_id: str, source_type: str, source_id: str, *, now_us: int
    ) -> int: ...
    def valid_evidence_count(self, relation_id: str) -> int: ...
    def recount_evidence(self, relation_id: str) -> int: ...
    def relations_citing_source(
        self, tenant_id: str, source_type: str, source_id: str
    ) -> tuple[str, ...]: ...


class ArtifactSurface(Protocol):
    """Repository surface for artifact metadata and the controlled blob store."""

    def get(self, artifact_id: str) -> ArtifactRecord: ...
    def find_active_by_hash(
        self,
        tenant_id: str,
        scope_key: str,
        content_hash: str,
        storage_kind: str,
        privacy_labels: tuple[str, ...] = (),
    ) -> ArtifactRecord | None: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        media_type: str,
        storage_kind: str,
        locator: str,
        content: bytes | None,
        content_hash: str,
        size_bytes: int,
        privacy_labels: tuple[str, ...],
        source_ref: dict[str, object] | None,
        status: str,
        artifact_id: str | None = None,
    ) -> str: ...
    def next_artifact_id(self) -> str: ...
    def set_status(self, artifact_id: str, status: str) -> int: ...
    def bump_refcount(self, artifact_id: str, delta: int) -> int: ...
    def inline_content(self, artifact_id: str) -> bytes: ...
    def write_blob(self, locator: str, payload: bytes) -> object: ...
    def read_blob(self, locator: str, *, expected_hash: str, expected_size: int) -> bytes: ...
    def blob_exists(self, locator: str) -> bool: ...
    def unlink_blob(self, locator: str) -> bool: ...
    def tombstoned_local_blob_locators(
        self, tenant_id: str, *, tombstone_seq_lo: int, tombstone_seq_hi: int
    ) -> tuple[str, ...]: ...
    def tombstone_row(self, artifact_id: str, *, now_us: int) -> None: ...
    def artifacts_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]: ...
    def artifacts_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]: ...
    def all_active_ids(self, tenant_id: str) -> tuple[str, ...]: ...
