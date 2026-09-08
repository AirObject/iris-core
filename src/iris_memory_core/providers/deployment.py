"""Strict, deployment-owned Provider reference and outbound policy selection."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from iris_memory_core.domain.errors import DomainError
from iris_memory_core.providers.configured import ConfiguredEmbeddingFactory
from iris_memory_core.providers.secret_lifecycle import deployment_secrets
from iris_memory_core.providers.secrets import read_private_file
from iris_memory_core.providers.transport import ProviderOutboundPolicy


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate deployment key")
        result[key] = value
    return result


def _strings(value: object, *, maximum: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(item, str) or not item or len(item) > 2048 for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid deployment string list")
    return tuple(value)


def load_embedding_deployment(
    database: Path,
    configuration: Path,
    *,
    master_key_file: Path | None = None,
    development_embedding: bool = False,
) -> ConfiguredEmbeddingFactory:
    """No network or credential lookup; malformed/private-path input fails startup.

    The file contains authority to access references, never plaintext provider
    credentials. It is read once per process; API and Worker restart together
    when the trusted policy changes. Environment/file credential rotation is
    resolved separately by the immutable configured binding cache.
    """
    try:
        path = configuration.expanduser().absolute()
        data = json.loads(read_private_file(path, maximum=65_536), object_pairs_hook=_object)
        if (
            not isinstance(data, dict)
            or set(data) - {"schema_version", "secret_references", "outbound"}
            or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1
        ):
            raise ValueError("invalid deployment schema")
        references = data.get("secret_references", {})
        if not isinstance(references, dict) or len(references) > 256:
            raise ValueError("invalid deployment reference map")
        allowed: dict[str, frozenset[str]] = {}
        for tenant, names in references.items():
            if not tenant or len(tenant) > 256:
                raise ValueError("invalid deployment tenant")
            allowed[tenant] = frozenset(_strings(names, maximum=64))
        if sum(len(names) for names in allowed.values()) > 1024:
            raise ValueError("too many deployment references")
        outbound = data.get("outbound", {})
        if not isinstance(outbound, dict) or set(outbound) - {
            field.name for field in fields(ProviderOutboundPolicy)
        }:
            raise ValueError("invalid deployment outbound policy")
        for name, value in tuple(outbound.items()):
            if name in {"allowed_hosts", "allowed_private_networks"}:
                outbound[name] = _strings(value, maximum=256)
            elif name == "allow_loopback":
                if type(value) is not bool:
                    raise ValueError("invalid deployment loopback switch")
            elif name in {"max_request_bytes", "max_response_bytes"}:
                if type(value) is not int:
                    raise ValueError("invalid deployment byte bound")
            elif type(value) not in (int, float):
                raise ValueError("invalid deployment timeout")
        policy = ProviderOutboundPolicy(**outbound)
        if policy.allow_loopback and not development_embedding:
            raise ValueError("loopback provider requires explicit development configuration")
        secrets = deployment_secrets(
            database,
            master_key_file,
            allowed_references=allowed,
            reserved_files=frozenset({path}),
        )
        return ConfiguredEmbeddingFactory(
            secrets,
            policy=policy,
            development_embedding=development_embedding,
        )
    except (DomainError, OSError, ValueError, TypeError, RecursionError):
        # Never echo JSON, env names, paths or bad ciphertext through startup
        # errors. The operator can inspect their own local deployment file.
        raise ValueError("invalid Provider deployment configuration") from None
