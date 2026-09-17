"""One bounded native review transition; original requests are never resent."""
import asyncio
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.semantic_records import Record
from companion_memory.provider.values import freeze
from companion_memory.memory.formats import record,sequence
from companion_memory.persistence import Committed,Found,NotFound
from companion_memory.persistence.deadlines import check_deadline
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge
from companion_memory.persistence.owned_statements import OwnerFailure
from .dream_review import PREPARE,DEFER,STORE,PLAN,DISPOSE,RETIRE,FAIL,DISCARD,IMPACT_PREPARE,IMPACT_DEFER,IMPACT_DISPOSE,IMPACT_DISCARD


async def select(owner,run,deadline):
    """Review each actually visited object before moving the fair memory cursor."""
    c=owner.control
    sid=run['active_step_id']
    if sid is None and run['steps_completed']:
        sid=owner.key('dream-step',run['run_id'],run['steps_completed']-1)
    if sid is None:return None
    step=await c.rows.read('steps',sid);check_deadline()
    if step is None:raise InvalidValue()
    values={'run_id':run['run_id'],'expected_revision':run['revision'],'mode_epoch':run['mode_epoch']}
    if step['kind']=='TIME_DECAY':
        limits=owner.configuration.candidate.text.record('dream.resources')
        if run['model_calls_used']+2>=limits['model_calls_per_run'] or not c.dispatch_enabled:return None
        memory=owner.memory.information
        if memory is None:raise InvalidValue()
        records=await memory.current_page('',1) if not run['object_cursor'] else await owner.memory.rows.read('objects_get',{'object_id':run['object_cursor']})
        check_deadline()
        if not records:return None
        current=owner.memory.decode_current(records[0]) if run['object_cursor'] else records[0]
        if current['lifecycle']!='ACTIVE':return None
        arguments=values|{'object_id':current['object_id'],'object_revision':current['revision']}
        try:return await c.execute(PREPARE,owner.key(PREPARE,run['run_id'],current['object_id']),arguments,actor='dream_coordinator')
        except OwnerFailure as failure:
            if not (failure.code in ('PRECONDITION_FAILED','ACCESS_DENIED') and failure.reason in ('SOURCE_CHANGED','REVISION_CONFLICT','OPERATION_NOT_GRANTED') or failure.code=='RESOURCE_BUSY' and failure.reason=='CAPACITY_REACHED'):raise
        except ValueTooLarge:pass
        return await c.execute(DEFER,owner.key(DEFER,run['run_id'],current['object_id']),arguments,actor='dream_coordinator')
    if step['kind']!='DREAM_REVIEW':return None
    work=await owner.rows.read('dream_work',owner.work_id(sid));check_deadline()
    if work is None:
        if step['material_id'] is None and step['state'] in ('DEFERRED_CAPACITY','DEFERRED_CONFLICT'):return None
        raise InvalidValue()
    await cleanup(owner,work,deadline);check_deadline()
    if run['active_step_id'] is None:
        await retire(owner,run,work,values,deadline)
        return None
    return await advance(owner,run,step,work,values,deadline)


async def advance(o,run,step,work,values,deadline):
    c=o.control;provider=o.provider;key=work['request_key'];values=values|{'work_id':work['object_id']}
    dispose=IMPACT_DISPOSE if step['origin_event_id'] is not None else DISPOSE
    discard=IMPACT_DISCARD if step['origin_event_id'] is not None else DISCARD
    if work['state']=='RECOVERY_REQUIRED':return Found(MappingProxyType({'state':'RECOVERY_REQUIRED','new_sends':0}))
    if work['state']=='FAILED' or work['state']=='RESULT_STORED' and (work['decision']!='CHANGE' or work['leaf_count']==0):
        try:return await c.execute(dispose,o.key(dispose,work['object_id']),values,actor='dream_coordinator')
        except OwnerFailure as failure:
            if failure.code!='PRECONDITION_FAILED' or failure.reason not in ('SOURCE_CHANGED','REVISION_CONFLICT'):raise
            return await c.execute(discard,o.key(discard,work['object_id']),values|{'reason':'SOURCE_CHANGED'},actor='dream_coordinator')
    if work['state']=='RESULT_STORED':
        if not c.dispatch_enabled:return Found(MappingProxyType({'state':'PAUSED','new_sends':0}))
        if c.now()>=work['deadline_at_us']:
            return await c.execute(discard,o.key(discard,work['object_id']),values|{'reason':'DEADLINE_EXCEEDED'},actor='dream_coordinator')
        plan_id=step['plan_id']
        try:
            if plan_id is None:
                material=await o.materials.read_manifest(work['result_material_id'],run['run_id']);check_deadline()
                if material is None:raise InvalidValue()
                # The persisted output is complete and immutable. A native read
                # only selects the branch; its UoW rebuilds and verifies it again.
                from companion_memory.persistence.content_codec import decode_content
                stored=await o.materials.borrow(material['object_id'],material['payload_digest'],run['run_id'],deadline)
                try:
                    body=cast(Record,freeze(decode_content(stored.material.body,262144),262144,owned=True))
                    output=record(record(body['terminal'])['output'])
                    memory=any(action['action']!='CREATE_GOAL' for action in cast(tuple[Record,...],output['actions']))
                finally:o.materials.release_reader(stored)
                if memory:return await c.execute(PLAN,o.key(PLAN,work['object_id']),values|{'previous_plan':None},actor='dream_coordinator')
                return await c.execute('apply_dream_candidate_goals',o.key('dream-goals',work['object_id']),values,actor='dream_coordinator')
            plans=await o.memory.rows.read('release_plans_get',{'plan_id':plan_id});check_deadline()
            if len(plans)!=1:raise InvalidValue()
            plan=plans[0]
            return await c.execute(plan['command_kind'],plan['execution_key'],values|{'plan_id':plan_id},actor='dream_coordinator')
        except OwnerFailure as failure:
            if failure.code!='PRECONDITION_FAILED' or failure.reason not in ('REVISION_CONFLICT','SOURCE_CHANGED'):raise
            return await c.execute(discard,o.key(discard,work['object_id']),values|{'reason':'SOURCE_CHANGED'},actor='dream_coordinator')
    original=await provider.read_daily_request(key,'DREAM_REVIEW',run['run_id'],deadline);check_deadline()
    if type(original) is NotFound:
        if not c.dispatch_enabled:return Found(MappingProxyType({'state':'PAUSED','new_sends':0}))
        if c.now()>=work['deadline_at_us']:
            return await c.execute(discard,o.key(discard,work['object_id']),values|{'reason':'DEADLINE_EXCEEDED'},actor='dream_coordinator')
        if provider.network is None:raise InvalidValue()
        provider.network.resume();admission=provider.network.admission_state(deadline)
        if admission!='READY':return Found(MappingProxyType({'state':admission,'new_sends':0}))
        lease=await o.materials.borrow(work['material_id'],work['material_digest'],run['run_id'],deadline);check_deadline()
        request=None;o.sending=(run,work)
        try:
            request=provider.generation_request('DREAM_REVIEW',lease,key,min(work['deadline_at_us'],time.time_ns()//1000+max(0,int((deadline-time.monotonic())*1000000))))
            check_deadline();sent=await provider.send_generation(request)
            await provider.wait_generation_actual(request)
        finally:
            if request is not None:
                await provider.wait_generation_actual(request);provider.release_unused_generation(request)
            o.materials.release_reader(lease);o.sending=None
            await provider.reconcile_daily_network()
        check_deadline();original=await provider.read_daily_request(key,'DREAM_REVIEW',run['run_id'],deadline);check_deadline()
        if type(original) is NotFound:return sent
    if type(original) is not Found:return original
    request=original.value
    if request['phase']=='OPEN':return Found(MappingProxyType({'state':'ORIGINAL_REQUEST_UNCONFIRMED','new_sends':0}))
    values.update(expected_revision=work['bound_revision'],request_id=request['object_id'],request_digest=request['fingerprint'])
    if request['phase']=='REMOTE_RESULT_UNKNOWN' or request['outcome']!='SUCCEEDED':
        return await c.execute(FAIL,o.key(FAIL,work['object_id']),values,actor='dream_coordinator')
    o.receiving=await provider.recover_daily_result(request['object_id'],deadline)
    try:stored=await c.execute(STORE,o.key(STORE,work['object_id']),values,actor='dream_coordinator')
    finally:
        if c._task is not None:await asyncio.wait((c._task,))
        if o.receiving is not None:provider.release_daily_result(o.receiving);o.receiving=None
    if type(stored) is Committed:
        if record(stored.receipt.result)['state']=='FAILED' and provider.trial_authorization is not None:
            await provider.trial_authorization.stop('MODEL_PROTOCOL_REJECTED',request['object_id'])
        check_deadline();result=await provider.cleanup_daily(request['object_id'],stored.receipt,deadline)
        if type(result) is not Found:raise OwnerFailure('RESULT_UNCONFIRMED','cleanup','COMMIT_UNCONFIRMED',True)
    return stored


async def cleanup(o,work,deadline):
    if work['handoff_id'] is None:return
    receipt=await o.control.operations[STORE].read_receipt(o.key(STORE,work['object_id']));check_deadline()
    if type(receipt) is not Found:raise InvalidValue()
    cleaned=await o.provider.cleanup_daily(work['request_id'],receipt.value,deadline);check_deadline()
    if type(cleaned) is not Found:raise OwnerFailure('RESULT_UNCONFIRMED','cleanup','COMMIT_UNCONFIRMED',True)


async def retire(o,run,work,values,deadline):
    for cid in (work['material_id'],work['result_material_id']):
        if cid is None:continue
        root=await o.materials.read_manifest(cid,run['run_id']);check_deadline()
        if root is None or root['state']=='RELEASED':continue
        o.materials.begin_retirement(cid)
        try:
            for offset in range(0,len(root['leaf_refs']),4):
                check_deadline()
                outcome=await o.control.execute(RETIRE,o.key(RETIRE,cid,offset),values|{'work_id':work['object_id'],'context_id':cid,'page_offset':offset},actor='dream_coordinator')
                if type(outcome) is not Committed:raise OwnerFailure('RESULT_UNCONFIRMED','material','COMMIT_UNCONFIRMED',True)
        finally:
            if o.control._task is not None:await asyncio.wait((o.control._task,))
            o.materials.end_retirement(cid)


async def influence(owner,run,deadline):
    """Claim pending work fairly, then enumerate one fixed dependency page."""
    c=owner.control;memory=owner.memory
    if memory.long_term is None or not c.dispatch_enabled or not c.settled(run):return None
    queue=memory.long_term.influence;limits=owner.configuration.candidate.text.record('dream.resources')
    values={'run_id':run['run_id'],'expected_revision':run['revision'],'mode_epoch':run['mode_epoch']}
    if run['edges_used']>=limits['dependency_edges_per_run']:return None
    if run['model_calls_used']+2<limits['model_calls_per_run']:
        work=await queue.pending(run['run_id']);check_deadline()
        if work is not None:
            if await queue.maybe_obsolete(work):
                from companion_memory.dream.influence import SKIP
                return await c.execute(SKIP,owner.key(SKIP,run['run_id'],work['object_id']),values|{'influence_id':work['object_id']},actor='dream_coordinator')
            rows=await memory.rows.read('objects_get',{'object_id':work['affected_object_id']});check_deadline()
            current=memory.decode_current(rows[0]) if rows else None
            arguments=values|{'object_id':work['affected_object_id'],'object_revision':work['observed_revision'] if current is None else current['revision'],'influence_id':work['object_id']}
            try:return await c.execute(IMPACT_PREPARE,owner.key(IMPACT_PREPARE,run['run_id'],work['object_id']),arguments,actor='dream_coordinator')
            except OwnerFailure as failure:
                if not (failure.code in ('PRECONDITION_FAILED','ACCESS_DENIED') and failure.reason in ('SOURCE_CHANGED','REVISION_CONFLICT','OPERATION_NOT_GRANTED') or failure.code=='RESOURCE_BUSY' and failure.reason=='CAPACITY_REACHED'):raise
            except ValueTooLarge:pass
            return await c.execute(IMPACT_DEFER,owner.key(IMPACT_DEFER,run['run_id'],work['object_id']),arguments,actor='dream_coordinator')
    if run['edges_used']+4<=limits['dependency_edges_per_run']:
        event=await queue.next_event(run['impact_cursor']);check_deadline()
        if event is not None:
            from companion_memory.dream.influence import SCAN
            walk=await queue.rows.read('influence_walks',queue.key('influence-walk',event['object_id']));check_deadline()
            return await c.execute(SCAN,owner.key(SCAN,run['run_id'],event['object_id'],0 if walk is None else walk['revision']),
                values|{'event_id':event['object_id'],'event_sequence':event['sequence']},actor='dream_coordinator')
    return None


async def retire_run(owner,run,deadline):
    """A run cannot exit while any completed review material remains owned."""
    after='';values={'run_id':run['run_id'],'expected_revision':run['revision'],'mode_epoch':run['mode_epoch']}
    while rows:=await owner.rows.rows.read('dream_work_run_page',{'run_id':run['run_id'],'after':after}):
        check_deadline()
        for row in rows:
            work=owner.rows.decode('dream_work',row)
            await cleanup(owner,work,deadline);await retire(owner,run,work,values,deadline);check_deadline()
            after=work['object_id']
