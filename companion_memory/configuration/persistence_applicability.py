"""Public read-only applicability checks for consumers of native snapshots.

Matching remains owned by configuration. Consumers get a fixed failure reason,
never rewritten values, defaults, or a privately constructed effective snapshot.
"""

from typing import Literal

from .persistence_resolution import _check_persistence_capabilities, _storage_path_valid
from .persistence_schema import _PERSISTENCE_REQUIREMENTS, _matches_persistence_definition
from .snapshots import EffectiveSnapshot, PresentValue

type PersistenceApplicabilityIssue = Literal["SNAPSHOT_REQUIRED", "DEFINITION_MISMATCH", "CAPABILITY_MISSING", "VALUE_INVALID"]


def persistence_snapshot_issue(snapshot: object, *, audit_only: bool = False, managed_paths: bool = False) -> PersistenceApplicabilityIssue | None:
    """Inspect native entries in definition, capability, then value order.

    audit_only selects the audit consumer's two declared parameters. This does
    not grant storage readiness, resources, permissions, or a new snapshot.
    """
    if type(snapshot) is not EffectiveSnapshot:
        return "SNAPSHOT_REQUIRED"
    entries = {entry.definition.key: entry for entry in snapshot.list_entries()}
    requirements = tuple(item for item in _PERSISTENCE_REQUIREMENTS if not audit_only or item.key.startswith("audit."))
    for requirement in requirements:
        entry = entries.get(requirement.key)
        if entry is None or not _matches_persistence_definition(entry.definition, requirement):
            return "DEFINITION_MISMATCH"
    definitions = tuple(entries[item.key].definition for item in requirements)
    if managed_paths:
        from .managed_resolution import _managed_common
        if any(_managed_common(d) is not None for d in definitions):
            return "CAPABILITY_MISSING"
    elif _check_persistence_capabilities(definitions) is not None:
        return "CAPABILITY_MISSING"
    for requirement in requirements:
        entry = entries[requirement.key]
        if requirement.validator is not None and requirement.validator not in entry.definition.validator:
            return "CAPABILITY_MISSING"
    for requirement in requirements:
        state = entries[requirement.key].state
        if type(state) is not PresentValue:
            return "VALUE_INVALID"
        value = state.value
        if requirement.limits is not None:
            if type(value) is not int or not requirement.limits[0] <= value <= requirement.limits[1]:
                return "VALUE_INVALID"
        elif not _storage_path_valid(value):
            return "VALUE_INVALID"
    return None
