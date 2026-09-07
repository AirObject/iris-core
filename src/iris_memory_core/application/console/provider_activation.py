"""Activation gates and server-owned plans over one configuration/serving snapshot."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from iris_memory_core.application.ports.provider_secrets import ConfiguredEmbeddingRuntime
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.provider_configs import (
    ProviderConfig,
    ProviderConfigRevision,
    ProviderProbe,
)

PROBE_MAX_AGE_US = 30 * 60 * 1_000_000


def gate(kind: str) -> ConflictError:
    return ConflictError("provider activation precondition failed", details={"kind": kind})


@dataclass(frozen=True, slots=True)
class ProviderActivationPlan:
    config_id: str
    content_revision: int
    expected_revision: int
    serving_epoch: int
    vector_epoch: int
    rebuild: bool
    estimated_resources: int
    rebuild_plan_hash: str
    reuse_generation_id: str | None = None

    def require_ack(self, acknowledgement: str | None) -> None:
        if self.rebuild and acknowledgement != self.rebuild_plan_hash:
            raise gate("provider_rebuild_ack_required")
        if not self.rebuild and acknowledgement not in (None, self.rebuild_plan_hash):
            raise gate("provider_rebuild_plan_changed")


class ProviderActivationPlanner:
    def __init__(
        self,
        runtime: ConfiguredEmbeddingRuntime,
        count_resources: Callable[[Transaction, str], int],
        retained_usable: Callable[[Transaction, ProviderConfigRevision, str], bool] | None = None,
    ) -> None:
        self.runtime, self.count_resources = runtime, count_resources
        self.retained_usable = retained_usable

    def verified_probe(
        self,
        tx: Transaction,
        config: ProviderConfig,
        revision: ProviderConfigRevision,
        *,
        now_us: int,
    ) -> ProviderProbe:
        if config.latest_probe_id is None:
            raise gate("provider_probe_required")
        probe = tx.providers.probe(config.tenant_id, config.latest_probe_id)
        if probe is None or not probe.ok:
            raise gate("provider_probe_required")
        if not 0 <= now_us - probe.created_us <= PROBE_MAX_AGE_US:
            raise gate("provider_probe_expired")
        if (
            probe.tenant_id,
            probe.config_id,
            probe.content_revision,
            probe.dimension_observed,
            probe.normalized,
        ) != (
            config.tenant_id,
            config.id,
            config.content_revision,
            revision.definition.space.dimension,
            True,
        ):
            raise gate("provider_probe_mismatch")
        operation = tx.console_operations.get(config.tenant_id, probe.operation_id)
        if (
            operation is None
            or operation.kind != "embedding_provider"
            or operation.provider is None
            or operation.provider.action != "probe"
            or operation.status != "completed"
            or operation.provider.config_id != config.id
            or operation.provider.content_revision != config.content_revision
        ):
            raise gate("provider_probe_mismatch")
        if self.runtime.resolve(revision).secret_fingerprint != probe.secret_fingerprint:
            raise gate("provider_secret_changed")
        return probe

    def plan(
        self, tx: Transaction, config: ProviderConfig, *, now_us: int
    ) -> ProviderActivationPlan:
        if config.status not in {"probed", "retired"}:
            raise gate("provider_probe_required")
        revision = tx.providers.revision(config.tenant_id, config.id, config.content_revision)
        if revision is None:
            raise gate("provider_configuration_unavailable")
        probe = self.verified_probe(tx, config, revision, now_us=now_us)
        serving = tx.providers.serving(config.tenant_id)
        pointer = tx.vector.pointer(config.tenant_id)
        previous = None
        if serving is not None:
            if pointer is None or pointer.generation_id != serving.generation_id:
                raise gate("provider_serving_moved")
            active = tx.providers.get(config.tenant_id, serving.config_id)
            previous = tx.providers.revision(
                config.tenant_id, serving.config_id, serving.content_revision
            )
            if (
                active is None
                or active.status != "active"
                or active.content_revision != serving.content_revision
                or previous is None
                or previous.definition.space != pointer.space
            ):
                raise gate("provider_serving_moved")
        rebuild = (
            previous is None
            or previous.definition.identity_hash() != revision.definition.identity_hash()
        )
        reuse_generation_id = None
        if (
            config.status == "retired"
            and config.last_generation_id is not None
            and self.retained_usable is not None
            and self.retained_usable(tx, revision, config.last_generation_id)
        ):
            reuse_generation_id = config.last_generation_id
            rebuild = False
        count = self.count_resources(tx, config.tenant_id) if rebuild else 0
        if type(count) is not int or count < 0:
            raise gate("provider_estimate_unavailable")
        serving_epoch = serving.epoch if serving is not None else 0
        vector_epoch = tx.vector.current_epoch(config.tenant_id)
        material = {
            "tenant_id": config.tenant_id,
            "config_id": config.id,
            "revision": config.revision,
            "content_revision": config.content_revision,
            "content_hash": revision.content_hash(),
            "probe_id": probe.id,
            "secret_fingerprint": probe.secret_fingerprint,
            "serving_epoch": serving_epoch,
            "vector_epoch": vector_epoch,
            "current_generation_id": pointer.generation_id if pointer else None,
            "deletion_watermark": tx.tombstone_watermark(),
            "rebuild": rebuild,
            "reuse_generation_id": reuse_generation_id,
            "estimated_resources": count,
        }
        return ProviderActivationPlan(
            config.id,
            config.content_revision,
            config.revision,
            serving_epoch,
            vector_epoch,
            rebuild,
            count,
            hashlib.sha256(canonical_json(material).encode()).hexdigest(),
            reuse_generation_id,
        )
