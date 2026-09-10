"""Check native snapshot applicability and read fixed service selections.

No defaults, private configuration imports, re-resolution, or local validator
are used. Native snapshots with the mandatory nonempty validator declarations
can only be produced by configuration's checked entry point: ordinary resolution
rejects those declarations across the entire registry. That entry checks all
metadata and values atomically. This proof relies on supported public snapshot
construction, not protection against malicious same-process reflection.
"""

from dataclasses import dataclass
from typing import cast

from companion_memory.configuration import (
    CheckedResolutionOk, EffectiveSnapshot, Ok, PresentValue, ResolutionOk,
)

from ._settings import _Settings, _read_settings
from .results import LoggingErr, LoggingError

_SUFFIXES = tuple(sorted((
    "instance_level", "module_levels", "console_enabled", "file_enabled",
    "console_level", "file_level", "console_stream", "file_directory",
    "event_max_bytes", "sink_capacity", "warning_reserve", "preparation_capacity",
    "rotation_bytes", "retained_segments", "io_timeout_ms", "probe_interval_ms",
    "flush_timeout_ms", "close_timeout_ms", "emergency_capacity", "emergency_interval_ms",
)))
_VALIDATORS = (
    ("module_levels", "logging_module_levels", None),
    ("file_directory", "logging_file_directory", None),
    ("warning_reserve", "logging_warning_reserve", "logging.sink_capacity"),
    ("rotation_bytes", "logging_rotation_bytes", "logging.event_max_bytes"),
)


@dataclass(frozen=True, slots=True)
class _ServiceSettings:
    """Lifecycle settings selected from the same fixed, checked native snapshot."""

    event: _Settings
    file_directory: str
    rotation_bytes: int
    retained_segments: int
    probe_interval_ms: int
    flush_timeout_ms: int
    close_timeout_ms: int
    emergency_capacity: int
    emergency_interval_ms: int


def _service_settings(snapshot: object) -> _ServiceSettings | LoggingErr:
    """Check carrier, complete presence, and checked-resolution capability in order."""
    def failure(reason: str) -> LoggingErr:
        return LoggingErr(LoggingError("INVALID_CONFIGURATION", "initialize",
                                      "snapshot" if reason == "SNAPSHOT_REQUIRED" else "configuration", reason))

    if type(snapshot) is not EffectiveSnapshot:
        return failure("SNAPSHOT_REQUIRED")
    registry = snapshot.get_registry()
    for suffix in _SUFFIXES:
        key = "logging." + suffix
        entry = snapshot.get_entry(key)
        definition = registry.get_definition(key)
        if (type(entry) is not ResolutionOk or type(entry.value.state) is not PresentValue
                or type(definition) is not Ok):
            return failure("CONFIGURATION_REQUIRED")
    entries = {entry.definition.key: entry for entry in snapshot.list_entries()}
    for suffix, validator, dependency in _VALIDATORS:
        definition = entries["logging." + suffix].definition
        if (validator not in definition.validator
                or (dependency is not None and dependency not in definition.dependencies)):
            return failure("CONFIGURATION_UNSUPPORTED")
    # No public constructor can produce a native snapshot with these declarations
    # without the complete schema, capability and value validation succeeding.
    checked = CheckedResolutionOk(snapshot)
    event = _read_settings(checked)

    def value(suffix: str) -> object:
        state = entries["logging." + suffix].state
        assert type(state) is PresentValue
        return state.value

    return _ServiceSettings(
        event, cast(str, value("file_directory")), cast(int, value("rotation_bytes")),
        cast(int, value("retained_segments")), cast(int, value("probe_interval_ms")),
        cast(int, value("flush_timeout_ms")), cast(int, value("close_timeout_ms")),
        cast(int, value("emergency_capacity")), cast(int, value("emergency_interval_ms")),
    )
