"""Resolve module and sink thresholds and evaluate encoded runtime events.

Results describe threshold eligibility only. Fault, capacity, lifecycle, and
atomic delivery decisions belong to the owner after this step; eligibility is
never labelled as enqueued or written. No Python logger or output is configured.
"""

from dataclasses import dataclass
from typing import Literal

from ._encoding import _EncodedEvent
from ._results import _Failure
from ._rules import _LEVEL_NUMBERS, _MODULES
from ._settings import _Settings


@dataclass(frozen=True, slots=True)
class _Thresholds:
    """Effective collection thresholds and resolved sink thresholds, without IO."""

    modules: tuple[tuple[str, int], ...]
    console: int
    file: int


@dataclass(frozen=True, slots=True)
class _TargetDecision:
    """A threshold decision, not a delivery receipt or loss counter."""

    sink: Literal["console", "file"]
    disposition: Literal["DISABLED", "FILTERED", "ELIGIBLE"]
    reason: Literal["SINK_DISABLED", "MODULE_THRESHOLD", "SINK_THRESHOLD", "NONE"]
    stream: Literal["stdout", "stderr"] | None


def _check_module(module: object) -> str | _Failure:
    """Accept exact known semantic names; never coerce or inherit prefixes."""
    if type(module) is not str or module not in _MODULES:
        return _Failure("INVALID_LOGGER", "get_logger", "module", "MODULE_NOT_ALLOWED")
    return module


def _resolve_thresholds(settings: _Settings) -> _Thresholds:
    """Resolve NOTSET through the instance and exclude disabled sinks from min.

    Explicit module levels set collection directly. When all sinks are disabled,
    the reported base collection threshold is the instance level, but decisions
    are always DISABLED. This immutable projection performs no hot mutation.
    """
    instance = _LEVEL_NUMBERS[settings.instance_level]
    console = instance if settings.console_level == "NOTSET" else _LEVEL_NUMBERS[settings.console_level]
    file = instance if settings.file_level == "NOTSET" else _LEVEL_NUMBERS[settings.file_level]
    candidates = [instance]
    if settings.console_enabled:
        candidates.append(console)
    if settings.file_enabled:
        candidates.append(file)
    inherited = min(candidates)
    overrides = dict(settings.module_levels)
    modules = tuple(
        (module, inherited if overrides.get(module, "NOTSET") == "NOTSET"
         else _LEVEL_NUMBERS[overrides[module]])
        for module in _MODULES
    )
    return _Thresholds(modules, console, file)


def _route_encoded(
    event: _EncodedEvent, settings: _Settings,
) -> tuple[_TargetDecision, _TargetDecision]:
    """Judge console then file only after safe construction and encoding.

    Requires an encoded event built with a checked module. Disabled precedes
    module filtering, which precedes sink filtering. ELIGIBLE leaves fault and
    capacity evaluation to delivery control and does not increment counters.
    """
    thresholds = _resolve_thresholds(settings)
    collection = dict(thresholds.modules)[event.record.logger]
    level = event.record.level_number

    def decide(
        sink: Literal["console", "file"], enabled: bool, threshold: int,
    ) -> _TargetDecision:
        if not enabled:
            return _TargetDecision(sink, "DISABLED", "SINK_DISABLED", None)
        if level < collection:
            return _TargetDecision(sink, "FILTERED", "MODULE_THRESHOLD", None)
        if level < threshold:
            return _TargetDecision(sink, "FILTERED", "SINK_THRESHOLD", None)
        stream = None
        if sink == "console":
            stream = ("stdout" if settings.console_stream == "split"
                      and level < _LEVEL_NUMBERS["WARNING"] else "stderr")
        return _TargetDecision(sink, "ELIGIBLE", "NONE", stream)

    return (decide("console", settings.console_enabled, thresholds.console),
            decide("file", settings.file_enabled, thresholds.file))
