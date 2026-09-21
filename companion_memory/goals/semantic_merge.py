"""Owner-local semantic merge guards and atomic attribution changes.

The original five-second task must already be terminal for deterministic work.
The comparison is frozen independently. A later revision or delivery race never
refreshes that comparison and never changes an already sent reminder fact.
"""
from hashlib import sha256
from types import MappingProxyType
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.record_primitives import Record,integer,text
from .service import GoalsService,GoalTransaction

STRUCTURE=('entry_id','subject_ids','world_scope','deadline','reminder_lead_seconds','route_id')

def digest(goal:Record) -> str:
    """Hash the complete original root, including its exact revision and text."""
    return sha256(encode_content(goal,4096)).hexdigest()


def merge_conflict(owner:GoalsService,tx:GoalTransaction,task:Record,goal:Record,canonical:Record,
                   original_task:Record,original_goal:Record,original_canonical:Record) -> bool:
    """Every conservative condition is evaluated before the first merge write."""
    if (task!=original_task or goal!=original_goal or canonical!=original_canonical
            or task['status']!='NEEDS_SEMANTIC_REVIEW' or goal['canonical_id']!=goal['goal_id']
            or canonical['canonical_id']!=canonical['goal_id'] or goal['status']!='OPEN' or canonical['status']!='OPEN'
            or any(goal[name]!=canonical[name] for name in STRUCTURE)
            or (integer(canonical['created_at']),text(canonical['goal_id'])) >= (integer(goal['created_at']),text(goal['goal_id']))
            or goal['alias_count']!=0 or integer(canonical['alias_count'])>=64):
        return True
    for root in (goal,canonical):
        if owner._records.rows.stage('daily_reminder_risk',tx.uow,{'goal_id':root['goal_id']})[0]['risk']:
            return True
    sources=tx.children('source',{'goal_id':goal['goal_id']})
    existing={text(s['source_id']):s for s in tx.children('source',{'goal_id':canonical['goal_id']})}
    if len(existing)!=canonical['source_count'] or len(sources)!=goal['source_count']:
        raise OwnerFailure('STORAGE_FAILED','goal','INTEGRITY_FAILURE')
    for source in sources:
        duplicate=existing.get(text(source['source_id']))
        if duplicate is not None and any(source[n]!=duplicate[n] for n in ('basis_id','origin')):
            return True
    return len(existing)+sum(s['source_id'] not in existing for s in sources)>8


def apply_merge(owner:GoalsService,uow:UnitOfWork,now:int,task:Record,goal:Record,canonical:Record,
                original_task:Record,original_goal:Record,original_canonical:Record) -> tuple[Record,...]|None:
    """Return actual changed roots or a conflict without any merge side effect.

    This module is part of the goals owner. It uses its private transaction
    adapter, never another owner's state or an alternate writer lease.
    """
    tx=GoalTransaction(owner,uow,now)
    if merge_conflict(owner,tx,task,goal,canonical,original_task,original_goal,original_canonical):
        return None
    sources=tx.children('source',{'goal_id':goal['goal_id']})
    existing={text(s['source_id']):s for s in tx.children('source',{'goal_id':canonical['goal_id']})}
    additions=tuple(s for s in sources if s['source_id'] not in existing)
    for source in additions:
        tx.write('source',dict(source)|{'goal_id':canonical['goal_id']})
    tx.write('alias',{'alias_id':goal['goal_id'],'canonical_id':canonical['goal_id'],'revision':1,'created_at':now})
    kept=tx.update('goal',canonical,source_count=len(existing)+len(additions),alias_count=integer(canonical['alias_count'])+1,updated_at=now)
    merged=tx.update('goal',goal,canonical_id=canonical['goal_id'],dedup_state='SEMANTIC_MERGED',updated_at=now)
    tx.cancel_plans(goal['goal_id'])
    terminal=tx.update('dedup_task',task,status='SEMANTIC_MERGED')
    return kept,merged,terminal
