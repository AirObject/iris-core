"""Finite deployment registry; production never falls back to empty fake results."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from iris_memory_core.application.reflection import ReflectionPipeline
from iris_memory_core.domain.errors import DomainError, ProviderUnavailableError
from iris_memory_core.domain.reflection import ProviderKind
from iris_memory_core.domain.vector import EmbeddingProviderError
from iris_memory_core.providers.cognitive import (
    CognitiveProviderLimits,
    DurableProviderState,
    ProviderGovernance,
)
from iris_memory_core.providers.cognitive_http import CognitiveAuthorization, HttpCognitiveProvider
from iris_memory_core.providers.deployment import _object, _strings
from iris_memory_core.providers.secret_lifecycle import deployment_secrets
from iris_memory_core.providers.secrets import read_private_file
from iris_memory_core.providers.transport import PinnedEmbeddingTransport, ProviderOutboundPolicy
from iris_memory_core.storage.uow import Store

_KINDS: tuple[ProviderKind, ...] = (
    "extraction",
    "summarization",
    "reconciliation",
    "persona_evolution",
)


@dataclass(frozen=True, slots=True)
class CognitiveDeployment:
    providers: dict[str, HttpCognitiveProvider]
    governance: ProviderGovernance

    def pipeline(self, store: Store, tenant: str) -> ReflectionPipeline:
        provider = self.providers.get(tenant)
        if provider is None:
            raise ProviderUnavailableError(reason_code="provider_disabled", retryable=False)
        return ReflectionPipeline(
            store,
            store.clock,
            governance=self.governance,
            extraction=provider,
            summarization=provider,
        )


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid cognitive integer")
    return value


def _text(value: Any, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid cognitive string")
    return value


def load_cognitive_deployment(
    store: Store,
    configuration: Path | None,
    *,
    development_cognitive: bool = False,
    master_key_file: Path | None = None,
) -> CognitiveDeployment | None:
    if configuration is None:
        return None
    try:
        path = configuration.expanduser().absolute()
        data = json.loads(read_private_file(path, maximum=65536), object_pairs_hook=_object)
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "outbound", "limits", "tenants"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
        ):
            raise ValueError("invalid cognitive schema")
        outbound = data["outbound"]
        if not isinstance(outbound, dict) or set(outbound) - {
            item.name for item in fields(ProviderOutboundPolicy)
        }:
            raise ValueError("invalid outbound policy")
        for name, value in tuple(outbound.items()):
            if name in {"allowed_hosts", "allowed_private_networks"}:
                outbound[name] = _strings(value, maximum=256)
            elif name == "allow_loopback":
                if type(value) is not bool:
                    raise ValueError("invalid loopback flag")
            elif name in {"max_request_bytes", "max_response_bytes"}:
                _integer(value, 256, 67108864)
            elif type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("invalid timeout")
        policy = ProviderOutboundPolicy(**outbound)
        if not policy.allowed_hosts or (policy.allow_loopback and not development_cognitive):
            raise ValueError("explicit host authorization is required")
        limits_raw = data["limits"]
        if not isinstance(limits_raw, dict) or set(limits_raw) != set(_KINDS):
            raise ValueError("four provider limits required")
        limits: dict[ProviderKind, CognitiveProviderLimits] = {}
        for kind in _KINDS:
            raw = limits_raw[kind]
            if not isinstance(raw, dict) or set(raw) != {
                item.name for item in fields(CognitiveProviderLimits)
            }:
                raise ValueError("explicit governance limits required")
            for name, value in raw.items():
                if name in {
                    "max_retries",
                    "daily_budget_microunits",
                    "concurrency",
                    "breaker_failures",
                }:
                    _integer(value, 0, 10**12)
                elif type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("invalid governance bound")
            limits[kind] = CognitiveProviderLimits(**raw)
        tenants = data["tenants"]
        if not isinstance(tenants, dict) or not 1 <= len(tenants) <= 256:
            raise ValueError("explicit tenant authorization required")
        references: dict[str, frozenset[str]] = {}
        for tenant, raw in tenants.items():
            _text(tenant)
            if (
                not isinstance(raw, dict)
                or set(raw)
                != {"adapter", "endpoint", "model", "model_version", "secret_ref", "authorization"}
                or raw["adapter"] != "openai-compatible-chat"
            ):
                raise ValueError("unsupported cognitive adapter")
            policy.endpoint(_text(raw["endpoint"], 2048))
            _text(raw["model"], 96)
            _text(raw["model_version"], 96)
            if len(json.dumps([raw["model"], raw["model_version"]], separators=(",", ":"))) > 256:
                raise ValueError("model identity exceeds the persisted bound")
            references[tenant] = frozenset({_text(raw["secret_ref"], 2048)})
        secrets = deployment_secrets(
            store.runtime.database,
            master_key_file,
            allowed_references=references,
            reserved_files=frozenset({path}),
        )
        providers: dict[str, HttpCognitiveProvider] = {}
        for tenant, raw in tenants.items():
            auth = raw["authorization"]
            if not isinstance(auth, dict) or set(auth) != {
                "authorization_id",
                "privacy_labels",
                "max_input_chars",
                "max_output_tokens",
                "request_cost_microunits",
            }:
                raise ValueError("explicit data and cost authorization required")
            cost = _integer(auth["request_cost_microunits"], 1, 10**12)
            if any(cost > value.daily_budget_microunits for value in limits.values()):
                raise ValueError("per request cost exceeds daily budget")
            authority = CognitiveAuthorization(
                _text(auth["authorization_id"]),
                frozenset(_strings(auth["privacy_labels"], maximum=64)),
                _integer(auth["max_input_chars"], 1, 200000),
                _integer(auth["max_output_tokens"], 1, 16384),
                cost,
            )
            # Syntax and exact tenant-reference ownership validate at startup;
            # temporary missing material remains a governed dependency failure.
            secrets.reference(tenant, raw["secret_ref"])
            providers[tenant] = HttpCognitiveProvider(
                tenant_id=tenant,
                endpoint=raw["endpoint"],
                model=raw["model"],
                model_version=raw["model_version"],
                reference=raw["secret_ref"],
                secrets=secrets,
                transport=PinnedEmbeddingTransport(policy),
                authorization=authority,
            )
        return CognitiveDeployment(
            providers,
            ProviderGovernance(limits, durable_state=DurableProviderState(store, store.clock)),
        )
    except (DomainError, EmbeddingProviderError, OSError, ValueError, TypeError, RecursionError):
        raise ValueError("invalid cognitive Provider deployment configuration") from None
