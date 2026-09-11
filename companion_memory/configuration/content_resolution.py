"""Complete content configuration resolution with isolated metadata and byte budgets.

Foundation retains its existing public parser. New instance/platform domains
validate their real declarations without impersonating an older registry scope.
No input value, directory or rejected identifier appears in a safe error.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from .definitions import MetadataValue, NotApplicable
from .content_material import ContentMaterialContract as MaterialContract, valid_content_material as valid_material
from .provider_resolution import resolve_configuration_with_provider_validation, _check_provider_capabilities, _check_provider_schema
from .persistence_resolution import _check_persistence_schema
from .registry import ReadOnlyRegistry
from .resolution import _prepare_resolution, _resolve_entries
from .resolution_results import ResolutionErr, ResolutionOk
from .provider_resolution_results import ProviderResolutionOk
from .snapshots import EffectiveSnapshot, PresentValue, SnapshotEntry
from .runtime_schema import matches_runtime_definition, platform_requirements
from .content_schema import CONTENT_RUNTIME_REQUIREMENTS as RUNTIME_REQUIREMENTS, CONTENT_REQUIREMENTS, matches_content_definition


@dataclass(frozen=True, slots=True)
class ContentConfigurationError:
    """Fixed public failure with no submitted data or nested error object."""
    code: str
    operation: str
    field: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContentConfigurationErr:
    error: ContentConfigurationError


@dataclass(frozen=True, slots=True)
class ContentConfigurationOk[T]:
    value: T


def configuration_failure(code: str, field: str, reason: str, operation: str = 'resolve_content_configuration') -> ContentConfigurationErr:
    return ContentConfigurationErr(ContentConfigurationError(code, operation, field, reason))


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ContentSettingsSnapshot:
    """Configuration-issued immutable complete instance domain, with no live edits."""
    _registry: ReadOnlyRegistry
    _entries: tuple[SnapshotEntry, ...]

    def __init__(self):
        raise TypeError('Obtain settings from runtime configuration resolution.')

    @classmethod
    def _issue(cls, registry: ReadOnlyRegistry, entries: tuple[SnapshotEntry, ...]):
        result = object.__new__(cls)
        object.__setattr__(result, '_registry', registry)
        object.__setattr__(result, '_entries', entries)
        return result

    def get_registry(self) -> ReadOnlyRegistry:
        return self._registry

    def list_entries(self) -> tuple[SnapshotEntry, ...]:
        return self._entries

    def get_entry(self, key: object) -> ResolutionOk[SnapshotEntry] | ContentConfigurationErr:
        if type(key) is not str:
            return configuration_failure('INVALID_INPUT','identity','INVALID_SHAPE','content_snapshot_issue')
        for entry in self._entries:
            if entry.definition.key == key:
                return ResolutionOk(entry)
        return configuration_failure('INVALID_INPUT','identity','UNKNOWN_KEY','content_snapshot_issue')

    def value(self, key: str) -> object:
        """Read a required declared value; never substitute an internal default."""
        result = self.get_entry(key)
        if type(result) is not ResolutionOk or type(result.value.state) is not PresentValue:
            raise ValueError('A required configuration value is unavailable.')
        return result.value.state.value

    def integer(self, key: str) -> int:
        value = self.value(key)
        if type(value) is not int:
            raise ValueError('A declared integer configuration value is required.')
        return value


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ContentPlatformSnapshot(ContentSettingsSnapshot):
    """Separate native platform-bound view; never an EffectiveSnapshot."""
    platform_id: str

    def parameter(self, name: str) -> object:
        return self.value('platforms.'+self.platform_id+'.buffer.'+name)

    def count(self, name: str) -> int:
        return self.integer('platforms.'+self.platform_id+'.buffer.'+name)


_CANDIDATE_ISSUER=object()


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ContentConfigurationCandidate:
    """Complete immutable candidate; no persistent version exists until commit."""
    foundation: EffectiveSnapshot
    runtime: ContentSettingsSnapshot
    content: ContentSettingsSnapshot
    platforms: tuple[ContentPlatformSnapshot, ...]
    material_contracts: tuple[MaterialContract, ...]
    _issuer: object

    def __init__(self):
        raise TypeError('Obtain a candidate from full configuration resolution.')

    def platform(self, platform_id: str) -> ContentPlatformSnapshot:
        for item in self.platforms:
            if item.platform_id == platform_id:
                return item
        raise ValueError('The platform has no bound configuration.')


def _domain(value: object, platform: bool = False) -> tuple[ReadOnlyRegistry, dict[str, MetadataValue]] | None:
    if type(value) is not dict or any(type(k) is not str for k in value) or set(value) != ({'platform_id','registry','explicit_values'} if platform else {'registry','explicit_values'}):
        return None
    registry, explicit = value['registry'], value['explicit_values']
    if type(registry) is not ReadOnlyRegistry or type(explicit) is not dict or any(type(k) is not str for k in explicit):
        return None
    return registry, explicit


def resolve_content_configuration(foundation: object, runtime: object, platforms: object, content: object, protected_directories: object, material_contracts: object) -> ContentConfigurationOk[ContentConfigurationCandidate] | ContentConfigurationErr:
    """Validate all domains, then publish exactly one isolated candidate.

    Six explicit carriers are mandatory. Full structural and capability checks
    precede all value checks; same-layer ordering is domain/platform/key order.
    Foundation diagnostics must be fully configured. Material versions and the
    worst full window are checked before any persistence side effect.
    """
    from companion_memory.persistence.schema import valid_identifier
    fail = configuration_failure
    first, second, third = _domain(foundation), _domain(runtime), _domain(content)
    if first is None or second is None or third is None or (type(platforms) is not list and type(platforms) is not tuple) or not 1 <= len(platforms) <= 64 or (type(material_contracts) is not list and type(material_contracts) is not tuple):
        return fail('INVALID_INPUT','context','INVALID_SHAPE')
    domains = [('foundation','',first),('runtime','',second),('content','',third)]
    for source in platforms:
        domain = _domain(source, True)
        if domain is None or not valid_identifier(source['platform_id']):
            return fail('INVALID_INPUT','platforms','INVALID_SHAPE')
        domains.append(('platforms',source['platform_id'],domain))
    ids = [identity for kind,identity,_ in domains if kind == 'platforms']
    if len(set(ids)) != len(ids):
        return fail('INVALID_INPUT','platforms','DUPLICATE_PLATFORM')
    if any(type(m) is not MaterialContract for m in material_contracts):
        return fail('VALUE_INVALID','platforms','BUDGET_INVALID')
    try:
        if any(not valid_identifier(object.__getattribute__(m,'platform_id')) for m in material_contracts):return fail('INVALID_INPUT','platforms','INVALID_SHAPE')
    except AttributeError:return fail('INVALID_INPUT','platforms','INVALID_SHAPE')
    contracts = cast(list[MaterialContract], material_contracts)
    if len(contracts) != len(ids) or {m.platform_id for m in contracts} != set(ids):
        return fail('INVALID_INPUT','platforms','UNKNOWN_KEY')
    from .validation import _is_identifier
    for kind,_,(_,explicit) in sorted(domains):
        if any(not _is_identifier(key) for key in explicit):return fail('INVALID_INPUT',kind,'INVALID_SHAPE')
    prepared = []
    for kind, identity, (registry, explicit) in sorted(domains):
        result = _prepare_resolution(registry, explicit)
        if type(result) is ResolutionErr:
            return fail('INVALID_INPUT',kind,'UNKNOWN_KEY' if result.error.code == 'UNKNOWN_PARAMETER' else 'INVALID_SHAPE')
        assert type(result) is ResolutionOk
        prepared.append((kind, identity, registry, explicit, result.value))
    # All declaration capabilities are checked before any required group or value.
    for kind, identity, _, _, definitions in prepared:
        if kind == 'foundation':
            if _check_provider_capabilities(definitions) is not None:
                return fail('UNSUPPORTED_CAPABILITY','foundation','FOUNDATION_UNSUPPORTED')
            continue
        requirements = platform_requirements(identity) if kind == 'platforms' else CONTENT_REQUIREMENTS if kind == 'content' else RUNTIME_REQUIREMENTS
        bindings = {(r.validator,r.key) for r in requirements if r.validator is not None}
        for d in definitions:
            if d.scope != (('platform',) if kind == 'platforms' else ('instance',)):
                return fail('UNSUPPORTED_CAPABILITY',kind,'SCOPE_UNSUPPORTED')
            if any((v,d.key) not in bindings for v in d.validator):
                return fail('UNSUPPORTED_CAPABILITY',kind,'VALIDATOR_UNSUPPORTED')
            if d.override_policy != 'no_override' or d.sensitivity != 'public' or d.deprecated or type(d.replacement) is not NotApplicable or type(d.upgrade_rule) is not NotApplicable:
                return fail('UNSUPPORTED_CAPABILITY',kind,'FOUNDATION_UNSUPPORTED')
    for kind, identity, _, _, definitions in prepared:
        if kind == 'foundation':
            from .checked_resolution import _check_logging_schema
            issue=_check_persistence_schema(definitions) or _check_provider_schema(definitions)
            if issue is None and any(d.key.startswith('logging.') for d in definitions):issue=_check_logging_schema(definitions)
            if issue is not None:
                reason=issue.error.issues[0].reason
                return fail('DEFINITION_MISMATCH','foundation','DEPENDENCY_REQUIRED' if reason=='REQUIRED_DEPENDENCY_MISSING' else 'REQUIRED_DEFINITION' if reason.endswith('DEFINITION_MISSING') else 'METADATA_MISMATCH')
            continue
        requirements = platform_requirements(identity) if kind == 'platforms' else CONTENT_REQUIREMENTS if kind == 'content' else RUNTIME_REQUIREMENTS
        by_key = {d.key:d for d in definitions}
        for r in sorted(requirements,key=lambda r:r.key):
            d = by_key.get(r.key)
            if d is None:
                return fail('DEFINITION_MISMATCH',kind,'REQUIRED_DEFINITION')
            if r.validator is not None and r.validator not in d.validator:
                return fail('DEFINITION_MISMATCH',kind,'METADATA_MISMATCH')
            if not set(r.dependencies) <= set(d.dependencies):
                return fail('DEFINITION_MISMATCH',kind,'DEPENDENCY_REQUIRED')
            if not (matches_content_definition(d,r) if kind == 'content' else matches_runtime_definition(d,r,kind == 'platforms')):
                return fail('DEFINITION_MISMATCH',kind,'METADATA_MISMATCH')
    resolved = resolve_configuration_with_provider_validation(first[0], first[1], cast(dict[str,list[str] | tuple[str,...]],protected_directories))
    if type(resolved) is not ProviderResolutionOk:
        return fail('VALUE_INVALID','foundation','RANGE_INVALID')
    foundation_values = {e.definition.key:e.state.value for e in resolved.value.list_entries() if type(e.state) is PresentValue}
    if 'logging.event_max_bytes' not in foundation_values or not any(foundation_values.get(k) is True for k in ('logging.console_enabled','logging.file_enabled')):
        return fail('UNSUPPORTED_CAPABILITY','foundation','FOUNDATION_UNSUPPORTED')
    built: dict[tuple[str,str],ContentSettingsSnapshot] = {}
    for kind,identity,registry,explicit,definitions in prepared:
        if kind == 'foundation':
            continue
        result = _resolve_entries(definitions,explicit)
        if type(result) is ResolutionErr:
            return fail('VALUE_INVALID','value' if kind == 'content' else kind,'RANGE_INVALID')
        assert type(result) is ResolutionOk
        view = ContentPlatformSnapshot._issue(registry,result.value) if kind == 'platforms' else ContentSettingsSnapshot._issue(registry,result.value)
        if kind == 'platforms':
            object.__setattr__(view,'platform_id',identity)
        built[kind,identity] = view
    for (kind,_),view in sorted(built.items()):
        values = {e.definition.key:e.state for e in view.list_entries()}
        if any(type(values.get(dep)) is not PresentValue for e in view.list_entries() for dep in e.definition.dependencies):
            return fail('VALUE_INVALID',kind,'DEPENDENCY_MISSING')
    settings = built['runtime','']
    ps = tuple(cast(ContentPlatformSnapshot,built['platforms',identity]) for identity in sorted(ids))
    if any(not valid_material(m) for m in contracts):return fail('VALUE_INVALID','platforms','BUDGET_INVALID')
    issue = _relationships(resolved.value,settings,built['content',''],ps,tuple(contracts),protected_directories)
    if issue is not None:
        return issue
    candidate = object.__new__(ContentConfigurationCandidate)
    object.__setattr__(candidate,"_issuer",_CANDIDATE_ISSUER)
    for key,value in dict(foundation=resolved.value,runtime=settings,content=built['content',''],platforms=ps,material_contracts=tuple(sorted(contracts,key=lambda m:m.platform_id))).items():
        object.__setattr__(candidate,key,value)
    from .content_codec import candidate_values
    try:
        candidate_values(candidate)
    except ValueError:
        return fail('VALUE_INVALID','content','CAPACITY_INSUFFICIENT')
    return ContentConfigurationOk(candidate)


def _relationships(foundation: EffectiveSnapshot, runtime: ContentSettingsSnapshot, content: ContentSettingsSnapshot, platforms: tuple[ContentPlatformSnapshot,...], contracts: tuple[MaterialContract,...], protected_directories: object = None) -> ContentConfigurationErr | None:
    f = {e.definition.key:e.state.value for e in foundation.list_entries() if type(e.state) is PresentValue}
    n = runtime.integer
    fail = configuration_failure
    if n('runtime.max_active_entries') > cast(int,f['provider.max_in_flight']):
        return fail('VALUE_INVALID','runtime','BUDGET_INVALID')
    capacity = 6*max(n('ingress.event_max_bytes'),content.integer('cognition.candidate_item_max_bytes'),content.integer('audit.history_item_max_bytes'),8192)+8192
    if capacity > min(cast(int,f['storage.command_max_bytes']),cast(int,f['storage.receipt_max_bytes'])):
        return fail('VALUE_INVALID','runtime','CAPACITY_INSUFFICIENT')
    if (n('logging.web_query_row_limit') > n('management.observation_row_limit') or n('logging.web_query_max_bytes') > n('management.observation_max_bytes') or n('logging.web_query_timeout_ms') > n('management.observation_timeout_ms')):
        return fail('VALUE_INVALID','runtime','CAPACITY_INSUFFICIENT')
    profiles = cast(tuple[MappingProxyType[str,object],...],f['provider.profiles'])
    roles = cast(MappingProxyType[str,tuple[str,...]],f['provider.role_profiles'])
    generation = tuple(p for p in profiles if p['profile_id'] in roles.get('LEARNING',()) and p['capability'] == 'GENERATION')
    if not generation:
        return fail('VALUE_INVALID','platforms','BUDGET_INVALID')
    for platform in platforms:
        h,t,r = (platform.count(k) for k in ('history_context_count','target_count','recent_context_count'))
        if h+t+r > 4 or platform.count('normal_soft_limit') < t+r:
            return fail('VALUE_INVALID','platforms','WINDOW_INVALID')
        if bool(platform.parameter('idle_tail_enabled')) != (platform.count('idle_timeout_ms') > 0):
            return fail('VALUE_INVALID','platforms','TRIGGER_INVALID')
        contract = next(c for c in contracts if c.platform_id == platform.platform_id)
        budget = contract.window_bound(h+t+r,n('ingress.event_max_bytes'),content.integer('media.event_occurrence_limit'),content.integer('media.interpretation_record_max_bytes'))
        if budget > min(n('learning.material_max_bytes'),n('learning.input_units_limit')):
            return fail('VALUE_INVALID','platforms','BUDGET_INVALID')
        if any(n('learning.input_units_limit') > cast(int,p['max_input_units']) or n('learning.output_units_limit') > cast(int,p['max_output_units']) or cast(int,p['max_items']) < 2 for p in generation):
            return fail('VALUE_INVALID','platforms','BUDGET_INVALID')
        # Request envelope includes maximum attribution IDs, fixed payload and LF escapes.
        if 4096+budget+(10+h+t+r) > cast(int,f['provider.request_max_bytes']):
            return fail('VALUE_INVALID','platforms','CAPACITY_INSUFFICIENT')
    from .content_validation import validate_content_relationships
    reason = validate_content_relationships(foundation, runtime, content, platforms, protected_directories)
    if reason is not None:
        return fail('VALUE_INVALID', 'content', reason)
    return None


def content_snapshot_issue(candidate: object) -> ContentConfigurationError | None:
    """Recheck complete native metadata, values, provenance and bound capabilities.

    This pure check grants no directory/resource authorization. Protected paths
    remain a separate explicit requirement when initializing persistent resources.
    """
    from .provider_applicability import provider_snapshot_issue
    from .resolution import _constraint_failure
    from .definitions import LiteralDefault
    from .snapshots import MissingValue
    failure=configuration_failure('ACCESS_DENIED','identity','BINDING_MISMATCH','content_snapshot_issue').error
    if type(candidate) is not ContentConfigurationCandidate:return failure
    try:
        if object.__getattribute__(candidate,'_issuer') is not _CANDIDATE_ISSUER:return failure
        if (type(candidate.foundation) is not EffectiveSnapshot or type(candidate.runtime) is not ContentSettingsSnapshot
                or type(candidate.content) is not ContentSettingsSnapshot
                or type(candidate.platforms) is not tuple or not 1<=len(candidate.platforms)<=64
                or type(candidate.material_contracts) is not tuple or any(type(p) is not ContentPlatformSnapshot for p in candidate.platforms)
                or any(not valid_material(m) for m in candidate.material_contracts)):
            return failure
        ids=tuple(p.platform_id for p in candidate.platforms)
        if ids!=tuple(sorted(set(ids))) or ids!=tuple(m.platform_id for m in candidate.material_contracts):return failure
        if provider_snapshot_issue(candidate.foundation) is not None:return failure
        for view,requirements in ((candidate.runtime,RUNTIME_REQUIREMENTS),(candidate.content,CONTENT_REQUIREMENTS),*((p,platform_requirements(p.platform_id)) for p in candidate.platforms)):
            definitions=tuple(e.definition for e in view.list_entries())
            by_key={d.key:d for d in definitions}
            for required in requirements:
                if required.key not in by_key or not (matches_content_definition(by_key[required.key],required) if view is candidate.content else matches_runtime_definition(by_key[required.key],required,type(view) is ContentPlatformSnapshot)):return failure
                definition=by_key[required.key]
                if required.validator is not None and required.validator not in definition.validator or not set(required.dependencies)<=set(definition.dependencies):return failure
            entries=view.list_entries()
            if tuple(e.definition for e in entries)!=view.get_registry().list_definitions():return failure
            states={e.definition.key:e.state for e in entries}
            for entry in entries:
                state=entry.state;definition=entry.definition
                if type(state) is MissingValue:
                    if definition.required or type(definition.default) is LiteralDefault:return failure
                elif type(state) is PresentValue:
                    if state.source not in ('EXPLICIT','DEFAULT') or _constraint_failure(definition,state.value,('value',)) is not None:return failure
                    if state.source=='DEFAULT' and (type(definition.default) is not LiteralDefault or state.value!=definition.default.value):return failure
                else:return failure
                if any(type(states.get(dep)) is not PresentValue for dep in definition.dependencies):return failure
        from .content_codec import candidate_values
        candidate_values(candidate)
        issue=_relationships(candidate.foundation,candidate.runtime,candidate.content,candidate.platforms,candidate.material_contracts)
        return issue.error if issue else None
    except (AttributeError,KeyError,TypeError,ValueError,StopIteration):
        return failure
