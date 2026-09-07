"""Immutable Embedding configuration content and mutable lifecycle metadata."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Literal

from iris_memory_core.domain.errors import InvalidRequestError
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.vector import (
    VECTOR_MAX_INPUT_CHARS,
    VectorSpaceConfig,
    builder_versions_trusted,
)


@dataclass(frozen=True, slots=True)
class ProviderSecret:
    """Private descriptor; sealed bytes are stored separately from immutable content."""

    mode: Literal["secret_ref", "sealed"]
    reference: str | None = field(default=None, repr=False)
    ciphertext: str | None = field(default=None, repr=False)
    digest_prefix: str = ""
    hint: str = ""


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    batch_size: int = 32
    timeout_us: int = 2_000_000
    max_qps: float = 50.0
    breaker_failures: int = 5
    breaker_cooldown_us: int = 10_000_000
    max_input_chars: int = VECTOR_MAX_INPUT_CHARS

    def __post_init__(self) -> None:
        integer_bounds = (
            (self.batch_size, 1, 512),
            (self.timeout_us, 1_000, 30_000_000),
            (self.breaker_failures, 1, 100),
            (self.breaker_cooldown_us, 1_000, 3_600_000_000),
            (self.max_input_chars, 16, 400_000),
        )
        if any(
            type(value) is not int or not lower <= value <= upper
            for value, lower, upper in integer_bounds
        ):
            raise InvalidRequestError("provider limits are outside supported bounds")
        if (
            type(self.max_qps) not in (int, float)
            or not math.isfinite(self.max_qps)
            or not 0.1 <= self.max_qps <= 10_000
        ):
            raise InvalidRequestError("provider QPS is outside supported bounds")


@dataclass(frozen=True, slots=True)
class EmbeddingDefinition:
    adapter: str
    endpoint: str
    space: VectorSpaceConfig
    limits: ProviderLimits = field(default_factory=ProviderLimits)
    label: str = "Embedding"

    def __post_init__(self) -> None:
        if self.adapter not in {"openai-compatible", "deterministic"}:
            raise InvalidRequestError("unsupported embedding adapter")
        if not self.label or len(self.label) > 128:
            raise InvalidRequestError("provider label must be within 1..128 characters")
        if len(self.endpoint) > 2048 or (self.adapter == "openai-compatible" and not self.endpoint):
            raise InvalidRequestError("embedding endpoint is required")
        if self.adapter == "deterministic" and self.endpoint:
            raise InvalidRequestError("deterministic provider has no endpoint")
        if not builder_versions_trusted(self.space.builder_version, self.space.template_version):
            raise InvalidRequestError("unsupported vector builder or template version")

    def encode(self) -> str:
        return canonical_json(asdict(self))

    @classmethod
    def decode(cls, encoded: str) -> EmbeddingDefinition:
        data = json.loads(encoded)
        return cls(
            adapter=data["adapter"],
            endpoint=data["endpoint"],
            space=VectorSpaceConfig(**data["space"]),
            limits=ProviderLimits(**data["limits"]),
            label=data["label"],
        )

    def identity_hash(self) -> str:
        # A different endpoint/adapter cannot claim hot-limit equivalence merely
        # by reusing a model name. All six VectorSpaceConfig fields remain bound.
        encoded = canonical_json(
            {
                "adapter": self.adapter,
                "endpoint": self.endpoint,
                "space": self.space.as_manifest_dict(),
            }
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderConfigRevision:
    tenant_id: str
    config_id: str
    content_revision: int
    definition: EmbeddingDefinition
    secret: ProviderSecret | None = field(repr=False)
    created_us: int
    created_by: str

    def content_hash(self) -> str:
        secret = (
            None
            if self.secret is None
            else {
                "mode": self.secret.mode,
                "reference": self.secret.reference,
                "digest_prefix": self.secret.digest_prefix,
                "hint": self.secret.hint,
            }
        )
        return hashlib.sha256(
            canonical_json(
                {"definition": json.loads(self.definition.encode()), "secret": secret}
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    id: str
    tenant_id: str
    status: str
    revision: int
    content_revision: int
    created_us: int
    updated_us: int
    created_by: str
    current_operation_id: str | None = None
    latest_probe_id: str | None = None
    last_generation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    id: str
    tenant_id: str
    config_id: str
    content_revision: int
    operation_id: str
    ok: bool
    dimension_observed: int | None
    normalized: bool | None
    latency_ms: float
    outcome: str
    secret_fingerprint: str
    created_us: int


@dataclass(frozen=True, slots=True)
class ProviderServing:
    tenant_id: str
    config_id: str
    content_revision: int
    generation_id: str
    epoch: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class ProviderProbeObservation:
    ok: bool
    dimension_observed: int | None
    normalized: bool | None
    latency_ms: float
    outcome: str


FIXED_PROBE_INPUTS = ("iris probe alpha", "iris probe beta")
PROBE_INPUT_CHARS = sum(map(len, FIXED_PROBE_INPUTS))
