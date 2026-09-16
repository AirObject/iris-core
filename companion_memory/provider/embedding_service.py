"""Single-owner embedding execution on the original durable Provider ledger.

Only a retained absence proof permits first registration. Registered work is
observed under its original key, even when its adapter never saw a byte. Full
results remain owned until their atomic handoff commits; callers cannot supply
vectors to the completion command or turn confirmation into physical cleanup.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass,replace
from datetime import datetime,timezone
from hashlib import sha256
import threading
import time
from types import MappingProxyType
from typing import cast
from companion_memory.configuration import PresentValue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration
from .daily_network import DailyNetwork,NetworkPermit
from companion_memory.persistence import Committed,Found,Staged,Receipt,ResultBoundCommandDefinition,ResultBoundCommand,UnitOfWork,Value,NotCommitted,Unconfirmed,Rejected,Failed
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope,check_deadline
from companion_memory.persistence.owned_statements import StatementCatalog,BoundStatements,OwnerFailure
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_records import Record,identity,isolate,number,string,INTENT,RECEIPT
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue
from companion_memory.ingress.events import plain
from .ledger import LedgerBinding,Mutation,LedgerFailure
from .values import as_record,freeze,dump,Record as ModelRecord,InvalidData
from .embedding_protocol import request_bytes,parse_response,EmbeddingUsage
from .embedding_material import HandoffMaterial,split_handoff,restore_handoff
from .embedding_repository import TABLES
from .embedding_usage import usage
from .embedding_allocated_usage import observe as allocated_usage
from .chat_transport import ChatTransport,WireObservation
from .resources import CancellationSource


@dataclass(frozen=True,slots=True,init=False)
class EmbeddingRequest:
    """Provider-issued exact request identity, retaining only one complete input."""
    owner: EmbeddingProvider
    description: Record
    fingerprint: str
    request_id: str
    attempt_id: str
    wire: bytes


@dataclass(frozen=True,slots=True,init=False)
class EmbeddingAbsence:
    """A consumable in-process absence observation, never sending authority alone."""
    owner: EmbeddingProvider
    request: EmbeddingRequest


@dataclass(frozen=True,slots=True,init=False)
class EmbeddingResult:
    """Retained complete Provider result with its original atomic completion."""
    owner: EmbeddingProvider
    request: ModelRecord
    attempt: ModelRecord
    handoff: ModelRecord
    completion: Receipt
    value: ModelRecord


@dataclass(slots=True)
class _Completion:
    request: EmbeddingRequest
    material: HandoffMaterial
    changes: tuple[Mutation,...]
    evidence: Receipt|None
    evidence_changes:tuple[Mutation,...]


class EmbeddingProvider:
    """Native embedding owner, with zero send queue and explicit first-send gate."""
    def __init__(self,ledger: LedgerBinding,configuration: StoredSemanticConfiguration | StoredDailyConfiguration,instance: str,
                 definitions: tuple[ResultBoundCommandDefinition,...],transport: ChatTransport|None,
                 checkpoint: Callable[[],None],permit: Callable[[Record,Record],bool],
                 dispatch:Callable[[Record,Record,Callable[[],asyncio.Future[WireObservation]]],asyncio.Future[WireObservation]]|None=None,*,network:DailyNetwork|None=None):
        if (type(ledger) is not LedgerBinding or not ledger.assembly.embedding_format or ledger.semantic_configuration is not configuration
                or transport is not None and (type(transport) is not ChatTransport or transport._format!='EMBEDDING')):
            raise ValueError('Native embedding ledger and transport bindings are required.')
        if (type(configuration) is StoredDailyConfiguration)!=(type(network) is DailyNetwork) or ledger.assembly.daily_format!=(network is not None):raise ValueError('Daily Provider requires its one native network owner.')
        self._unknown_requests:set[str]=set()
        self.network=network;self._network_permit:NetworkPermit|None=None;self._network_work:str|None=None
        self.ledger=ledger;self.configuration=configuration;self.instance=instance;self.checkpoint=checkpoint;self.permit=permit
        self.transport=transport;self.storage=ledger.storage
        self.dispatch=dispatch
        self.simulated=configuration.candidate.text.record('retrieval.embedding')['qualification_profile']=='OFFLINE_CAPACITY'
        if self.simulated!=(transport is None):raise ValueError('Transport and complete configuration tier differ.')
        values={entry.definition.key:entry.state.value for entry in configuration.candidate.foundation.list_entries() if type(entry.state) is PresentValue}
        self.profiles=tuple(as_record(v) for v in cast(tuple,values['provider.profiles']))
        account_id=next(p['account_id'] for p in self.profiles if p['profile_id']==configuration.candidate.text.record('retrieval.embedding')['document_profile'])
        self.account=next(as_record(a) for a in cast(tuple,values['provider.accounts']) if as_record(a)['account_id']==account_id) if network is not None else as_record(cast(tuple,values['provider.accounts'])[0])
        self.usage_only=self.account.get('billing_mode')=='USAGE_ONLY_TRIAL'
        self.version=5 if network is not None else 4 if self.usage_only else 3
        self.billing_mode='USAGE_ONLY_TRIAL' if self.usage_only else 'SIMULATED' if self.simulated else 'TOKEN_METERED'
        if self.usage_only!=ledger.assembly.embedding_usage_only:raise ValueError('Native metering format differs.')
        self.space=string(configuration.candidate.text.record('retrieval.semantic')['space_id'])
        self.catalog=StatementCatalog(ledger.assembly.repository.definition,ledger.assembly.repository.statements)
        self.rows=SemanticRecords(self.catalog,TABLES,self.storage,'provider')
        self.definitions={d.operation_kind:d for d in definitions if d.owner_namespace=='provider'}
        self.reception=next(d for d in definitions if d.owner_namespace=='retrieval' and d.operation_kind=='record_result')
        self._retrieval: BoundStatements|None=None
        self._failure:tuple[Mutation,...]|None=None
        self._read_views=BoundStatements(self.catalog,self.storage,instance)
        if set(self.definitions)!={'store_embedding_handoff','confirm_embedding_handoff','retire_embedding_handoff'}:raise ValueError('Complete embedding commands are required.')
        self.operations={kind:self.storage.bind_operation(d,'provider') for kind,d in self.definitions.items()}
        self._lock=threading.RLock();self._absence: EmbeddingAbsence|None=None
        self._active: asyncio.Task[object]|None=None;self._pending: _Completion|None=None
        self._active_work: str|None=None
        self._results: dict[int,EmbeddingResult]={};self._reading=0;self._retiring:set[str]=set()
        self._closed=False;self._executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='embedding-provider') if network is None else None
        self._cancel=CancellationSource();self._initialized=False;self.executions=0
        if not ledger.acquire(self):
            if self._executor is not None:self._executor.shutdown(wait=False)
            raise ValueError('Provider owner is already occupied.')

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec='microseconds')

    def request(self,work: Record,text: str,deadline_at: int) -> EmbeddingRequest:
        """Freeze an exact prepared work description before its durable bind."""
        if self._closed or work['kind']!='EMBED' or work['space_id']!=self.space:raise ValueError('Original embedding work is required.')
        config=work['config'];assert type(config) is MappingProxyType
        if config!={'database_id':self.configuration.database_id,'instance_id':self.instance,'snapshot_id':self.configuration.snapshot_id}:raise ValueError('Configuration binding differs.')
        if sha256(text.encode()).hexdigest()!=work['material_digest']:raise ValueError('Original text differs.')
        profile_id=self.configuration.candidate.text.record('retrieval.embedding')['document_profile' if work['purpose']=='DOCUMENT' else 'query_profile']
        payload=MappingProxyType({'texts':(text,),'purpose':work['purpose'],'dimensions':1024})
        description=MappingProxyType({'v':1,'config':config,'work_id':work['work_id'],'original_request_key':work['original_request_key'],
            'profile_id':profile_id,'space_id':self.space,'payload':payload,'deadline_at':deadline_at})
        wire=encode_content(payload,65536) if self.simulated else request_bytes(as_record(freeze(payload,16384,owned=True)))
        fingerprint=sha256(encode_content(description,65536)).hexdigest()
        result=object.__new__(EmbeddingRequest)
        rid=identity('embedding-request',self.instance,work['original_request_key'])
        for name,value in dict(owner=self,description=description,fingerprint=fingerprint,request_id=rid,
                attempt_id=identity('embedding-attempt',rid,1),wire=wire).items():object.__setattr__(result,name,value)
        return result

    async def initialize(self) -> None:
        """Verify existing bounded ledger roots; never activate or send work."""
        if self._initialized:return
        after=''
        while page:=await self.ledger.read('requests_page',{'after':after,'limit':8}):
            for request in page:
                self.checkpoint()
                if request['caller_scope']!=self.instance:raise ValueError('Foreign request in isolated semantic instance.')
                attempts=await self.ledger.read('attempts_for_request',{'request_id':request['object_id']})
                self.checkpoint()
                if len(attempts)!=request['attempt_count']:raise ValueError('Incomplete original attempt set.')
                if request['phase']=='TERMINAL' and (not attempts or attempts[0]['logical_outcome']!=request['outcome']):raise ValueError('Terminal disagreement.')
                if request['phase']=='OPEN' and attempts and attempts[0]['state']=='PREPARED':
                    await self._mark_unknown(request,attempts[0])
                if request['phase']=='REMOTE_RESULT_UNKNOWN':
                    self._unknown_requests.add(cast(str,request['object_id']))
                    if self.network is not None:self.network.block_account(cast(str,request['account_id']))
                after=cast(str,request['object_id'])
        self._initialized=True

    async def verify_unsent(self,request: EmbeddingRequest) -> EmbeddingAbsence|None:
        """Inspect only the original key; a registered request gets no new proof."""
        if type(request) is not EmbeddingRequest or request.owner is not self:raise ValueError('Native request required.')
        with self._lock:
            if self._closed or self._active is not None or self._absence is not None:return None
            seal=object.__new__(EmbeddingAbsence);object.__setattr__(seal,'owner',self);object.__setattr__(seal,'request',request)
            self._absence=seal
        checked=False
        try:
            existing=await self.ledger.get('requests',request.request_id)
            if existing is not None:
                if existing['fingerprint']!=request.fingerprint:raise ValueError('Original request key conflicts.')
                return None
            checked=True
            return seal
        finally:
            if not checked:
                with self._lock:
                    if self._absence is seal:self._absence=None

    def release_absence(self,seal:EmbeddingAbsence) -> None:
        """Release an unused native read proof; this never changes registration."""
        with self._lock:
            if type(seal) is not EmbeddingAbsence or seal.owner is not self:raise InvalidValue()
            if self._absence is seal:self._absence=None

    async def send_verified_first(self,request: EmbeddingRequest,seal: EmbeddingAbsence,intent: Record,*,admission_deadline:float|None=None) -> object:
        """Consume exact absence under one owner lock; never replay registration.

        An optional monotonic admission deadline caps first registration and
        sending. The actual owner retains already-started reception afterwards.
        """
        intent=isolate(INTENT,intent)
        with self._lock:
            if (type(request) is not EmbeddingRequest or request.owner is not self or type(seal) is not EmbeddingAbsence
                    or self._absence is not seal or seal.request is not request or self._active is not None or self._closed):
                raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
            self._absence=None
            self.checkpoint()
            if not self.permit(request.description,intent) or intent['request_digest']!=request.fingerprint:raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
            deadline=min(number(request.description['deadline_at']),number(intent['expires_at']))
            remaining=(deadline-time.time_ns()//1000)/1000000
            if admission_deadline is not None:remaining=min(remaining,admission_deadline-time.monotonic())
            if not 0<remaining<=60:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
            self._absence=None
            absolute=time.monotonic()+remaining
            if admission_deadline is not None:absolute=min(absolute,admission_deadline)
            if self.network is not None:
                self._network_permit=self.network.reserve(request.request_id,string(request.description['original_request_key']),cast(str,self.account['account_id']),absolute,consumer_required=True)
                self._network_work=string(request.description['work_id'])
            task,logical=start_owned(self._execute_admitted(request,intent,absolute))
            self._active=task;self._active_work=string(request.description['work_id'])
        def ended(done: asyncio.Task[object]) -> None:
            if not done.cancelled():done.exception()
            with self._lock:
                if self._active is done:self._active=None;self._active_work=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=remaining)
        return logical.result() if done else MappingProxyType({'state':'PENDING','cleanup_pending':True})

    async def _execute_admitted(self,request:EmbeddingRequest,intent:Record,deadline:float) -> object:
        from companion_memory.persistence.completion import CompletionScope
        with CompletionScope() as actual:
            try:return await self._execute_first(request,intent,deadline)
            finally:
                await actual.wait()
                if self.network is not None:
                    try:await self.reconcile_network()
                    except (LedgerFailure,OwnerFailure,InvalidData):pass

    async def reconcile_network(self) -> None:
        """Confirm only the retained request after its actual storage tail ended."""
        permit=self._network_permit
        if self.network is None or permit is None:return
        request=await self.ledger.get('requests',permit.request_id)
        if request is None:
            self.network.cancel_unregistered(permit);self._network_permit=None;self._network_work=None
        elif request['phase'] in ('TERMINAL','REMOTE_RESULT_UNKNOWN'):
            if request['phase']=='REMOTE_RESULT_UNKNOWN':self.network.block_account(permit.account_id)
            self.network.confirm_terminal(permit)
            if request['outcome']=='SUCCEEDED':
                handoff=await self.ledger.get('handoffs',cast(str,request['handoff_id']))
                if handoff is None or as_record(handoff['embedding_cleanup'])['state']!='RETIRED':return
            if self._pending is not None or self._failure is not None or self._reading or self._results:return
            self.network.confirm_consumed(permit);self._network_permit=None;self._network_work=None

    async def _commit(self,kind: str,key: str,changes: tuple[Mutation,...],request_id: str|None,attempt_id: str|None,
                      previous_state: str,state: str,complete: bool) -> Receipt:
        try:
            result,_=await self.ledger.mutate(kind,key,changes,'embedding_provider',request_id,attempt_id,previous_state,state,complete)
        except LedgerFailure as failure:
            raise LedgerFailure(self._retained_outcome(failure.result)) from None
        except OwnerFailure as failure:
            raise OwnerFailure(failure.code,failure.field,failure.reason,
                failure.cleanup_pending or self._pending is not None or self._failure is not None) from None
        if type(result) is not Committed:raise LedgerFailure(self._retained_outcome(result))
        return result.receipt

    async def before_first_registration(self,request) -> None:
        """Optional native outer-purpose accounting; legacy hosts have none."""

    async def after_first_registration(self,request,receipt:Receipt) -> None:
        """Observe the original registration only; never creates another attempt."""

    def _retained_outcome(self,result:object) -> object:
        """Merge retained response ownership without changing confirmation or cause."""
        if (self._pending is not None or self._failure is not None) and isinstance(result,(NotCommitted,Unconfirmed,Rejected,Failed)) and result.error is not None:
            return replace(result,error=replace(result.error,cleanup_pending=True))
        return result

    def _usage(self,*,reserved:int,rate:int,price_revision:str|None,reported:EmbeddingUsage|None=None,
               simulated:bool=False,simulated_reported:bool=False,not_sent:bool=False) -> Record:
        """Use the frozen account branch; null money is never an estimate."""
        return allocated_usage() if self.usage_only else usage(reserved=reserved,rate=rate,price_revision=price_revision,reported=reported,
            simulated=simulated,simulated_reported=simulated_reported,not_sent=not_sent)

    async def _check_allocated_stop(self) -> None:
        """A zero monetary hold cannot bypass an open, unknown or failed request."""
        if self.usage_only and await self.ledger.read('requests_blocked',{'account_id':self.account['account_id']}):
            raise OwnerFailure('RESOURCE_BUSY','account','ORIGINAL_RESULT_UNCONFIRMED')

    async def check_budget(self,work:Record) -> None:
        """Observe current whole-account liability before reserving a new slot."""
        profile_id=self.configuration.candidate.text.record('retrieval.embedding')['document_profile' if work['purpose']=='DOCUMENT' else 'query_profile']
        profile=next(p for p in self.profiles if p['profile_id']==profile_id)
        from .token_costs import rounded_cost
        price=None if self.simulated or self.usage_only else as_record(self.account['price'])
        reserve=0 if self.usage_only else 1 if price is None else rounded_cost(cast(int,profile['max_input_units']),cast(int,price['input_atoms_per_million']))
        if price is not None and price['per_attempt_money_bound'] is not None:reserve=max(reserve,cast(int,price['per_attempt_money_bound']))
        budget=await self.ledger.get('budget_windows',identity('embedding-budget',cast(str,self.account['account_id']),cast(str,self.account['window_id'])))
        await self._check_allocated_stop()
        if budget is not None and (budget['held_atoms']!=0 or cast(int,budget['attempt_count'])>=cast(int,self.account['attempt_limit'])
                or not self.usage_only and cast(int,budget['known_subtotal_atoms'])+reserve>cast(int,self.account['cost_limit_atoms'])):
            raise OwnerFailure('RESOURCE_BUSY','budget','CAPACITY_REACHED')

    async def _execute_first(self,request: EmbeddingRequest,intent: Record,deadline: float) -> object:
        with DeadlineScope(deadline):
            check_deadline()
            existing=await self.ledger.get('requests',request.request_id)
            if existing is not None:return MappingProxyType({'state':existing['phase'],'request_id':request.request_id})
            self.checkpoint()
            if not self.permit(request.description,intent):raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
            profile=next(p for p in self.profiles if p['profile_id']==request.description['profile_id'])
            price=None if self.simulated or self.usage_only else as_record(self.account['price'])
            rate=1 if price is None else cast(int,price['input_atoms_per_million'])
            from .token_costs import rounded_cost
            reserve=0 if self.usage_only else 1 if self.simulated else rounded_cost(cast(int,profile['max_input_units']),rate)
            if price is not None and price['per_attempt_money_bound'] is not None:reserve=max(reserve,cast(int,price['per_attempt_money_bound']))
            budget_id=identity('embedding-budget',cast(str,self.account['account_id']),cast(str,self.account['window_id']))
            budget=await self.ledger.get('budget_windows',budget_id)
            await self._check_allocated_stop()
            check_deadline()
            if budget is None:
                budget=as_record(freeze({'object_id':budget_id,'revision':1,'account_id':self.account['account_id'],'window_id':self.account['window_id'],
                    'policy':self.account,'attempt_count':0,'known_subtotal_atoms':0,'held_atoms':0,'risk_state':'CLEAR','format_version':self.version,
                    'quota_reserved':0,'quota_known':None if self.usage_only else 0,'quota_held':0,**({'billing_mode':self.billing_mode} if self.usage_only else {})},8192,owned=True))
                check_deadline()
                await self._commit('initialize_budget',identity('embedding-budget-initialize',budget_id),(Mutation('budget_windows',None,budget),),None,None,'NONE','CLEAR',not self.usage_only)
            if (budget['held_atoms']!=0 or cast(int,budget['attempt_count'])>=cast(int,self.account['attempt_limit'])
                    or not self.usage_only and cast(int,budget['known_subtotal_atoms'])+reserve>cast(int,self.account['cost_limit_atoms'])):
                raise OwnerFailure('RESOURCE_BUSY','budget','CAPACITY_REACHED')
            now=self._now();price_revision=None if price is None else cast(str,price['revision_ref'])
            metering=self._usage(reserved=reserve,rate=rate,price_revision=price_revision,simulated=self.simulated)
            req=as_record(freeze({'object_id':request.request_id,'revision':1,'caller_module':'retrieval','caller_scope':self.instance,'extension_id':None,
                'operation_key':request.description['original_request_key'],'capability':'EMBEDDING','task_role':'EMBEDDING_'+cast(str,as_record(request.description['payload'])['purpose']),
                'result_owner':'retrieval','profile_id':profile['profile_id'],'account_id':self.account['account_id'],'created_at':now,'updated_at':now,
                'format_version':self.version,'fingerprint_version':self.version,'attribution':{'run_id':request.description['work_id'],'entry_ids':(),
                    'parent_request_id':None,'trace_id':None,'batch_id':None,'dream_run_id':None,'prompt_revision':None},
                'source':'SIMULATED' if self.simulated else 'REMOTE_PROVIDER','configuration_origin':'PERSISTED_CONFIGURATION','config_snapshot_id':self.configuration.snapshot_id,
                'profile_revision':identity('daily-profile' if self.network is not None else 'embedding-profile',self.configuration.snapshot_id,cast(str,profile['profile_id'])),'price_revision':price_revision,
                'execution_evidence':{'profile':profile,'account':self.account,'request_timeout_ms':60000,'retry_delay_ms':0,'request_max_bytes':65536,'result_max_bytes':40960},
                'fingerprint':request.fingerprint,'phase':'OPEN','outcome':None,'first_error':None,'attempt_count':1,'ever_unknown':False,'handoff_id':None},8192,owned=True))
            attempt=as_record(freeze({'object_id':request.attempt_id,'revision':1,'request_id':request.request_id,'ordinal':1,'state':'PREPARED','logical_outcome':None,
                'account_id':self.account['account_id'],'profile_id':profile['profile_id'],'capability':'EMBEDDING','wire_protocol':profile['wire_protocol'],
                'execution_owner_id':identity('embedding-executor',request.request_id),'created_at':now,'updated_at':now,'adapter_duration_ms':None,
                'handoff_id':None,'confirmed_started':None,'ever_unknown':False,'first_error':None,'terminal_error':None,'usage':metering,'result_fingerprint':None,'evidence_revision':0},8192,owned=True))
            reservation=as_record(freeze({'object_id':identity('embedding-reservation',request.attempt_id),'revision':1,'attempt_id':request.attempt_id,
                'account_id':self.account['account_id'],'budget_id':budget_id,'reserved_atoms':reserve,'known_subtotal_atoms':0,'held_atoms':reserve,
                'known_cost_atoms':None,'cost_complete':False,'format_version':self.version,'quota_reserved':0,'quota_known':None if self.usage_only else 0,'quota_held':0,**({'billing_mode':self.billing_mode} if self.usage_only else {})},8192,owned=True))
            advanced=as_record(freeze(dict(budget)|{'revision':cast(int,budget['revision'])+1,'attempt_count':cast(int,budget['attempt_count'])+1,'held_atoms':reserve},8192,owned=True))
            check_deadline()
            await self.before_first_registration(request)
            check_deadline()
            registered=await self._commit('register',identity('embedding-register',request.request_id),(Mutation('requests',None,req),Mutation('attempts',None,attempt),
                Mutation('budget_windows',budget,advanced),Mutation('reservations',None,reservation)),request.request_id,request.attempt_id,'NONE','PREPARED',False)
            try:await self.after_first_registration(request,registered)
            except (OwnerFailure,InvalidValue):
                return await self._fail_terminal(req,attempt,advanced,reservation,
                    self._usage(reserved=reserve,rate=rate,price_revision=price_revision,not_sent=True),True,'COMMIT_UNCONFIRMED')
        if time.monotonic()>=deadline or not self.permit(request.description,intent):
            return await self._unknown(request,req,attempt)
        self.executions+=1
        start=time.monotonic()
        if self.simulated:
            axis=int(sha256(request.wire).hexdigest()[:8],16)%1024
            result=as_record(freeze({'vectors':(tuple(1.0 if n==axis else 0.0 for n in range(1024)),),'dimensions':1024,
                'space_id':self.space,'model_id':'synthetic_dense','input_items':1},40960,owned=True))
            reported=self._usage(reserved=reserve,rate=rate,price_revision=None,simulated=True,simulated_reported=True)
        else:
            assert self.transport is not None
            def begin():
                assert self.transport is not None
                if self.network is not None:
                    permit=self._network_permit
                    if permit is None:raise OwnerFailure('ACCESS_DENIED','request','BINDING_MISMATCH')
                    transport=self.transport
                    return self.network.start(permit,lambda:transport.exchange(request.wire,min(deadline,start+30),self._cancel.token))
                return asyncio.get_running_loop().run_in_executor(self._executor,self.transport.exchange,request.wire,min(deadline,start+30),self._cancel.token)
            try:wire=await (self.dispatch(request.description,intent,begin) if self.dispatch is not None else begin())
            except OwnerFailure:return await self._fail_terminal(req,attempt,advanced,reservation,
                self._usage(reserved=reserve,rate=rate,price_revision=price_revision,not_sent=True),True,'MODE_BLOCKED')
            if wire.state=='NOT_SENT':
                return await self._fail_terminal(req,attempt,advanced,reservation,
                    self._usage(reserved=reserve,rate=rate,price_revision=price_revision,not_sent=True),True,wire.reason or 'NOT_SENT')
            if wire.state!='RESPONSE':return await self._unknown(request,req,attempt)
            if wire.body is None:
                return await self._fail_terminal(req,attempt,advanced,reservation,metering,False,'PROTOCOL')
            parsed=parse_response(wire.body,space_id=self.space,expected_models=cast(tuple[str,...],self.configuration.candidate.text.record('provider.embedding_transport')['expected_reported_models']),usage_only=self.usage_only)
            reported=allocated_usage(wire.body) if self.usage_only else metering
            try:
                if parsed.usage is not None and not self.usage_only:
                    parsed.usage.estimate(cast(int,profile['max_input_units']),rate)
                    reported=self._usage(reserved=reserve,rate=rate,price_revision=price_revision,reported=parsed.usage)
            except (ValueError,InvalidValue,InvalidData):
                return await self._fail_terminal(req,attempt,advanced,reservation,metering,False,'USAGE')
            if parsed.result is None or wire.status!=200:
                return await self._fail_terminal(req,attempt,advanced,reservation,reported,False,parsed.error if parsed.error!='NONE' else 'PROVIDER_HTTP_FAILURE')
            result=parsed.result
        handoff_id=identity('embedding-handoff',request.request_id)
        material=split_handoff(result,handoff_id=handoff_id,request_id=request.request_id,attempt_id=request.attempt_id,
            space_id=self.space,model_id=cast(str,profile['model_id']))
        observed=as_record(freeze(dict(attempt)|{'revision':2,'updated_at':self._now(),'usage':reported,
            'adapter_duration_ms':int((time.monotonic()-start)*1000),'confirmed_started':True,'result_fingerprint':material.payload['payload_digest'],'evidence_revision':1},8192,owned=True))
        ended=as_record(freeze(dict(observed)|{'revision':3,'state':'COMPLETED','logical_outcome':'SUCCEEDED','handoff_id':handoff_id},8192,owned=True))
        ended_req=as_record(freeze(dict(req)|{'revision':2,'phase':'TERMINAL','outcome':'SUCCEEDED','handoff_id':handoff_id,'updated_at':self._now()},8192,owned=True))
        cost=reported['known_cost_atoms'];subtotal=number(reported['known_subtotal_atoms']);complete=reported['cost_complete']
        settled_budget=as_record(freeze(dict(advanced)|{'revision':cast(int,advanced['revision'])+1,'known_subtotal_atoms':cast(int,advanced['known_subtotal_atoms'])+subtotal,'held_atoms':reported['held_atoms']},8192,owned=True))
        settled_reservation=as_record(freeze(dict(reservation)|{'revision':2,'known_subtotal_atoms':subtotal,'known_cost_atoms':cost,'held_atoms':reported['held_atoms'],'cost_complete':complete},8192,owned=True))
        part=as_record(cast(tuple,reported['items'])[0])
        cost_item=as_record(freeze({'object_id':identity('embedding-cost',request.attempt_id),'revision':1,'attempt_id':request.attempt_id,'item':'input',
            'cost_atoms':cost,'evidence_revision':1,'source':'LOCALLY_ESTIMATED' if complete else 'UNAVAILABLE','unit':'ITEM' if self.simulated else 'TOKEN','known_subtotal_atoms':subtotal,
            'cost_complete':complete,'format_version':self.version,**({'billing_mode':self.billing_mode} if self.usage_only else {}),'quantity':part['quantity'],'price_numerator':part['price_numerator'],'price_denominator':part['price_denominator']},8192,owned=True))
        handoff=as_record(freeze({'object_id':handoff_id,'revision':1,'request_id':request.request_id,'owner_id':'retrieval','checksum':material.payload['payload_digest'],
            'source':req['source'],'artifact_id':identity('embedding-artifact',request.request_id),'format_version':self.version,'created_at':self._now(),
            'embedding_payload':material.payload,'embedding_cleanup':{'received_receipt':None,'retired_through':None,'state':'HELD'},'payload':''},8192,owned=True))
        self._pending=_Completion(request,material,(Mutation('requests',req,ended_req),Mutation('attempts',observed,ended),Mutation('budget_windows',advanced,settled_budget),
            Mutation('reservations',reservation,settled_reservation),Mutation('cost_items',None,cost_item),Mutation('handoffs',None,handoff)),None,(Mutation('attempts',attempt,observed),))
        return await self.finish_pending()

    async def finish_original_pending(self,work_id:str,original_key:str) -> None:
        """Finish only this work's retained registration or failure notification."""
        if self._pending is not None:
            if self._pending.request.description['work_id']!=work_id:raise OwnerFailure('RESOURCE_BUSY','state','CLEANUP_PENDING',True)
            finished=await self.finish_pending()
            if type(finished) is not Committed:raise LedgerFailure(finished)
        if self._failure is not None:
            if self._failure[0].current['operation_key']!=original_key:raise OwnerFailure('RESOURCE_BUSY','state','CLEANUP_PENDING',True)
            await self.finish_failure()

    async def finish_pending(self) -> object:
        """Confirm retained original evidence and handoff without another send."""
        pending=self._pending
        if pending is None:raise ValueError('No retained original completion.')
        request=pending.request
        if pending.evidence is None:
            pending.evidence=await self._commit('evidence',identity('embedding-evidence',request.request_id),pending.evidence_changes,
                request.request_id,request.attempt_id,'PREPARED','PREPARED',bool(as_record(pending.evidence_changes[0].current['usage'])['cost_complete']))
        evidence=pending.evidence
        payload=MappingProxyType({'request_ref':MappingProxyType({'request_id':request.request_id,'attempt_id':request.attempt_id}),
            'original_request_digest':request.fingerprint,'terminal_evidence_ref':self.reference(evidence)})
        completed=await self.execute('store_embedding_handoff',identity('embedding-complete',request.request_id),payload)
        if type(completed) is Committed:
            self._pending=None
            if self.network is not None:await self.reconcile_network()
        return self._retained_outcome(completed)

    async def _fail_terminal(self,request:ModelRecord,attempt:ModelRecord,budget:ModelRecord,reservation:ModelRecord,
                             metering:Record,not_sent:bool,reason:str) -> Receipt:
        """Persist known remote outcome independently from known or held liability."""
        error={'code':'PROVIDER_FAILED','field':'result','reason':reason};now=self._now()
        def frozen(value):return as_record(freeze(value,8192,owned=True))
        ended=frozen(dict(request)|{'revision':cast(int,request['revision'])+1,'phase':'TERMINAL','outcome':'FAILED','first_error':error,'updated_at':now})
        result=frozen(dict(attempt)|{'revision':cast(int,attempt['revision'])+1,'state':'NOT_SENT' if not_sent else 'COMPLETED','logical_outcome':'FAILED',
            'usage':metering,'confirmed_started':not not_sent,'first_error':error,'terminal_error':error,'evidence_revision':1,'updated_at':now})
        settled=frozen(dict(budget)|{'revision':cast(int,budget['revision'])+1,'known_subtotal_atoms':cast(int,budget['known_subtotal_atoms'])+number(metering['known_subtotal_atoms']),
            'held_atoms':metering['held_atoms']})
        reserved=frozen(dict(reservation)|{'revision':cast(int,reservation['revision'])+1,**{k:metering[k] for k in ('known_cost_atoms','known_subtotal_atoms','held_atoms','cost_complete')}})
        item=cast(Record,cast(tuple,metering['items'])[0])
        cost=frozen({'object_id':identity('embedding-cost',cast(str,attempt['object_id'])),'revision':1,'attempt_id':attempt['object_id'],'item':'input',
            'cost_atoms':metering['known_cost_atoms'],'evidence_revision':1,'source':'LOCALLY_ESTIMATED' if metering['cost_complete'] else 'UNAVAILABLE',
            'unit':'ITEM' if self.simulated else 'TOKEN','known_subtotal_atoms':metering['known_subtotal_atoms'],'cost_complete':metering['cost_complete'],'format_version':self.version,
            **({'billing_mode':self.billing_mode} if self.usage_only else {}),'quantity':item['quantity'],'price_numerator':item['price_numerator'],'price_denominator':item['price_denominator']})
        self._failure=(Mutation('requests',request,ended),Mutation('attempts',attempt,result),Mutation('budget_windows',budget,settled),
            Mutation('reservations',reservation,reserved),Mutation('cost_items',None,cost))
        return await self.finish_failure()

    async def finish_failure(self) -> Receipt:
        """Confirm one retained original failure, preserving its first observation."""
        changes=self._failure
        if changes is None:raise ValueError('No retained original failure.')
        request=changes[0].current;attempt=changes[1].current
        receipt=await self._commit('terminate',identity('embedding-terminate',cast(str,request['object_id'])),changes,cast(str,request['object_id']),
            cast(str,attempt['object_id']),'OPEN','TERMINAL',bool(as_record(attempt['usage'])['cost_complete']))
        self._failure=None
        if self.network is not None:await self.reconcile_network()
        return receipt

    async def _unknown(self,request: EmbeddingRequest,req: ModelRecord,attempt: ModelRecord) -> object:
        return await self._mark_unknown(req,attempt)

    async def _mark_unknown(self,req:ModelRecord,attempt:ModelRecord) -> Receipt:
        error={'code':'REMOTE_RESULT_UNKNOWN','field':'result','reason':'ORIGINAL_RESULT_UNCONFIRMED'}
        current=as_record(freeze(dict(req)|{'revision':cast(int,req['revision'])+1,'phase':'REMOTE_RESULT_UNKNOWN','ever_unknown':True,'first_error':error},8192,owned=True))
        unknown=as_record(freeze(dict(attempt)|{'revision':cast(int,attempt['revision'])+1,'state':'REMOTE_RESULT_UNKNOWN','ever_unknown':True,'first_error':error},8192,owned=True))
        receipt=await self._commit('recover',identity('embedding-unknown',cast(str,req['object_id'])),(Mutation('requests',req,current),Mutation('attempts',attempt,unknown)),
            cast(str,req['object_id']),cast(str,attempt['object_id']),'PREPARED','REMOTE_RESULT_UNKNOWN',False)
        self._unknown_requests.add(cast(str,req['object_id']))
        if self.network is not None:self.network.block_account(cast(str,req['account_id']))
        return receipt

    @staticmethod
    def reference(receipt: Receipt) -> Record:
        return isolate(RECEIPT,{'kind':receipt.identity.operation_kind,'key':receipt.identity.operation_key,'fingerprint':receipt.fingerprint})

    async def execute(self,kind: str,key: str,payload: Record) -> object:
        """Execute or confirm an exact local Provider command, never a send."""
        definition=self.definitions[kind]
        envelope={'binding_id':self.instance,'request_key':key,'expected':(),'observed_at':0,'payload':encode_content(payload,24576).decode()}
        command=ResultBoundCommand(definition.command_version,envelope,
            {a.event_slot:{'actor':'embedding_provider'} for a in definition.required_audits})
        port=self.operations[kind]
        prior=await port.resolve_operation(port.recovery_handle(key,command))
        if type(prior) is not NotCommitted or prior.error is not None:return prior
        return await port.execute(key,command)

    def handle(self,kind: str,uow: UnitOfWork,envelope: Record,payload: Record) -> object:
        """Materialize only this live owner's retained original complete response."""
        operation,_=self.storage.semantic_operation_context(uow,self.catalog.definition)
        if operation.scope_id!='provider' or operation.operation_kind!=kind or envelope['binding_id']!=self.instance:raise ValueError('Provider command scope differs.')
        self.checkpoint()
        if kind!='store_embedding_handoff':return self._cleanup(kind,uow,payload)
        pending=self._pending
        if pending is None or pending.evidence is None or payload['original_request_digest']!=pending.request.fingerprint or payload['request_ref']!={'request_id':pending.request.request_id,'attempt_id':pending.request.attempt_id}:
            raise ValueError('No matching original execution owner.')
        evidence=self.storage.confirm_prior_operation(uow,next(d for d in self.ledger.assembly.commands if d.operation_kind=='evidence'),pending.evidence.identity.operation_key)
        if evidence!=pending.evidence or payload['terminal_evidence_ref']!=self.reference(pending.evidence):raise ValueError('Original terminal evidence differs.')
        changes=tuple(MappingProxyType({'table':m.table,'object_id':m.current['object_id'],'expected_revision':None if m.previous is None else m.previous['revision'],
            'body':dump(MappingProxyType({k:v for k,v in m.current.items() if k!='payload'})),'payload':m.current.get('payload')}) for m in pending.changes)
        self.ledger._apply(uow,cast(tuple[Record,...],changes))
        for leaf in pending.material.leaves:self.rows.write('embedding_handoff_leaf',uow,leaf)
        targets=tuple({'object_id':m.current['object_id'],'previous_revision':None if m.previous is None else m.previous['revision'],'revision':m.current['revision']} for m in pending.changes)
        req=pending.changes[0];attempt=pending.changes[1]
        return {'outcome':'APPLIED','targets':targets,'items':(pending.changes[-1].current['object_id'],),'facts':{'provider':{
            'request_id':req.current['object_id'],'attempt_id':attempt.current['object_id'],'previous_revision':req.previous['revision'] if req.previous else None,
            'revision':req.current['revision'],'previous_state':'OPEN','state':'TERMINAL','cost_complete':as_record(attempt.current['usage'])['cost_complete'],
            'billing_mode':self.billing_mode,'currency':self.account['currency'],
            'quota_known':None if self.usage_only else 0,'quota_held':0,'config_snapshot_id':self.configuration.snapshot_id}}}

    def bind_retrieval(self,catalog: StatementCatalog) -> None:
        """Bind only the actual reception participant's bounded artifact read."""
        if self._retrieval is not None or not any(catalog.definition is p for p in self.reception.participants) or catalog.definition.owner_module!='retrieval':
            raise ValueError('Native retrieval participant required.')
        self._retrieval=BoundStatements(catalog,self.storage,'provider')

    def verify_result(self,uow: UnitOfWork,result: EmbeddingResult,work: Record) -> None:
        """Recheck the native retained result and original completion in reception."""
        if type(result) is not EmbeddingResult or self._results.get(id(result)) is not result or work['kind']!='EMBED':raise ValueError('Native result consumer required.')
        request_id=cast(str,result.request['object_id'])
        originals={}
        for table in ('requests','attempts','handoffs'):
            rows=self._read_views.stage('transaction_'+table,uow,{'request_id':request_id})
            if len(rows)!=1:raise ValueError('Original Provider root is missing.')
            originals[table]=self.ledger._decode(table,rows[0])
        if any(originals[table]!=value for table,value in (('requests',result.request),('attempts',result.attempt),('handoffs',result.handoff))):
            raise ValueError('Retained result changed.')
        intent=work['intent'];assert type(intent) is MappingProxyType
        if (result.request['fingerprint']!=intent['request_digest'] or result.request['operation_key']!=work['original_request_key']
                or as_record(result.request['attribution'])['run_id']!=work['work_id']
                or result.request['task_role']!='EMBEDDING_'+string(work['purpose'])):raise ValueError('Result belongs to another work.')
        receipt=self.storage.confirm_embedding_operation(uow,self.definitions['store_embedding_handoff'],result.completion.identity.operation_key,request_id)
        if receipt!=result.completion:raise ValueError('Original completion is unconfirmed.')

    def work_request(self,uow: UnitOfWork,work: Record) -> ModelRecord|None:
        """Read the exact original embedding key through the instance-only view."""
        if work['kind']!='EMBED':raise ValueError('Deletion never accesses Provider.')
        rid=identity('embedding-request',self.instance,work['original_request_key'])
        rows=self._read_views.stage('transaction_requests',uow,{'request_id':rid})
        if not rows:return None
        if len(rows)!=1:raise ValueError('Original request is ambiguous.')
        request=self.ledger._decode('requests',rows[0])
        intent=work['intent']
        if (type(intent) is not MappingProxyType or request['fingerprint']!=intent['request_digest']
                or request['caller_scope']!=self.instance or request['operation_key']!=work['original_request_key']
                or as_record(request['attribution'])['run_id']!=work['work_id']):raise ValueError('Original work identity differs.')
        return request

    def verify_cleanup(self,uow: UnitOfWork,work: Record,proof: Record) -> None:
        """Require durable retirement and actual ended worker/consumer ownership."""
        request=self.work_request(uow,work)
        if request is None or request['phase'] not in ('TERMINAL','REMOTE_RESULT_UNKNOWN'):raise ValueError('No original terminal or unknown evidence.')
        rid=cast(str,request['object_id'])
        with self._lock:
            if (self._active is not None and not self._active.done() or self._pending is not None or self._failure is not None or self._reading
                or any(r.request['object_id']==rid for r in self._results.values())):raise OwnerFailure('RESOURCE_BUSY','state','CLEANUP_PENDING',True)
        if request['outcome']!='SUCCEEDED':
            self.verify_failure(uow,work,record_reference=proof,request_ref=work['request_ref'])
            return
        rows=self._read_views.stage('transaction_handoffs',uow,{'request_id':rid})
        if len(rows)!=1:raise ValueError('Original handoff is missing.')
        handoff=self.ledger._decode('handoffs',rows[0])
        if as_record(handoff['embedding_cleanup'])['state']!='RETIRED' or handoff['artifact_id']!=work['artifact_id'] or proof['kind']!='retire_embedding_handoff':
            raise ValueError('Resource retirement is incomplete.')
        receipt=self.storage.confirm_embedding_operation(uow,self.definitions['retire_embedding_handoff'],string(proof['key']),rid)
        if (receipt is None or receipt.fingerprint!=proof['fingerprint'] or not any(type(t) is MappingProxyType and t['object_id']==handoff['object_id']
                and t['revision']==handoff['revision'] for t in cast(tuple,cast(Record,receipt.result)['targets']))):raise ValueError('Original retirement is unconfirmed.')

    def verify_failure(self,uow:UnitOfWork,work:Record,*,record_reference:Record,request_ref:Value) -> ModelRecord:
        """Bind failure to the original request, attempt and necessary audit receipt."""
        request=self.work_request(uow,work)
        if request is None or request['phase'] not in ('TERMINAL','REMOTE_RESULT_UNKNOWN') or request['outcome']=='SUCCEEDED':raise ValueError('Original failure is missing.')
        rid=cast(str,request['object_id']);kind='recover' if request['phase']=='REMOTE_RESULT_UNKNOWN' else 'terminate'
        attempts=self._read_views.stage('transaction_attempts',uow,{'request_id':rid})
        if len(attempts)!=1:raise ValueError('Original attempt is missing.')
        attempt=self.ledger._decode('attempts',attempts[0])
        if request_ref!={'request_id':rid,'attempt_id':attempt['object_id']} or record_reference['kind']!=kind:raise ValueError('Failure identity differs.')
        definition=next(d for d in self.ledger.assembly.commands if d.operation_kind==kind)
        receipt=self.storage.confirm_embedding_operation(uow,definition,string(record_reference['key']),rid)
        if receipt is None or receipt.fingerprint!=record_reference['fingerprint']:raise ValueError('Original failure receipt is unconfirmed.')
        result=cast(Record,receipt.result)
        if result.get('object_id')!=rid or result.get('revision')!=request['revision']:raise ValueError('Original failure revision differs.')
        return attempt

    def _root(self,uow: UnitOfWork,table: str,key: str) -> ModelRecord:
        staged=self.ledger.statements[table+'_get'].participate(uow,{'object_id':key})
        if type(staged) is not Staged or type(staged.value) is not tuple or len(staged.value)!=1:raise ValueError('Original Provider root is missing.')
        return self.ledger._decode(table,cast(Record,staged.value[0]))

    def _cleanup(self,kind: str,uow: UnitOfWork,payload: Record) -> object:
        reference=payload['request_ref'];assert type(reference) is MappingProxyType
        rid=string(reference['request_id']);request=self._root(uow,'requests',rid)
        if request['caller_scope']!=self.instance or request['phase']!='TERMINAL' or request['outcome']!='SUCCEEDED':raise ValueError('No complete original handoff.')
        attempt=self._root(uow,'attempts',string(reference['attempt_id']))
        if attempt['request_id']!=rid or attempt['handoff_id']!=request['handoff_id']:raise ValueError('Attempt differs.')
        handoff=self._root(uow,'handoffs',cast(str,request['handoff_id']));cleanup=as_record(handoff['embedding_cleanup']);metadata=as_record(handoff['embedding_payload'])
        items: list[str]=[]
        if kind=='confirm_embedding_handoff':
            if cleanup['state']!='HELD' or self._retrieval is None:raise ValueError('Handoff already confirmed or receiver unavailable.')
            received=payload['receipt'];assert type(received) is MappingProxyType
            if received['kind']!='record_result' or payload['artifact_id']!=handoff['artifact_id']:raise ValueError('Reception identity differs.')
            receipt=self.storage.confirm_embedding_operation(uow,self.reception,string(received['key']),rid)
            if receipt is None or receipt.fingerprint!=received['fingerprint']:raise ValueError('Reception is unconfirmed.')
            rows=self._retrieval.stage('provider_received_artifact',uow,{'caller_scope':self.instance,'artifact_id':payload['artifact_id'],'request_id':rid})
            if len(rows)!=1:raise ValueError('Complete artifact is missing.')
            from companion_memory.retrieval.semantic_schema import validate
            from companion_memory.persistence.content_codec import decode_content
            artifact=validate('embedding_artifact',decode_content(string(rows[0]['body']).encode(),8192))
            if artifact['request_ref']!=reference or artifact['completion_ref']!=received or artifact['artifact_id'] not in cast(tuple,cast(Record,receipt.result)['items']):
                raise ValueError('Original artifact receipt differs.')
            updated=dict(cleanup)|{'received_receipt':received,'state':'RELEASABLE'};items.append(string(payload['artifact_id']))
        elif kind=='retire_embedding_handoff':
            if (cleanup['state']!='RELEASABLE' or payload['expected_handoff_revision']!=handoff['revision']
                    or payload['after_ordinal']!=cleanup['retired_through']):raise ValueError('Retirement revision differs.')
            with self._lock:
                if self._reading or any(r.request['object_id']==rid for r in self._results.values()):raise OwnerFailure('RESOURCE_BUSY','state','CLEANUP_PENDING',True)
                self._retiring.add(rid)
            start=0 if cleanup['retired_through'] is None else cast(int,cleanup['retired_through'])+1
            end=min(start+8,cast(int,metadata['leaf_count']))
            if start>=end:raise ValueError('No remaining handoff page.')
            for ordinal in range(start,end):
                leaf_id=identity('embedding-handoff-leaf',cast(str,handoff['object_id']),ordinal)
                leaf=self.rows.get('embedding_handoff_leaf',uow,leaf_id)
                if leaf is None or leaf['request_id']!=rid or leaf['attempt_id']!=reference['attempt_id']:raise ValueError('Original leaf is missing.')
                self.rows.remove('embedding_handoff_leaf',uow,leaf_id,1);items.append(leaf_id)
            updated=dict(cleanup)|{'retired_through':end-1,'state':'RETIRED' if end==metadata['leaf_count'] else 'RELEASABLE'}
        else:raise ValueError('Unsupported Provider command.')
        current=as_record(freeze(dict(handoff)|{'revision':cast(int,handoff['revision'])+1,'embedding_cleanup':updated},8192,owned=True))
        change=MappingProxyType({'table':'handoffs','object_id':handoff['object_id'],'expected_revision':handoff['revision'],
            'body':dump(MappingProxyType({k:v for k,v in current.items() if k!='payload'})),'payload':''})
        self.ledger._apply(uow,(cast(Record,change),))
        return {'outcome':'APPLIED','targets':({'object_id':handoff['object_id'],'previous_revision':handoff['revision'],'revision':current['revision']},),
            'items':tuple(items),'facts':{'provider':{'request_id':rid,'attempt_id':reference['attempt_id'],'previous_revision':handoff['revision'],
            'revision':current['revision'],'previous_state':'TERMINAL','state':'TERMINAL','cost_complete':bool(as_record(attempt['usage'])['cost_complete']) if self.usage_only else True,
            'billing_mode':self.billing_mode,'currency':self.account['currency'],
            'quota_known':None if self.usage_only else 0,'quota_held':0,'config_snapshot_id':self.configuration.snapshot_id}}}

    async def recover_result(self,request_id: str) -> EmbeddingResult:
        """Reserve a real receiving consumer before asynchronous reads begin."""
        with self._lock:
            if self._closed or request_id in self._retiring or self._reading+len(self._results)>=2:raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
            self._reading+=1
        try:return await self._recover_result(request_id)
        finally:
            with self._lock:self._reading-=1

    async def _recover_result(self,request_id: str) -> EmbeddingResult:
        """Retain a complete original handoff; missing leaves never trigger work."""
        request=await self.ledger.get('requests',request_id)
        if request is None or request['caller_scope']!=self.instance or request['phase']!='TERMINAL' or request['outcome']!='SUCCEEDED':raise ValueError('No complete original result.')
        attempts=await self.ledger.read('attempts_for_request',{'request_id':request_id})
        if len(attempts)!=1 or attempts[0]['state']!='COMPLETED':raise ValueError('Original attempt is incomplete.')
        attempt=attempts[0];handoff=await self.ledger.get('handoffs',cast(str,request['handoff_id']))
        if handoff is None or as_record(handoff['embedding_cleanup'])['state']!='HELD':raise ValueError('Original result is not retained for reception.')
        metadata=as_record(handoff['embedding_payload']);leaves=[]
        for ordinal in range(cast(int,metadata['leaf_count'])):
            leaf=await self.rows.read('embedding_handoff_leaf',identity('embedding-handoff-leaf',cast(str,handoff['object_id']),ordinal))
            if leaf is None:raise ValueError('Original result has a missing leaf.')
            leaves.append(leaf)
        profile=as_record(as_record(request['execution_evidence'])['profile'])
        value=restore_handoff(metadata,leaves,handoff_id=cast(str,handoff['object_id']),request_id=request_id,attempt_id=cast(str,attempt['object_id']),space_id=self.space,model_id=cast(str,profile['model_id']))
        found=await self.operations['store_embedding_handoff'].read_receipt(identity('embedding-complete',request_id))
        if type(found) is Failed:raise LedgerFailure(found)
        if type(found) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
        result=object.__new__(EmbeddingResult)
        for name,item in dict(owner=self,request=request,attempt=attempt,handoff=handoff,completion=found.value,value=value).items():object.__setattr__(result,name,item)
        self._results[id(result)]=result
        return result

    def release_result(self,result: EmbeddingResult) -> None:
        """Release only the actual receiving consumer; no persistent rows are deleted."""
        if type(result) is not EmbeddingResult or self._results.get(id(result)) is not result:raise ValueError('Unknown result consumer.')
        del self._results[id(result)]

    @property
    def cleanup_pending(self) -> bool:
        """Actual ownership across all work, independent of observation paging."""
        with self._lock:
            return bool(self._active is not None and not self._active.done() or self._pending is not None
                or self._failure is not None or self._reading or self._results or self._network_permit is not None)

    def pending_for(self,work_id: str) -> bool:
        """Actual retained execution/results, independent of remote outcome."""
        with self._lock:
            return (self._network_work==work_id and self._network_permit is not None or self._active_work==work_id and self._active is not None and not self._active.done()
                or self._pending is not None and self._pending.request.description['work_id']==work_id
                or self._failure is not None and as_record(self._failure[0].current['attribution'])['run_id']==work_id
                or any(as_record(r.request['attribution'])['run_id']==work_id for r in self._results.values()))

    async def close(self,deadline: float) -> bool:
        """Keep pending response and SQL ownership until actual work has ended."""
        self._closed=True;self._cancel.cancel();self._absence=None
        if self._active is not None:
            done,_=await asyncio.wait((self._active,),timeout=max(0,deadline-time.monotonic()))
            if not done:return False
        if self._reading or self._results or self._pending is not None or self._failure is not None:return False
        if self.network is not None:
            observation=self.network.close()
            if observation.occupied:return False
        if self._executor is not None:self._executor.shutdown(wait=False)
        return self.ledger.release()
