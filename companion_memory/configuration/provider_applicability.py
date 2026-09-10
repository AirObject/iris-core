"""Pure applicability of native snapshots to provider assembly, without IDs or I/O."""
from types import MappingProxyType
from typing import Literal

from .logging_schema import _LOGGING_REQUIREMENTS, _matches_logging_definition
from .persistence_applicability import persistence_snapshot_issue
from .persistence_schema import _PERSISTENCE_REQUIREMENTS, _matches_persistence_definition
from .provider_resolution import _check_provider_capabilities
from .provider_schema import REQUIREMENTS, matches, run_validator
from .snapshots import EffectiveSnapshot, PresentValue


def provider_snapshot_issue(snapshot: object) -> Literal["SNAPSHOT_REQUIRED", "DEFINITION_MISMATCH", "CAPABILITY_MISSING", "VALUE_INVALID"] | None:
    """Check every required definition before any capability or value failure.

    A valid result grants no resource safety, authorization, persistent version,
    or activation. Protected directory inputs are not recreated from a snapshot.
    """
    if type(snapshot) is not EffectiveSnapshot:
        return "SNAPSHOT_REQUIRED"
    entries = snapshot.list_entries()
    definitions = tuple(entry.definition for entry in entries)
    by_key = {entry.definition.key: entry for entry in entries}
    logging = _LOGGING_REQUIREMENTS if any(item.key.startswith("logging.") for item in definitions) else ()
    for provider in REQUIREMENTS:
        if provider.key not in by_key or not matches(by_key[provider.key].definition, provider):
            return "DEFINITION_MISMATCH"
    for storage in _PERSISTENCE_REQUIREMENTS:
        if storage.key not in by_key or not _matches_persistence_definition(by_key[storage.key].definition, storage):
            return "DEFINITION_MISMATCH"
    for diagnostic in logging:
        if diagnostic.key not in by_key or not _matches_logging_definition(by_key[diagnostic.key].definition, diagnostic):
            return "DEFINITION_MISMATCH"
    if _check_provider_capabilities(definitions) is not None:
        return "CAPABILITY_MISSING"
    for provider in REQUIREMENTS:
        definition = by_key[provider.key].definition
        if ((provider.validator and provider.validator not in definition.validator)
                or any(key not in definition.dependencies for key in provider.dependencies)):
            return "CAPABILITY_MISSING"
    for storage in _PERSISTENCE_REQUIREMENTS:
        if storage.validator and storage.validator not in by_key[storage.key].definition.validator:
            return "CAPABILITY_MISSING"
    for diagnostic in logging:
        definition = by_key[diagnostic.key].definition
        if ((diagnostic.validator and diagnostic.validator not in definition.validator)
                or (diagnostic.dependency and diagnostic.dependency not in definition.dependencies)):
            return "CAPABILITY_MISSING"
    if persistence_snapshot_issue(snapshot) is not None:
        return "VALUE_INVALID"
    for entry in entries:
        if any(type(by_key[key].state) is not PresentValue for key in entry.definition.dependencies):
            return "VALUE_INVALID"
    values = {entry.definition.key: entry.state.value for entry in entries if type(entry.state) is PresentValue}
    for provider in REQUIREMENTS:
        if provider.key not in values:
            return "VALUE_INVALID"
        value = values[provider.key]
        if provider.limits is not None and (type(value) is not int or not provider.limits[0] <= value <= provider.limits[1]):
            return "VALUE_INVALID"
        if provider.validator:
            dependencies = MappingProxyType({key: values[key] for key in provider.dependencies})
            if run_validator(provider.validator, value, dependencies) is not None:
                return "VALUE_INVALID"
    return None
