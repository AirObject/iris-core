"""Advance a durable abort without sending, publishing or cancelling actual work."""
from types import MappingProxyType
from companion_memory.persistence import Found,NotFound
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.deadlines import check_deadline


async def advance_abort(owner,run,persona,deadline):
    control=owner.control;review=owner.review_work
    values={'run_id':run['run_id'],'expected_revision':run['revision'],'mode_epoch':run['mode_epoch']}
    if run['remote_result']=='UNKNOWN' or run['local_confirmation']!='CONFIRMED' or run['state']=='RECOVERY_REQUIRED':
        return Found(MappingProxyType({'state':'RECOVERY_REQUIRED','new_sends':0}))
    if run['active_step_id'] is not None:
        step=await control.rows.read('steps',run['active_step_id']);check_deadline()
        if step is None:raise InvalidValue()
        if step['kind']=='DREAM_REVIEW':
            from companion_memory.cognition.dream_drive import advance,cleanup
            from companion_memory.cognition.dream_review import DISCARD,IMPACT_DISCARD
            if review is None:raise InvalidValue()
            work=await review.rows.read('dream_work',review.work_id(step['object_id']));check_deadline()
            if work is None:raise InvalidValue()
            await cleanup(review,work,deadline);check_deadline()
            if work['state']=='FROZEN':
                original=await owner.provider.read_daily_request(work['request_key'],'DREAM_REVIEW',run['run_id'],deadline);check_deadline()
                if type(original) is not NotFound:return await advance(review,run,step,work,values,deadline)
            kind=IMPACT_DISCARD if step['origin_event_id'] is not None else DISCARD
            return await control.execute(kind,owner.key('abort-review',step['object_id']),values|{'work_id':work['object_id'],'reason':'ABORTED_SAFELY'},actor='dream_coordinator')
        if persona is None:raise InvalidValue()
        from companion_memory.self_model.periodic_drive import cleanup_results,step as advance_persona
        from companion_memory.self_model.periodic_persona import DISCARD,KEEP
        await cleanup_results(owner,persona,deadline);check_deadline()
        if persona['state']=='KEPT_PREVIOUS':
            return await control.execute(KEEP,owner.key(KEEP,run['run_id']),values,actor='dream_coordinator')
        for prefix,role in (('generation','PERSONA_DREAM'),('review','PERSONA_REVIEW')):
            if persona[prefix+'_request_id'] is not None:continue
            original=await owner.provider.read_daily_request(persona[prefix+'_key'],role,run['run_id'],deadline);check_deadline()
            if type(original) is not NotFound:return await advance_persona(owner,deadline,finishing_abort=True)
        return await control.execute(DISCARD,owner.key('abort-persona',run['run_id']),values|{'reason':'ABORTED_SAFELY'},actor='dream_coordinator')
    if persona is not None:
        from companion_memory.self_model.periodic_drive import step
        return await step(owner,deadline,finishing_abort=True)
    from companion_memory.self_model.periodic_drive import finish_run
    return await finish_run(owner,run,values,deadline,True,exit_key=owner.key('abort-finish',run['run_id']))
