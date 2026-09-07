"""Application ports for indexes; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from iris_memory_core.domain.fts import (
    FtsCurrentPointer,
    FtsDocumentInput,
    FtsDocumentRecord,
    FtsGenerationRecord,
)
from iris_memory_core.domain.graph import (
    GraphCurrentPointer,
    GraphEdgeDraft,
    GraphEdgeRecord,
    GraphGenerationRecord,
)
from iris_memory_core.domain.profile import (
    ProfileCurrentPointer,
    ProfileFieldDraft,
    ProfileFieldRecord,
    ProfileGenerationRecord,
    ProfileSubjectKey,
    ProfileSubjectRecord,
)
from iris_memory_core.domain.vector import (
    VectorCurrentPointer,
    VectorGenerationRecord,
    VectorIdMapRecord,
    VectorSpaceConfig,
)


class FtsSurface(Protocol):
    """Repository surface for the FTS5 projection (ADR-0014 §1-2)."""

    def ensure_index(self) -> bool: ...

    def drop_index(self) -> None: ...

    def projection_state(self) -> str: ...

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None: ...

    def reset_projection(self, *, now_us: int | None = None) -> None: ...

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        tokenizer_version: int,
        config_json: str,
        source_watermark: int,
        tombstone_watermark: int,
        document_count: int,
        content_checksum: str,
        now_us: int | None = None,
    ) -> FtsGenerationRecord: ...

    def get_generation(self, generation_id: str) -> FtsGenerationRecord: ...

    def generations_for_tenant(self, tenant_id: str) -> tuple[FtsGenerationRecord, ...]: ...

    def retire_generation(self, generation_id: str) -> int: ...

    def pointer(self, tenant_id: str) -> FtsCurrentPointer | None: ...

    def switch_pointer(
        self, *, tenant_id: str, generation: FtsGenerationRecord, now_us: int | None = None
    ) -> None: ...

    def upsert_document(
        self,
        *,
        generation_id: str,
        document: FtsDocumentInput,
        source_watermark: int,
        tombstone_watermark: int,
        builder_version: int,
        now_us: int | None = None,
    ) -> FtsDocumentRecord: ...

    def invalidate_document(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        now_us: int | None = None,
    ) -> int: ...

    def document_for_resource(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> FtsDocumentRecord | None: ...

    def delete_invalid_documents(self, tenant_id: str, *, limit: int = 500) -> int: ...

    def delete_retired_generations(
        self, tenant_id: str, *, keep: int = 2, now_us: int | None = None
    ) -> int: ...

    def documents_for_generation(self, generation_id: str) -> tuple[FtsDocumentRecord, ...]: ...

    def count_documents(self, generation_id: str) -> int: ...

    def sample_query(
        self, generation_id: str, match_expression: str, *, limit: int = 5
    ) -> tuple[int, ...]: ...

    def search(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        generation_id: str,
        match_expression: str,
        space_group_id: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        statuses: Sequence[str] = ("active", "disputed", "open", "sealed", "inbox", "pinned"),
        valid_at_us: int | None = None,
        limit: int = 50,
    ) -> tuple[tuple[FtsDocumentRecord, float], ...]: ...


class VectorSurface(Protocol):
    """Repository surface for the vector projection (ADR-0015 §3-5)."""

    def projection_state(self) -> str: ...

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None: ...

    def reset_projection(self, *, now_us: int | None = None) -> None: ...

    def allocate_surrogate_ids(self, count: int) -> tuple[int, ...]: ...

    def id_map_get(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> VectorIdMapRecord | None: ...

    def id_map_by_surrogate(
        self, tenant_id: str, surrogate_id: int
    ) -> VectorIdMapRecord | None: ...

    def id_map_count(self, tenant_id: str, *, active_only: bool = True) -> int: ...

    def id_map_upsert(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        resource_revision: int,
        surrogate_id: int,
        agent_id: str,
        space: VectorSpaceConfig,
        content_hash: str,
        now_us: int | None = None,
        incorporated_generation: str | None = None,
    ) -> VectorIdMapRecord: ...

    def id_map_stamp_generation(
        self,
        tenant_id: str,
        generation_id: str,
        surrogates: Sequence[int],
    ) -> int: ...

    def id_map_invalidate(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        now_us: int | None = None,
    ) -> int: ...

    def id_map_delete_invalid(self, tenant_id: str, *, limit: int = 500) -> int: ...

    def delta_upsert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        resource_type: str,
        resource_id: str,
        resource_revision: int,
        op: str,
        source_watermark: int,
        now_us: int | None = None,
    ) -> None: ...

    def delta_count(self, tenant_id: str, agent_id: str | None = None) -> int: ...

    def delta_clear(self, tenant_id: str) -> int: ...

    def id_map_invalidate_tombstoned(self, tenant_id: str, *, now_us: int | None = None) -> int: ...

    def insert_generation(
        self,
        *,
        tenant_id: str,
        space: VectorSpaceConfig,
        source_watermark: int,
        tombstone_watermark: int,
        vector_count: int,
        content_checksum: str,
        id_map_checksum: str,
        index_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> VectorGenerationRecord: ...

    def get_generation(self, generation_id: str) -> VectorGenerationRecord: ...

    def generations_for_tenant(self, tenant_id: str) -> tuple[VectorGenerationRecord, ...]: ...

    def all_generation_ids(self) -> tuple[str, ...]: ...

    def all_pointer_generation_ids(self) -> tuple[str, ...]: ...

    def reactivate_generation(
        self,
        tenant_id: str,
        generation: VectorGenerationRecord,
        *,
        expected_epoch: int,
        source_watermark: int,
        tombstone_watermark: int,
        agent_watermarks: dict[str, int],
    ) -> VectorGenerationRecord: ...

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int: ...

    def delete_retired_generations(
        self,
        tenant_id: str,
        *,
        keep: int = 2,
        older_than_us: int | None = None,
    ) -> tuple[str, ...]: ...

    def pointer(self, tenant_id: str) -> VectorCurrentPointer | None: ...

    def current_epoch(self, tenant_id: str) -> int: ...

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: VectorGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> VectorCurrentPointer: ...

    def pointer_info(self, tenant_id: str) -> dict[str, object]: ...


class ProfileSurface(Protocol):
    def subjects_citing_claim(
        self, tenant_id: str, generation_id: str, claim_id: str
    ) -> tuple[ProfileSubjectKey, ...]: ...

    """Repository surface for the profile projection (ADR-0016 §2)."""

    def projection_state(self) -> str: ...

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None: ...

    def reset_projection(self, *, now_us: int | None = None) -> None: ...

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        source_watermark: int,
        tombstone_watermark: int,
        subject_count: int,
        field_count: int,
        content_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> ProfileGenerationRecord: ...

    def get_generation(self, generation_id: str) -> ProfileGenerationRecord: ...

    def generations_for_tenant(self, tenant_id: str) -> tuple[ProfileGenerationRecord, ...]: ...

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int: ...

    def delete_retired_generations(
        self,
        tenant_id: str,
        *,
        keep: int = ...,
        older_than_us: int | None = ...,
    ) -> tuple[str, ...]: ...

    def bump_generation_watermark(
        self, tenant_id: str, generation_id: str, agent_id: str, watermark: int
    ) -> None: ...

    def subjects_for_generation(
        self, tenant_id: str, generation_id: str
    ) -> tuple[ProfileSubjectRecord, ...]: ...

    def fields_for_subject(
        self, tenant_id: str, generation_id: str, subject: ProfileSubjectKey
    ) -> tuple[ProfileFieldRecord, ...]: ...

    def field_count(self, tenant_id: str, generation_id: str) -> int: ...

    def subject_count(self, tenant_id: str, generation_id: str) -> int: ...

    def insert_fields(
        self,
        tenant_id: str,
        generation_id: str,
        drafts: Sequence[ProfileFieldDraft],
        *,
        now_us: int | None = None,
    ) -> int: ...

    def delete_subject_rows(
        self, tenant_id: str, generation_id: str, subject: ProfileSubjectKey
    ) -> int: ...

    def recompute_generation_manifest(self, tenant_id: str, generation_id: str) -> None: ...

    def pointer(self, tenant_id: str) -> ProfileCurrentPointer | None: ...

    def current_epoch(self, tenant_id: str) -> int: ...

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: ProfileGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> ProfileCurrentPointer: ...

    def pointer_info(self, tenant_id: str) -> dict[str, object]: ...


class GraphSurface(Protocol):
    """Repository surface for the relation graph projection (ADR-0016 §3)."""

    def projection_state(self) -> str: ...

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None: ...

    def reset_projection(self, *, now_us: int | None = None) -> None: ...

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        source_watermark: int,
        tombstone_watermark: int,
        node_count: int,
        edge_count: int,
        content_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> GraphGenerationRecord: ...

    def get_generation(self, generation_id: str) -> GraphGenerationRecord: ...

    def generations_for_tenant(self, tenant_id: str) -> tuple[GraphGenerationRecord, ...]: ...

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int: ...

    def delete_retired_generations(
        self,
        tenant_id: str,
        *,
        keep: int = ...,
        older_than_us: int | None = ...,
    ) -> tuple[str, ...]: ...

    def bump_generation_watermark(
        self, tenant_id: str, generation_id: str, agent_id: str, watermark: int
    ) -> None: ...

    def node_count(self, tenant_id: str, generation_id: str) -> int: ...

    def edge_count(self, tenant_id: str, generation_id: str) -> int: ...

    def node_exists(
        self, tenant_id: str, generation_id: str, node_id: str, node_kind: str
    ) -> bool: ...

    def edges_for_source(
        self,
        tenant_id: str,
        generation_id: str,
        node_id: str,
        *,
        node_kind: str,
        limit: int | None = ...,
    ) -> tuple[GraphEdgeRecord, ...]: ...

    def edges_for_resource(
        self, tenant_id: str, generation_id: str, resource_type: str, resource_id: str
    ) -> tuple[GraphEdgeRecord, ...]: ...

    def edges_for_entity(
        self, tenant_id: str, generation_id: str, entity_id: str
    ) -> tuple[GraphEdgeRecord, ...]: ...

    def all_edges(self, tenant_id: str, generation_id: str) -> tuple[GraphEdgeRecord, ...]: ...

    def insert_edges(
        self,
        tenant_id: str,
        generation_id: str,
        drafts: Sequence[GraphEdgeDraft],
        *,
        node_status: str | Mapping[str, str] = "canonical",
        now_us: int | None = None,
    ) -> int: ...

    def delete_resource_edges(
        self, tenant_id: str, generation_id: str, resource_type: str, resource_id: str
    ) -> int: ...

    def delete_entity_edges(self, tenant_id: str, generation_id: str, entity_id: str) -> int: ...

    def prune_orphan_nodes(self, tenant_id: str, generation_id: str) -> int: ...

    def recompute_generation_manifest(self, tenant_id: str, generation_id: str) -> None: ...

    def pointer(self, tenant_id: str) -> GraphCurrentPointer | None: ...

    def current_epoch(self, tenant_id: str) -> int: ...

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: GraphGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> GraphCurrentPointer: ...

    def pointer_info(self, tenant_id: str) -> dict[str, object]: ...
