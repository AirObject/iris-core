"""Explicit synthetic configuration, isolated directories and parameter declarations.

All tunable values are selected here by trusted test assembly. Production modules
carry constraints only; nothing is registered as an import side effect.
"""
from pathlib import Path
from typing import cast
from companion_memory.configuration import (
    Bound, Declared, MetadataValue, NoDefault, NotApplicable, Ok, RangeDescriptor,
    ParameterDefinitionInput, ReadOnlyRegistry, bind_synthetic_material,
    resolve_runtime_configuration, RuntimeConfigurationOk,
)
from companion_memory.configuration.runtime_schema import RUNTIME_REQUIREMENTS, RuntimeRequirement, platform_requirements
from companion_memory.configuration import create_registry_builder
from tests.configuration.resolution_support import resolution_definition
from tests.configuration.provider_support import provider_definitions, provider_values
from tests.configuration.logging_support import logging_definitions

RUNTIME_VALUES: dict[str,MetadataValue] = {
 'ingress.event_max_bytes':1024,'runtime.max_active_entries':1,'runtime.operation_timeout_ms':5000,
 'runtime.recovery_timeout_ms':30000,'runtime.close_timeout_ms':5000,'runtime.focus_drain_timeout_ms':30000,
 'runtime.claim_lease_ms':15000,'runtime.local_retry_limit':1,'runtime.read_page_size':16,'runtime.transfer_page_size':16,
 'learning.material_max_bytes':8192,'learning.input_units_limit':8192,'learning.output_units_limit':1024,
 'management.observation_row_limit':32,'management.observation_max_bytes':32768,'management.observation_timeout_ms':2000,
 'management.observation_concurrency':2,'management.refresh_min_interval_ms':1000,
 'logging.web_window_events':512,'logging.web_query_row_limit':32,'logging.web_query_max_bytes':32768,'logging.web_query_timeout_ms':1000,
}
PLATFORM_VALUES: dict[str,MetadataValue] = {'history_context_count':1,'recent_context_count':1,'target_count':2,'normal_soft_limit':1000,'explicit_short_enabled':False,'idle_tail_enabled':False,'idle_timeout_ms':0}


def registry(definitions: list[ParameterDefinitionInput]) -> ReadOnlyRegistry:
    builder=create_registry_builder()
    for d in definitions:
        result=builder.register(d)
        assert type(result) is Ok,result
    frozen=builder.freeze()
    assert type(frozen) is Ok,frozen
    return frozen.value


def definitions(requirements: tuple[RuntimeRequirement,...], platform: bool = False) -> list[ParameterDefinitionInput]:
    return [resolution_definition(key=r.key,owner_module=r.owner,consumers=list(r.consumers),schema_revision='synthetic_runtime',
        type='integer' if r.limits else 'boolean',default=NoDefault(),required=True,nullable=False,
        unit=Declared(r.unit) if r.unit else NotApplicable('A boolean switch has no unit.'),
        range=Declared(RangeDescriptor(Bound(r.limits[0],True),Bound(r.limits[1],True))) if r.limits else NotApplicable('A boolean switch has no numeric range.'),
        enum=NotApplicable('No whole-value enumeration.'),scope=['platform' if platform else 'instance'],override_policy='no_override',sensitivity='public',
        validator=[r.validator] if r.validator else [],dependencies=list(r.dependencies),read_roles=['trusted_operator'],write_roles=['trusted_operator'],
        apply_mode='NEXT_BATCH' if platform else 'INITIALIZE_ONLY',activation_group=NotApplicable('No live updates.')) for r in requirements]


def inputs(root: Path, *, platform_ids: tuple[str,...] = ('sample_platform',),runtime_changes:dict[str,MetadataValue] | None=None,platform_changes:dict[str,MetadataValue] | None=None,foundation_changes:dict[str,MetadataValue] | None=None):
    root=root.resolve()
    for directory in ('database','logs','media','backup'):
        (root/directory).mkdir(exist_ok=True)
    values=provider_values(str(root/'database'/'runtime.sqlite3'))
    values.update({'storage.command_max_bytes':1048576,'storage.receipt_max_bytes':65536,'provider.request_max_bytes':16384,
        'logging.file_directory':str(root/'logs'),'logging.console_enabled':True,'logging.file_enabled':True})
    profiles=cast(list[dict[str,MetadataValue]],values['provider.profiles'])
    profiles[0].update({'profile_id':'sample_learning','model_id':'sample_generation','max_input_units':8192,'max_output_units':1024,'max_items':2})
    values['provider.role_profiles']={'LEARNING':['sample_learning'],'DREAM':['sample_learning']}
    values.update(foundation_changes or {})
    foundation={'registry':registry(provider_definitions()+logging_definitions()),'explicit_values':values}
    runtime={'registry':registry(definitions(RUNTIME_REQUIREMENTS)),'explicit_values':{**RUNTIME_VALUES,**(runtime_changes or {})}}
    platforms=[{'platform_id':identity,'registry':registry(definitions(platform_requirements(identity),True)),
                'explicit_values':{'platforms.'+identity+'.buffer.'+k:v for k,v in {**PLATFORM_VALUES,**(platform_changes or {})}.items()}} for identity in platform_ids]
    directories={'media':[str(root/'media')],'database':[str(root/'database')],'audit':[str(root/'database')],
                 'provider_usage':[str(root/'database')],'backup':[str(root/'backup')]}
    materials=[bind_synthetic_material(identity) for identity in platform_ids]
    return foundation,runtime,platforms,directories,materials


def candidate(root: Path,**changes):
    supplied=inputs(root,**changes)
    result=resolve_runtime_configuration(*supplied)
    assert type(result) is RuntimeConfigurationOk,result
    return result.value,supplied


def event(key: str, body: str = 'x') -> dict[str,object]:
    return {'body':body,'client_event_key':key,'correlation':None,'event_kind':'MESSAGE','event_version':1,
            'extensions':{},'media':[],'occurred_at':None,'quotation':[],
            'sender':{'display_name':None,'identity_source':'HOST','role':'UNKNOWN','subject_id':'s'}}
