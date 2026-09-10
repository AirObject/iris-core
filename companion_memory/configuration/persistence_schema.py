"""Single matching specification for storage and same-transaction audit values.

Requirements carry no effective defaults and do not register parameters. Trusted
assembly must supply complete declarations and explicit values to resolution.
"""

from dataclasses import dataclass

from .definitions import Bound, Declared, DeclaredType, NoDefault, NotApplicable, ParameterDefinition


@dataclass(frozen=True, slots=True)
class _PersistenceRequirement:
    """Behavioral metadata to compare against one frozen parameter definition."""

    key: str
    type: DeclaredType
    unit: str | None = None
    limits: tuple[int, int] | None = None
    validator: str | None = None


_PERSISTENCE_REQUIREMENTS = tuple(sorted((
    _PersistenceRequirement("storage.database_file", "string", validator="storage_database_file"),
    _PersistenceRequirement("storage.operation_timeout_ms", "integer", "milliseconds", (1, 60000)),
    _PersistenceRequirement("storage.lock_wait_ms", "integer", "milliseconds", (0, 60000)),
    _PersistenceRequirement("storage.close_timeout_ms", "integer", "milliseconds", (1, 60000)),
    _PersistenceRequirement("storage.read_capacity", "integer", "connections", (1, 16)),
    _PersistenceRequirement("storage.command_max_bytes", "integer", "bytes", (256, 1048576)),
    _PersistenceRequirement("storage.receipt_max_bytes", "integer", "bytes", (256, 65536)),
    _PersistenceRequirement("storage.wal_checkpoint_pages", "integer", "pages", (1, 65536)),
    _PersistenceRequirement("audit.event_max_bytes", "integer", "bytes", (256, 65536)),
    _PersistenceRequirement("audit.events_per_operation", "integer", "events", (1, 256)),
), key=lambda item: item.key))


def _matches_persistence_definition(definition: ParameterDefinition, expected: _PersistenceRequirement) -> bool:
    owner = "logging_service" if expected.key.startswith("audit.") else "persistence"
    if (definition.key != expected.key or definition.type != expected.type
            or definition.owner_module != owner or owner not in definition.consumers
            or not definition.required or definition.nullable
            or type(definition.default) is not NoDefault
            or set(definition.read_roles) != {"trusted_operator"}
            or set(definition.write_roles) != {"trusted_operator"}
            or definition.apply_mode != "INITIALIZE_ONLY"
            or type(definition.activation_group) is not NotApplicable
            or type(definition.enum) is not NotApplicable):
        return False
    if expected.unit is None:
        if type(definition.unit) is not NotApplicable:
            return False
    elif type(definition.unit) is not Declared or definition.unit.value != expected.unit:
        return False
    if expected.limits is None:
        return type(definition.range) is NotApplicable
    if type(definition.range) is not Declared:
        return False
    return all(type(bound) is Bound and type(bound.value) is int
               and bound.inclusive and bound.value == value
               for bound, value in zip((definition.range.value.lower, definition.range.value.upper), expected.limits))
