"""Explicit management partitions expose progress, never historical/model bodies."""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Found
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.semantic_records import Record
from companion_memory.persistence.deadlines import current_deadline,check_deadline


async def observe(host,scope:str,after:str):
    control=host.combination.dream;periodic=host.combination.periodic
    if control is None or periodic is None or host.provider is None or control.now is None:raise InvalidValue()
    now=control.now();root=await control.schedule();check_deadline()
    run=None if root is None or root['last_run_id'] is None else await control.inspect(root['last_run_id'])
    if scope=='dream':
        if after:raise InvalidValue()
        keys=('run_id','revision','state','mode','mode_epoch','deadline_at_us','active_step_id','objects_used','edges_used','model_calls_used',
            'steps_completed','steps_deferred','remaining_work','coverage','end_reason','exit_result','local_confirmation','remote_result','cleanup_pending')
        limits=control.configuration.candidate.text.record('dream.resources')
        health=host.provider.get_health()
        return MappingProxyType({'current':None if run is None else MappingProxyType({k:run[k] for k in keys}),
            'dispatch_enabled':control.dispatch_enabled,'scheduler':MappingProxyType({'enabled':host.dream_scheduler.enabled,
                'actual_running':host.dream_scheduler.task is not None and not host.dream_scheduler.task.done(),'failure':host.dream_scheduler.failure}),'actual_cleanup_pending':control._task is not None or periodic.job is not None or health.cleanup_pending,
            'unknown_requests':health.unknown_observations,'budgets':MappingProxyType({k:limits[k] for k in ('objects_per_run','dependency_edges_per_run','model_calls_per_run')}),
            'mode':None if host.runtime is None else host.runtime.gate.state})
    if scope=='maintenance':
        memory=host.assembly.memory.long_term
        if memory is None:raise InvalidValue()
        page=await memory.rows.page('maintenance_anchors',after);check_deadline()
        items=[]
        for value in page:
            items.append(MappingProxyType({**{k:value[k] for k in ('memory_id','memory_revision','accounted_until','last_used_at','lifecycle','last_decay_run','run_intervals')},
                'unsettled_intervals':max(0,now-value['accounted_until'])//86400000000,'clock_regressed':now<value['accounted_until']}))
        return MappingProxyType({'items':tuple(items),'next_after':page[-1]['object_id'] if len(page)==4 else None,'observed_at_us':now})
    if scope=='persona':
        if after:raise InvalidValue()
        current=await periodic.current.read_current(current_deadline(2));check_deadline()
        value=None if type(current) is not Found else MappingProxyType({k:current.value[k] for k in ('publication_id','revision','generated_at_us','review','publication_origin','stale')})
        work=None if run is None else await periodic.rows.read('periodic_persona_runs',periodic.work_id(run['run_id']));check_deadline()
        keys=('run_id','state','created_at_us','updated_at_us','deadline_at_us','local_confirmation','remote_result','cleanup_pending')
        return MappingProxyType({'current':value,'work':None if work is None else MappingProxyType({k:work[k] for k in keys})})
    raise InvalidValue()
