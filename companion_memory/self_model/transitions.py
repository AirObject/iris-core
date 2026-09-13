"""Deterministic first-persona review and generation transitions.

These functions validate complete retained records and return proposed versions.
They grant no management permission and do not commit. The owning command must
check live mode, cleanup, budget and original receipts within its native scope,
then stage these versions and every required owner audit in one transaction.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import Value,InvalidValue,freeze_value
from companion_memory.persistence.text_records import ID,UINT,OPERATION,isolate_record,digest
from companion_memory.memory.formats import record
from companion_memory.memory.initial_self import isolate_initial_self
from .formats import isolate_run,isolate_candidate,candidate_digest
from .request_material import PersonaRequestMaterial


@dataclass(frozen=True,slots=True)
class ReviewedPersona:
    """Both review writes must be staged together under the same original key."""
    run: MappingProxyType[str,Value]
    candidate: MappingProxyType[str,Value]


def _operation(value: object,kind: str,instance: Value):
    operation=isolate_record(OPERATION,value,1024)
    if (operation['owner_namespace'],operation['operation_kind'],operation['scope_id'])!=('self_model',kind,instance):raise InvalidValue()
    return operation


def matching_generation(run: MappingProxyType[str,Value],candidate: MappingProxyType[str,Value]) -> None:
    """Reject old-generation and cross-database results before any review or retry."""
    if any(run[name]!=candidate[name] for name in ('database_id','instance_id','config_snapshot_id','generation','provider_operation_key','provider_request_id','input_id','input_digest','binding_digest')):
        raise InvalidValue()
    if candidate['run_id']!=run['object_id'] or run['resolution_id']!=candidate['object_id']:raise InvalidValue()


def review(run: object,candidate: object,expected_revision: int,candidate_revision: int,expected_digest: str,
           decision: str,actor: str,operation: object,now_us: int) -> ReviewedPersona:
    """Only pending successful text may be reviewed once; text identity is immutable."""
    current=isolate_run(run);proposal=isolate_candidate(candidate)
    freeze_value(ID,actor);freeze_value(UINT,now_us)
    key=_operation(operation,'review_initial_persona',current['instance_id'])
    matching_generation(current,proposal)
    if (current['state']!='WAITING_REVIEW' or current['revision']!=expected_revision or proposal['revision']!=candidate_revision
            or proposal['review']!='PENDING' or proposal['resolution']!='SUCCEEDED' or candidate_digest(proposal)!=expected_digest
            or decision not in ('APPROVE','REJECT') or now_us<cast(int,current['updated_at_us']) or now_us<cast(int,proposal['created_at_us'])):raise InvalidValue()
    reviewed=isolate_candidate({**proposal,'revision':candidate_revision+1,'review':'APPROVED' if decision=='APPROVE' else 'REJECTED',
        'reviewed_by':actor,'reviewed_at_us':now_us,'review_operation':key})
    if candidate_digest(reviewed)!=expected_digest:raise InvalidValue()
    updated=isolate_run({**current,'revision':expected_revision+1,'state':'APPROVED' if decision=='APPROVE' else 'USER_REJECTED','last_operation':key,'updated_at_us':now_us})
    return ReviewedPersona(updated,reviewed)


def next_generation(run: object,prior_candidate: object,initial: object,expected_revision: int,expected_generation: int,
                    prior_resolution_id: str,mode_epoch: int,operation: object,now_us: int,material: PersonaRequestMaterial) -> MappingProxyType[str,Value]:
    """Create exactly the next original key; previous candidates remain untouched.

    Mode, original receipt, money and actual cleanup checks belong to the native
    management coordinator. Passing those checks cannot override these CAS and
    three-generation bounds or change the retained input and configuration.
    """
    current=isolate_run(run);prior=isolate_candidate(prior_candidate);source=isolate_initial_self(initial)
    matching_generation(current,prior)
    key=_operation(operation,'retry_initial_persona',current['instance_id'])
    if (current['revision']!=expected_revision or current['generation']!=expected_generation or not 1<=expected_generation<=2
            or current['state'] not in ('KNOWN_FAILED','USER_REJECTED') or current['publication_id'] is not None
            or current['resolution_id']!=prior_resolution_id or source['object_id']!=current['input_id'] or source['input_digest']!=current['input_digest']
            or current['state']=='KNOWN_FAILED' and prior['resolution'] not in ('KNOWN_FAILED','NOT_SENT')
            or current['state']=='USER_REJECTED' and prior['review']!='REJECTED'
            or now_us<cast(int,current['updated_at_us']) or mode_epoch<=cast(int,current['mode_epoch']) or type(material) is not PersonaRequestMaterial):raise InvalidValue()
    request=material.request;payload=record(request['payload'])
    if request['run_id']!=current['object_id'] or request['operation_key']==current['provider_operation_key'] or payload['context_digest']!=material.context_digest:
        raise InvalidValue()
    if material.binding['config_snapshot_id']!=current['config_snapshot_id']:raise InvalidValue()
    return isolate_run({**current,'revision':expected_revision+1,'generation':expected_generation+1,'state':'PREPARED','provider_operation_key':request['operation_key'],
        'provider_request_id':None,'resolution_id':None,'mode_epoch':mode_epoch,'binding_digest':digest(material.binding),'last_operation':key,'updated_at_us':now_us})
