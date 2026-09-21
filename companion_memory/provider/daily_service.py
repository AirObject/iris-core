"""One mixed Provider owner: generation, image, goals and dense embeddings.

All capabilities share the original ledger lease and the one physical network
worker. Native material and business authority are rechecked in registration;
local confirmation and retirement never dispatch HTTP or create another key.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
from companion_memory.persistence.schema import InvalidValue
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import time
from types import MappingProxyType
from typing import cast,TYPE_CHECKING
from companion_memory.configuration import PresentValue
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration
from companion_memory.cognition.daily_material_storage import DailyMaterialStorage,DailyMaterialReadLease
from companion_memory.cognition.daily_resources import prompt_resource,output_schema
from companion_memory.persistence import Committed,Found,NotCommitted,Receipt,ResultBoundCommand,UnitOfWork,Failed
from companion_memory.persistence.completion import start_owned,CompletionScope
from companion_memory.persistence.deadlines import DeadlineScope,check_deadline
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import Record as StoredRecord,identity,string
from companion_memory.persistence.content_codec import encode_content
from .embedding_service import EmbeddingProvider
from .embedding_material import HandoffMaterial
from .daily_execution import DailyRequest,row,registration,settlement,reservation_amount,usage
from .daily_protocol import DailyChatBinding,encode_daily_request,decode_daily_response
from .dream_protocol import DreamChatBinding,encode_dream_request,decode_dream_response,ROLES as DREAM_ROLES
from companion_memory.cognition.dream_resources import prompt_resource as dream_prompt, output_schema as dream_schema
from .daily_commands import DailyProviderCommands
from .daily_handoff import split,restore
from .daily_network import DailyNetwork,NetworkPermit
from .ledger import LedgerBinding,LedgerFailure,Mutation
from .chat_transport import ChatTransport
from .values import Record,as_record,dump,InvalidData,plain
from .media_input import ImageInput
if TYPE_CHECKING:
    from companion_memory.runtime.daily_trial_authorization import DailyTrialAuthorization

@dataclass(frozen=True,slots=True,init=False)
class DailyResult:
    owner:DailyProvider
    request:Record
    attempt:Record
    handoff:Record
    completion:Receipt
    value:Record

@dataclass(slots=True)
class _Pending:
    request:DailyRequest
    changes:tuple[Mutation,...]
    evidence_changes:tuple[Mutation,...]
    material:HandoffMaterial|None
    evidence:Receipt|None=None

class DailyProvider(EmbeddingProvider):
    """The sole ledger and transport owner for a complete native daily assembly."""
    @property
    def profiles(self) -> tuple[Record,...]:
        from .managed_versions import profiles
        versions = getattr(self, 'managed_versions', None)
        return profiles(versions.versions.current.candidate) if versions is not None else self._birth_profiles

    @profiles.setter
    def profiles(self, value: tuple[Record,...]) -> None:
        self._birth_profiles = value

    def __init__(self,ledger:LedgerBinding,configuration:StoredCognitionConfiguration,instance:str,definitions,embedding_transport:ChatTransport,
                 checkpoint:Callable[[],None],embedding_permit,*,network:DailyNetwork,commands:DailyProviderCommands,
                 materials:DailyMaterialStorage,transports:dict[str,ChatTransport],
                 authorize:Callable[[DailyRequest,UnitOfWork|None],bool],received:Callable[[UnitOfWork,Record,Record,StoredRecord],bool]):
        expected_roles={'LEARNING','PERSONA','GOAL_DEDUP','MEDIA'} | (set(DREAM_ROLES) if ledger.assembly.dream_format else set())
        if (type(commands) is not DailyProviderCommands or type(materials) is not DailyMaterialStorage or set(transports)!=expected_roles
                or any(type(t) is not ChatTransport for t in transports.values()) or commands.handler is not None):raise InvalidData()
        bindings:dict[str,DailyChatBinding]={};dream_bindings:dict[str,DreamChatBinding]={};settings=cast(tuple[StoredRecord,...],configuration.candidate.text.record('provider.transport')['roles'])
        for setting in settings:
            role=string(setting['role'])
            if not (transports[role].matches_dream(cast(Record,setting)) if role in DREAM_ROLES else transports[role].matches_daily(cast(Record,setting))):raise InvalidData()
            profile=next(as_record(entry) for entry in self._profiles(configuration) if entry['profile_id']==setting['profile_id'])
            if role in DREAM_ROLES:
                dream_bindings[role]=DreamChatBinding(role,cast(str,profile['model_id']),string(setting['schema_ref']),string(setting['schema_digest']),dream_schema(role),
                    string(setting['prompt_digest']),dream_prompt(role))
            else:
                bindings[role]=DailyChatBinding(role,cast(str,profile['model_id']),string(setting['schema_ref']),string(setting['schema_digest']),output_schema(role),
                    string(setting['prompt_digest']),prompt_resource(role))
        super().__init__(ledger,configuration,instance,definitions,embedding_transport,checkpoint,embedding_permit,network=network)
        self.execution_configuration=lambda:configuration.candidate
        from .managed_versions import ManagedProviderVersions
        self.managed_versions:ManagedProviderVersions|None=None
        self.chat_commands=commands;self.materials=materials;self.transports=dict(transports);self.bindings=bindings;self.dream_bindings=dream_bindings
        self.authorize=authorize;self.received=received
        self.chat_operations={d.operation_kind:self.storage.bind_operation(d,'provider') for d in commands.commands}
        self._chat_task:asyncio.Task|None=None;self._chat_registration:tuple[DailyRequest,tuple[Mutation,...]]|None=None
        self._chat_pending:_Pending|None=None;self._chat_permit:NetworkPermit|None=None;self._chat_results:dict[int,DailyResult]={}
        self._chat_readers:set[asyncio.Task]=set();self._chat_lookups:set[asyncio.Task]=set();self._chat_cleanup:asyncio.Task|None=None;self._chat_reconciliation_failed=False
        self._chat_requests:dict[int,DailyRequest]={}
        self._chat_input:DailyRequest|None=None
        self.trial_authorization:DailyTrialAuthorization|None=None
        self.managed_dispatch:Callable[[],bool]|None=None
        commands.handler=self._handle_daily

    async def before_first_registration(self,request) -> None:
        if self.trial_authorization is not None:
            await self.trial_authorization.reserve(request)
        elif type(self.configuration) is StoredManagedConfiguration and self.managed_dispatch is not None:
            if not self.managed_dispatch():
                raise OwnerFailure('ACCESS_DENIED','authorization','OPERATION_NOT_GRANTED')
        elif any(t.execution_kind=='REAL' for t in (*self.transports.values(),self.transport) if t is not None):
            raise OwnerFailure('ACCESS_DENIED','authorization','OPERATION_NOT_GRANTED')

    async def after_first_registration(self,request,receipt:Receipt) -> None:
        if self.trial_authorization is not None:await self.trial_authorization.registered(request,receipt)

    async def trial_registration_receipt(self,request) -> Receipt:
        """Attest the actual original native registration to its outer journal."""
        from .embedding_service import EmbeddingRequest
        if type(request) not in (DailyRequest,EmbeddingRequest) or request.owner is not self:raise InvalidData()
        embedding=type(request) is EmbeddingRequest
        port=self.ledger.operations['register'] if embedding else self.chat_operations['register_daily_request']
        key=identity(('embedding' if embedding else 'daily')+'-register',request.request_id)
        found=await port.read_receipt(key);root=await self.ledger.get('requests',request.request_id)
        if (type(found) is not Found or root is None or root['fingerprint']!=request.fingerprint
                or root['operation_key']!=request.description['original_request_key']):raise InvalidData()
        return found.value

    async def registered_request_receipt(self,request_id:str) -> Receipt:
        """Read the actual initial registration, even after original completion."""
        root=await self.ledger.get('requests',request_id)
        if root is None or root['caller_scope']!=self.instance:raise InvalidData()
        embedding=root['capability']=='EMBEDDING'
        port=self.ledger.operations['register'] if embedding else self.chat_operations['register_daily_request']
        found=await port.read_receipt(identity(('embedding' if embedding else 'daily')+'-register',request_id))
        if type(found) is not Found:raise InvalidData()
        return found.value

    @staticmethod
    def _profiles(configuration:StoredCognitionConfiguration) -> tuple[Record,...]:
        state=next(entry.state for entry in configuration.candidate.foundation.list_entries() if entry.definition.key=='provider.profiles')
        if type(state) is not PresentValue:raise InvalidData()
        return cast(tuple[Record,...],state.value)

    async def initialize(self) -> None:
        """Verify all mixed accounts before recovering original unknown work."""
        if self._initialized:return
        from .daily_recovery import verify_mixed_ledger,recover_confirmed_unsent
        try:
            existing=await verify_mixed_ledger(self)
            await recover_confirmed_unsent(self)
            await super().initialize()
            self._initialized=False
            await verify_mixed_ledger(self)
        except (InvalidData,InvalidValue,ValueError,KeyError,TypeError):
            self._initialized=False
            raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE') from None
        if existing and self.network is not None:self.network.restore_previous_process()
        self._initialized=True

    @property
    def ready(self) -> bool:
        return self._initialized and not self._closed

    def get_health(self):
        """Observe this one owner's actual jobs; no reads, sends or cleanup run here."""
        from .values import Health
        network=self.network.observation() if self.network is not None else None
        occupied=int(bool(network and network.occupied))
        authorization_fault=self.trial_authorization is not None and self.trial_authorization.faulted
        return Health('CLOSING' if self._closed and self.cleanup_pending else 'CLOSED' if self._closed else 'READY' if self.ready else 'RECOVERING',
            occupied,len(self._unknown_requests),self.cleanup_pending,self._chat_reconciliation_failed or authorization_fault,
            'CLEANUP_PENDING' if self.cleanup_pending else 'AUTHORIZATION_UNCONFIRMED' if authorization_fault else None)

    def require_dream_exit(self,uow:UnitOfWork) -> None:
        """Fence quiescence from a native transaction without touching loop-owned I/O."""
        def quiet():
            with self._lock:
                return (self.ledger.assembly.dream_format and self.ready and not self._unknown_requests
                    and not self.cleanup_pending and not self._chat_reconciliation_failed)
        if not quiet():raise OwnerFailure('RESOURCE_BUSY','provider','CLEANUP_PENDING',True)
        uow.require_commit_permission(quiet)

    def generation_request(self,role:str,lease:DailyMaterialReadLease,key:str,deadline_at_us:int,entry_ids:tuple[str,...]=()) -> DailyRequest:
        """Freeze generation from a retained owner-issued complete material lease."""
        if role not in (('LEARNING','PERSONA','GOAL_DEDUP') + (DREAM_ROLES if self.ledger.assembly.dream_format else ())) or self._closed or self._chat_requests:raise InvalidData()
        material=self.materials.verify_lease(lease);root=material.manifest;binding=self.dream_bindings[role] if role in DREAM_ROLES else self.bindings[role]
        if (root['schema_ref']!=binding.schema_ref or root['prompt_ref']!=next(s['prompt_ref'] for s in cast(tuple[StoredRecord,...],self.configuration.candidate.text.record('provider.transport')['roles']) if s['role']==role)
                or root['context_kind']!=role):
            raise InvalidData()
        wire=encode_dream_request(binding,material.body.decode()) if type(binding) is DreamChatBinding else encode_daily_request(cast(DailyChatBinding,binding),material.body.decode())
        if root['wire_digest']!=sha256(wire).hexdigest():raise InvalidData()
        from .managed_versions import profiles
        version = lease.execution_version
        candidate = version.candidate if version is not None else self.configuration.candidate
        profile=next(p for p in profiles(candidate) if p['material_role']==role)
        transport = self.managed_versions.transports(version)[role] if self.managed_versions is not None and version is not None else self.transports[role]
        state=next(e.state for e in self.configuration.candidate.foundation.list_entries() if e.definition.key=='provider.accounts')
        if type(state) is not PresentValue:raise InvalidData()
        account=next(as_record(a) for a in cast(tuple[Record,...],state.value) if a['account_id']==profile['account_id'])
        from companion_memory.persistence.semantic_records import record,ID,H,N,SequenceSchema,isolate
        schema=record(config=record(database_id=ID,instance_id=ID,snapshot_id=ID),work_id=ID,original_request_key=ID,role=ID,
            profile_id=ID,material_id=ID,material_digest=H,wire_digest=H,deadline_at_us=N,entry_ids=SequenceSchema(ID,0,1))
        if version is not None:
            from companion_memory.persistence import Field,RecordSchema
            schema=RecordSchema(schema.fields+(Field('execution_version_id',ID),))
        description=isolate(schema,{'config':{'database_id':self.configuration.database_id,'instance_id':self.instance,'snapshot_id':self.configuration.snapshot_id},
            'work_id':root['owner_ref'],'original_request_key':key,'role':role,'profile_id':profile['profile_id'],'material_id':root['object_id'],
            'material_digest':root['payload_digest'],'wire_digest':sha256(wire).hexdigest(),'deadline_at_us':deadline_at_us,'entry_ids':entry_ids,
            **({'execution_version_id':version.version_id} if version is not None else {})})
        request=object.__new__(DailyRequest);rid=identity('daily-request',self.instance,key)
        for name,value in dict(owner=self,description=description,request_id=rid,attempt_id=identity('daily-attempt',rid,1),
                fingerprint=sha256(encode_content(description,8192)).hexdigest(),wire=wire,material=lease,batch_id=root['batch_id'],binding=binding,account=account,profile=profile,
                execution_version=version,transport=transport).items():object.__setattr__(request,name,value)
        self._chat_requests[id(request)]=request
        return request

    def image_request(self,lease: ImageInput,key:str) -> DailyRequest:
        """Encode only an adapter-bound, fully decoded original image input."""
        from .image_protocol import encode_deepseek_image,encode_minimax_image,IMAGE_SYSTEM,IMAGE_USER
        if type(lease) is not ImageInput or self._closed or self._chat_requests:raise InvalidData()
        if not lease.matches_provider(self.storage,self.configuration.database_id,self.instance):raise InvalidData()
        image=lease.verify(None);work=lease.binding;binding=self.bindings['MEDIA']
        if work.operation_key!=key or work.prompt_revision!=next(s['prompt_ref'] for s in cast(tuple[StoredRecord,...],self.configuration.candidate.text.record('provider.transport')['roles']) if s['role']=='MEDIA'):raise InvalidData()
        encoder=encode_minimax_image if binding.requested_model=='MiniMax-M3' else encode_deepseek_image
        wire=encoder(image,system=IMAGE_SYSTEM,text=IMAGE_USER)
        profile=next(p for p in self.profiles if p['material_role']=='MEDIA')
        if profile['profile_id']!=work.profile_id:raise InvalidData()
        accounts=next(e.state.value for e in self.configuration.candidate.foundation.list_entries() if e.definition.key=='provider.accounts' and type(e.state) is PresentValue)
        account=next(as_record(a) for a in cast(tuple,accounts) if as_record(a)['account_id']==profile['account_id'])
        from companion_memory.persistence.semantic_records import record,ID,H,N,SequenceSchema,isolate
        shape=record(config=record(database_id=ID,instance_id=ID,snapshot_id=ID),work_id=ID,original_request_key=ID,role=ID,
            profile_id=ID,material_id=ID,material_digest=H,wire_digest=H,deadline_at_us=N,entry_ids=SequenceSchema(ID,1,1),
            occurrence_id=ID,blob_id=ID,generation=N,byte_count=N,descriptor_digest=H)
        description=isolate(shape,{'config':{'database_id':self.configuration.database_id,'instance_id':self.instance,'snapshot_id':self.configuration.snapshot_id},
            'work_id':work.work_id,'original_request_key':key,'role':'MEDIA','profile_id':profile['profile_id'],'material_id':lease.artifact_id,
            'material_digest':image.sha256,'wire_digest':sha256(wire).hexdigest(),'deadline_at_us':work.deadline_at_us,'entry_ids':(work.entry_id,),
            'occurrence_id':work.occurrence_id,'blob_id':work.blob_id,'generation':work.generation,'byte_count':len(image.data),
            'descriptor_digest':work.descriptor_digest})
        rid=identity('daily-request',self.instance,key);request=object.__new__(DailyRequest)
        for name,value in dict(owner=self,description=description,request_id=rid,attempt_id=identity('daily-attempt',rid,1),
                fingerprint=sha256(encode_content(description,8192)).hexdigest(),wire=wire,material=lease,batch_id=None,binding=binding,account=account,profile=profile,
                execution_version=None,transport=self.transports['MEDIA']).items():object.__setattr__(request,name,value)
        self._chat_requests[id(request)]=request;return request

    def verify_request_material(self,request:DailyRequest,uow:UnitOfWork|None=None):
        """Each native material owner rechecks its own actual immutable lease."""
        lease=request.material
        if type(lease) is ImageInput:
            if request.binding.role!='MEDIA':raise InvalidData()
            return lease.verify(uow)
        if type(lease) is not DailyMaterialReadLease or request.binding.role=='MEDIA':raise InvalidData()
        return self.materials.verify_lease(lease,uow)

    def release_request(self,request:DailyRequest) -> None:
        """Release an unused frozen request without consuming a request slot."""
        if self._chat_requests.get(id(request)) is not request or self._chat_task is not None:raise InvalidData()
        del self._chat_requests[id(request)]

    def release_unused_generation(self,request:DailyRequest) -> None:
        """Release an original description only after its actual execution ended."""
        if type(request) is not DailyRequest or request.owner is not self:raise InvalidData()
        if self._chat_requests.get(id(request)) is request:self.release_request(request)

    def generation_consumers_ended(self,owner_ref:str) -> bool:
        """Observe this exact business owner's retained original request material."""
        if any(request.description['work_id']==owner_ref for request in self._chat_requests.values()):return False
        return self._chat_pending is None or self._chat_pending.request.description['work_id']!=owner_ref

    def participate_unsent_daily(self,uow:UnitOfWork,operation_key:str,owner_ref:str) -> bool:
        """Attest absence only after this original owner's actual registration ended."""
        if not self.generation_consumers_ended(owner_ref):return False
        request_id=identity('daily-request',self.instance,operation_key)
        return not self._read_views.stage('transaction_requests',uow,{'request_id':request_id})

    def verify_persona_retry(self,uow:UnitOfWork,run:StoredRecord) -> None:
        """Fence an explicit new generation on actual terminal, cleanup and costs."""
        if not self.ready or self.cleanup_pending or self._chat_reconciliation_failed:
            raise OwnerFailure('RESOURCE_BUSY','provider','CLEANUP_PENDING',self.cleanup_pending)
        if not self.generation_consumers_ended(string(run['object_id'])):raise OwnerFailure('RESOURCE_BUSY','provider','CLEANUP_PENDING',True)
        rid=run['provider_request_id']
        if rid is None:
            if not self.participate_unsent_daily(uow,string(run['provider_operation_key']),string(run['object_id'])):raise InvalidData()
        else:
            rows=self._read_views.stage('transaction_requests',uow,{'request_id':rid})
            if len(rows)!=1:raise InvalidData()
            request=self.ledger._decode('requests',rows[0])
            if (request['task_role']!='PERSONA' or request['operation_key']!=run['provider_operation_key'] or as_record(request['attribution'])['run_id']!=run['object_id']
                    or request['phase']!='TERMINAL' or request['account_id']!=run['account_id']):raise InvalidData()
            self.verify_original_receipt(uow,request)
            rows=self._read_views.stage('transaction_reservations',uow,{'request_id':rid})
            if len(rows)!=1:raise InvalidData()
            reservation=self.ledger._decode('reservations',rows[0])
            if reservation['held_atoms']!=0 or reservation['billing_mode']!='USAGE_ONLY_TRIAL' and reservation['cost_complete'] is not True:
                raise OwnerFailure('RESOURCE_BUSY','budget','LIABILITY_UNRESOLVED')
            if request['outcome']=='SUCCEEDED':
                rows=self._read_views.stage('transaction_handoffs',uow,{'request_id':rid})
                if len(rows)!=1:raise InvalidData()
                if as_record(self.ledger._decode('handoffs',rows[0])['embedding_cleanup'])['state']!='RETIRED':
                    raise OwnerFailure('RESOURCE_BUSY','handoff','CLEANUP_PENDING',False)
        rows=self._read_views.stage('transaction_unsent_budget',uow,{'caller_scope':self.instance,'account_id':run['account_id'],'window_id':run['window_id']})
        if len(rows)>1:raise InvalidData()
        if rows:
            budget=self.ledger._decode('budget_windows',rows[0]);account=as_record(budget['policy'])
            from .text_accounting import liability
            profile=next(p for p in self.profiles if p['material_role']=='PERSONA')
            amount=0 if account['billing_mode']=='USAGE_ONLY_TRIAL' else liability(account,profile)[0]
            explicit=None if account['price'] is None else as_record(account['price'])['per_attempt_money_bound']
            if explicit is not None:amount=max(amount,cast(int,explicit))
            if budget['risk_state']!='CLEAR' or budget['held_atoms']!=0 or cast(int,budget['attempt_count'])>=cast(int,account['attempt_limit']) or account['billing_mode']!='USAGE_ONLY_TRIAL' and cast(int,budget['known_subtotal_atoms'])+amount>cast(int,account['cost_limit_atoms']):
                raise OwnerFailure('RESOURCE_BUSY','budget','CAPACITY_REACHED')

    async def execute_daily(self,kind:str,key:str,payload:object) -> object:
        """Confirm the original local command before any possible first execution."""
        definition=next(d for d in self.chat_commands.commands if d.operation_kind==kind)
        command=ResultBoundCommand(definition.command_version,plain(row({'operation_id':key,**cast(dict,payload)})), {'provider_change':{'actor':'daily_provider'}})
        port=self.chat_operations[kind];prior=await port.resolve_operation(port.recovery_handle(key,command))
        if type(prior) is not NotCommitted or prior.error is not None:return prior
        if self.ledger.assembly.managed_format and kind != 'register_daily_request':
            if self.ledger.lease is None:raise InvalidData()
            return await self.storage.reconcile_managed_provider(self.ledger.lease,port,key,command)
        return await port.execute(key,command)

    async def send_generation(self,request:DailyRequest) -> object:
        """One registered attempt, exact original key, no automatic repair or retry."""
        if self._chat_requests.get(id(request)) is not request or type(request) is not DailyRequest or request.owner is not self:raise InvalidData()
        self.checkpoint()
        if self._closed or self._chat_task is not None or self._chat_pending is not None or self._chat_cleanup is not None or self._active is not None or self._chat_reconciliation_failed:
            raise OwnerFailure('RESOURCE_BUSY','provider','CLEANUP_PENDING',True)
        if not self.authorize(request,None):raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
        self.verify_request_material(request)
        maximum=60 if request.binding.role in ('GOAL_DEDUP','MEDIA') else 1200
        remaining=(cast(int,request.description['deadline_at_us'])-time.time_ns()//1000)/1000000
        if not 0<remaining<=maximum:raise OwnerFailure('TIMEOUT','request','DEADLINE_EXCEEDED')
        deadline=min(time.monotonic()+min(remaining,60),request.material.deadline)
        if self.network is None:raise InvalidData()
        # Network admission and ownership precede the first asynchronous read.
        task,logical=start_owned(self._execute_chat_owned(request,deadline));self._chat_task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._chat_task is job:self._chat_task=None;self._chat_registration=None;self._chat_requests.pop(id(request),None)
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        return logical.result() if done else MappingProxyType({'state':'PENDING','cleanup_pending':True})

    async def _execute_chat_owned(self,request:DailyRequest,deadline:float) -> object:
        with CompletionScope() as actual:
            try:return await self._execute_chat(request,deadline)
            finally:
                await actual.wait()
                # Reconciliation failure must not replace an already known
                # receipt or the original logical failure. The slot stays held.
                try:await self.reconcile_daily_network()
                except (LedgerFailure,OwnerFailure,InvalidData):self._chat_reconciliation_failed=True

    async def observe_network_terminal(self,request:Record) -> None:
        """Generation and embedding share the same durable package stop rule."""
        if self.trial_authorization is not None and self.trial_authorization.dream and (request['phase']=='REMOTE_RESULT_UNKNOWN' or request['outcome']!='SUCCEEDED'):
            await self.trial_authorization.stop('REMOTE_UNKNOWN' if request['phase']=='REMOTE_RESULT_UNKNOWN' else 'PROVIDER_FAILURE',cast(str,request['object_id']))

    async def reconcile_daily_network(self) -> None:
        permit=self._chat_permit
        if permit is None:return
        if self.network is None:raise InvalidData()
        request=await self.ledger.get('requests',permit.request_id)
        if request is None:
            self.network.cancel_unregistered(permit);self._chat_permit=None;self._chat_input=None
        elif request['phase'] in ('TERMINAL','REMOTE_RESULT_UNKNOWN'):
            await self.observe_network_terminal(request)
            if request['phase']=='REMOTE_RESULT_UNKNOWN':self.network.block_account(permit.account_id)
            self.network.confirm_terminal(permit)
            if request['outcome']=='SUCCEEDED':
                handoff=await self.ledger.get('handoffs',cast(str,request['handoff_id']))
                if handoff is None or as_record(handoff['embedding_cleanup'])['state']!='RETIRED':return
            if self._chat_pending or self._chat_readers or self._chat_results or self._chat_cleanup:return
            if self._chat_input is not None:
                lease=self._chat_input.material
                if type(lease) is ImageInput:active=lease.reader_active()
                elif type(lease) is DailyMaterialReadLease:active=lease.issuer.reader_active(lease)
                else:raise InvalidData()
                if active:return
            self.network.confirm_consumed(permit);self._chat_permit=None;self._chat_input=None
        self._chat_reconciliation_failed=False

    async def _execute_chat(self,request:DailyRequest,deadline:float) -> object:
        authorization_failure=False
        with DeadlineScope(deadline):
            original=await self.ledger.get('requests',request.request_id)
            if original is not None:
                if original['fingerprint']!=request.fingerprint:raise OwnerFailure('CONFLICT','request','BINDING_MISMATCH')
                return MappingProxyType({'state':original['phase'],'request_id':request.request_id,'new_sends':0})
            check_deadline();self.verify_request_material(request)
            if not self.authorize(request,None):raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
            if self.network is None:raise InvalidData()
            self._chat_permit=self.network.reserve(request.request_id,string(request.description['original_request_key']),cast(str,request.account['account_id']),deadline,consumer_required=True)
            self._chat_input=request
            account=request.account;amount=reservation_amount(request)
            if await self.ledger.read('requests_blocked',{'account_id':account['account_id']}):raise OwnerFailure('RESOURCE_BUSY','account','ORIGINAL_RESULT_UNCONFIRMED')
            bid=identity('daily-budget',cast(str,account['account_id']),cast(str,account['window_id']))
            budget=await self.ledger.get('budget_windows',bid)
            if budget is None:
                budget=row({'object_id':bid,'revision':1,'account_id':account['account_id'],'window_id':account['window_id'],'policy':account,
                    'attempt_count':0,'known_subtotal_atoms':0,'held_atoms':0,'risk_state':'CLEAR','format_version':self.ledger.assembly.version,
                    'quota_reserved':0,'quota_known':None if account['billing_mode']=='USAGE_ONLY_TRIAL' else 0,'quota_held':0,'billing_mode':account['billing_mode']})
                await self._commit('initialize_budget',identity('daily-budget-initialize',bid),(Mutation('budget_windows',None,budget),),None,None,'NONE','CLEAR',True)
            if (budget['risk_state']!='CLEAR' or budget['held_atoms']!=0 or cast(int,budget['attempt_count'])>=cast(int,account['attempt_limit'])
                    or account['billing_mode']!='USAGE_ONLY_TRIAL' and cast(int,budget['known_subtotal_atoms'])+amount>cast(int,account['cost_limit_atoms'])):raise OwnerFailure('RESOURCE_BUSY','budget','CAPACITY_REACHED')
            changes=registration(request,budget,self._now());self._chat_registration=request,changes
            await self.before_first_registration(request)
            check_deadline()
            registered=await self.execute_daily('register_daily_request',identity('daily-register',request.request_id),
                {'request_ref':{'request_id':request.request_id,'attempt_id':request.attempt_id},'original_request_digest':request.fingerprint})
            if type(registered) is not Committed:return registered
            try:await self.after_first_registration(request,registered.receipt)
            except (OwnerFailure,InvalidValue,InvalidData):authorization_failure=True
        req,attempt,budget,reservation=(c.current for c in changes)
        start=time.monotonic();not_sent=False
        if authorization_failure or self._closed or start>=deadline or not self.authorize(request,None):
            from .deepseek_protocol import observe_usage
            metering=usage(observe_usage(None),request,not_sent=True);outcome='FAILED' if authorization_failure else 'MODE_BLOCKED';reason='COMMIT_UNCONFIRMED' if authorization_failure else 'OPERATION_NOT_GRANTED';value=None;not_sent=True
        else:
            if self.network is None or self._chat_permit is None:raise InvalidData()
            transport=request.transport
            try:
                self.executions+=1
                wire=await self.network.start(self._chat_permit,lambda:transport.exchange(request.wire,min(deadline,start+30),self._cancel.token))
            except OwnerFailure:
                from .chat_transport import WireObservation
                wire=WireObservation('NOT_SENT',None,None,'OPERATION_NOT_GRANTED')
            if wire.state=='REMOTE_RESULT_UNKNOWN':return await self._mark_unknown(req,attempt)
            not_sent=wire.state=='NOT_SENT'
            parsed=decode_dream_response(wire.body or b'',request.binding) if type(request.binding) is DreamChatBinding else decode_daily_response(wire.body or b'',cast(DailyChatBinding,request.binding))
            if parsed.outcome=='REMOTE_RESULT_UNKNOWN':return await self._mark_unknown(req,attempt)
            metering=usage(parsed.usage,request,not_sent=not_sent)
            value=parsed.result if wire.state=='RESPONSE' and wire.status==200 else None
            outcome=parsed.outcome if value is not None or parsed.outcome in ('SENSITIVE_REFUSAL','OTHER_REFUSAL') else 'FAILED'
            if not_sent:outcome='FAILED'
            reason=wire.reason or parsed.outcome if wire.status in (None,200) else 'PROVIDER_HTTP_FAILURE'
            if wire.status in (401,403):reason='AUTHENTICATION_FAILED'
            elif wire.status in (400,404,405,415,422):reason='INPUT_FORMAT_UNSUPPORTED'
        handoff_id=identity('daily-handoff',request.request_id) if value is not None else None
        material=split(value,request.binding,handoff_id=cast(str,handoff_id),request_id=request.request_id,attempt_id=request.attempt_id) if value is not None else None
        cause=None if value is not None else {'code':'PROVIDER_FAILED','field':'result','reason':reason}
        observed=row(dict(attempt)|{'revision':2,'updated_at':self._now(),'adapter_duration_ms':int((time.monotonic()-start)*1000),
            'confirmed_started':not not_sent,'usage':metering,'result_fingerprint':material.payload['payload_digest'] if material else None,'evidence_revision':1,'first_error':cause})
        ended=row(dict(observed)|{'revision':3,'state':'NOT_SENT' if not_sent else 'COMPLETED','logical_outcome':outcome,
            'handoff_id':handoff_id,'first_error':cause,'terminal_error':cause})
        ended_req=row(dict(req)|{'revision':2,'phase':'TERMINAL','outcome':outcome,'first_error':cause,'handoff_id':handoff_id,'updated_at':self._now()})
        terminal=(Mutation('requests',req,ended_req),Mutation('attempts',observed,ended),*settlement(budget,reservation,metering,request.attempt_id))
        if material is not None:
            handoff=row({'object_id':handoff_id,'revision':1,'request_id':request.request_id,'owner_id':req['result_owner'],'checksum':material.payload['payload_digest'],
                'source':'REMOTE_PROVIDER','artifact_id':identity('daily-artifact',request.request_id),'format_version':self.ledger.assembly.version,'created_at':self._now(),
                'embedding_payload':material.payload,'embedding_cleanup':{'received_receipt':None,'retired_through':None,'state':'HELD'},'payload':''})
            terminal+= (Mutation('handoffs',None,handoff),)
        self._chat_pending=_Pending(request,terminal,(Mutation('attempts',attempt,observed),),material)
        return await self.confirm_daily_completion()

    async def confirm_daily_completion(self) -> object:
        """Persist only the retained original observation; never invoke transport."""
        pending=self._chat_pending
        if pending is None:raise InvalidData()
        request=pending.request
        if pending.evidence is None:
            pending.evidence=await self._commit('evidence',identity('daily-evidence',request.request_id),pending.evidence_changes,request.request_id,request.attempt_id,
                'PREPARED','PREPARED',bool(as_record(pending.evidence_changes[0].current['usage'])['cost_complete']))
        if pending.material is None:
            receipt=await self._commit('terminate',identity('daily-terminate',request.request_id),pending.changes,request.request_id,request.attempt_id,
                'OPEN','TERMINAL',bool(as_record(pending.changes[1].current['usage'])['cost_complete']))
            self._chat_pending=None;return receipt
        outcome=await self.execute_daily('store_daily_handoff',identity('daily-complete',request.request_id),
            {'request_ref':{'request_id':request.request_id,'attempt_id':request.attempt_id},'original_request_digest':request.fingerprint,
                'terminal_evidence_ref':self.reference(pending.evidence)})
        if type(outcome) is Committed:self._chat_pending=None
        return outcome

    def _apply_daily_changes(self,uow:UnitOfWork,changes:tuple[Mutation,...]) -> None:
        self.ledger._apply(uow,cast(tuple[StoredRecord,...],tuple(MappingProxyType({'table':m.table,'object_id':m.current['object_id'],
            'expected_revision':None if m.previous is None else m.previous['revision'],
            'body':dump(MappingProxyType({k:v for k,v in m.current.items() if k!='payload'})),'payload':m.current.get('payload')}) for m in changes)))

    def _daily_result(self,key:str,state:str,changes:tuple[Mutation,...],request:Record,attempt:Record):
        first=changes[0];metering=as_record(attempt['usage'])
        return {'operation_id':key,'state':state,'targets':tuple({'object_id':m.current['object_id'],'previous_revision':m.previous['revision'] if m.previous else None,
            'revision':m.current['revision']} for m in changes),'fact':{'request_id':request['object_id'],'attempt_id':attempt['object_id'],
            'previous_revision':first.previous['revision'] if first.previous else None,'revision':first.current['revision'],
            'previous_state':'NONE' if state=='REGISTERED' else 'OPEN' if state=='STORED' else 'TERMINAL','state':'PREPARED' if state=='REGISTERED' else 'TERMINAL',
            'cost_complete':metering['cost_complete'],'billing_mode':metering['billing_mode'],'currency':'CNY','quota_known':metering['quota_known'],'quota_held':0,'config_snapshot_id':self.configuration.snapshot_id}}

    def _handle_daily(self,kind:str,uow:UnitOfWork,payload:StoredRecord):
        operation=self.storage.cognition_operation_context(uow,self.ledger.assembly.repository.definition)
        if operation.scope_id!='provider' or operation.operation_kind!=kind:raise InvalidData()
        if kind=='register_daily_request':
            active=self._chat_registration
            if active is None or self._closed:raise InvalidData()
            request,changes=active
            if payload['original_request_digest']!=request.fingerprint or payload['request_ref']!={'request_id':request.request_id,'attempt_id':request.attempt_id}:raise InvalidData()
            self.verify_request_material(request,uow)
            if type(self.configuration) is StoredManagedConfiguration and (self.managed_dispatch is None or not self.managed_dispatch()):raise OwnerFailure('ACCESS_DENIED','authorization','OPERATION_NOT_GRANTED')
            if not self.authorize(request,uow):raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
            uow.require_commit_permission(lambda:not self._closed and self.authorize(request,None)
                and (type(self.configuration) is not StoredManagedConfiguration or self.managed_dispatch is not None and self.managed_dispatch())
                and time.time_ns()//1000<cast(int,request.description['deadline_at_us']))
            self._apply_daily_changes(uow,changes)
            return self._daily_result(string(payload['operation_id']),'REGISTERED',changes,changes[0].current,changes[1].current)
        if kind=='store_daily_handoff':
            pending=self._chat_pending
            if pending is None or pending.material is None or pending.evidence is None:raise InvalidData()
            request=pending.request
            if payload['original_request_digest']!=request.fingerprint or payload['request_ref']!={'request_id':request.request_id,'attempt_id':request.attempt_id}:raise InvalidData()
            definition=next(d for d in self.ledger.assembly.commands if d.operation_kind=='evidence')
            receipt=self.storage.confirm_prior_operation(uow,definition,pending.evidence.identity.operation_key)
            if receipt is None or receipt!=pending.evidence or payload['terminal_evidence_ref']!=self.reference(receipt):raise InvalidData()
            self._apply_daily_changes(uow,pending.changes)
            for leaf in pending.material.leaves:self.rows.write('embedding_handoff_leaf',uow,leaf)
            return self._daily_result(string(payload['operation_id']),'STORED',pending.changes,pending.changes[0].current,pending.changes[1].current)
        return self._daily_cleanup(kind,uow,payload)

    def _daily_cleanup(self,kind:str,uow:UnitOfWork,payload:StoredRecord):
        reference=cast(StoredRecord,payload['request_ref']);request=self._root(uow,'requests',string(reference['request_id']))
        attempt=self._root(uow,'attempts',string(reference['attempt_id']))
        if request['caller_scope']!=self.instance or request['capability']=='EMBEDDING' or request['phase']!='TERMINAL' or request['outcome']!='SUCCEEDED' or attempt['request_id']!=request['object_id']:raise InvalidData()
        handoff=self._root(uow,'handoffs',cast(str,request['handoff_id']));cleanup=as_record(handoff['embedding_cleanup'])
        if attempt['handoff_id']!=handoff['object_id'] or handoff['request_id']!=request['object_id']:raise InvalidData()
        if kind=='confirm_daily_handoff':
            if cleanup['state']!='HELD' or not self.received(uow,request,handoff,cast(StoredRecord,payload['receipt'])):raise InvalidData()
            updated=dict(cleanup)|{'state':'RELEASABLE','received_receipt':payload['receipt']};state='RECEIVED'
        else:
            if (kind!='retire_daily_handoff' or cleanup['state']!='RELEASABLE' or handoff['revision']!=payload['expected_handoff_revision'] or cleanup['retired_through']!=payload['after_ordinal']):raise InvalidData()
            if self._chat_readers or any(r.request['object_id']==request['object_id'] for r in self._chat_results.values()):raise OwnerFailure('RESOURCE_BUSY','handoff','CLEANUP_PENDING',True)
            start=0 if cleanup['retired_through'] is None else cast(int,cleanup['retired_through'])+1
            count=cast(int,as_record(handoff['embedding_payload'])['leaf_count']);end=min(start+4,count)
            if start>=end:raise InvalidData()
            for ordinal in range(start,end):
                key=identity('embedding-handoff-leaf',cast(str,handoff['object_id']),ordinal);leaf=self.rows.get('embedding_handoff_leaf',uow,key)
                if leaf is None or leaf['request_id']!=request['object_id'] or leaf['attempt_id']!=attempt['object_id']:raise InvalidData()
                self.rows.remove('embedding_handoff_leaf',uow,key,1)
            updated=dict(cleanup)|{'retired_through':end-1,'state':'RETIRED' if end==count else 'RELEASABLE'};state='RETIRED' if end==count else 'RETIRING'
        changes=(Mutation('handoffs',handoff,row(dict(handoff)|{'revision':cast(int,handoff['revision'])+1,'embedding_cleanup':updated})),)
        self._apply_daily_changes(uow,changes)
        return self._daily_result(string(payload['operation_id']),state,changes,request,attempt)

    async def cleanup_daily(self,request_id:str,received_receipt:Receipt,deadline:float):
        """Confirm actual consumption and retire at most three original leaf pages."""
        if self._chat_cleanup is not None:raise OwnerFailure('RESOURCE_BUSY','handoff','CLEANUP_PENDING',True)
        async def clean():
            with DeadlineScope(deadline):
                request=await self.ledger.get('requests',request_id)
                if request is None or request['caller_scope']!=self.instance or request['outcome']!='SUCCEEDED' or request['task_role'] not in self.bindings and request['task_role'] not in self.dream_bindings:raise InvalidData()
                attempts=await self.ledger.read('attempts_for_request',{'request_id':request_id})
                if len(attempts)!=1:raise InvalidData()
                reference={'request_id':request_id,'attempt_id':attempts[0]['object_id']}
                for _ in range(4):
                    check_deadline()
                    handoff=await self.ledger.get('handoffs',cast(str,request['handoff_id']))
                    if handoff is None:raise InvalidData()
                    cleanup=as_record(handoff['embedding_cleanup'])
                    if cleanup['state']=='RETIRED':return Found(MappingProxyType({'state':'RETIRED','cleanup_pending':False,'new_sends':0}))
                    if cleanup['state']=='HELD':
                        outcome=await self.execute_daily('confirm_daily_handoff',identity('daily-received',request_id),
                            {'request_ref':reference,'receipt':self.reference(received_receipt)})
                    else:
                        outcome=await self.execute_daily('retire_daily_handoff',identity('daily-retire',request_id,cast(int,handoff['revision'])),
                            {'request_ref':reference,'expected_handoff_revision':handoff['revision'],'after_ordinal':cleanup['retired_through']})
                    if type(outcome) is not Committed:return outcome
                handoff=await self.ledger.get('handoffs',cast(str,request['handoff_id']))
                if handoff is None or as_record(handoff['embedding_cleanup'])['state']!='RETIRED':raise InvalidData()
                return Found(MappingProxyType({'state':'RETIRED','cleanup_pending':False,'new_sends':0}))
        task,logical=start_owned(clean());self._chat_cleanup=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._chat_cleanup is job:self._chat_cleanup=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','handoff','DEADLINE_EXCEEDED',True)
        value=logical.result()
        # The receipt can be known before the actual leaf writer/consumer tail.
        # Wait only for this cleanup job and preserve its original logical fact.
        if not task.done():await asyncio.wait((task,),timeout=max(0,deadline-time.monotonic()))
        if type(value) is Found:
            if task.done():await self.reconcile_daily_network()
            return Found(MappingProxyType(dict(value.value)|{'cleanup_pending':not task.done()}))
        return value

    async def wait_generation_actual(self,request:DailyRequest) -> None:
        """Observe this issued request's actual end; no new operation is started."""
        if type(request) is not DailyRequest or request.owner is not self:raise InvalidData()
        task=self._chat_task if self._chat_requests.get(id(request)) is request else None
        if task is not None:
            await asyncio.wait((task,))

    async def lookup_daily(self,key:str,fingerprint:str,deadline:float):
        """Observe only the original key and verified receipt without send admission."""
        if self._closed or len(self._chat_lookups)>=2:raise OwnerFailure('RESOURCE_BUSY','lookup','ADMISSION_FULL')
        async def read():
            with DeadlineScope(deadline):
                rid=identity('daily-request',self.instance,key);request=await self.ledger.get('requests',rid)
                if request is None:
                    from companion_memory.persistence import NotFound
                    return NotFound()
                if request['caller_scope']!=self.instance or request['fingerprint']!=fingerprint or request['operation_key']!=key:raise InvalidData()
                if request['phase']=='TERMINAL' and request['outcome']=='SUCCEEDED':
                    port=self.chat_operations['store_daily_handoff'];operation_key=identity('daily-complete',rid)
                elif request['phase']=='TERMINAL':
                    port=self.ledger.operations['terminate'];operation_key=identity('daily-terminate',rid)
                elif request['phase']=='REMOTE_RESULT_UNKNOWN':
                    port=self.ledger.operations['recover'];operation_key=identity('embedding-unknown',rid)
                else:
                    port=self.chat_operations['register_daily_request'];operation_key=identity('daily-register',rid)
                found=await port.read_receipt(operation_key)
                if type(found) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
                return Found(request)
        task,logical=start_owned(read());self._chat_lookups.add(task)
        def ended(job):
            if not job.cancelled():job.exception()
            self._chat_lookups.discard(job)
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','lookup','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def read_daily_request(self,key:str,role:str,owner_ref:str,deadline:float):
        """Read an existing role-bound key for local reception and cleanup only."""
        if self._closed or self._chat_lookups:raise OwnerFailure('RESOURCE_BUSY','lookup','CLEANUP_PENDING',True)
        async def read():
            with DeadlineScope(deadline):
                request=await self.ledger.get('requests',identity('daily-request',self.instance,key))
                if request is None:
                    from companion_memory.persistence import NotFound
                    return NotFound()
                if request['operation_key']!=key or request['task_role']!=role or request['caller_scope']!=self.instance or as_record(request['attribution'])['run_id']!=owner_ref:raise InvalidData()
                return Found(request)
        task,logical=start_owned(read());self._chat_lookups.add(task)
        def ended(job):
            if not job.cancelled():job.exception()
            self._chat_lookups.discard(job)
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','lookup','DEADLINE_EXCEEDED',True)
        return logical.result()

    def participate_original(self,uow:UnitOfWork,request_id:str,*,role:str,owner_ref:str,operation_key:str,request_digest:str) -> Record:
        """Read the exact original request in its actual business consumer scope."""
        roots=self._read_views.stage('transaction_requests',uow,{'request_id':request_id})
        if len(roots)!=1:raise InvalidData()
        request=self.ledger._decode('requests',roots[0])
        if (request['task_role']!=role or request['caller_scope']!=self.instance or as_record(request['attribution'])['run_id']!=owner_ref
                or request['operation_key']!=operation_key or request['fingerprint']!=request_digest):raise InvalidData()
        return request

    def verify_original_receipt(self,uow:UnitOfWork,request:Record) -> Receipt:
        """A consumer verifies the actual Provider operation behind a known root."""
        rid=cast(str,request['object_id'])
        if request['phase']=='TERMINAL' and request['outcome']=='SUCCEEDED':
            kind='store_daily_handoff';key=identity('daily-complete',rid)
        elif request['phase']=='TERMINAL':kind='terminate';key=identity('daily-terminate',rid)
        elif request['phase']=='REMOTE_RESULT_UNKNOWN':kind='recover';key=identity('embedding-unknown',rid)
        else:kind='register_daily_request';key=identity('daily-register',rid)
        definitions=self.chat_commands.commands if kind in self.chat_operations else self.ledger.assembly.commands
        definition=next(d for d in definitions if d.operation_kind==kind)
        receipt=self.storage.confirm_cognition_provider_operation(uow,definition,key,rid)
        if receipt is None:raise InvalidData()
        return receipt

    async def recover_daily_result(self,request_id:str,deadline:float) -> DailyResult:
        """Retain a complete original output with bounded actual read ownership."""
        if self._closed or len(self._chat_readers)+len(self._chat_results)>=2:raise OwnerFailure('RESOURCE_BUSY','handoff','ADMISSION_FULL')
        task,logical=start_owned(self._read_daily_result(request_id,deadline));self._chat_readers.add(task)
        def ended(job):
            if not job.cancelled():job.exception()
            self._chat_readers.discard(job)
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','handoff','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def _read_daily_result(self,request_id:str,deadline:float) -> DailyResult:
        with DeadlineScope(deadline):
            request=await self.ledger.get('requests',request_id)
            if (request is None or request['caller_scope']!=self.instance or request['phase']!='TERMINAL' or request['outcome']!='SUCCEEDED'
                    or request['task_role'] not in self.bindings and request['task_role'] not in self.dream_bindings):raise OwnerFailure('PRECONDITION_FAILED','handoff','ORIGINAL_RESULT_UNCONFIRMED')
            attempts=await self.ledger.read('attempts_for_request',{'request_id':request_id})
            if len(attempts)!=1:raise InvalidData()
            attempt=attempts[0];handoff=await self.ledger.get('handoffs',cast(str,request['handoff_id']))
            if (handoff is None or handoff['request_id']!=request_id or handoff['owner_id']!=request['result_owner']
                    or attempt['handoff_id']!=handoff['object_id'] or attempt['state']!='COMPLETED' or attempt['logical_outcome']!='SUCCEEDED'
                    or handoff['checksum']!=attempt['result_fingerprint'] or as_record(handoff['embedding_cleanup'])['state']!='HELD'):
                raise InvalidData()
            metadata=as_record(handoff['embedding_payload']);leaves=[]
            for ordinal in range(cast(int,metadata['leaf_count'])):
                check_deadline()
                leaf=await self.rows.read('embedding_handoff_leaf',identity('embedding-handoff-leaf',cast(str,handoff['object_id']),ordinal))
                if leaf is None:raise InvalidData()
                leaves.append(leaf)
            value=restore(metadata,leaves,(self.dream_bindings if request['task_role'] in DREAM_ROLES else self.bindings)[cast(str,request['task_role'])],handoff_id=cast(str,handoff['object_id']),request_id=request_id,attempt_id=cast(str,attempt['object_id']))
            found=await self.chat_operations['store_daily_handoff'].read_receipt(identity('daily-complete',request_id))
            if type(found) is Failed:raise LedgerFailure(found)
            if type(found) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
            if not all(any(t['object_id']==root['object_id'] and t['revision']==root['revision'] for t in cast(tuple[StoredRecord,...],cast(StoredRecord,found.value.result)['targets'])) for root in (request,attempt,handoff)):
                raise InvalidData()
            if await self.ledger.get('handoffs',cast(str,handoff['object_id']))!=handoff:raise OwnerFailure('CONFLICT','handoff','BINDING_MISMATCH')
            if self._closed:raise OwnerFailure('INVALID_STATE','handoff','SERVICE_CLOSED')
            result=object.__new__(DailyResult)
            for name,item in dict(owner=self,request=request,attempt=attempt,handoff=handoff,completion=found.value,value=value).items():object.__setattr__(result,name,item)
            self._chat_results[id(result)]=result
            return result

    def verify_daily_result(self,uow:UnitOfWork,result:DailyResult,*,role:str,owner_ref:str,operation_key:str,request_digest:str) -> None:
        """The actual consumer atomically rechecks original roots, receipt and role."""
        if type(result) is not DailyResult or self._chat_results.get(id(result)) is not result or result.owner is not self:raise InvalidData()
        request=result.request
        if (request['task_role']!=role or as_record(request['attribution'])['run_id']!=owner_ref or request['operation_key']!=operation_key
                or request['fingerprint']!=request_digest or request['caller_scope']!=self.instance):raise InvalidData()
        for table,expected in (('requests',result.request),('attempts',result.attempt),('handoffs',result.handoff)):
            roots=self._read_views.stage('transaction_'+table,uow,{'request_id':request['object_id']})
            if len(roots)!=1 or self.ledger._decode(table,roots[0])!=expected:raise InvalidData()
        definition=next(d for d in self.chat_commands.commands if d.operation_kind=='store_daily_handoff')
        receipt=self.storage.confirm_cognition_provider_operation(uow,definition,result.completion.identity.operation_key,cast(str,request['object_id']))
        if receipt!=result.completion:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')

    def release_daily_result(self,result:DailyResult) -> None:
        """Release only this native consumer; complete durable leaves remain held."""
        if type(result) is not DailyResult or self._chat_results.get(id(result)) is not result:raise InvalidData()
        del self._chat_results[id(result)]

    @property
    def cleanup_pending(self) -> bool:
        return super().cleanup_pending or bool(self._chat_task or self._chat_pending or self._chat_readers or self._chat_results or self._chat_permit or self._chat_lookups or self._chat_cleanup)

    async def close(self,deadline:float) -> bool:
        self._closed=True;self._cancel.cancel()
        if self._chat_task is not None:
            done,_=await asyncio.wait((self._chat_task,),timeout=max(0,deadline-time.monotonic()))
            if not done:return False
        if self._chat_pending or self._chat_readers or self._chat_results or self._chat_lookups or self._chat_cleanup:return False
        if self._chat_permit is not None:
            await self.reconcile_daily_network()
            if self._chat_permit is not None:
                if self.network is None:return False
                if self._chat_input is not None:
                    lease=self._chat_input.material
                    if type(lease) is ImageInput:active=lease.reader_active()
                    elif type(lease) is DailyMaterialReadLease:active=lease.issuer.reader_active(lease)
                    else:raise InvalidData()
                    if active:return False
                observed=self.network.close()
                if observed.io_pending or observed.terminal_confirmation_pending:return False
                self.network.detach_closed(self._chat_permit);self._chat_permit=None;self._chat_input=None
        complete=await super().close(deadline)
        if complete and self.trial_authorization is not None:return self.trial_authorization.close()
        return complete
