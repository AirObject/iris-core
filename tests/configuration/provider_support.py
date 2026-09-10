"""Independent complete simulation settings for explicit provider test assembly."""
from typing import cast
from companion_memory.configuration import (Bound, Declared, DeclaredType, EffectiveSnapshot, MetadataValue, NoDefault,
    NotApplicable, Ok, ParameterDefinitionInput, ProviderResolutionOk, RangeDescriptor, create_registry_builder,
    resolve_configuration_with_provider_validation)
from tests.configuration.persistence_support import persistence_definitions, persistence_values
from tests.configuration.resolution_support import resolution_definition


def provider_definitions() -> list[ParameterDefinitionInput]:
    rows: tuple[tuple[str, DeclaredType, str | None, tuple[int, int] | None, str | None, list[str]], ...] = (
        ("max_in_flight", "integer", "requests", (1, 8), "provider_resource_limits", ["provider.result_max_bytes", "storage.command_max_bytes", "storage.receipt_max_bytes"]),
        ("request_timeout_ms", "integer", "milliseconds", (1, 60000), None, []),
        ("close_timeout_ms", "integer", "milliseconds", (1, 60000), None, []),
        ("retry_delay_ms", "integer", "milliseconds", (0, 60000), None, []),
        ("request_max_bytes", "integer", "bytes", (256, 1048576), None, []),
        ("result_max_bytes", "integer", "bytes", (256, 8192), None, []),
        ("query_row_limit", "integer", "rows", (1, 1000), None, []),
        ("accounts", "array", None, None, "provider_accounts", ["provider.max_in_flight"]),
        ("profiles", "array", None, None, "provider_profiles", ["provider.accounts", "provider.request_timeout_ms"]),
        ("role_profiles", "object", None, None, "provider_role_profiles", ["provider.profiles"]),
    )
    result = persistence_definitions()
    for name, kind, unit, bounds, validator, dependencies in rows:
        result.append(resolution_definition(key="provider."+name, owner_module="provider", type=kind, default=NoDefault(), required=True, nullable=False,
            unit=Declared(unit) if unit else NotApplicable("A complete object has no scalar unit."),
            range=Declared(RangeDescriptor(Bound(bounds[0], True), Bound(bounds[1], True))) if bounds else NotApplicable("A record is not a number."),
            enum=NotApplicable("No complete value enumeration."), validator=[validator] if validator else [], dependencies=dependencies,
            scope=["instance"], override_policy="no_override", sensitivity="public", read_roles=["trusted_operator"], write_roles=["trusted_operator"],
            apply_mode="INITIALIZE_ONLY", activation_group=NotApplicable("No live replacement."), consumers=["provider"], schema_revision="synthetic-provider",
            description="Explicit synthetic provider parameter.", rationale="Exercise the service with known values.", validation_method="Check exact finite input.",
            cost_impact="Synthetic TEST units only.", migration_impact="No storage format migration."))
    return result


def provider_values(path: str) -> dict[str, MetadataValue]:
    values = persistence_values(path)
    values.update({"storage.command_max_bytes": 65536, "storage.receipt_max_bytes": 65536,
                   "provider.max_in_flight": 2, "provider.request_timeout_ms": 1000, "provider.close_timeout_ms": 1000,
                   "provider.retry_delay_ms": 10, "provider.request_max_bytes": 4096, "provider.result_max_bytes": 4096, "provider.query_row_limit": 100,
                   "provider.accounts": [{"account_id": "sample_account", "window_id": "sample_window", "currency": "TEST", "max_in_flight": 1,
                                          "attempt_limit": 20, "cost_limit_atoms": 1000000}]})
    profiles = []
    for name, capability in (("generation", "GENERATION"), ("embedding", "EMBEDDING"), ("rerank", "RERANK"), ("media", "MEDIA_UNDERSTANDING")):
        profiles.append({"profile_id": name, "account_id": "sample_account", "model_id": "sample_model", "wire_protocol": "SIMULATED", "capability": capability,
                         "max_attempts": 2, "attempt_timeout_ms": 100, "max_input_units": 1024, "max_output_units": 128 if name == "generation" else 0,
                         "max_items": 1 if name == "media" else 64, "input_price_atoms": 2, "output_price_atoms": 3 if name == "generation" else 0,
                         "dimensions": 2 if name == "embedding" else None, "space_id": "sample_space" if name == "embedding" else None,
                         "media_tasks": [{"modality": "IMAGE", "task": "DESCRIBE"}, {"modality": "AUDIO", "task": "TRANSCRIBE"}, {"modality": "VIDEO", "task": "DESCRIBE"}] if name == "media" else []})
    values["provider.profiles"] = profiles
    values["provider.role_profiles"] = {"LEARNING": ["generation", "embedding", "rerank", "media"], "DREAM": ["generation"]}
    return values


def provider_snapshot(path: str, changes: dict[str, MetadataValue] | None = None) -> EffectiveSnapshot:
    builder = create_registry_builder()
    for definition in provider_definitions():
        registered = builder.register(definition)
        assert type(registered) is Ok, registered
    registry = builder.freeze()
    assert type(registry) is Ok
    values = provider_values(path)
    values.update(changes or {})
    resolved = resolve_configuration_with_provider_validation(registry.value, values, None)
    assert type(resolved) is ProviderResolutionOk, resolved
    return cast(EffectiveSnapshot, resolved.value)
