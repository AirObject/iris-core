"""Complete read-only configuration values bound to one frozen definition registry.

Entries preserve absence, null, and explicit/default provenance separately.
Snapshots are created only after resolution succeeds. They have no persistent
identity, activation state, refresh operation, permission checks, or external IO.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Self

from .definitions import FrozenMetadataValue, Identifier, ParameterDefinition
from .registry import ReadOnlyRegistry
from .resolution_results import ResolutionOk, ResolutionResult, _resolution_failure
from .validation import _is_identifier

type ValueSource = Literal["EXPLICIT", "DEFAULT"]


@dataclass(frozen=True, slots=True)
class MissingValue:
    """An optional registered value with no explicit input and no schema default.

    This state has neither a value nor a source and cannot be submitted as a value.
    """


@dataclass(frozen=True, slots=True)
class PresentValue:
    """An owned immutable value, including null, and its selection source.

    Resolution isolates mutable inputs before creating this record. DEFAULT
    identifies the bound definition's literal; it conveys no deployment priority.
    """

    value: FrozenMetadataValue
    source: ValueSource


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """A bound immutable definition and its resolved presence state.

    The definition provides the exact key, logical type, and opaque schema
    revision; that revision does not identify configuration content or activation.
    """

    definition: ParameterDefinition
    state: MissingValue | PresentValue


@dataclass(frozen=True, slots=True, init=False, eq=False)
class EffectiveSnapshot:
    """A fully resolved in-memory collection with one entry per registered key.

    Obtain instances from successful configuration resolution. All returned data is
    immutable and safely published instances support concurrent reads. Callers
    cannot refresh or rebind a snapshot; it is not a malicious-code sandbox.
    """

    _registry: ReadOnlyRegistry
    _entries: Mapping[Identifier, SnapshotEntry]
    _ordered: tuple[SnapshotEntry, ...]

    def __init__(self):
        raise TypeError("Obtain a snapshot from successful configuration resolution.")

    @classmethod
    def _from_entries(
        cls, registry: ReadOnlyRegistry, entries: tuple[SnapshotEntry, ...],
    ) -> Self:
        snapshot = object.__new__(cls)
        object.__setattr__(snapshot, "_registry", registry)
        object.__setattr__(snapshot, "_ordered", entries)
        object.__setattr__(snapshot, "_entries", MappingProxyType({
            entry.definition.key: entry for entry in entries
        }))
        return snapshot

    def get_registry(self) -> ReadOnlyRegistry:
        """Return the original frozen registry handle without looking up newer schemas."""
        return self._registry

    def get_entry(self, key: Identifier) -> ResolutionResult[SnapshotEntry]:
        """Read an exact key without fallback or side effects.

        Invalid key format precedes unknown-key failure. A registered missing
        value returns a successful MissingValue entry, not null or an error.
        Repeated reads preserve content; entry object identity is not promised.
        """
        if not _is_identifier(key):
            return _resolution_failure(
                "INVALID_PARAMETER_KEY", "get_entry", ("key",), "INVALID_IDENTIFIER",
            )
        if key not in self._entries:
            return _resolution_failure("UNKNOWN_PARAMETER", "get_entry", ("key",), "UNKNOWN_KEY")
        return ResolutionOk(self._entries[key])

    def list_entries(self) -> tuple[SnapshotEntry, ...]:
        """Return all entries, including missing states, in Unicode key order.

        An empty registry yields (). No filtering, IO, or dynamic evaluation
        occurs; repeated reads have the same content and order.
        """
        return self._ordered
