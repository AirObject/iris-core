"""Independent complete synthetic persistence declarations and explicit values.

Paths are supplied by the caller, never chosen for production. Matching metadata
is restated independently to detect drift in the configuration implementation.
"""

from typing import cast

from companion_memory.configuration import (
    Bound, Declared, DeclaredType, EffectiveSnapshot, MetadataValue, NoDefault, NotApplicable,
    Ok, ParameterDefinitionInput, PersistenceResolutionOk, RangeDescriptor,
    create_registry_builder, resolve_configuration_with_persistence_validation,
)
from tests.configuration.resolution_support import resolution_definition


def persistence_definitions() -> list[ParameterDefinitionInput]:
    rows: tuple[tuple[str, DeclaredType, str | None, tuple[int, int] | None], ...] = (
        ("storage.database_file", "string", None, None),
        ("storage.operation_timeout_ms", "integer", "milliseconds", (1, 60000)),
        ("storage.lock_wait_ms", "integer", "milliseconds", (0, 60000)),
        ("storage.close_timeout_ms", "integer", "milliseconds", (1, 60000)),
        ("storage.read_capacity", "integer", "connections", (1, 16)),
        ("storage.command_max_bytes", "integer", "bytes", (256, 1048576)),
        ("storage.receipt_max_bytes", "integer", "bytes", (256, 65536)),
        ("storage.wal_checkpoint_pages", "integer", "pages", (1, 65536)),
        ("audit.event_max_bytes", "integer", "bytes", (256, 65536)),
        ("audit.events_per_operation", "integer", "events", (1, 256)),
    )
    definitions = []
    for key, kind, unit, limits in rows:
        owner = "logging_service" if key.startswith("audit.") else "persistence"
        definitions.append(resolution_definition(
            key=key, owner_module=owner, type=kind, default=NoDefault(), required=True, nullable=False,
            unit=Declared(unit) if unit else NotApplicable("File text has no unit."),
            range=Declared(RangeDescriptor(Bound(limits[0], True), Bound(limits[1], True))) if limits else NotApplicable("File text has no numeric interval."),
            enum=NotApplicable("The complete value is not enumerated."),
            validator=["storage_database_file"] if key == "storage.database_file" else [],
            dependencies=[], scope=["instance"], override_policy="no_override", sensitivity="public",
            read_roles=["trusted_operator"], write_roles=["trusted_operator"],
            apply_mode="INITIALIZE_ONLY", activation_group=NotApplicable("No live replacement is supported."),
            consumers=[owner], schema_revision="synthetic_persistence",
            description="Synthetic " + key + " declaration.", rationale="Exercise complete storage and audit validation.",
            validation_method="Check exact inputs, bounded values and immutable publication.",
            cost_impact="Local operations make no paid calls.", migration_impact="Recreate service bindings; no database migration.",
        ))
    return definitions


def persistence_values(path: str = "/synthetic-public/database/application.sqlite3") -> dict[str, MetadataValue]:
    return {
        "storage.database_file": path, "storage.operation_timeout_ms": 1000,
        "storage.lock_wait_ms": 50, "storage.close_timeout_ms": 1000,
        "storage.read_capacity": 2, "storage.command_max_bytes": 16384,
        "storage.receipt_max_bytes": 4096, "storage.wal_checkpoint_pages": 100,
        "audit.event_max_bytes": 2048, "audit.events_per_operation": 8,
    }


def persistence_snapshot(path: str, changes: dict[str, MetadataValue] | None = None) -> EffectiveSnapshot:
    builder = create_registry_builder()
    for definition in persistence_definitions():
        result = builder.register(definition)
        assert type(result) is Ok
    registry = builder.freeze()
    assert type(registry) is Ok
    values = persistence_values(path)
    values.update(changes or {})
    resolved = resolve_configuration_with_persistence_validation(registry.value, values, None)
    assert type(resolved) is PersistenceResolutionOk
    return cast(EffectiveSnapshot, resolved.value)
