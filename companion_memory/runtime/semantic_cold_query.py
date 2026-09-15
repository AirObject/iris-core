"""Explicit controlled cold-query authority and one retained actual owner.

Ordinary trial activation grants only prewarming. This additional native grant
names exact QUERY slots and one permission partition, and rejects real service
activation. Caller deadlines bound waiting, never the actual owner's lifetime.
"""
from __future__ import annotations
import asyncio
from contextvars import Context
from collections.abc import Callable
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.persistence import Committed,Receipt
from companion_memory.persistence.deadlines import DeadlineScope,check_deadline
from companion_memory.persistence.semantic_records import Record,identity,string,number
from companion_memory.persistence.owned_statements import OwnerFailure
from .semantic_authorization import SemanticActivation
from .semantic_results import LocalFailure
if TYPE_CHECKING:
    from .semantic_host import SemanticHost


@dataclass(frozen=True,slots=True,init=False)
class ControlledColdGrant:
    """Verified exact activation, slot names and permission partition."""
    activation: SemanticActivation
    partition: str
    queries: tuple[Record,...]


class ControlledColdAuthority:
    """Trusted fixture authority verifies the complete additional use binding."""
    def __init__(self,verify:Callable[[Record],bool]):
        self.verify=verify

    def grant(self,activation:SemanticActivation,partition:str,query_ids:tuple[str,...]) -> ControlledColdGrant:
        if (type(activation) is not SemanticActivation or activation.binding['execution'] not in ('CONTROLLED','SIMULATED')
                or type(partition) is not str or not partition or type(query_ids) is not tuple
                or not 1<=len(query_ids)<=6 or len(set(query_ids))!=len(query_ids)):
            raise ValueError('Exact controlled cold-query authority required.')
        available=activation.binding['queries'];assert type(available) is tuple
        queries=tuple(q for q in available if type(q) is MappingProxyType and q['query_id'] in query_ids)
        if len(queries)!=len(query_ids):raise ValueError('Unknown original QUERY slot.')
        binding=MappingProxyType({'purpose':'CONTROLLED_COLD_QUERY','activation_digest':activation.digest,
            'partition_id':partition,'query_ids':query_ids,'queries':queries})
        if not self.verify(binding):raise ValueError('Cold-query use was not granted.')
        grant=object.__new__(ControlledColdGrant)
        for name,value in dict(activation=activation,partition=partition,queries=queries).items():object.__setattr__(grant,name,value)
        return grant


@dataclass(frozen=True,slots=True)
class ColdResult:
    artifact: Record|None
    reason: str|None


class ControlledColdQueries:
    """No send queue, one original key owner, and no implicit prewarming reuse."""
    def __init__(self,host:SemanticHost,grant:ControlledColdGrant):
        if type(grant) is not ControlledColdGrant or host.authorization is None or grant.activation is not host.authorization.grant:
            raise ValueError('Native matching cold-query grant required.')
        self.host=host;self.grant=grant
        self._task:asyncio.Task[Record|LocalFailure]|None=None
        self._key:str|None=None;self._remote_deadline=0.0

    @property
    def pending(self) -> bool:
        return self._task is not None and not self._task.done()

    async def obtain(self,text:str,partition:str,deadline:float) -> ColdResult:
        """Spend at most 250ms waiting, reserving the caller's 200ms tail."""
        if partition!=self.grant.partition:return ColdResult(None,'QUERY_VECTOR_MISSING')
        query=next((q for q in self.grant.queries if q['text']==text),None)
        if query is None:return ColdResult(None,'QUERY_VECTOR_MISSING')
        settings=self.host.configuration.text.record('retrieval.query_vectors')
        tail=number(settings['local_tail_ms'])/1000;remote=number(settings['cold_remote_ms'])/1000
        now=time.monotonic();latest=deadline-tail
        if latest<=now:return ColdResult(None,'DEADLINE')
        key=identity('semantic-cold-query',self.grant.activation.digest,partition,query['query_id'])
        if self.pending and key!=self._key:return ColdResult(None,'RESOURCE_BUSY')
        if self._task is None or self._key!=key:
            provider=self.host.embedding;assert provider is not None
            if provider._active is not None or provider._pending is not None or provider._failure is not None:
                return ColdResult(None,'RESOURCE_BUSY')
            self._key=key;self._remote_deadline=min(latest,now+remote)
            # The actual host owner is not a descendant of either query waiter.
            # It may finish local reception after both immutable replies return.
            self._task=asyncio.create_task(self._advance(text,key,query,self._remote_deadline),context=Context())
            self._task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        task=self._task;assert task is not None
        done,_=await asyncio.wait((task,),timeout=max(0,min(latest,self._remote_deadline)-time.monotonic()))
        if not done:return ColdResult(None,'DEADLINE')
        try:result=task.result()
        except OwnerFailure as error:
            return ColdResult(None,'BUDGET_PAUSED' if error.field=='budget' else 'RESOURCE_BUSY')
        if type(result) is not MappingProxyType:
            provider=self.host.embedding;assert provider is not None
            budget=await provider.ledger.get('budget_windows',identity('embedding-budget',cast(str,provider.account['account_id']),cast(str,provider.account['window_id'])))
            return ColdResult(None,'PROVIDER_UNKNOWN' if budget is not None and budget['held_atoms'] else 'RESOURCE_BUSY')
        if result['state']=='REMOTE_UNKNOWN':return ColdResult(None,'PROVIDER_UNKNOWN')
        if result['state']=='KNOWN_FAILED':return ColdResult(None,'PROVIDER_KNOWN_FAILURE')
        cache=self.host.combination.cache;assert cache is not None
        artifact=await cache.reusable(text,partition)
        return ColdResult(artifact,None if artifact is not None else 'QUERY_VECTOR_MISSING')

    async def _advance(self,text:str,key:str,query:Record,deadline:float) -> Record|LocalFailure:
        port=self.host.semantic;assert port is not None
        with DeadlineScope(deadline):
            work=await port.prepare_query(text,key,self.grant.partition)
            if not isinstance(work,str):return work
            assert type(work) is str
            check_deadline()
        deadline_at=time.time_ns()//1000+max(0,int((deadline-time.monotonic())*1000000))
        slot=identity('semantic-slot',self.grant.activation.binding['package_id'],'QUERY',query['query_id'])
        result=await port.run_work(work,slot_id=slot,_request_deadline_at=deadline_at,_admission_deadline=deadline)
        provider=self.host.embedding;assert provider is not None
        if provider._active is not None and not provider._active.done():
            active=provider._active
            await asyncio.wait((active,))
            terminal=active.result()
            if type(terminal) in (Committed,Receipt):
                # One original local continuation receives the late paid result.
                result=await port.run_work(work)
        if type(result) is not MappingProxyType:
            current=await port.work(work)
            if current['state'] in ('PREPARED','BOUND') and current['intent'] is not None:
                request=provider.request(current,await port.owner.text(current),number(current['deadline_at']))
                root=await provider.ledger.get('requests',request.request_id)
                if root is not None and root['phase'] in ('TERMINAL','REMOTE_RESULT_UNKNOWN'):
                    result=await port.run_work(work)
        return result
