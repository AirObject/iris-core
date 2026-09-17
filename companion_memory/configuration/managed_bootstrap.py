"""Protected storage and logging snapshot before business configuration exists.

No Provider profile or business default is fabricated. The complete storage and
diagnostic declarations are resolved by configuration and later reused with the
same resource binding when the administrator finishes business setup.
"""
from dataclasses import replace
from typing import cast
from .definitions import (ParameterDefinitionInput, MetadataValue, NoDefault, NotApplicable,
                          LiteralDefault, Declared, Bound, RangeDescriptor)
from .deployment import DeploymentSettings, definition
from .logging_schema import _LOGGING_REQUIREMENTS
from .persistence_schema import _PERSISTENCE_REQUIREMENTS
from . import create_registry_builder, Ok
from .resolution import _resolve_entries
from .resolution_results import ResolutionOk
from .snapshots import EffectiveSnapshot
from .checked_resolution import _check_logging_schema
from .persistence_resolution import _check_persistence_schema, _storage_path_valid
from .logging_validation import _prepare_directories, _run_fixed_validator
from .checked_resolution_results import CheckedResolutionOk
from .snapshots import PresentValue
from types import MappingProxyType

STORAGE_DEFAULTS = {
    'storage.operation_timeout_ms': 60000, 'storage.lock_wait_ms': 1000,
    'storage.close_timeout_ms': 5000, 'storage.read_capacity': 4,
    'storage.command_max_bytes': 1048576, 'storage.receipt_max_bytes': 65536,
    'storage.wal_checkpoint_pages': 1000, 'audit.event_max_bytes': 16384,
    'audit.events_per_operation': 16,
}


def bootstrap_snapshot(settings: DeploymentSettings, directories: dict[str, tuple[str, ...]]) -> EffectiveSnapshot:
    """Resolve all local startup fields; no business configuration is returned."""
    root = settings.text('deployment.data_root')
    builder = create_registry_builder()
    definitions: list[ParameterDefinitionInput] = []
    for requirement in _PERSISTENCE_REQUIREMENTS:
        d = definition(requirement.key, kind=requirement.type, limits=requirement.limits,
                       unit=requirement.unit, sensitivity='administrator' if requirement.key == 'storage.database_file' else 'public')
        owner = 'logging_service' if requirement.key.startswith('audit.') else 'persistence'
        d.update(owner_module=owner, consumers=[owner], apply_mode='INITIALIZE_ONLY',
                 default=NoDefault(), validator=[requirement.validator] if requirement.validator else [])
        definitions.append(d)
    for requirement in _LOGGING_REQUIREMENTS:
        d = definition(requirement.key, kind=requirement.type, limits=requirement.limits,
                       unit=requirement.unit, sensitivity='administrator' if requirement.key == 'logging.file_directory' else 'public')
        default = requirement.default
        if type(default) is LiteralDefault and type(default.value) is MappingProxyType:
            default = LiteralDefault(dict(default.value))
        d.update(owner_module='logging_service', consumers=['logging_service'], apply_mode='INITIALIZE_ONLY',
                 default=cast(LiteralDefault[MetadataValue] | NoDefault, default),
                 validator=[requirement.validator] if requirement.validator else [],
                 dependencies=[requirement.dependency] if requirement.dependency else [],
                 enum=Declared(requirement.enum) if requirement.enum else NotApplicable('Not enumerated.'))
        definitions.append(d)
    for d in definitions:
        if type(builder.register(d)) is not Ok:
            raise ValueError('Invalid protected bootstrap definition.')
    frozen = builder.freeze()
    if type(frozen) is not Ok:
        raise ValueError('Invalid protected bootstrap registry.')
    values: dict[str, MetadataValue] = {**STORAGE_DEFAULTS,
        'storage.database_file': root + '/db/memory.sqlite3', 'logging.file_directory': root + '/logs/runtime'}
    resolved = _resolve_entries(frozen.value.list_definitions(), values)
    if type(resolved) is not ResolutionOk:
        raise ValueError('Invalid protected bootstrap values.')
    snapshot = EffectiveSnapshot._from_entries(frozen.value, resolved.value)
    if managed_bootstrap_issue(snapshot, directories) is not None:
        raise ValueError('Invalid protected bootstrap relationships.')
    return snapshot


def managed_bootstrap_issue(snapshot: object, directories: dict[str, tuple[str, ...]]) -> str | None:
    """Validate the complete local bootstrap format with administrator path labels."""
    if type(snapshot) is not EffectiveSnapshot:
        return 'SNAPSHOT_REQUIRED'
    definitions = snapshot.get_registry().list_definitions()
    if len(definitions) != 30 or _check_logging_schema(definitions) or _check_persistence_schema(definitions):
        return 'DEFINITION_MISMATCH'
    from .managed_resolution import _managed_common
    if any(_managed_common(d) is not None for d in definitions):
        return 'CAPABILITY_MISSING'
    protected = _prepare_directories(directories)
    if type(protected) is not CheckedResolutionOk:
        return 'VALUE_INVALID'
    entries = {e.definition.key: e for e in snapshot.list_entries()}
    for entry in entries.values():
        if type(entry.state) is not PresentValue:
            return 'VALUE_INVALID'
        for validator in entry.definition.validator:
            if validator == 'storage_database_file':
                if not _storage_path_valid(entry.state.value):return 'VALUE_INVALID'
            elif _run_fixed_validator(validator, entry.state.value,
                    MappingProxyType({key: entries[key] for key in entry.definition.dependencies}), protected.value) is not None:
                return 'VALUE_INVALID'
    return None
