"""Resolve the complete immutable local information configuration.

All five separately supplied domains are mandatory. Existing domains retain
content validation and provenance; the information domain is independently
closed and validated before the complete candidate is issued. This module
performs no persistence, directory creation, model call or runtime activation.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from .content_resolution import (
    ContentConfigurationCandidate, ContentConfigurationOk, ContentConfigurationErr,
    resolve_content_configuration, content_snapshot_issue, ContentSettingsSnapshot,
    ContentPlatformSnapshot,
)
from .content_material import ContentMaterialContract
from .snapshots import EffectiveSnapshot, PresentValue, SnapshotEntry
from .registry import ReadOnlyRegistry
from .resolution import _resolve_entries
from .resolution_results import ResolutionOk
from .definitions import MetadataValue
from .information_schema import valid_information_entries, matches_information_definition, SUPPORTED_VALUES, SettingValue
from .information_vector import supports_inherited_values


@dataclass(frozen=True, slots=True)
class InformationConfigurationError:
    """Safe fixed envelope; submitted values and lower exception text are absent."""
    code: str
    operation: str
    field: str
    reason: str


@dataclass(frozen=True, slots=True)
class InformationConfigurationErr:
    error: InformationConfigurationError


@dataclass(frozen=True, slots=True)
class InformationConfigurationOk[T]:
    value: T


def configuration_failure(code: str, field: str, reason: str,
                          operation: str = 'resolve_information_configuration') -> InformationConfigurationErr:
    """Build a configuration failure without retaining external input."""
    return InformationConfigurationErr(InformationConfigurationError(code, operation, field, reason))


@dataclass(frozen=True, slots=True, init=False)
class InformationSettingsSnapshot:
    """Issued complete records with no fallback defaults or editable dictionaries."""
    _registry: ReadOnlyRegistry
    _entries: tuple[SnapshotEntry, ...]

    def __init__(self) -> None:
        raise TypeError('Resolve the complete information configuration.')

    def get_registry(self) -> ReadOnlyRegistry:
        return self._registry

    def list_entries(self) -> tuple[SnapshotEntry, ...]:
        return self._entries

    def record(self, key: str) -> MappingProxyType[str, SettingValue]:
        """Read one validated record using its registered semantic parameter key."""
        for entry in self._entries:
            if entry.definition.key == key and type(entry.state) is PresentValue and type(entry.state.value) is MappingProxyType:
                return cast(MappingProxyType[str, SettingValue], entry.state.value)
        raise ValueError('A declared information record is required.')


_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class InformationConfigurationCandidate:
    """A complete candidate has no durable identity until configuration commits."""
    _content_candidate: ContentConfigurationCandidate
    information: InformationSettingsSnapshot
    _issuer: object

    def __init__(self) -> None:
        raise TypeError('Resolve all five configuration domains.')

    @property
    def foundation(self) -> EffectiveSnapshot:
        return self._content_candidate.foundation

    @property
    def runtime(self) -> ContentSettingsSnapshot:
        return self._content_candidate.runtime

    @property
    def content(self) -> ContentSettingsSnapshot:
        return self._content_candidate.content

    @property
    def platforms(self) -> tuple[ContentPlatformSnapshot, ...]:
        return self._content_candidate.platforms

    @property
    def material_contracts(self) -> tuple[ContentMaterialContract, ...]:
        return self._content_candidate.material_contracts

    def platform(self, platform_id: str) -> ContentPlatformSnapshot:
        return self._content_candidate.platform(platform_id)


def resolve_information_configuration(foundation: object, runtime: object, platforms: object,
                                      content: object, information: object, protected_directories: object,
                                      material_contracts: object) -> InformationConfigurationOk[InformationConfigurationCandidate] | InformationConfigurationErr:
    """Validate each independent domain and issue only the complete supported pack.

    The existing content parser receives its original explicit domain carriers;
    no new key is discarded or reinterpreted as an old-domain setting. Nested
    information values must match an explicitly supported closed vector.
    """
    fail = configuration_failure
    if type(information) is not dict or any(type(k) is not str for k in information) or set(information) != {'registry', 'explicit_values'}:
        return fail('INVALID_INPUT', 'information', 'INVALID_SHAPE')
    registry, values = information['registry'], information['explicit_values']
    if type(registry) is not ReadOnlyRegistry or type(values) is not dict or any(type(k) is not str for k in values):
        return fail('INVALID_INPUT', 'information', 'INVALID_SHAPE')
    definitions = registry.list_definitions()
    if tuple(d.key for d in definitions) != tuple(sorted(SUPPORTED_VALUES)) or any(not matches_information_definition(d) for d in definitions):
        return fail('DEFINITION_MISMATCH', 'definition', 'METADATA_MISMATCH')
    if set(values) != set(SUPPORTED_VALUES):
        return fail('VALUE_INVALID', 'information', 'INVALID_SHAPE')
    resolved = _resolve_entries(definitions, cast(dict[str, MetadataValue], values))
    if type(resolved) is not ResolutionOk:
        return fail('VALUE_INVALID', 'information', 'RANGE_INVALID')
    try:
        if not valid_information_entries(resolved.value):
            return fail('VALUE_INVALID', 'information', 'BUDGET_INVALID')
    except (ValueError, TypeError, UnicodeError):
        return fail('VALUE_INVALID', 'information', 'CAPACITY_INSUFFICIENT')
    base = resolve_content_configuration(foundation, runtime, platforms, content, protected_directories, material_contracts)
    if type(base) is ContentConfigurationErr:
        return fail(base.error.code, base.error.field, base.error.reason)
    if type(base) is not ContentConfigurationOk or len(base.value.platforms) != 1:
        return fail('VALUE_INVALID', 'platforms', 'BUDGET_INVALID')
    if not supports_inherited_values(base.value):
        return fail('VALUE_INVALID', 'value', 'BUDGET_INVALID')
    view = object.__new__(InformationSettingsSnapshot)
    object.__setattr__(view, '_registry', registry)
    object.__setattr__(view, '_entries', resolved.value)
    candidate = object.__new__(InformationConfigurationCandidate)
    object.__setattr__(candidate, '_content_candidate', base.value)
    object.__setattr__(candidate, 'information', view)
    object.__setattr__(candidate, '_issuer', _ISSUER)
    from .information_codec import candidate_values
    try:
        candidate_values(candidate)
    except (ValueError, TypeError, UnicodeError):
        return fail('VALUE_INVALID', 'information', 'CAPACITY_INSUFFICIENT')
    return InformationConfigurationOk(candidate)


def information_snapshot_issue(candidate: object) -> InformationConfigurationError | None:
    """Recheck native issuance and all metadata, values and persistent byte bounds."""
    failure = configuration_failure('ACCESS_DENIED', 'identity', 'BINDING_MISMATCH', 'information_snapshot_issue').error
    if type(candidate) is not InformationConfigurationCandidate:
        return failure
    try:
        if (candidate._issuer is not _ISSUER or content_snapshot_issue(candidate._content_candidate) is not None
                or not supports_inherited_values(candidate._content_candidate)):
            return failure
        if type(candidate.information) is not InformationSettingsSnapshot or not valid_information_entries(candidate.information.list_entries()):
            return failure
        if tuple(e.definition for e in candidate.information.list_entries()) != candidate.information.get_registry().list_definitions():
            return failure
        from .information_codec import candidate_values
        candidate_values(candidate)
        return None
    except (AttributeError, TypeError, ValueError):
        return failure
