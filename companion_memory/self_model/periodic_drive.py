"""Advance one original periodic step while retaining physical Provider work."""
import asyncio
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Committed,Found,NotFound
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope,current_deadline,check_deadline
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue,Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.memory.formats import record
from .periodic_persona import PREPARE,DEFER,GENERATION,REVIEW,PUBLISH,KEEP,FAIL,INVALID,UNKNOWN,RETIRE,DISCARD
from .periodic_output import decode_persona_candidate,decode_persona_review


async def advance(owner):
    """The host calls this bounded worker only under an explicit current run grant."""
    if not owner.bound or owner.closed or owner.job is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',owner.job is not None)
    deadline=current_deadline(10)
    async def run():
        with DeadlineScope(deadline):return await step(owner,deadline)
    actual,logical=start_owned(run());owner.job=actual
    def ended(task):
        if not task.cancelled():task.exception()
        if owner.job is task:owner.job=None
    actual.add_done_callback(ended)
    done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
    return logical.result() if done else Found(MappingProxyType({'state':'PENDING','cleanup_pending':True,'new_sends':0}))


async def step(o,deadline,*,finishing_abort=False):
    control=o.control;root=await control.schedule();check_deadline()
    if root is None or root['active_run_id'] is None:return Found(MappingProxyType({'state':'IDLE','new_sends':0}))
    run=await control.inspect(root['active_run_id']);check_deadline()
    if run is None:raise InvalidValue()
    if run['state']=='DRAINING':return await complete_exit(o,run)
    work=await o.rows.read('periodic_persona_runs',o.work_id(run['run_id']));check_deadline()
    values={'run_id':run['run_id'],'expected_revision':run['revision'],'mode_epoch':run['mode_epoch']}
    if not finishing_abort and await control.abort_request(run['run_id']) is not None:
        from companion_memory.dream.abort import advance_abort
        return await advance_abort(o,run,work,deadline)
    deferred=await o.rows.read('periodic_persona_deferrals',o.key('periodic-deferral',run['run_id']));check_deadline()
    if deferred is not None:return await finish_run(o,run,values,deadline,finishing_abort)
    if work is None:
        if control.dispatch_enabled and control.now()>=run['deadline_at_us'] and control.settled(run):
            return await control.execute(DEFER,o.key(DEFER,run['run_id']),values,actor='dream_coordinator')
        from companion_memory.cognition.dream_drive import select,influence
        reviewed=await select(o.review_work,run,deadline);check_deadline()
        if reviewed is not None:return reviewed
        influenced=await influence(o.review_work,run,deadline);check_deadline()
        if influenced is not None:return influenced
        if not control.dispatch_enabled:return Found(MappingProxyType({'state':'PAUSED','new_sends':0}))
        information=o.memory.information
        if information is None or o.memory.long_term is None:raise InvalidValue()
        limits=control.execution_configuration().text.record('dream.resources')
        if run['objects_used']<limits['objects_per_run']:
            if o.expiry is not None:
                expired=await o.expiry.next_due(run);check_deadline()
                if expired is not None:return await o.expiry.select(expired,run)
            page=await information.current_page(run['object_cursor'],1);check_deadline()
            if page:
                current=page[0];anchor=await o.memory.long_term.rows.read('maintenance_anchors',o.memory.long_term.anchor_id(current['object_id']));check_deadline()
                if anchor is None or control.now is None:raise InvalidValue()
                now=control.now()
                elapsed=o.memory.long_term.elapsed(current,anchor,run['run_id'],now)
                kind='observe_dream_retention' if elapsed.applied_intervals==0 else 'account_dream_time' if elapsed.retention==record(current['scores'])['retention'] else 'decay_dream_memory'
                return await control.execute(kind,o.key('dream-decay',run['run_id'],current['object_id']),values|{'memory_id':current['object_id'],'memory_revision':current['revision'],'observed_at_us':now},actor='dream_coordinator')
        from companion_memory.persistence.schema import ValueTooLarge
        try:return await control.execute(PREPARE,o.key(PREPARE,run['run_id']),values,actor='dream_coordinator')
        except ValueTooLarge:pass
        except OwnerFailure as failure:
            if failure.code!='RESOURCE_BUSY' or failure.reason!='CAPACITY_REACHED':raise
        return await control.execute(DEFER,o.key(DEFER,run['run_id']),values,actor='dream_coordinator')
    if work is None:raise InvalidValue()
    await cleanup_results(o,work,deadline);check_deadline()
    if work['state'] in ('PUBLISHED','KEPT_PREVIOUS'):
        if run['active_step_id'] is not None:
            return await control.execute(KEEP,o.key(KEEP,run['run_id']),values,actor='dream_coordinator')
        for role in ('PERSONA_DREAM','PERSONA_REVIEW'):
            for domain in ('periodic-material','periodic-invalid'):
                cid=o.key(domain,run['run_id'],role);root=await o.materials.read_manifest(cid,run['run_id']);check_deadline()
                if root is None or root['state']=='RELEASED':continue
                o.materials.begin_retirement(cid)
                try:
                    for offset in range(0,len(root['leaf_refs']),4):
                        check_deadline()
                        retired=await control.execute(RETIRE,o.key(RETIRE,cid,offset),values|{'context_id':cid,'page_offset':offset},actor='dream_coordinator')
                        if type(retired) is not Committed:return retired
                finally:
                    if control._task is not None:await asyncio.wait((control._task,))
                    o.materials.end_retirement(cid)
        check_deadline()
        return await finish_run(o,run,values,deadline,finishing_abort)
    if work['state']=='RECOVERY_REQUIRED':return Found(MappingProxyType({'state':'RECOVERY_REQUIRED','new_sends':0}))
    if work['state']=='REVIEWED':
        review=await o.rows.read('periodic_persona_reviews',work['review_id']);check_deadline()
        if review is None:raise InvalidValue()
        kind=PUBLISH if review['decision']=='APPROVE' else KEEP
        if kind==PUBLISH and not control.dispatch_enabled:return Found(MappingProxyType({'state':'PAUSED','new_sends':0}))
        if o.control.now()>=work['deadline_at_us']:kind=DISCARD;values['reason']='DEADLINE_EXCEEDED'
        try:return await control.execute(kind,o.key(kind,run['run_id']),values,actor='dream_coordinator')
        except OwnerFailure as failure:
            if kind!=PUBLISH or failure.code!='PRECONDITION_FAILED' or failure.reason not in ('REVISION_CONFLICT','SOURCE_CHANGED'):raise
            return await control.execute(DISCARD,o.key(DISCARD,run['run_id']),values|{'reason':'SOURCE_CHANGED'},actor='dream_coordinator')
    role='PERSONA_DREAM' if work['state']=='FROZEN' else 'PERSONA_REVIEW'
    prefix='generation' if role=='PERSONA_DREAM' else 'review';key=work[prefix+'_key']
    original=await o.provider.read_daily_request(key,role,run['run_id'],deadline);check_deadline()
    if type(original) is NotFound:
        if not control.dispatch_enabled:return Found(MappingProxyType({'state':'PAUSED','new_sends':0}))
        root=await o.materials.read_manifest(o.key('periodic-material',run['run_id'],role),run['run_id']);check_deadline()
        if root is None:raise InvalidValue()
        if o.control.now()>=work['deadline_at_us']:
            return await control.execute(DISCARD,o.key(DISCARD,run['run_id']),values|{'reason':'DEADLINE_EXCEEDED'},actor='dream_coordinator')
        provider=o.provider
        if provider.network is None:raise InvalidValue()
        # Leave the frozen work untouched while the shared network is occupied
        # or cooling. A later tick still uses the original absolute work limit.
        provider.network.resume();admission=provider.network.admission_state(deadline)
        if admission!='READY':return Found(MappingProxyType({'state':admission,'new_sends':0}))
        lease=await o.materials.borrow(root['object_id'],root['payload_digest'],run['run_id'],deadline);check_deadline()
        request=None;o.sending=(run,work,key,root,role)
        try:
            request=provider.generation_request(role,lease,key,min(work['deadline_at_us'],time.time_ns()//1000+max(0,int((deadline-time.monotonic())*1000000))))
            check_deadline();sent=await provider.send_generation(request)
            await provider.wait_generation_actual(request)
        finally:
            if request is not None:
                await provider.wait_generation_actual(request);provider.release_unused_generation(request)
            o.materials.release_reader(lease);o.sending=None
            await provider.reconcile_daily_network()
        check_deadline()
        original=await provider.read_daily_request(key,role,run['run_id'],deadline);check_deadline()
        if type(original) is NotFound:return sent
    if type(original) is not Found:return original
    request=original.value
    if request['phase']=='OPEN':return Found(MappingProxyType({'state':'ORIGINAL_REQUEST_UNCONFIRMED','new_sends':0}))
    values.update(expected_revision=work[prefix+'_bound_revision'],request_id=request['object_id'],request_digest=request['fingerprint'])
    kind=GENERATION if role=='PERSONA_DREAM' else REVIEW
    if request['phase']=='REMOTE_RESULT_UNKNOWN':
        return await control.execute(UNKNOWN,o.key(UNKNOWN,run['run_id'],role),values|{'role':role},actor='dream_coordinator')
    if request['outcome']!='SUCCEEDED':return await control.execute(FAIL,o.key(FAIL,run['run_id'],role),values|{'role':role},actor='dream_coordinator')
    o.receiving=await o.provider.recover_daily_result(request['object_id'],deadline)
    try:
        terminal=o.receiving
        view=await o.rows.read('persona_self_views',work['view_id']);check_deadline()
        if view is None:raise InvalidValue()
        try:
            raw=encode_content(cast(Value,terminal.value['output']),16384)
            if role=='PERSONA_DREAM':o.validate_candidate(raw,view,run['run_id'],o.control.now())
            else:decode_persona_review(raw)
        except InvalidValue:
            kind=INVALID;values['role']=role
            if o.provider.trial_authorization is not None:await o.provider.trial_authorization.stop('MODEL_PROTOCOL_REJECTED',request['object_id'])
        stored=await control.execute(kind,o.key(kind,run['run_id'],role),values,actor='dream_coordinator')
    finally:
        if control._task is not None:await asyncio.wait((control._task,))
        if o.receiving is not None:o.provider.release_daily_result(o.receiving);o.receiving=None
    if type(stored) is Committed:
        check_deadline()
        cleaned=await o.provider.cleanup_daily(request['object_id'],stored.receipt,deadline)
        if type(cleaned) is not Found:raise OwnerFailure('RESULT_UNCONFIRMED','cleanup','COMMIT_UNCONFIRMED',True)
    return stored


async def cleanup_results(o,work,deadline):
    """Reconfirm existing consumers; never recover a retired output by resending."""
    for prefix,role,kind in (('generation','PERSONA_DREAM',GENERATION),('review','PERSONA_REVIEW',REVIEW)):
        rid=work[prefix+'_request_id']
        if rid is None or work[prefix+'_handoff_id'] is None:continue
        receipt=await o.control.operations[kind].read_receipt(o.key(kind,work['run_id'],role));check_deadline()
        if type(receipt) is not Found:
            receipt=await o.control.operations[INVALID].read_receipt(o.key(INVALID,work['run_id'],role));check_deadline()
        if type(receipt) is not Found:raise InvalidValue()
        outcome=await o.provider.cleanup_daily(rid,receipt.value,deadline);check_deadline()
        if type(outcome) is not Found:raise OwnerFailure('RESULT_UNCONFIRMED','cleanup','COMMIT_UNCONFIRMED',True)


async def finish_run(o,run,values,deadline,finishing_abort=False,*,exit_key=None):
    """Commit exit once, then use its durable identity for local FIFO closure."""
    if run['state']=='DRAINING':return await complete_exit(o,run)
    if o.review_work is not None:
        from companion_memory.cognition.dream_drive import retire_run
        await retire_run(o.review_work,run,deadline);check_deadline()
    kind=('exit_focused_dream' if run['mode']=='FOCUSED' else 'abort_background_dream') if finishing_abort else ('finish_focused_dream' if run['mode']=='FOCUSED' else 'finish_background_dream')
    key=o.key(kind,run['run_id']) if exit_key is None else exit_key
    finished=await o.control.confirm(kind,key);check_deadline()
    if finished is None:finished=await o.control.execute(kind,key,values,actor='dream_coordinator')
    if type(finished) is Committed and run['mode']=='FOCUSED':
        check_deadline();current=await o.control.inspect(run['run_id']);check_deadline()
        if current is None:raise InvalidValue()
        return await complete_exit(o,current)
    return finished


async def complete_exit(o,run):
    """Confirm the persisted exit, transfer a bounded page and finish its run.

    Dispatch can stay revoked throughout recovery. Never rebuild the old exit
    command with the DRAINING revision or treat queue backpressure as failure.
    """
    from companion_memory.memory.formats import record
    control=o.control
    if o.mode is None or run['state']!='DRAINING':raise InvalidValue()
    if not control.settled(run):return Found(MappingProxyType({'state':'RECOVERY_REQUIRED','new_sends':0}))
    exit=await control.rows.read('exits',o.key('dream-exit',run['run_id']));check_deadline()
    if exit is None or exit['outcome']!=run['exit_result']:raise InvalidValue()
    original=record(exit['original_operation'])
    confirmed=await control.confirm(original['operation_kind'],original['operation_key']);check_deadline()
    if type(confirmed) is not Committed:
        raise OwnerFailure('RESULT_UNCONFIRMED','exit','COMMIT_UNCONFIRMED',True)
    mode=await o.mode.synchronize();check_deadline()
    if mode['state']=='DRAINING':return Found(MappingProxyType({'state':'DRAINING','new_sends':0}))
    key=o.key('abort-drained' if original['operation_key']==o.key('abort-finish',run['run_id']) else 'complete_dream_exit',run['run_id'])
    completed=await control.confirm('complete_dream_exit',key);check_deadline()
    if completed is not None:return completed
    return await control.execute('complete_dream_exit',key,{'run_id':run['run_id'],
        'expected_revision':run['revision'],'mode_epoch':run['mode_epoch']},actor='dream_coordinator')
