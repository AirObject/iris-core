"""Frozen daily request and exact original-account settlement transformations.

Every change is still applied only by the native Provider's atomic command. A
complete known result and an unknown bill remain independent observations.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING,cast
from companion_memory.persistence.semantic_records import Record as StoredRecord,identity
from companion_memory.cognition.daily_material_storage import DailyMaterialReadLease
from .values import Record,as_record,freeze
from .ledger import Mutation
from .daily_protocol import DailyChatBinding
from .daily_stored_schema import OWNERS,DREAM_OWNERS
from .dream_protocol import DreamChatBinding
from .text_accounting import normalize,liability
from .chat_protocol import UsageObservation
if TYPE_CHECKING:
    from .daily_service import DailyProvider
    from companion_memory.media.daily_image import DailyImageLease
    from companion_memory.configuration.execution_versions import ExecutionVersion
    from .chat_transport import ChatTransport

@dataclass(frozen=True,slots=True,init=False)
class DailyRequest:
    owner:DailyProvider
    description:StoredRecord
    request_id:str
    attempt_id:str
    fingerprint:str
    wire:bytes
    material:DailyMaterialReadLease|DailyImageLease
    batch_id:str|None
    binding:DailyChatBinding|DreamChatBinding
    account:Record
    profile:Record
    execution_version:ExecutionVersion|None
    transport:ChatTransport


def row(value:object) -> Record:
    return as_record(freeze(value,8192,owned=True))


def usage(observation:UsageObservation,request:DailyRequest,*,not_sent:bool=False) -> Record:
    if request.account['billing_mode']=='USAGE_ONLY_TRIAL':
        from .daily_usage import normalize as normalize_daily
        return normalize_daily(observation,request.account,request.profile,not_sent=not_sent)
    reserved,_=liability(request.account,request.profile)
    result=normalize(observation,request.account,request.profile,reserved,not_sent=not_sent)
    bound=as_record(request.account['price'])['per_attempt_money_bound']
    if bound is not None and not result['cost_complete']:
        result=row(dict(result)|{'held_atoms':max(reserved,cast(int,bound))})
    return result


def reservation_amount(request:DailyRequest) -> int:
    if request.account['billing_mode']=='USAGE_ONLY_TRIAL':return 0
    amount,_=liability(request.account,request.profile)
    return max(amount,cast(int,as_record(request.account['price'])['per_attempt_money_bound']) or 0) if as_record(request.account['price'])['per_attempt_money_bound'] is not None else amount


def registration(request:DailyRequest,budget:Record,now:str) -> tuple[Mutation,...]:
    from .deepseek_protocol import observe_usage as deepseek
    from .minimax_protocol import observe_usage as minimax
    observe_usage=minimax if request.binding.requested_model=='MiniMax-M3' else deepseek
    account=request.account;profile=request.profile;role=request.binding.role;description=request.description
    config=cast(StoredRecord,description['config']);amount=reservation_amount(request);model_usage=usage(observe_usage(None),request)
    owners=DREAM_OWNERS if request.owner.ledger.assembly.dream_format else OWNERS
    version=request.owner.ledger.assembly.version
    req=row({'object_id':request.request_id,'revision':1,'caller_module':owners[role],'caller_scope':config['instance_id'],'extension_id':None,
        'operation_key':description['original_request_key'],'capability':profile['capability'],'task_role':role,'result_owner':owners[role],
        'profile_id':profile['profile_id'],'account_id':account['account_id'],'created_at':now,'updated_at':now,'format_version':version,'fingerprint_version':version,
        'attribution':{'run_id':description['work_id'],'entry_ids':description['entry_ids'],'parent_request_id':None,'trace_id':None,
            'batch_id':request.batch_id,'dream_run_id':description['work_id'] if type(request.binding) is DreamChatBinding else None,'prompt_revision':request.binding.prompt_digest},
        'source':'REMOTE_PROVIDER','configuration_origin':'PERSISTED_CONFIGURATION','config_snapshot_id':config['snapshot_id'],
        'profile_revision':request.execution_version.version_id if request.execution_version is not None else identity('daily-profile',config['snapshot_id'],cast(str,profile['profile_id'])),
        'price_revision':None if account['price'] is None else as_record(account['price'])['revision_ref'],'execution_evidence':{'profile':profile,'account':account,
            'request_timeout_ms':60000,'retry_delay_ms':0,
            'request_max_bytes':2097152 if role=='MEDIA' else 1048576,'result_max_bytes':40960},
        'fingerprint':request.fingerprint,'phase':'OPEN','outcome':None,'first_error':None,'attempt_count':1,'ever_unknown':False,'handoff_id':None})
    attempt=row({'object_id':request.attempt_id,'revision':1,'request_id':request.request_id,'ordinal':1,'state':'PREPARED','logical_outcome':None,
        'account_id':account['account_id'],'profile_id':profile['profile_id'],'capability':profile['capability'],'wire_protocol':profile['wire_protocol'],
        'execution_owner_id':identity('daily-executor',request.request_id),'created_at':now,'updated_at':now,'adapter_duration_ms':None,
        'handoff_id':None,'confirmed_started':None,'ever_unknown':False,'first_error':None,'terminal_error':None,'usage':model_usage,
        'result_fingerprint':None,'evidence_revision':0})
    reserved=row({'object_id':identity('daily-reservation',request.attempt_id),'revision':1,'attempt_id':request.attempt_id,
        'account_id':account['account_id'],'budget_id':budget['object_id'],'reserved_atoms':amount,'known_subtotal_atoms':0,'held_atoms':amount,
        'known_cost_atoms':None,'cost_complete':False,'format_version':version,'quota_reserved':0,'quota_known':None if account['billing_mode']=='USAGE_ONLY_TRIAL' else 0,'quota_held':0,'billing_mode':account['billing_mode']})
    advanced=row(dict(budget)|{'revision':cast(int,budget['revision'])+1,'attempt_count':cast(int,budget['attempt_count'])+1,
        'held_atoms':cast(int,budget['held_atoms'])+amount})
    return (Mutation('requests',None,req),Mutation('attempts',None,attempt),Mutation('budget_windows',budget,advanced),Mutation('reservations',None,reserved))


def settlement(budget:Record,reservation:Record,metering:Record,attempt_id:str) -> tuple[Mutation,...]:
    advanced=row(dict(budget)|{'revision':cast(int,budget['revision'])+1,
        'known_subtotal_atoms':cast(int,budget['known_subtotal_atoms'])+cast(int,metering['known_subtotal_atoms']),
        'held_atoms':cast(int,budget['held_atoms'])-cast(int,reservation['held_atoms'])+cast(int,metering['held_atoms'])})
    reserved=row(dict(reservation)|{'revision':cast(int,reservation['revision'])+1,
        **{k:metering[k] for k in ('known_cost_atoms','known_subtotal_atoms','held_atoms','cost_complete')}})
    costs=[]
    for part in cast(tuple[Record,...],metering['items']):
        cost=part['cost_atoms'];complete=cost is not None
        costs.append(Mutation('cost_items',None,row({'object_id':identity('daily-cost',attempt_id,cast(str,part['item'])),'revision':1,
            'attempt_id':attempt_id,'item':part['item'],'cost_atoms':cost,'evidence_revision':1,'source':'LOCALLY_ESTIMATED' if complete else 'UNAVAILABLE',
            'unit':'TOKEN','known_subtotal_atoms':cost if complete else 0,'cost_complete':complete,'format_version':reservation['format_version'],
            'billing_mode':metering['billing_mode'],**{k:part[k] for k in ('quantity','price_numerator','price_denominator')}})))
    return (Mutation('budget_windows',budget,advanced),Mutation('reservations',reservation,reserved),*costs)
