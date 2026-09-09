"""Central matching requirements for runtime diagnostic configuration definitions.

Literal defaults and behavioral metadata live here once. These private records
are comparison requirements, not registered parameters or deployment settings.
No path default or production path sensitivity decision is supplied. Assembly
must submit complete definitions explicitly through the registry's public API.
"""

from dataclasses import dataclass
from types import MappingProxyType

from .definitions import (
    Bound, Declared, DeclaredType, FrozenMetadataValue, LiteralDefault, NoDefault,
    NotApplicable, ParameterDefinition,
)
from .validation import _metadata_equal

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_INHERITED_LEVELS = (*_LEVELS, "NOTSET")
_LOGGING_MODULES = frozenset({
    "ingress", "runtime", "buffers", "media", "cognition", "memory", "self_model",
    "retrieval", "state", "goals", "dream", "management", "provider",
    "logging_service", "configuration", "bootstrap",
})
_MODULE_LEVEL_LIMIT = 16
_PATH_CHARACTER_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class _LoggingRequirement:
    """Immutable matching data; no definition construction or default fallback."""

    key: str
    type: DeclaredType
    default: NoDefault | LiteralDefault[FrozenMetadataValue]
    unit: str | None = None
    limits: tuple[int, int] | None = None
    enum: tuple[str, ...] | None = None
    validator: str | None = None
    dependency: str | None = None


_LOGGING_REQUIREMENTS = tuple(sorted((
    _LoggingRequirement("logging.instance_level", "string", LiteralDefault("INFO"), enum=_LEVELS),
    _LoggingRequirement("logging.module_levels", "object", LiteralDefault(MappingProxyType({})),
                        validator="logging_module_levels"),
    _LoggingRequirement("logging.console_enabled", "boolean", LiteralDefault(True)),
    _LoggingRequirement("logging.file_enabled", "boolean", LiteralDefault(True)),
    _LoggingRequirement("logging.console_level", "string", LiteralDefault("INFO"), enum=_INHERITED_LEVELS),
    _LoggingRequirement("logging.file_level", "string", LiteralDefault("DEBUG"), enum=_INHERITED_LEVELS),
    _LoggingRequirement("logging.console_stream", "string", LiteralDefault("stderr"), enum=("stderr", "split")),
    _LoggingRequirement("logging.file_directory", "string", NoDefault(), validator="logging_file_directory"),
    _LoggingRequirement("logging.event_max_bytes", "integer", LiteralDefault(4096), "bytes", (512, 65536)),
    _LoggingRequirement("logging.sink_capacity", "integer", LiteralDefault(1024), "events", (2, 65536)),
    _LoggingRequirement("logging.warning_reserve", "integer", LiteralDefault(128), "events", (1, 65535),
                        validator="logging_warning_reserve", dependency="logging.sink_capacity"),
    _LoggingRequirement("logging.preparation_capacity", "integer", LiteralDefault(16), "slots", (2, 256)),
    _LoggingRequirement("logging.rotation_bytes", "integer", LiteralDefault(10485760), "bytes", (512, 1073741824),
                        validator="logging_rotation_bytes", dependency="logging.event_max_bytes"),
    _LoggingRequirement("logging.retained_segments", "integer", LiteralDefault(5), "segments", (1, 100)),
    _LoggingRequirement("logging.io_timeout_ms", "integer", LiteralDefault(200), "milliseconds", (1, 60000)),
    _LoggingRequirement("logging.probe_interval_ms", "integer", LiteralDefault(1000), "milliseconds", (1, 60000)),
    _LoggingRequirement("logging.flush_timeout_ms", "integer", LiteralDefault(1000), "milliseconds", (1, 60000)),
    _LoggingRequirement("logging.close_timeout_ms", "integer", LiteralDefault(2000), "milliseconds", (1, 60000)),
    _LoggingRequirement("logging.emergency_capacity", "integer", LiteralDefault(8), "events", (1, 64)),
    _LoggingRequirement("logging.emergency_interval_ms", "integer", LiteralDefault(1000), "milliseconds", (1, 60000)),
), key=lambda requirement: requirement.key))

_VALIDATOR_BINDINGS = MappingProxyType({
    requirement.validator: requirement
    for requirement in _LOGGING_REQUIREMENTS if requirement.validator is not None
})


def _matches_logging_definition(
    definition: ParameterDefinition, expected: _LoggingRequirement,
) -> bool:
    """Compare behavior on frozen metadata, retaining free text and schema identity.

    Capability and required-declaration errors are handled before calling this
    predicate. Enum order, explanation text, extra consumers and dependencies do
    not change matching. Roles declare only the trusted operator; no auth occurs.
    """
    if (definition.key != expected.key or definition.type != expected.type
            or definition.owner_module != "logging_service"
            or not definition.required or definition.nullable
            or "logging_service" not in definition.consumers
            or set(definition.read_roles) != {"trusted_operator"}
            or set(definition.write_roles) != {"trusted_operator"}
            or definition.apply_mode != "INITIALIZE_ONLY"
            or type(definition.activation_group) is not NotApplicable):
        return False
    if type(definition.default) is not type(expected.default):
        return False
    if type(expected.default) is LiteralDefault:
        if type(definition.default) is not LiteralDefault:
            return False
        if not _metadata_equal(definition.default.value, expected.default.value):
            return False
    if expected.unit is None:
        if type(definition.unit) is not NotApplicable:
            return False
    elif type(definition.unit) is not Declared or definition.unit.value != expected.unit:
        return False
    if expected.limits is None:
        if type(definition.range) is not NotApplicable:
            return False
    else:
        if type(definition.range) is not Declared:
            return False
        for actual, endpoint in zip(
            (definition.range.value.lower, definition.range.value.upper), expected.limits,
        ):
            if (type(actual) is not Bound or not actual.inclusive
                    or not _metadata_equal(actual.value, endpoint)):
                return False
    if expected.enum is None:
        return type(definition.enum) is NotApplicable
    return (type(definition.enum) is Declared
            and len(definition.enum.value) == len(expected.enum)
            and all(any(_metadata_equal(member, allowed) for allowed in expected.enum)
                    for member in definition.enum.value))
