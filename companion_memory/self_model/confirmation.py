"""Native confirmation of an absent persona's actual self-model receipt.

The original receipt and required audits are read by the storage owner. Its
fingerprint is reconstructed from the exact absent branch and actual CAS target;
no nonexistent Provider completion key is requested or manufactured.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING,cast
from weakref import WeakSet
from companion_memory.persistence import Found,Receipt,RecoveryHandle,ResultBoundCommand
from companion_memory.persistence.schema import InvalidValue
from companion_memory.memory.formats import record,sequence
from .formats import isolate_candidate,candidate_digest
if TYPE_CHECKING:
    from .transactions import PersonaTransactions


@dataclass(frozen=True,slots=True,weakref_slot=True,eq=False,init=False)
class ConfirmedAbsentResolution:
    """Issuer-bound confirmed original receipt and immutable candidate identity."""
    _owner: PersonaTransactions
    receipt: Receipt
    digest: str
    def __init__(self):raise TypeError('Resolution confirmation requires actual original storage evidence.')


_issued:WeakSet[ConfirmedAbsentResolution]=WeakSet()


def confirmed_absent(owner: PersonaTransactions,confirmation: object,candidate: object) -> bool:
    """Recheck issuer identity and the complete immutable resolution at commit."""
    return (type(confirmation) is ConfirmedAbsentResolution and confirmation in _issued
        and confirmation._owner is owner and confirmation.digest==candidate_digest(isolate_candidate(candidate)))


async def confirm_absent(owner: PersonaTransactions,candidate: object) -> ConfirmedAbsentResolution:
    """Read actual result-bound receipt plus audit and verify original command bytes."""
    proposal=isolate_candidate(candidate);key=record(proposal['terminal_receipt'])
    if (proposal['resolution']!='NOT_SENT' or proposal['provider_request_id'] is not None
            or proposal['instance_id']!=owner.assembly.instance_id):raise InvalidValue()
    definition=owner.definitions['record_initial_persona_resolution']
    operation=owner.assembly.storage.bind_operation(definition,owner.assembly.instance_id)
    found=await operation.read_receipt(cast(str,key['operation_key']))
    if type(found) is not Found:raise InvalidValue()
    receipt=found.value;body=record(receipt.result);targets=tuple(record(item) for item in sequence(body['targets']))
    candidate_targets=tuple(item for item in targets if item['object_id']==proposal['object_id'])
    run_targets=tuple(item for item in targets if item['object_id']==proposal['run_id'])
    if (body['state']!='KNOWN_FAILED' or len(targets)!=2 or len(candidate_targets)!=1 or len(run_targets)!=1
            or candidate_targets[0]!={'object_id':proposal['object_id'],'previous_revision':None,'revision':1}):raise InvalidValue()
    previous=run_targets[0]['previous_revision']
    if type(previous) is not int or previous<1 or run_targets[0]['revision']!=previous+1:raise InvalidValue()
    values={'operation_id':key['operation_key'],'run_id':proposal['run_id'],'expected_revision':previous,'generation':proposal['generation'],
        'provider_reference':proposal['provider_operation_key'],'evidence_revision':0}
    command=ResultBoundCommand(definition.command_version,values,{r.event_slot:{'actor':owner.actor} for r in definition.required_audits})
    handle=operation.recovery_handle(cast(str,key['operation_key']),command)
    if (type(handle) is not RecoveryHandle or receipt.identity!=handle.identity or receipt.command_version!=handle.command_version
            or receipt.fingerprint_version!=handle.fingerprint_version or receipt.fingerprint!=handle.fingerprint):raise InvalidValue()
    evidence=object.__new__(ConfirmedAbsentResolution)
    for name,value in (('_owner',owner),('receipt',receipt),('digest',candidate_digest(proposal))):object.__setattr__(evidence,name,value)
    _issued.add(evidence)
    return evidence
