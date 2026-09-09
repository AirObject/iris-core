"""Read event and routing settings from configuration's checked public result.

The caller must pass the successful result of
resolve_configuration_with_logging_validation, with its native snapshot intact.
This internal adapter is not service initialization or a substitute schema
validator. It reads only needed entries, supplies no defaults, and retains no
path or unrelated configuration. Physical resource checks are not performed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from companion_memory.configuration import (
    CheckedResolutionOk, EffectiveSnapshot, FrozenMetadataValue, PresentValue,
    ResolutionOk,
)


@dataclass(frozen=True, slots=True)
class _Settings:
    """Owned immutable selections from one checked configuration snapshot."""

    instance_level: str
    module_levels: tuple[tuple[str, str], ...]
    console_enabled: bool
    file_enabled: bool
    console_level: str
    file_level: str
    console_stream: str
    event_max_bytes: int
    sink_capacity: int
    warning_reserve: int
    preparation_capacity: int


def _read_settings(checked: CheckedResolutionOk[EffectiveSnapshot]) -> _Settings:
    """Project a checked native snapshot using get_entry, with no fallback.

Passing an ordinary resolution result, a duck-typed snapshot, or a manually
assembled success is outside this internal precondition. Obvious misuse raises
a fixed TypeError; this does not claim full initialize error ordering or verify
the provenance of a success wrapper against malicious same-process code.
"""
    if type(checked) is not CheckedResolutionOk or type(checked.value) is not EffectiveSnapshot:
        raise TypeError("A successful checked native configuration result is required.")

    def value(suffix: str) -> FrozenMetadataValue:
        result = checked.value.get_entry("logging." + suffix)
        if type(result) is not ResolutionOk or type(result.value.state) is not PresentValue:
            raise TypeError("Checked logging configuration must contain every required value.")
        return result.value.state.value

    # Types and value constraints were checked by configuration before publication.
    modules = cast(Mapping[str, str], value("module_levels"))
    return _Settings(
        cast(str, value("instance_level")), tuple(modules.items()),
        cast(bool, value("console_enabled")), cast(bool, value("file_enabled")),
        cast(str, value("console_level")), cast(str, value("file_level")),
        cast(str, value("console_stream")), cast(int, value("event_max_bytes")),
        cast(int, value("sink_capacity")), cast(int, value("warning_reserve")),
        cast(int, value("preparation_capacity")),
    )
