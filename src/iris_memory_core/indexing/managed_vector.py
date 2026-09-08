"""Tenant configuration and vector generation binding within a caller's snapshot."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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
from iris_memory_core.domain.errors import DomainError
from iris_memory_core.domain.provider_configs import ProviderConfigRevision, ProviderServing
from iris_memory_core.domain.vector import (
    VECTOR_REASON_INDEX_CORRUPT,
    VECTOR_REASON_REBUILD_PENDING,
    VECTOR_REASON_SPACE_MISMATCH,
    VECTOR_REASON_UNAVAILABLE,
    VectorDegradedError,
    VectorSpaceConfig,
)
from iris_memory_core.indexing.provider_generations import ProviderGenerationRuntime
from iris_memory_core.indexing.vector import (
    VectorProjectionMaintenance,
    VectorProjectionService,
    VectorRebuildReport,
)


@dataclass(frozen=True, slots=True)
class _ServingEmbedding:
    """A persisted approved configuration needs no implicit network probe."""

    binding: ConfiguredEmbedding

    @property
    def space(self) -> VectorSpaceConfig:
        return self.binding.space

    @property
    def circuit_state(self) -> str:
        return self.binding.circuit_state

    @property
    def limits(self) -> object:
        return getattr(self.binding, "limits", None)

    def probe(self) -> tuple[bool, str]:
        return True, "configuration_verified"

    def embed_batch(
        self, texts: Sequence[str], *, deadline_monotonic_us: int | None = None
    ) -> list[Sequence[float]]:
        return self.binding.embed_batch(texts, deadline_monotonic_us=deadline_monotonic_us)


@dataclass(frozen=True, slots=True)
class ManagedGeneration:
    serving: ProviderServing
    generation: PreparedProviderGeneration


class ManagedVectorProjection:
    def __init__(
        self, uow: UnitOfWork, clock: Clock, runtime: ConfiguredEmbeddingRuntime, vector_root: Path
    ) -> None:
        self.uow, self.clock, self.runtime, self.vector_root = uow, clock, runtime, vector_root
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str, int, str, str], VectorProjectionService] = (
            OrderedDict()
        )

    def _binding(self, revision: ProviderConfigRevision) -> ConfiguredEmbedding:
        try:
            return self.runtime.resolve(revision)
        except (DomainError, OSError, ValueError):
            # A temporarily absent deployment secret must degrade reads and
            # use the existing finite Outbox retry budget for maintenance.
            raise VectorDegradedError(VECTOR_REASON_UNAVAILABLE) from None

    def _revision(
        self, tx: Transaction, tenant_id: str
    ) -> tuple[ProviderServing, ProviderConfigRevision]:
        serving = tx.providers.serving(tenant_id)
        pointer = tx.vector.pointer(tenant_id)
        if serving is None or pointer is None:
            raise VectorDegradedError(VECTOR_REASON_REBUILD_PENDING)
        config = tx.providers.get(tenant_id, serving.config_id)
        revision = tx.providers.revision(tenant_id, serving.config_id, serving.content_revision)
        if (
            config is None
            or config.status != "active"
            or config.content_revision != serving.content_revision
            or revision is None
            or serving.generation_id != pointer.generation_id
        ):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT)
        generation = tx.vector.get_generation(pointer.generation_id)
        origin = tx.providers.generation_binding(tenant_id, generation.id)
        original = tx.providers.revision(tenant_id, *origin) if origin else None
        if (
            generation.tenant_id != tenant_id
            or generation.status != "verified"
            or original is None
            or original.definition.identity_hash() != revision.definition.identity_hash()
            or revision.definition.space != pointer.space
            or pointer.space != generation.space
        ):
            raise VectorDegradedError(VECTOR_REASON_SPACE_MISMATCH)
        probe = (
            tx.providers.probe(tenant_id, config.latest_probe_id)
            if config.latest_probe_id
            else None
        )
        if (
            probe is None
            or not probe.ok
            or probe.config_id != config.id
            or probe.content_revision != config.content_revision
            or probe.dimension_observed != revision.definition.space.dimension
            or not probe.normalized
        ):
            raise VectorDegradedError(VECTOR_REASON_UNAVAILABLE)
        operation = tx.console_operations.get(tenant_id, probe.operation_id)
        if (
            operation is None
            or operation.status != "completed"
            or operation.provider is None
            or operation.provider.action != "probe"
            or operation.provider.config_id != config.id
            or operation.provider.content_revision != config.content_revision
        ):
            raise VectorDegradedError(VECTOR_REASON_UNAVAILABLE)
        # Thirty-minute freshness gates activation, not the lifetime of an
        # already serving generation. Credential rotation resolves new material
        # for future requests; an in-flight binding retains exactly its material.
        return serving, revision

    def _projection(self, revision: ProviderConfigRevision) -> tuple[VectorProjectionService, str]:
        binding = self._binding(revision)
        key = (
            revision.tenant_id,
            revision.config_id,
            revision.content_revision,
            revision.content_hash(),
            binding.secret_fingerprint,
        )
        with self._lock:
            service = self._cache.get(key)
            if service is None:
                service = VectorProjectionService(
                    self.uow,
                    self.clock,
                    provider=_ServingEmbedding(binding),
                    vector_root=self.vector_root,
                    space=revision.definition.space,
                )
                self._cache[key] = service
                if len(self._cache) > 64:
                    _, removed = self._cache.popitem(last=False)
                    removed.manager.drop_current()
            self._cache.move_to_end(key)
        return service, binding.secret_fingerprint

    def resolve_in_tx(self, tx: Transaction, tenant_id: str) -> VectorProjectionService:
        try:
            _, revision = self._revision(tx, tenant_id)
            return self._projection(revision)[0]
        except VectorDegradedError:
            raise
        except (DomainError, OSError, ValueError):
            raise VectorDegradedError(VECTOR_REASON_UNAVAILABLE) from None

    def capability_available(self, tenant_id: str | None = None) -> bool:
        # Readiness is evaluated for the authenticated tenant. A process-wide
        # positive answer must never borrow another tenant's provider or secret.
        if tenant_id is None:
            return False
        try:
            with self.uow.read() as tx:
                service = self.resolve_in_tx(tx, tenant_id)
                if not service.capability_available():
                    return False
                pointer = tx.vector.pointer(tenant_id)
                assert pointer is not None
                handle = service.manager.handle_for(
                    pointer, generation=tx.vector.get_generation(pointer.generation_id)
                )
                handle.release()
                return True
        except (VectorDegradedError, DomainError, OSError, ValueError):
            return False

    def apply_change_in_tx(
        self, tx: Transaction, *, tenant_id: str, resource_type: str, resource_id: str
    ) -> None:
        if tx.providers.serving(tenant_id) is None:
            # First activation builds from canonical state; no invented space
            # is assigned while there is no approved serving configuration.
            return
        self.resolve_in_tx(tx, tenant_id).apply_change_in_tx(
            tx, tenant_id=tenant_id, resource_type=resource_type, resource_id=resource_id
        )

    def prepare_generation(
        self, tenant_id: str, *, check_lease: Callable[[Transaction], None] | None = None
    ) -> ManagedGeneration:
        with self.uow.read() as tx:
            if check_lease is not None:
                check_lease(tx)
            serving, revision = self._revision(tx, tenant_id)
            fingerprint = self._binding(revision).secret_fingerprint

        def check() -> None:
            with self.uow.read() as tx:
                if check_lease is not None:
                    check_lease(tx)
                current, definition = self._revision(tx, tenant_id)
                if (
                    current != serving
                    or self._binding(definition).secret_fingerprint != fingerprint
                ):
                    raise ProviderBuildSnapshotMoved(
                        "provider binding changed during vector rebuild"
                    )

        generations = ProviderGenerationRuntime(
            self.uow, self.clock, self.runtime, self.vector_root
        )
        return ManagedGeneration(serving, generations.prepare(revision, check))

    def switch_in_tx(
        self, tx: Transaction, tenant_id: str, prepared: ManagedGeneration
    ) -> VectorRebuildReport:
        serving, revision = self._revision(tx, tenant_id)
        if (
            serving != prepared.serving
            or self._binding(revision).secret_fingerprint != prepared.generation.secret_fingerprint
        ):
            raise ProviderBuildSnapshotMoved("provider binding changed during vector rebuild")
        generation_id = prepared.generation.publish(tx)
        generation = tx.vector.get_generation(generation_id)
        report = VectorRebuildReport(
            generation_id,
            generation.vector_count,
            generation.content_checksum,
            generation.source_watermark,
            generation.tombstone_watermark,
            serving.generation_id,
        )
        now = max(self.clock.now_us(), serving.updated_us)
        tx.providers.bind_generation(
            tenant_id, report.generation_id, serving.config_id, serving.content_revision, now_us=now
        )
        config = tx.providers.get(tenant_id, serving.config_id)
        assert config is not None
        tx.providers.advance(
            replace(
                config,
                revision=config.revision + 1,
                last_generation_id=report.generation_id,
                updated_us=max(now, config.updated_us),
            ),
            expected_revision=config.revision,
        )
        tx.providers.switch_serving(
            replace(
                serving, generation_id=report.generation_id, epoch=serving.epoch + 1, updated_us=now
            ),
            expected_epoch=serving.epoch,
        )
        return report

    def emit_generation(self, tenant_id: str) -> None:
        with self.uow.read() as tx:
            service = self.resolve_in_tx(tx, tenant_id)
        service.emit_generation(tenant_id)

    def cleanup_service_in_tx(
        self, tx: Transaction, tenant_id: str
    ) -> VectorProjectionMaintenance | None:
        configs = tx.providers.list_configs(tenant_id, limit=1)
        if not configs:
            return None
        config = configs[0]
        revision = tx.providers.revision(tenant_id, config.id, config.content_revision)
        assert revision is not None
        # Cleanup only uses directory identity and committed metadata. It can
        # finish during a credential outage or before the first activation.
        return VectorProjectionMaintenance(
            self.uow, self.clock, vector_root=self.vector_root, space=revision.definition.space
        )
