"""Complete metadata requirements for explicit runtime and platform settings.

These are schema constraints, never effective defaults or import-time registry
entries. Trusted assembly must register and submit all selected parameter values.
"""
from dataclasses import dataclass
from .definitions import Bound, Declared, NoDefault, NotApplicable, ParameterDefinition


@dataclass(frozen=True, slots=True)
class RuntimeRequirement:
    """One fixed parameter definition with its owner and mandatory consumers."""
    key: str
    owner: str
    unit: str | None
    limits: tuple[int, int] | None
    consumers: tuple[str, ...]
    validator: str | None = None
    dependencies: tuple[str, ...] = ()


RUNTIME_REQUIREMENTS = (
    RuntimeRequirement('ingress.event_max_bytes','ingress','bytes',(256,8192),('ingress',)),
    RuntimeRequirement('runtime.max_active_entries','runtime','entries',(1,8),('runtime',),'runtime_limits',('ingress.event_max_bytes','learning.material_max_bytes','learning.input_units_limit','learning.output_units_limit')),
    *(RuntimeRequirement('runtime.'+key,'runtime','milliseconds',(1,60000),consumers) for key,consumers in (
        ('operation_timeout_ms',('runtime','ingress','buffers')),('recovery_timeout_ms',('runtime',)),
        ('close_timeout_ms',('runtime',)),('focus_drain_timeout_ms',('runtime',)),('claim_lease_ms',('runtime',)))),
    RuntimeRequirement('runtime.local_retry_limit','runtime','attempts',(0,1),('runtime','ingress','buffers')),
    RuntimeRequirement('runtime.read_page_size','runtime','rows',(1,128),('runtime','buffers')),
    RuntimeRequirement('runtime.transfer_page_size','buffers','rows',(1,128),('buffers',)),
    RuntimeRequirement('learning.material_max_bytes','cognition','bytes',(256,8192),('runtime','buffers','cognition')),
    RuntimeRequirement('learning.input_units_limit','cognition','simulated_input_units',(256,1048576),('cognition','runtime')),
    RuntimeRequirement('learning.output_units_limit','cognition','simulated_output_units',(1,8192),('cognition','runtime')),
    RuntimeRequirement('management.observation_row_limit','management','rows',(1,128),('management','runtime')),
    RuntimeRequirement('management.observation_max_bytes','management','bytes',(1024,65536),('management','runtime'),'observation_limits',(
        'management.observation_row_limit','management.observation_timeout_ms','management.observation_concurrency','management.refresh_min_interval_ms',
        'logging.web_window_events','logging.web_query_row_limit','logging.web_query_max_bytes','logging.web_query_timeout_ms')),
    RuntimeRequirement('management.observation_timeout_ms','management','milliseconds',(1,60000),('management','runtime')),
    RuntimeRequirement('management.observation_concurrency','management','requests',(1,16),('management',)),
    RuntimeRequirement('management.refresh_min_interval_ms','management','milliseconds',(100,60000),('management',)),
    RuntimeRequirement('logging.web_window_events','logging_service','events',(1,65536),('logging_service',)),
    RuntimeRequirement('logging.web_query_row_limit','logging_service','rows',(1,128),('logging_service','management')),
    RuntimeRequirement('logging.web_query_max_bytes','logging_service','bytes',(1024,65536),('logging_service',)),
    RuntimeRequirement('logging.web_query_timeout_ms','logging_service','milliseconds',(1,60000),('logging_service',)),
)


def platform_requirements(platform_id: str) -> tuple[RuntimeRequirement, ...]:
    """Expand only a trusted registered platform's finite parameter keys."""
    prefix = 'platforms.'+platform_id+'.buffer.'
    return tuple(RuntimeRequirement(prefix+key,'buffers',unit,bounds,consumers,validator,tuple(prefix+d for d in dependencies))
        for key,unit,bounds,consumers,validator,dependencies in (
        ('history_context_count','messages',(0,128),('buffers',),None,()),
        ('recent_context_count','messages',(0,128),('buffers',),None,()),
        ('target_count','messages',(1,128),('buffers',),'batch_windows',('history_context_count','recent_context_count','normal_soft_limit')),
        ('normal_soft_limit','messages',(1,1000000),('buffers','runtime'),None,()),
        ('explicit_short_enabled',None,None,('buffers','runtime'),None,()),
        ('idle_tail_enabled',None,None,('buffers','runtime'),'batch_triggers',('explicit_short_enabled','idle_timeout_ms')),
        ('idle_timeout_ms','milliseconds',(0,86400000),('buffers','runtime'),None,()),
    ))


def matches_runtime_definition(d: ParameterDefinition, r: RuntimeRequirement, platform: bool, *, sensitivity: str = 'public') -> bool:
    if (d.key != r.key or d.type != ('boolean' if r.limits is None else 'integer')
            or d.owner_module != r.owner or not set(r.consumers) <= set(d.consumers)
            or not d.required or d.nullable or type(d.default) is not NoDefault
            or d.scope != (('platform',) if platform else ('instance',))
            or d.override_policy != 'no_override' or d.sensitivity != sensitivity
            or set(d.read_roles) != {'trusted_operator'} or set(d.write_roles) != {'trusted_operator'}
            or d.apply_mode != ('NEXT_BATCH' if platform else 'INITIALIZE_ONLY')
            or type(d.activation_group) is not NotApplicable or type(d.enum) is not NotApplicable
            or d.deprecated or type(d.replacement) is not NotApplicable or type(d.upgrade_rule) is not NotApplicable):
        return False
    if r.unit is None:
        return type(d.unit) is NotApplicable and type(d.range) is NotApplicable
    if type(d.unit) is not Declared or d.unit.value != r.unit or type(d.range) is not Declared or r.limits is None:
        return False
    return all(type(b) is Bound and type(b.value) is int and b.inclusive and b.value == n
               for b,n in zip((d.range.value.lower,d.range.value.upper),r.limits))
