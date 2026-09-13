"""Independent complete text configuration resolution and native applicability.

All six domains, original declaration semantics and explicit resource paths are
checked before a candidate is issued. Pure parsing never attests supplier facts
or grants a send capability. Old content candidates are not manufactured here.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from .definitions import MetadataValue, NotApplicable
from .registry import ReadOnlyRegistry
from .resolution import _prepare_resolution, _resolve_entries, _unsupported_common_declaration
from .resolution_results import ResolutionOk
from .snapshots import EffectiveSnapshot, PresentValue, SnapshotEntry
from .content_resolution import ContentSettingsSnapshot, ContentPlatformSnapshot, _domain
from .content_schema import matches_content_definition
from .runtime_schema import matches_runtime_definition, platform_requirements
from .information_schema import matches_information_definition, valid_information_entries, SUPPORTED_VALUES
from .text_schema import TEXT_RUNTIME_REQUIREMENTS, TEXT_CONTENT_REQUIREMENTS, TEXT_PROVIDER_REQUIREMENTS, text_definitions, matches_text_definition
from .text_validation import validate_text_values, validate_inherited_vector, validate_relationships
from companion_memory.persistence.schema import valid_identifier, InvalidValue, Value

_ISSUER=object()


@dataclass(frozen=True,slots=True)
class TextConfigurationError:
    code: str
    operation: str
    field: str
    reason: str


@dataclass(frozen=True,slots=True)
class TextConfigurationErr:
    error: TextConfigurationError


@dataclass(frozen=True,slots=True)
class TextConfigurationOk[T]:
    value: T


def configuration_failure(code: str, field: str, reason: str, operation: str='resolve_text_learning_configuration') -> TextConfigurationErr:
    return TextConfigurationErr(TextConfigurationError(code,operation,field,reason))


@dataclass(frozen=True,slots=True,init=False)
class TextMaterialContract:
    """Native fixed no-media text format, distinct from legacy base64 material."""
    platform_id: str
    format_version: str
    media_processing: bool

    def __init__(self) -> None:
        raise TypeError('Bind the fixed text material format.')


def bind_text_material(platform_id: str) -> TextMaterialContract:
    if not valid_identifier(platform_id):raise ValueError('A native platform identity is required.')
    value=object.__new__(TextMaterialContract)
    for key,item in dict(platform_id=platform_id,format_version='TEXT_CONTEXT_V1',media_processing=False).items():
        object.__setattr__(value,key,item)
    return value


@dataclass(frozen=True,slots=True,init=False)
class TextSettingsSnapshot(ContentSettingsSnapshot):
    """Configuration-owned complete records; no mutable nested references."""
    def record(self,key: str) -> MappingProxyType[str, Value]:
        value=self.value(key)
        if type(value) is not MappingProxyType:raise ValueError('A declared record is required.')
        return cast(MappingProxyType[str, Value], value)


@dataclass(frozen=True,slots=True,init=False)
class TextConfigurationCandidate:
    foundation: EffectiveSnapshot
    runtime: ContentSettingsSnapshot
    content: ContentSettingsSnapshot
    information: TextSettingsSnapshot
    text: TextSettingsSnapshot
    platforms: tuple[ContentPlatformSnapshot,...]
    material_contracts: tuple[TextMaterialContract,...]
    _issuer: object
    _directories: MappingProxyType[str, tuple[str, ...]]

    def __init__(self) -> None:
        raise TypeError('Resolve the complete six-domain text configuration.')

    def platform(self,platform_id: str) -> ContentPlatformSnapshot:
        for p in self.platforms:
            if p.platform_id==platform_id:return p
        raise ValueError('A bound platform is required.')


def _foundation_definitions(definitions) -> bool:
    from .checked_resolution import _check_logging_schema
    from .persistence_resolution import _check_persistence_schema
    from .provider_schema import matches
    from .logging_schema import _VALIDATOR_BINDINGS
    if len(definitions)!=40 or _check_persistence_schema(definitions) or _check_logging_schema(definitions):return False
    by_key={d.key:d for d in definitions}
    bindings={r.validator:(r.key,r.type) for r in TEXT_PROVIDER_REQUIREMENTS if r.validator}
    bindings.update({key:(r.key,r.type) for key,r in _VALIDATOR_BINDINGS.items()})
    bindings['storage_database_file']=('storage.database_file','string')
    for d in definitions:
        if _unsupported_common_declaration(d) is not None or any(bindings.get(v)!=(d.key,d.type) for v in d.validator):return False
    for r in TEXT_PROVIDER_REQUIREMENTS:
        d=by_key.get(r.key)
        if d is None or not matches(d,r) or d.validator!=((r.validator,) if r.validator else ()) or not set(r.dependencies)<=set(d.dependencies):return False
        if r.key in ('provider.accounts','provider.profiles','provider.role_profiles') and not {'provider','runtime'}<=set(d.consumers):return False
    return True


def _domain_definitions(kind,identity,definitions) -> bool:
    if kind=='foundation':return _foundation_definitions(definitions)
    if kind=='information':return len(definitions)==8 and all(matches_information_definition(d) for d in definitions)
    if kind=='text':return tuple(d.key for d in definitions)==tuple(sorted(d['key'] for d in text_definitions())) and all(matches_text_definition(d) for d in definitions)
    requirements=TEXT_RUNTIME_REQUIREMENTS if kind=='runtime' else TEXT_CONTENT_REQUIREMENTS if kind=='content' else platform_requirements(identity)
    if {d.key for d in definitions}!={r.key for r in requirements}:return False
    by_key={d.key:d for d in definitions}
    for r in requirements:
        d=by_key[r.key]
        if (not (matches_content_definition(d,r) if kind=='content' else matches_runtime_definition(d,r,kind=='platform'))
                or d.validator!=((r.validator,) if r.validator else ()) or not set(r.dependencies)<=set(d.dependencies)):
            return False
    return True


def _foundation_values(snapshot,protected_directories):
    from .logging_validation import _prepare_directories,_run_fixed_validator
    from .checked_resolution_results import CheckedResolutionOk
    from .persistence_resolution import _storage_path_valid
    from .provider_schema import run_validator
    directories=_prepare_directories(protected_directories)
    if type(directories) is not CheckedResolutionOk:return False
    entries=snapshot.list_entries();by_key={e.definition.key:e for e in entries}
    values={e.definition.key:e.state.value for e in entries if type(e.state) is PresentValue}
    if not any(values.get(k) is True for k in ('logging.console_enabled','logging.file_enabled')):return False
    for e in entries:
        if type(e.state) is not PresentValue:return False
        deps=MappingProxyType({k:by_key[k] for k in e.definition.dependencies})
        for v in e.definition.validator:
            if v.startswith('text_'):continue
            if v=='storage_database_file':
                if not _storage_path_valid(e.state.value):return False
            elif v=='provider_resource_limits':
                if run_validator(v,e.state.value,MappingProxyType({k:values[k] for k in deps})) is not None:return False
            elif _run_fixed_validator(v,e.state.value,deps,directories.value) is not None:return False
    return True


def resolve_text_learning_configuration(foundation:object,runtime:object,platforms:object,content:object,
        information:object,text:object,protected_directories:object,material_contracts:object) -> TextConfigurationOk[TextConfigurationCandidate] | TextConfigurationErr:
    """Resolve every original and new declaration/value before publishing identity."""
    fail=configuration_failure
    if (type(platforms) is not list and type(platforms) is not tuple) or len(platforms)!=1 or (type(material_contracts) is not list and type(material_contracts) is not tuple) or len(material_contracts)!=1:
        return fail('INVALID_INPUT','platforms','INVALID_SHAPE')
    platform=platforms[0];pd=_domain(platform,True)
    if pd is None or not valid_identifier(platform['platform_id']):return fail('INVALID_INPUT','platforms','INVALID_SHAPE')
    identity=platform['platform_id'];contract=material_contracts[0]
    if type(contract) is not TextMaterialContract or contract!=bind_text_material(identity):return fail('DEFINITION_MISMATCH','material','METADATA_MISMATCH')
    supplied=[('foundation','',foundation),('runtime','',runtime),('content','',content),('information','',information),('text','',text),('platform',identity,platform)]
    prepared=[]
    for kind,identity,raw in sorted(supplied):
        domain=_domain(raw,kind=='platform')
        if domain is None:return fail('INVALID_INPUT',kind,'INVALID_SHAPE')
        registry,explicit=domain
        r=_prepare_resolution(registry,explicit)
        if type(r) is not ResolutionOk:return fail('INVALID_INPUT',kind,'INVALID_SHAPE')
        prepared.append((kind,identity,registry,explicit,r.value))
    for kind,identity,_,_,definitions in prepared:
        if not _domain_definitions(kind,identity,definitions):return fail('DEFINITION_MISMATCH',kind,'METADATA_MISMATCH')
    built={}
    for kind,identity,registry,explicit,definitions in prepared:
        r=_resolve_entries(definitions,explicit)
        if type(r) is not ResolutionOk:return fail('VALUE_INVALID',kind,'RANGE_INVALID')
        states={e.definition.key:e.state for e in r.value}
        if any(type(states.get(dep)) is not PresentValue for e in r.value for dep in e.definition.dependencies):return fail('VALUE_INVALID',kind,'DEPENDENCY_MISSING')
        if kind=='foundation':view=EffectiveSnapshot._from_entries(registry,r.value)
        else:
            cls=ContentPlatformSnapshot if kind=='platform' else TextSettingsSnapshot if kind in ('text','information') else ContentSettingsSnapshot
            view=cls._issue(registry,r.value)
            if kind=='platform':object.__setattr__(view,'platform_id',identity)
        built[kind]=view
    from .logging_validation import _prepare_directories
    from .checked_resolution_results import CheckedResolutionOk
    checked_directories=_prepare_directories(protected_directories)
    if type(checked_directories) is not CheckedResolutionOk:return fail('INVALID_INPUT','context','INVALID_SHAPE')
    candidate=object.__new__(TextConfigurationCandidate)
    for key,value in dict(foundation=built['foundation'],runtime=built['runtime'],content=built['content'],
            information=built['information'],text=built['text'],platforms=(built['platform'],),material_contracts=(contract,),_issuer=_ISSUER,_directories=checked_directories.value).items():
        object.__setattr__(candidate,key,value)
    try:
        if not _foundation_values(candidate.foundation,protected_directories) or not valid_information_entries(candidate.information.list_entries()):
            return fail('VALUE_INVALID','foundation','BUDGET_INVALID')
        validate_text_values(candidate.foundation,candidate.text)
        if not validate_inherited_vector(candidate) or not validate_relationships(candidate,protected_directories):return fail('VALUE_INVALID','value','BUDGET_INVALID')
        from .text_codec import candidate_values
        candidate_values(candidate)
    except (InvalidValue,ValueError,TypeError,KeyError,AttributeError,UnicodeError,OverflowError):
        return fail('VALUE_INVALID','value','CAPACITY_INSUFFICIENT')
    return TextConfigurationOk(candidate)


def text_learning_snapshot_issue(candidate:object) -> TextConfigurationError | None:
    """Revalidate native ownership, metadata, values and all format relationships."""
    failure=configuration_failure('ACCESS_DENIED','identity','BINDING_MISMATCH','text_learning_snapshot_issue').error
    if type(candidate) is not TextConfigurationCandidate:return failure
    try:
        if candidate._issuer is not _ISSUER:return failure
        from .text_codec import candidate_inputs
        directories={key:list(paths) for key,paths in candidate._directories.items()}
        result=resolve_text_learning_configuration(*candidate_inputs(candidate,directories))
        if type(result) is not TextConfigurationOk:return failure
        from .text_codec import candidate_values
        if candidate_values(result.value)!=candidate_values(candidate):return failure
        return None
    except (AttributeError,ValueError,TypeError,KeyError):return failure
