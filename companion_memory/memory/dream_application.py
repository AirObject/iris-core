"""Memory's fixed original release plans for independently grounded dream effects."""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.daily_records import identity
from companion_memory.persistence.schema import InvalidValue,freeze_value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.cognition.dream_candidates import DreamCandidate
from .formats import record,sequence
from .transactions import ApplyScope
from .release_plans import PLAN,CheckedRelease,observe_change_set_release,read_plan,digest


def branch(candidate:DreamCandidate,leaves:tuple) -> str:
    """Derive the exact business writers; the caller cannot supply an owner mask."""
    kinds={cast(str,leaf['action']) for leaf in candidate.leaves}
    memory=bool(kinds-{'CREATE_GOAL'})
    history=bool(kinds & {'SET_SCORES','REPLACE_CURRENT'})
    ingress=any(leaf['payloads'] for leaf in leaves)
    media=any(leaf['has_media'] for leaf in leaves)
    return 'apply_dream_candidate'+('_memory' if memory else '')+('_history' if history else '')+('_ingress' if ingress else '')+('_media' if media else '')+('_goals' if 'CREATE_GOAL' in kinds else '')


def publish_plan(memory,uow:UnitOfWork,candidate:DreamCandidate,*,previous_id=None,definitions=()):
    """A replan confirms the old immutable execution absent before replacing it."""
    if type(candidate) is not DreamCandidate or candidate.origin!='DREAM':raise InvalidValue()
    changes=tuple(leaf for leaf in candidate.leaves if leaf['action']!='CREATE_GOAL')
    if not changes:raise InvalidValue()
    leaves=observe_change_set_release(memory,uow,changes)
    config=memory.configuration;root=identity('dream-application',config.database_id,config.scope_id,candidate.candidate_id)
    roots=memory.rows.stage('release_roots_get',uow,{'root_id':root})
    ordinal=1
    if roots:
        current=roots[0]
        if current['semantic_digest']!=candidate.digest or current['successful_plan'] is not None or previous_id is None:raise InvalidValue()
        previous,_=read_plan(memory,uow,previous_id)
        if previous['root_id']!=root or previous['ordinal']!=current['last_ordinal']:raise InvalidValue()
        definition=next((d for d in definitions if d.operation_kind==previous['command_kind']),None)
        if definition is None:raise InvalidValue()
        if memory.storage.confirm_prior_operation(uow,definition,cast(str,previous['execution_key'])) is not None:
            raise OwnerFailure('PRECONDITION_FAILED','source','ALREADY_COMMITTED')
        ordinal=cast(int,current['last_ordinal'])+1
    elif previous_id is not None:raise InvalidValue()
    mask=('_'.join(part for part,exists in (('INGRESS',any(leaf['payloads'] for leaf in leaves)),('MEDIA',any(leaf['has_media'] for leaf in leaves))) if exists) or 'NONE')
    checksums=tuple(digest(leaf,8192) for leaf in leaves)
    pid=identity('dream-release',config.database_id,config.scope_id,root,ordinal,checksums)
    kind=branch(candidate,leaves)
    plan=record(freeze_value(PLAN,{'plan_version':1,'plan_id':pid,'root_id':root,'ordinal':ordinal,'semantic_digest':candidate.digest,
        'mask':mask,'command_kind':kind,'execution_key':identity('dream-apply',config.database_id,config.scope_id,pid),
        'previous_plan':previous_id,'leaf_digests':checksums}))
    if roots:
        if len(memory.rows.stage('root_advance',uow,{'root_id':root,'ordinal':ordinal,'previous_ordinal':ordinal-1}))!=1:raise InvalidValue()
    else:memory.rows.stage('release_roots_insert',uow,{'root_id':root,'semantic_digest':candidate.digest,'last_ordinal':ordinal,'successful_plan':None})
    memory.rows.stage('release_plans_insert',uow,{'plan_id':pid,'root_id':root,'ordinal':ordinal,'execution_key':plan['execution_key'],
        'command_kind':kind,'body':encode_content(plan,4096).decode()})
    for index,leaf in enumerate(leaves):memory.rows.stage('release_leaves_insert',uow,{'plan_id':pid,'ordinal':index,'body':encode_content(leaf,8192).decode()})
    return plan


def apply(memory,goals,uow:UnitOfWork,candidate:DreamCandidate,scope:ApplyScope,entry_id:str,routes:tuple[str,...],now:int,plan_id:str|None,kind:str,*,impact=None):
    """Reuse actual formal owners, source closure and history in the caller's UoW."""
    if type(candidate) is not DreamCandidate or candidate.origin!='DREAM' or scope.candidate_id!=candidate.candidate_id or scope.batch_id is not None:raise InvalidValue()
    changes=tuple(leaf for leaf in candidate.leaves if leaf['action']!='CREATE_GOAL')
    applied=None;release=None;plan=None
    if changes:
        if plan_id is None:raise InvalidValue()
        plan,leaves=read_plan(memory,uow,plan_id)
        roots=memory.rows.stage('release_roots_get',uow,{'root_id':plan['root_id']})
        operation=memory.storage.cognition_operation_context(uow,memory.repository_definition())
        if (not roots or roots[0]['successful_plan'] is not None or roots[0]['last_ordinal']!=plan['ordinal']
                or plan['semantic_digest']!=candidate.digest or kind!=plan['command_kind'] or kind!=branch(candidate,leaves)
                or operation.operation_key!=plan['execution_key']):raise InvalidValue()
        if observe_change_set_release(memory,uow,changes)!=leaves:raise OwnerFailure('PRECONDITION_FAILED','source','OWNERSHIP_CHANGED')
        release=CheckedRelease(memory,memory.sources,leaves)
        proof=None
        if impact is not None:
            if memory.long_term is None:raise InvalidValue()
            proof=memory.long_term.influence.score_proof(uow,impact,changes)
        try:applied=memory.apply_change_set(uow,scope,changes,None,release,now,candidate.candidate_id,'MAINTENANCE',cause_root=candidate.step_id if impact is None else impact['cause_root'],retention_basis=proof)
        finally:
            if proof is not None:memory.long_term._retention_basis=None
        if len(memory.rows.stage('root_success',uow,{'root_id':plan['root_id'],'plan_id':plan_id,'ordinal':plan['ordinal']}))!=1:raise InvalidValue()
    elif plan_id is not None or kind!=branch(candidate,()):raise InvalidValue()
    from companion_memory.goals.service import GoalAuthority
    effects=[]
    for leaf in candidate.leaves:
        if leaf['action']!='CREATE_GOAL':continue
        goal=record(leaf['proposed_value'])
        if any(sid not in scope.readable_subjects for sid in sequence(goal['subject_ids'])):raise InvalidValue()
        def verify_basis(transaction,oid):
            if transaction is not uow or oid not in scope.readable_objects|scope.writable_objects:return False
            current=memory.current(uow,oid)
            return current is not None and current['lifecycle']=='ACTIVE'
        authority=GoalAuthority(routes,candidate.candidate_id,verify_basis,entry_id)
        effect=goals.apply('goal_inject_internal',uow,dict(goal)|{'source_id':candidate.candidate_id},
            identity('dream-goal-effect',memory.configuration.database_id,memory.instance_id,candidate.candidate_id,leaf['target_id']),
            now,authority,trusted_goal_id=cast(str,leaf['target_id']))
        effects.append(effect.summary)
    return applied,release,tuple(effects)
