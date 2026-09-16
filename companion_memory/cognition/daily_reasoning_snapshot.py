"""Original complete reasoning evidence for deterministic candidate conversion."""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.schema import InvalidValue,Value
from companion_memory.persistence.content_codec import decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.values import freeze
from companion_memory.memory.formats import record,sequence

Record=MappingProxyType[str,Value]

def checked_snapshot(run,turn,initial,result,tools):
    if (turn['run_id']!=run['object_id'] or turn['object_id']!=run['active_turn_id'] or turn['phase']!='RESULT_STORED'
            or turn['result_kind'] not in ('FINAL','FAILED','SENSITIVE') or run['context_digest']!=initial.manifest['payload_digest']
            or turn['result_digest']!=result.manifest['payload_digest']):raise InvalidValue()
    normalized=cast(Record,freeze(decode_content(result.body,40960),40960,owned=True))
    return run,turn,initial.body,record(normalized['output']) if turn['result_kind']=='FINAL' else None,tuple(tools)

async def read_snapshot(owner,run_id:str,deadline:float):
    run=await owner.rows.read('reasoning_runs',run_id)
    if run is None:raise InvalidValue()
    turn=await owner.rows.read('reasoning_turns',cast(str,run['active_turn_id']))
    if turn is None:raise InvalidValue()
    async def material(mid,checksum):
        lease=await owner.materials.borrow(mid,checksum,run_id,deadline)
        try:return owner.materials.verify_lease(lease)
        finally:owner.materials.release_reader(lease)
    initial=await material(run['context_id'],run['context_digest']);result=await material(turn['result_ref'],turn['result_digest']);tools=[]
    for ordinal in range(cast(int,run['tool_count'])):
        step=await owner.rows.read('reasoning_tools',owner.key('reasoning-tool',run_id,ordinal))
        if step is None or step['run_id']!=run_id or step['state'] not in ('RESULT_STORED','FAILED'):raise InvalidValue()
        value=await material(step['result_ref'],step['result_digest'])
        tools.append(cast(Record,freeze(decode_content(value.body,8192),8192,owned=True)))
    return checked_snapshot(run,turn,initial,result,tools)

def participate_snapshot(owner,uow:UnitOfWork,run_id:str):
    run=owner.required('reasoning_runs',uow,run_id);turn=owner.required('reasoning_turns',uow,cast(str,run['active_turn_id']))
    owner._admit(uow,run,fresh=False)
    def material(mid,checksum):return owner.materials.participate_material(uow,mid,checksum,run_id)
    initial=material(run['context_id'],run['context_digest']);result=material(turn['result_ref'],turn['result_digest']);tools=[]
    for ordinal in range(cast(int,run['tool_count'])):
        step=owner.required('reasoning_tools',uow,owner.key('reasoning-tool',run_id,ordinal))
        if step['run_id']!=run_id or step['state'] not in ('RESULT_STORED','FAILED'):raise InvalidValue()
        value=material(step['result_ref'],step['result_digest']);tools.append(cast(Record,freeze(decode_content(value.body,8192),8192,owned=True)))
        definition=next(d for d in owner.commands if d.operation_kind=='store_reasoning_tool')
        proof=owner.storage.confirm_prior_operation(uow,definition,owner.key('reasoning-tool-store',step['object_id']))
        if proof is None:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
    kind='store_reasoning_result' if turn['handoff_id'] is not None else 'finish_reasoning_turn'
    key=owner.key('reasoning-result' if turn['handoff_id'] is not None else 'reasoning-failure',turn['object_id'])
    proof=owner.storage.confirm_prior_operation(uow,next(d for d in owner.commands if d.operation_kind==kind),key)
    if proof is None:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
    return checked_snapshot(run,turn,initial,result,tools)
