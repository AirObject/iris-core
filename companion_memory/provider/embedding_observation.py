"""Read-only embedding ledger observations with the original bounded API.

One native instance and its single configured account define visibility. Usage
is aggregated by SQLite in one snapshot. Nullable metering stays nullable and
neither a remote result nor physical retirement is a claim of a supplier bill.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timezone
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.owned_statements import OwnerFailure
from .embedding_service import EmbeddingProvider
from .normalization import keys
from .values import Found,NotFound,as_record,freeze,is_identifier,dump,InvalidData


@dataclass(frozen=True,slots=True,init=False)
class EmbeddingObserver:
    """Issued native observer, with no request, activation or result capability."""
    owner:EmbeddingProvider

    def __init__(self):raise TypeError('Embedding observation is issued by the host.')

    def ready(self) -> None:
        if self.owner._closed or not self.owner._initialized:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')

    async def get_request(self,request_id:object):
        self.ready()
        if not is_identifier(request_id):raise InvalidData()
        visible=await self.owner.ledger.read('requests_visible',{'object_id':request_id,'scopes':dump((self.owner.instance,)),
            'caller_module':None,'extension_id':None})
        if not visible:return NotFound()
        request=visible[0]
        attempts=await self.owner.ledger.read('attempts_for_request',{'request_id':request_id})
        return Found(MappingProxyType({'request':request,'attempts':attempts,'handoff_present':request['handoff_id'] is not None}))

    async def get_budget_state(self):
        self.ready()
        budgets=await self.owner.ledger.read('budget_windows_page',{'after':'','limit':4})
        return Found(tuple(MappingProxyType(dict(b)|{'available_atoms':None if self.owner.usage_only else cast(int,self.owner.account['cost_limit_atoms'])-cast(int,b['known_subtotal_atoms'])-cast(int,b['held_atoms'])})
            for b in budgets if b['account_id']==self.owner.account['account_id']))

    async def query_usage(self,raw:object):
        self.ready();query=as_record(freeze(raw,8192))
        if not keys(query,{'start','end','caller_scope','capability','task_role','profile_id','account_id','group_by'}):raise InvalidData()
        if query['group_by'] not in ('NONE','CAPABILITY','TASK_ROLE','PROFILE','ACCOUNT'):raise InvalidData()
        times={}
        for name in ('start','end'):
            value=query[name]
            if type(value) is not str or len(value)>40:raise InvalidData()
            parsed=datetime.fromisoformat(value)
            if parsed.tzinfo is not timezone.utc:raise InvalidData()
            times[name]=parsed
        if times['start']>=times['end']:raise InvalidData()
        for name in ('caller_scope','capability','task_role','profile_id','account_id'):
            if query[name] is not None and not is_identifier(query[name]):raise InvalidData()
        if query['caller_scope'] not in (None,self.owner.instance):raise OwnerFailure('ACCESS_DENIED','capability','OPERATION_NOT_GRANTED')
        if query['account_id'] not in (None,self.owner.account['account_id']):raise OwnerFailure('ACCESS_DENIED','capability','OPERATION_NOT_GRANTED')
        if query['capability'] not in (None,'EMBEDDING') or query['task_role'] not in (None,'EMBEDDING_DOCUMENT','EMBEDDING_QUERY'):raise InvalidData()
        parameters:dict[str,object]={n:query[n] for n in ('capability','task_role','profile_id','account_id','group_by')}
        parameters.update({n:v.isoformat(timespec='microseconds') for n,v in times.items()})
        parameters.update(scopes=dump((self.owner.instance,)),limit=17)
        rows=await self.owner.ledger.read('usage_aggregate',parameters)
        if len(rows)>16 or any(r['invalid_count']!=0 or any(type(v) is not int or v<0 for n,v in r.items() if n!='group_id') for r in rows):
            raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        return Found(MappingProxyType({'range':query,'as_of':self.owner._now(),'sample_count':sum(cast(int,r['request_count']) for r in rows),
            'source':'SIMULATED' if self.owner.simulated else 'REMOTE_PROVIDER','rows':rows}))


def observer(owner:EmbeddingProvider) -> EmbeddingObserver:
    """Trusted host binds only its already initialized native Provider owner."""
    if type(owner) is not EmbeddingProvider or not owner._initialized:raise ValueError('Native initialized embedding owner required.')
    port=object.__new__(EmbeddingObserver);object.__setattr__(port,'owner',owner);return port
