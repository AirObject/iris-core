"""Explicit capacity disposition preserves the previous persona and scan progress."""
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.dream_view import eligible_self_page,scan_self
from companion_memory.dream.records import validate_run
from companion_memory.dream.results import result,target
from .periodic_material import freeze_periodic


def collect(owner,uow,run):
    pointer=owner.rows.get('current_persona',uow,owner.current.pointer_id)
    if pointer is None:raise InvalidValue()
    previous=owner.current.participate_current(uow,pointer['publication_id'],pointer['revision'])
    if previous is None:raise InvalidValue()
    refs,selected,watermark,cursor,excluded=eligible_self_page(owner.memory,uow,cast(str,run['self_cursor']))
    policy=owner.control.execution_configuration().text.record('self_model.initial_persona')
    body={'previous_persona':previous,'evidence':selected,'generation_goal':policy['generation_goal'],
        'supervision_prompt':policy['supervision_prompt'],'coverage':{'watermark':watermark,'complete':False,'next_after':cursor,'excluded':excluded}}
    return pointer,refs,watermark,cursor,body


def defer(owner,uow,v,run,now,operation):
    control=owner.control;control.require_dispatch(uow,run['run_id'],run['revision'],run['mode_epoch'])
    if not control.settled(run):raise OwnerFailure('RESOURCE_BUSY','run','RUN_ACTIVE')
    reason='DEADLINE_EXCEEDED' if now>=run['deadline_at_us'] else 'CAPACITY_REACHED'
    if reason=='CAPACITY_REACHED':
        try:
            _,_,_,_,body=collect(owner,uow,run)
            freeze_periodic(owner,'PERSONA_DREAM',run,body,operation,now)
        except ValueTooLarge:pass
        except OwnerFailure as failure:
            if failure.code!='RESOURCE_BUSY' or failure.reason!='CAPACITY_REACHED':raise
        else:raise InvalidValue()
    pointer=owner.rows.get('current_persona',uow,owner.current.pointer_id)
    if pointer is None:raise InvalidValue()
    refs,watermark,cursor=scan_self(owner.memory,uow,run['self_cursor'])
    sid=owner.key('dream-step',run['run_id'],run['steps_completed']);key=owner.key('periodic-deferral',run['run_id'])
    saved=owner.rows.write('periodic_persona_deferrals',uow,owner.base(key,now)|{'run_id':run['run_id'],'step_id':sid,
        'publication_id':pointer['publication_id'],'pointer_revision':pointer['revision'],'basis_refs':refs,'watermark':watermark,
        'next_after':cursor,'reason':reason,'original_operation':operation})
    control.rows.write('steps',uow,control._base(sid,now)|{'run_id':run['run_id'],'ordinal':run['steps_completed'],'kind':'PERSONA_GENERATION',
        'state':'DEFERRED_CAPACITY','mode_epoch':run['mode_epoch'],'permit_digest':sha256(encode_content(v,8192)).hexdigest(),
        'origin_event_id':None,'cause_root':None,'object_ref':None,'accounted_from':None,'accounted_through':None,
        'material_id':None,'material_digest':None,'candidate_id':None,'request_key':None,'request_id':None,'handoff_id':None,
        'execution_key':v['operation_id'],'plan_id':None,'local_confirmation':'CONFIRMED','remote_result':'NONE','cleanup_pending':False,
        'deadline_at_us':run['deadline_at_us'],'result_digest':None,'original_operation':operation,'last_operation':operation})
    information=owner.memory.information
    if information is None:raise InvalidValue()
    cursors={} if information.has_after(uow,run['object_cursor']) else {'object_cursor':''}
    if owner.expiry is not None:cursors.update(owner.expiry.exhausted_cursor(uow,run,now))
    control.rows.write('runs',uow,validate_run(dict(run)|cursors|{'revision':run['revision']+1,'updated_at_us':now,
        'steps_completed':run['steps_completed']+1,'steps_deferred':run['steps_deferred']+1,'self_cursor':cursor,
        'remaining_work':True,'coverage':'PARTIAL','end_reason':'WORK_DEFERRED' if reason=='CAPACITY_REACHED' else 'DEADLINE_EXCEEDED','last_operation':operation}),run['revision'])
    counts=owner.storage.transaction_row_changes(uow)
    return MappingProxyType(dict(result(v['operation_id'],'DEFERRED_CAPACITY',{
        'self_model':{'rows_changed':counts['self_model'],'targets':(target(key,1),)},
        'dream':{'rows_changed':counts['dream'],'targets':(target(sid,1),target(run['run_id'],run['revision']+1,run['revision']))}}))|
        {'records':(owner.record_proof(saved),)})
