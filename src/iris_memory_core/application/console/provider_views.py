"""Authorized, bounded Provider views and server-owned activation effects."""

from __future__ import annotations

import json
import math
from typing import Any

from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.console.provider_activation import (
    ProviderActivationPlan,
    ProviderActivationPlanner,
)
from iris_memory_core.application.console.provider_configs import ProviderConfigCommands
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.console_operations import ConsoleOperation
from iris_memory_core.domain.errors import DomainError, InvalidRequestError, NotFoundError
from iris_memory_core.domain.provider_configs import ProviderConfig, ProviderConfigRevision
from iris_memory_core.domain.vector import EmbeddingProviderError


def effects(plan: ProviderActivationPlan, revision: ProviderConfigRevision) -> dict[str, object]:
    limits = revision.definition.limits
    batches = math.ceil(plan.estimated_resources / limits.batch_size) if plan.rebuild else 0
    return {
        "rebuild": plan.rebuild,
        "estimated_resources": str(plan.estimated_resources),
        "worker_required": True,
        "estimated_duration_seconds": {
            "lower": 0,
            "upper": max(
                1, math.ceil(batches * (limits.timeout_us / 1_000_000 + 1 / limits.max_qps))
            ),
        },
        "estimate_basis": "request_limits_excluding_queue_and_index_io",
    }


class ProviderViews:
    def __init__(
        self, commands: ProviderConfigCommands, planner: ProviderActivationPlanner | None
    ) -> None:
        self.commands, self.planner = commands, planner
        self.context = commands.context

    def _view(self, tx: Transaction, config: ProviderConfig, *, details: bool) -> dict[str, Any]:
        value: dict[str, Any] = self.commands.view(tx, config)
        if not details:
            return value
        probe = (
            tx.providers.probe(config.tenant_id, config.latest_probe_id)
            if config.latest_probe_id
            else None
        )
        value["probe"] = (
            None
            if probe is None
            else {
                "id": probe.id,
                "ok": probe.ok,
                "dimension_observed": probe.dimension_observed,
                "normalized": probe.normalized,
                "latency_ms": probe.latency_ms,
                "outcome": probe.outcome,
                "created_us": probe.created_us,
            }
        )
        value["activation_plan"], value["activation_blocked_reason"] = None, None
        if self.planner is None:
            value["activation_blocked_reason"] = "deployment_unavailable"
        elif config.status not in {"probed", "retired"}:
            value["activation_blocked_reason"] = "provider_probe_required"
        else:
            try:
                plan = self.planner.plan(tx, config, now_us=self.context.clock.now_us())
                revision = tx.providers.revision(
                    config.tenant_id, config.id, config.content_revision
                )
                assert revision is not None
                value["activation_plan"] = self.plan_view(plan, revision)
            except DomainError as error:
                value["activation_blocked_reason"] = error.details.get(
                    "kind", "provider_configuration_unavailable"
                )
            except EmbeddingProviderError:
                value["activation_blocked_reason"] = "provider_configuration_unavailable"
        return value

    @staticmethod
    def plan_view(plan: ProviderActivationPlan, revision: ProviderConfigRevision) -> dict[str, Any]:
        return {
            "config_id": plan.config_id,
            "content_revision": plan.content_revision,
            "expected_revision": plan.expected_revision,
            "rebuild_plan_hash": plan.rebuild_plan_hash,
            "reuse_generation_id": plan.reuse_generation_id,
            "side_effects": effects(plan, revision),
        }

    def detail(self, principal: OperatorPrincipal, identifier: str) -> dict[str, Any]:
        with self.context.uow.read() as tx:
            fresh = self.commands.principal(tx, principal)
            return self._view(
                tx, self.commands.current(tx, fresh.key.tenant_id, identifier), details=True
            )

    def overview(
        self, principal: OperatorPrincipal, *, limit: int, after: tuple[int, str] | None
    ) -> dict[str, Any]:
        if not 1 <= limit <= 101:
            raise InvalidRequestError("invalid provider page size")
        with self.context.uow.read() as tx:
            fresh = self.commands.principal(tx, principal)
            tenant = fresh.key.tenant_id
            serving = tx.providers.serving(tenant)
            pointer = tx.vector.pointer(tenant)
            configs = tx.providers.list_configs(tenant, limit=limit, after=after)
            return {
                "configured": self.planner is not None,
                "active_config_id": serving.config_id if serving else None,
                "generation_id": pointer.generation_id if pointer else None,
                "projection_state": tx.vector.projection_state(),
                "can_manage": "providers.manage" in fresh.key.grant.permissions,
                "configs": [self._view(tx, config, details=False) for config in configs],
            }

    def history(
        self, principal: OperatorPrincipal, identifier: str, *, before: int | None, limit: int
    ) -> tuple[dict[str, Any], ...]:
        with self.context.uow.read() as tx:
            fresh = self.commands.principal(tx, principal)
            config = self.commands.current(tx, fresh.key.tenant_id, identifier)
            rows = tx.providers.history(config.tenant_id, identifier, before=before, limit=limit)
            values = []
            for row in rows:
                secret: dict[str, object] = {
                    "secret_mode": None,
                    "secret_hint": "",
                    "secret_digest_prefix": "",
                    "resolved": True,
                }
                if row.secret is not None:
                    secret = self.commands.secrets.describe(
                        config.tenant_id, identifier, row.content_revision, row.secret
                    )
                values.append(
                    {
                        "config_id": identifier,
                        "content_revision": row.content_revision,
                        "definition": json.loads(row.definition.encode()),
                        "created_us": row.created_us,
                        **secret,
                    }
                )
            return tuple(values)

    def operation(
        self, tx: Transaction, principal: OperatorPrincipal, operation: ConsoleOperation
    ) -> dict[str, Any]:
        payload = operation.provider
        if payload is None or payload.action not in {"activate", "rollback"}:
            raise NotFoundError("provider rebuild not found")
        metadata = ConsoleOperations.metadata(operation, key_id=operation.key_id)
        allowed = (principal.key.id, principal.key.revision, principal.key.grant.fingerprint) == (
            operation.key_id,
            operation.key_revision,
            operation.grant_fingerprint,
        )
        metadata["cancellable"] = (
            metadata["cancellable"]
            and allowed
            and "providers.manage" in principal.key.grant.permissions
        )
        metadata["available_actions"] = ["cancel"] if metadata["cancellable"] else []
        revision = tx.providers.revision(
            operation.tenant_id, payload.config_id, payload.content_revision
        )
        assert revision is not None and payload.plan_json is not None
        plan = ProviderActivationPlan(**json.loads(payload.plan_json))
        serving = tx.providers.serving(operation.tenant_id)
        return {
            "operation": metadata,
            "config_id": payload.config_id,
            "action": payload.action,
            "generation_id": payload.generation_id,
            "serving_generation_id": serving.generation_id if serving else None,
            "side_effects": effects(plan, revision),
            "estimated_remaining_seconds": 0 if operation.status == "completed" else None,
        }

    def rebuild(self, principal: OperatorPrincipal, identifier: str) -> dict[str, Any]:
        with self.context.uow.read() as tx:
            fresh = self.commands.principal(tx, principal)
            operation = tx.console_operations.get(fresh.key.tenant_id, identifier)
            if operation is None:
                raise NotFoundError("provider rebuild not found")
            return self.operation(tx, fresh, operation)

    def rebuilds(
        self, principal: OperatorPrincipal, *, limit: int, after: tuple[int, str] | None
    ) -> tuple[dict[str, Any], ...]:
        with self.context.uow.read() as tx:
            fresh = self.commands.principal(tx, principal)
            rows = []
            for identifier in tx.providers.rebuild_ids(
                fresh.key.tenant_id, limit=limit, after=after
            ):
                operation = tx.console_operations.get(fresh.key.tenant_id, identifier)
                assert operation is not None
                rows.append(self.operation(tx, fresh, operation))
            return tuple(rows)
