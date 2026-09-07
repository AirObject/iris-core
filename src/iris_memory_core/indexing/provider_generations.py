"""Actual FAISS preparation for immutable Provider configuration revisions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.provider_generations import (
    PreparedProviderGeneration,
    ProviderBuildSnapshotMoved,
)
from iris_memory_core.application.ports.provider_secrets import (
    ConfiguredEmbedding,
    ConfiguredEmbeddingRuntime,
)
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.domain.errors import ConflictError, NotFoundError
from iris_memory_core.domain.provider_configs import ProviderConfigRevision
from iris_memory_core.domain.vector import (
    VectorCurrentPointer,
    VectorDegradedError,
    VectorEntryInput,
    VectorGenerationRecord,
    VectorSpaceConfig,
    generation_content_hash,
)
from iris_memory_core.indexing.vector import VectorProjectionService, collect_vector_entries


@dataclass(frozen=True, slots=True)
class _CancellableEmbedding:
    binding: ConfiguredEmbedding
    batch_size: int
    check: Callable[[], None]

    @property
    def space(self) -> VectorSpaceConfig:
        return self.binding.space

    def embed_batch(
        self, texts: Sequence[str], *, deadline_monotonic_us: int | None = None
    ) -> list[Sequence[float]]:
        result: list[Sequence[float]] = []
        for index in range(0, len(texts), self.batch_size):
            self.check()
            result.extend(
                self.binding.embed_batch(
                    texts[index : index + self.batch_size],
                    deadline_monotonic_us=deadline_monotonic_us,
                )
            )
        self.check()
        return result


class ProviderGenerationRuntime:
    def __init__(
        self, uow: UnitOfWork, clock: Clock, runtime: ConfiguredEmbeddingRuntime, vector_root: Path
    ) -> None:
        self.uow, self.clock, self.runtime, self.vector_root = uow, clock, runtime, vector_root

    def count_resources(self, tx: Transaction, tenant_id: str) -> int:
        return len(collect_vector_entries(tx, tenant_id)[0])

    def prepare(
        self, revision: ProviderConfigRevision, check: Callable[[], None]
    ) -> PreparedProviderGeneration:
        check()
        binding = self.runtime.resolve(revision)
        projection = VectorProjectionService(
            self.uow,
            self.clock,
            provider=_CancellableEmbedding(binding, revision.definition.limits.batch_size, check),
            vector_root=self.vector_root,
            space=revision.definition.space,
        )
        prepared = projection.prepare_generation(revision.tenant_id)
        check()

        def publish(tx: Transaction) -> str:
            try:
                report = projection.switch_in_tx(tx, revision.tenant_id, prepared)
            except ConflictError as error:
                if error.details.get("reason") == "snapshot_moved":
                    raise ProviderBuildSnapshotMoved("canonical vector snapshot moved") from None
                raise
            return report.generation_id

        return PreparedProviderGeneration(
            prepared.generation_id,
            prepared.expected_epoch,
            prepared.vector_count,
            binding.secret_fingerprint,
            publish,
            lambda: projection.emit_generation(revision.tenant_id),
        )

    def _retained(
        self,
        tx: Transaction,
        revision: ProviderConfigRevision,
        generation_id: str,
    ) -> tuple[VectorGenerationRecord, list[tuple[VectorEntryInput, int]], dict[str, int]]:
        generation = tx.vector.get_generation(generation_id)
        binding = tx.providers.generation_binding(revision.tenant_id, generation_id)
        original = tx.providers.revision(revision.tenant_id, *binding) if binding else None
        if (
            generation.tenant_id != revision.tenant_id
            or generation.space != revision.definition.space
            or generation.status not in {"verified", "retired"}
            or original is None
            or original.definition.identity_hash() != revision.definition.identity_hash()
            or tx.tombstone_watermark() < generation.tombstone_watermark
        ):
            raise ConflictError("retained generation is not reusable")
        entries, watermarks = collect_vector_entries(tx, revision.tenant_id)
        if max(watermarks.values(), default=0) < generation.source_watermark:
            raise ConflictError("retained source watermark moved backwards")
        projection = VectorProjectionService(
            self.uow,
            self.clock,
            provider=self.runtime.resolve(revision),
            vector_root=self.vector_root,
            space=revision.definition.space,
        )
        candidate = VectorCurrentPointer(
            revision.tenant_id,
            generation_id,
            tx.vector.current_epoch(revision.tenant_id),
            generation.space,
            generation.source_watermark,
            generation.tombstone_watermark,
            self.clock.now_us(),
        )
        handle = projection.manager.handle_for(candidate, generation=generation)
        try:
            assigned: list[tuple[VectorEntryInput, int]] = []
            if len(entries) != handle.vector_count:
                raise ConflictError("retained canonical set moved")
            for entry in entries:
                mapped = tx.vector.id_map_get(
                    revision.tenant_id, entry.resource_type, entry.resource_id
                )
                if (
                    mapped is None
                    or mapped.status != "active"
                    or mapped.agent_id != entry.agent_id
                    or mapped.resource_revision != entry.resource_revision
                    or handle.lookup(mapped.surrogate_id)
                    != (
                        entry.resource_type,
                        entry.resource_id,
                        entry.resource_revision,
                        entry.content_hash,
                    )
                ):
                    raise ConflictError("retained canonical content moved")
                assigned.append((entry, mapped.surrogate_id))
            if generation_content_hash(assigned) != generation.content_checksum:
                raise ConflictError("retained canonical checksum moved")
            return generation, assigned, watermarks
        finally:
            handle.release()

    def can_reuse(
        self, tx: Transaction, revision: ProviderConfigRevision, generation_id: str
    ) -> bool:
        try:
            self._retained(tx, revision, generation_id)
            return True
        except (ConflictError, NotFoundError, VectorDegradedError, OSError, ValueError):
            return False

    def prepare_rollback(
        self,
        revision: ProviderConfigRevision,
        generation_id: str | None,
        check: Callable[[], None],
    ) -> PreparedProviderGeneration:
        check()
        if generation_id is not None:
            with self.uow.read() as tx:
                reusable = self.can_reuse(tx, revision, generation_id)
                epoch = tx.vector.current_epoch(revision.tenant_id)
                generation = tx.vector.get_generation(generation_id) if reusable else None
            if generation is not None:
                fingerprint = self.runtime.resolve(revision).secret_fingerprint

                def publish(tx: Transaction) -> str:
                    try:
                        retained, assigned, watermarks = self._retained(tx, revision, generation.id)
                    except (ConflictError, NotFoundError, VectorDegradedError, OSError, ValueError):
                        raise ProviderBuildSnapshotMoved(
                            "retained generation changed before publication"
                        ) from None
                    restored = tx.vector.reactivate_generation(
                        revision.tenant_id,
                        retained,
                        expected_epoch=epoch,
                        source_watermark=max(watermarks.values(), default=0),
                        tombstone_watermark=tx.tombstone_watermark(),
                        agent_watermarks=watermarks,
                    )
                    for entry, surrogate in assigned:
                        tx.vector.id_map_upsert(
                            tenant_id=revision.tenant_id,
                            resource_type=entry.resource_type,
                            resource_id=entry.resource_id,
                            resource_revision=entry.resource_revision,
                            surrogate_id=surrogate,
                            agent_id=entry.agent_id,
                            space=revision.definition.space,
                            content_hash=entry.content_hash,
                            now_us=self.clock.now_us(),
                        )
                    tx.vector.switch_pointer(
                        tenant_id=revision.tenant_id, generation=restored, expected_epoch=epoch
                    )
                    tx.vector.id_map_invalidate_tombstoned(revision.tenant_id)
                    tx.vector.id_map_stamp_generation(
                        revision.tenant_id,
                        generation.id,
                        tuple(surrogate for _, surrogate in assigned),
                    )
                    return generation.id

                check()
                return PreparedProviderGeneration(
                    generation.id,
                    epoch,
                    generation.vector_count,
                    fingerprint,
                    publish,
                    lambda: None,
                    reused=True,
                )
        return self.prepare(revision, check)

    def verify_current(
        self, tx: Transaction, revision: ProviderConfigRevision, generation_id: str
    ) -> None:
        pointer = tx.vector.pointer(revision.tenant_id)
        generation = tx.vector.get_generation(generation_id)
        if (
            pointer is None
            or pointer.generation_id != generation_id
            or pointer.space != revision.definition.space
            or generation.space != pointer.space
            or generation.status != "verified"
            or generation.tenant_id != revision.tenant_id
        ):
            raise ConflictError("provider serving generation moved")
        projection = VectorProjectionService(
            self.uow,
            self.clock,
            provider=self.runtime.resolve(revision),
            vector_root=self.vector_root,
            space=revision.definition.space,
        )
        # Opening the actual handle independently checks persisted manifest,
        # checksums, space identity and loaded FAISS state before a hot switch.
        handle = projection.manager.handle_for(pointer, generation=generation)
        handle.release()
