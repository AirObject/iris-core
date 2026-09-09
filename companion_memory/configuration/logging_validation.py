"""Fixed pure-memory validators for runtime logging candidates and directory text.

Only isolated immutable values and declared dependency entries are inspected.
Paths use lexical POSIX components: no environment expansion, filesystem probes,
credential detection, or assurance about physical resource isolation is provided.
"""

from collections.abc import Mapping
from types import MappingProxyType

from .checked_resolution_results import (
    CheckedResolutionOk, CheckedResolutionReason,
    CheckedResolutionResult, _checked_failure,
)
from .definitions import FrozenMetadataValue
from .logging_schema import (
    _INHERITED_LEVELS, _LOGGING_MODULES, _MODULE_LEVEL_LIMIT, _PATH_CHARACTER_LIMIT,
)
from .snapshots import PresentValue, SnapshotEntry

_DIRECTORY_CATEGORIES = ("media", "database", "audit", "provider_usage", "backup")
type _ProtectedDirectories = Mapping[str, tuple[str, ...]]

# Each validator may report only its own fixed failures, never arbitrary text.
_VALIDATOR_FAILURES = MappingProxyType({
    "logging_module_levels": ("MODULE_LEVELS_INVALID",),
    "logging_warning_reserve": ("RESERVE_NOT_LESS_THAN_CAPACITY",),
    "logging_rotation_bytes": ("EVENT_EXCEEDS_ROTATION",),
    "logging_file_directory": ("PATH_SYNTAX_INVALID", "PATH_OVERLAP"),
})


def _path_components(path: str) -> tuple[str, ...] | None:
    """Return exact components for canonical absolute text, with root as ().

    Control characters include ASCII and the C1 control range. Literal ordinary
    characters are preserved; nothing is expanded, resolved, or normalized.
    """
    if (not path or len(path) > _PATH_CHARACTER_LIMIT or not path.startswith("/")
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)):
        return None
    if path == "/":
        return ()
    components = tuple(path[1:].split("/"))
    if any(component in ("", ".", "..") for component in components):
        return None
    return components


def _prepare_directories(protected_directories: object) -> CheckedResolutionResult[_ProtectedDirectories]:
    """Validate five explicit nonempty lists in category order and own their text.

    Check exact key types before lookup so foreign equality/hash hooks cannot
    execute. No submitted keys or path text are retained in errors.
    """
    root = ("protected_directories",)
    if (type(protected_directories) is not dict
            or any(type(key) is not str for key in protected_directories)
            or set(protected_directories) != set(_DIRECTORY_CATEGORIES)):
        return _checked_failure("INVALID_VALIDATION_CONTEXT", root, "INVALID_SHAPE")
    owned = {}
    for category in _DIRECTORY_CATEGORIES:
        directories = protected_directories[category]
        path = root + (category,)
        if ((type(directories) is not list and type(directories) is not tuple)
                or not directories):
            return _checked_failure("INVALID_VALIDATION_CONTEXT", path, "INVALID_SHAPE")
        for index, directory in enumerate(directories):
            if type(directory) is not str:
                return _checked_failure("INVALID_VALIDATION_CONTEXT", path + (index,), "INVALID_SHAPE")
            if _path_components(directory) is None:
                return _checked_failure("INVALID_VALIDATION_CONTEXT", path + (index,), "PATH_SYNTAX_INVALID")
        owned[category] = tuple(directories)
    return CheckedResolutionOk(MappingProxyType(owned))


def _validate_module_levels(value: Mapping[str, FrozenMetadataValue]) -> CheckedResolutionReason | None:
    if len(value) > _MODULE_LEVEL_LIMIT:
        return "MODULE_LEVELS_INVALID"
    if any(module not in _LOGGING_MODULES or type(level) is not str
           or level not in _INHERITED_LEVELS for module, level in value.items()):
        return "MODULE_LEVELS_INVALID"
    return None


def _validate_warning_reserve(
    value: int, dependencies: Mapping[str, SnapshotEntry],
) -> CheckedResolutionReason | None:
    capacity = dependencies["logging.sink_capacity"].state
    if type(capacity) is not PresentValue or type(capacity.value) is not int:
        return "VALIDATOR_FAILED"
    if value >= capacity.value:
        return "RESERVE_NOT_LESS_THAN_CAPACITY"
    return None


def _validate_rotation_bytes(
    value: int, dependencies: Mapping[str, SnapshotEntry],
) -> CheckedResolutionReason | None:
    event_size = dependencies["logging.event_max_bytes"].state
    if type(event_size) is not PresentValue or type(event_size.value) is not int:
        return "VALIDATOR_FAILED"
    if value < event_size.value:
        return "EVENT_EXCEEDS_ROTATION"
    return None


def _validate_file_directory(
    value: str, directories: _ProtectedDirectories,
) -> CheckedResolutionReason | None:
    components = _path_components(value)
    if components is None or not components:
        return "PATH_SYNTAX_INVALID"
    for category in _DIRECTORY_CATEGORIES:
        for directory in directories[category]:
            protected = _path_components(directory)
            if protected is None:
                return "VALIDATOR_FAILED"
            shared_length = min(len(components), len(protected))
            if components[:shared_length] == protected[:shared_length]:
                return "PATH_OVERLAP"
    return None


def _run_fixed_validator(
    identifier: str, value: FrozenMetadataValue,
    dependencies: Mapping[str, SnapshotEntry], directories: _ProtectedDirectories,
) -> CheckedResolutionReason | None:
    """Dispatch only program-owned validators and sanitize all ordinary failures.

    Dependency mappings contain only declared entries; only the path validator
    receives directory context. No public registration or callable input exists.
    Resource exhaustion and process-control exceptions remain runtime faults.
    """
    try:
        if identifier == "logging_module_levels":
            if type(value) is not MappingProxyType:
                return "VALIDATOR_FAILED"
            result = _validate_module_levels(value)
        elif identifier == "logging_warning_reserve":
            if type(value) is not int:
                return "VALIDATOR_FAILED"
            result = _validate_warning_reserve(value, dependencies)
        elif identifier == "logging_rotation_bytes":
            if type(value) is not int:
                return "VALIDATOR_FAILED"
            result = _validate_rotation_bytes(value, dependencies)
        elif identifier == "logging_file_directory":
            if type(value) is not str:
                return "VALIDATOR_FAILED"
            result = _validate_file_directory(value, directories)
        else:
            return "VALIDATOR_FAILED"
        if result is None:
            return None
        if type(result) is str and result in _VALIDATOR_FAILURES[identifier]:
            return result
    except MemoryError:
        raise
    except Exception:
        # Never retain, inspect, format, or log the exception or its traceback.
        return "VALIDATOR_FAILED"
    return "VALIDATOR_FAILED"
