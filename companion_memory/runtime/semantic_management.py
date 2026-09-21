"""Explicit semantic management through native transaction and Provider ports.

The coordinator owns one actual work task. It restores the first submitted
command from durable admission before replay, and separates result reception,
financial completion, artifact application and actual handoff retirement.
"""
from __future__ import annotations
import asyncio
import time
from collections.abc import Callable,Awaitable
from contextlib import nullcontext
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.persistence import Committed,Found,Receipt,ResultBoundCommand,Failed
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.deadlines import DeadlineScope,current_deadline,check_deadline
from companion_memory.persistence.completion import CompletionScope,start_owned
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import Record,identity,isolate,string,number
from companion_memory.persistence.record_primitives import COMMAND_INPUT
from companion_memory.memory.formats import record
from companion_memory.ingress.events import plain
from companion_memory.retrieval.semantic_commands import PAYLOADS
from companion_memory.retrieval.semantic_material import render_document,cache_key
from companion_memory.retrieval.semantic_work import SemanticWork
from .semantic_results import outcome,LocalConfirmation,LocalFailure
if TYPE_CHECKING:
    from .semantic_host import SemanticHost
    from .daily_host import DailyCognitionHost


class SemanticManagementPort:
    """Trusted host's bounded management, with no implicit activation or sends."""
    def __init__(self,host:SemanticHost|DailyCognitionHost):
        self.host=host;owner=host.combination.work;assert owner is not None
        self.owner:SemanticWork=owner
        self._task:asyncio.Task[Record|LocalFailure]|None=None
        self._local_task:asyncio.Task[object]|None=None
        self.definitions={d.operation_kind:d for d in host.combination.semantic.commands if d.owner_namespace=='retrieval'}

    async def control(self) -> Record:
        result=await self.owner.rows.read('semantic_control',self.owner.control_id)
        if result is None:raise ValueError('Semantic control is missing.')
        return result

    async def work(self,work_id:str) -> Record:
        result=await self.owner.rows.read('embedding_work',work_id)
        if result is None:raise ValueError('Original semantic work is missing.')
        if result['kind']=='EMBED' and self.owner.provider.pending_for(work_id):
            return MappingProxyType(dict(result)|{'cleanup_pending':True})
        return result

    def envelope(self,kind:str,key:str,payload:Record,observed_at:int) -> Record:
        """Freeze a closed native envelope before the first local submission."""
        definition=self.definitions[kind];admission=self.host.admission;assert admission is not None
        original=admission.original('retrieval',kind,self.owner.instance,key)
        if original is not None:return isolate(COMMAND_INPUT,original,1048576)
        from companion_memory.persistence.semantic_commands import decode_payload
        body=encode_content(payload,24576).decode();decode_payload(PAYLOADS[kind],body)
        return isolate(COMMAND_INPUT,{'binding_id':self.owner.instance,'request_key':key,'expected':(),
            'observed_at':observed_at,'payload':body},1048576)

    def _command(self,kind:str,envelope:Record) -> ResultBoundCommand:
        definition=self.definitions[kind]
        return ResultBoundCommand(definition.command_version,plain(envelope),{a.event_slot:{'actor':'semantic_host'} for a in definition.required_audits})

    async def execute(self,kind:str,envelope:Record,deadline:float) -> object:
        """Submit exactly the supplied original values; no derived side effects."""
        with DeadlineScope(deadline):
            self.host.checkpoint()
            return await self.host.storage.bind_operation(self.definitions[kind],self.owner.instance).execute(
                string(envelope['request_key']),self._command(kind,envelope))

    async def resolve(self,kind:str,envelope:Record,deadline:float) -> object:
        """Read original confirmation only; never prepare, activate or send."""
        with DeadlineScope(deadline):
            port=self.host.storage.bind_operation(self.definitions[kind],self.owner.instance)
            return await port.resolve_operation(port.recovery_handle(string(envelope['request_key']),self._command(kind,envelope)))

    async def _commit(self,kind:str,key:str,payload:Record) -> Receipt:
        check_deadline()
        envelope=self.envelope(kind,key,payload,time.time_ns()//1000)
        result=await self.execute(kind,envelope,current_deadline(5))
        if type(result) is not Committed:raise LocalConfirmation(result)
        return result.receipt

    @outcome
    async def resume(self,key:str) -> Receipt:
        """Explicitly activate the trusted original package and local scheduler."""
        self.host.normal();authorization=self.host.authorization
        if authorization is None:raise ValueError('Verified package is not bound.')
        authorization.activate();control=await self.control()
        if control['scheduler']=='ENABLED':
            original=await self.host.storage.bind_operation(self.definitions['resume'],self.owner.instance).read_receipt(key)
            if type(original) is Failed:raise LocalConfirmation(original)
            if type(original) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
            self.host.enable_semantic_dispatch();return original.value
        receipt=await self._commit('resume',key,MappingProxyType({'space_id':self.owner.space,'expected_revision':control['revision'],
            'authorization_digest':authorization.grant.digest}))
        self.host.enable_semantic_dispatch();return receipt

    @outcome
    async def prepare_document(self,object_id:str,original_request_key:str,partition_id:str) -> str:
        """Prepare authoritative current bytes and the exact semantic gap."""
        self.host.normal();current,gap=await self.owner.memory.semantic_current(object_id)
        if current is None or gap is None or gap['action']!='UPSERT':raise ValueError('No current document gap.')
        payload=MappingProxyType({'kind':'EMBED','config':self.owner.config,'space_id':self.owner.space,'purpose':'DOCUMENT',
            'object_ref':MappingProxyType({'object_id':object_id,'revision':current['revision']}),'change_seq':gap['latest_change_seq'],
            'partition_id':partition_id,'rendered_text':render_document(current).decode(),'original_request_key':original_request_key})
        return await self._prepare(payload)

    @outcome
    async def prepare_query(self,text:str,original_request_key:str,partition_id:str) -> str:
        """Explicit prewarming preparation; ordinary query never invokes it."""
        self.host.normal()
        return await self._prepare(MappingProxyType({'kind':'EMBED','config':self.owner.config,'space_id':self.owner.space,'purpose':'QUERY',
            'object_ref':None,'change_seq':None,'partition_id':partition_id,'rendered_text':text,'original_request_key':original_request_key}))

    @outcome
    async def prepare_delete(self,object_id:str,revision:int,change_seq:int,deletion_ref:Record) -> str:
        """Prepare only local deletion proof; this path has no Provider access."""
        self.host.checkpoint()
        return await self._prepare(MappingProxyType({'kind':'DELETE_LOCAL','config':self.owner.config,'space_id':self.owner.space,
            'object_ref':MappingProxyType({'object_id':object_id,'revision':revision}),'change_seq':change_seq,'deletion_ref':deletion_ref}))

    async def _prepare(self,payload:Record) -> str:
        work_id=SemanticWork.work_id(payload,self.owner.instance)
        await self._commit('prepare',identity('semantic-prepare',work_id),MappingProxyType(dict(payload)|{'work_id':work_id}))
        return work_id

    @outcome
    async def run_work(self,work_id:str,*,slot_id:str|None=None,_request_deadline_at:int|None=None,_admission_deadline:float|None=None) -> Record|LocalFailure:
        """Advance one original work, rejecting a second actual worker.

        The optional internal monotonic admission bound limits first reservation
        and registration only. Already-started result reception keeps its owner.
        """
        if self._task is not None and not self._task.done():raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
        self._task,result=start_owned(self._run(work_id,slot_id,_request_deadline_at,_admission_deadline))
        return await asyncio.shield(result)

    @outcome
    async def _run(self,work_id:str,slot_id:str|None,request_deadline_at:int|None=None,admission_deadline:float|None=None) -> Record:
        with DeadlineScope(admission_deadline) if admission_deadline is not None else nullcontext():
            check_deadline()
            work=await self.work(work_id)
            check_deadline()
        if work['kind']=='DELETE_LOCAL':
            if work['state']=='LOCAL_PREPARED':
                await self._commit('apply',identity('semantic-apply',work_id),MappingProxyType({'kind':'DELETE_LOCAL','work_id':work_id,
                    'expected_revision':work['revision'],'object_ref':work['object_ref'],'latest_seq':work['change_seq'],'deletion_ref':work['deletion_ref']}))
                work=await self.work(work_id)
            if work['state'] in ('LOCAL_APPLIED','LOCAL_FAILED','LOCAL_SUPERSEDED'):
                await self._commit('record_cleanup',identity('semantic-cleanup',work_id),MappingProxyType({'kind':'DELETE_LOCAL','work_id':work_id,
                    'expected_revision':work['revision'],'completion_receipt':work['completion_ref'],'cleanup_pending':False}))
            return await self.work(work_id)
        provider=self.owner.provider
        await provider.finish_original_pending(work_id,string(work['original_request_key']))
        if work['state']=='PREPARED' and work['intent'] is None:
            # Admission shares the original cold deadline; paid completion does not.
            with DeadlineScope(admission_deadline) if admission_deadline is not None else nullcontext():
                check_deadline()
                self.host.normal();authorization=self.host.authorization
                if authorization is None or slot_id is None:raise ValueError('Original authorized slot required.')
                control=await self.control();self.host.observe_semantic_control(control)
                if not self.host.semantic_dispatch_allowed(control):
                    raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
                check_deadline()
                await provider.check_budget(work)
                check_deadline()
                text=await self.owner.text(work)
                check_deadline()
                old=authorization.original(work_id)
                deadline=number(record(old['intent'])['expires_at']) if old else min(time.time_ns()//1000+60000000,number(authorization.grant.binding['expires_at']),
                    request_deadline_at if request_deadline_at is not None else 2**63-1)
                if deadline<=time.time_ns()//1000:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
                request=provider.request(work,text,deadline)
                intent=authorization.reserve(work,request.fingerprint,deadline,slot_id)
                check_deadline()
                if deadline<=time.time_ns()//1000:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
                release_binding=self.owner.hold_binding(request,intent)
                completion=CompletionScope()
                try:
                    with completion:
                        await self._commit('bind',identity('semantic-bind',work_id),MappingProxyType({'work_id':work_id,'expected_revision':work['revision'],'intent':intent,'deadline_at':deadline}))
                finally:completion.when_ended(release_binding)
                work=await self.work(work_id)
        if work['state'] in ('PREPARED','BOUND') and work['intent'] is not None:
            with DeadlineScope(admission_deadline) if admission_deadline is not None else nullcontext():
                check_deadline()
                request=provider.request(work,await self.owner.text(work),number(work['deadline_at']))
                check_deadline()
                registered=await provider.ledger.get('requests',request.request_id)
                check_deadline()
            if registered is None and number(work['deadline_at'])<=time.time_ns()//1000:
                absence=await provider.verify_unsent(request)
                if absence is None:raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
                release_unsent=self.owner.hold_unsent(work_id,'DEADLINE')
                try:
                    await self._commit('fail',identity('semantic-fail',work_id),MappingProxyType({'kind':'EMBED','work_id':work_id,
                        'expected_revision':work['revision'],'error':'DEADLINE','request_ref':None,'terminal_receipt':None}))
                finally:
                    release_unsent();provider.release_absence(absence)
                if self.host.authorization is not None:self.host.authorization.complete(work_id)
                return await self.work(work_id)
            if registered is None:
                with DeadlineScope(admission_deadline) if admission_deadline is not None else nullcontext():
                    check_deadline()
                    self.host.observe_semantic_control(await self.control())
                    check_deadline()
                    absence=await provider.verify_unsent(request)
                    if absence is None:raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
                    try:check_deadline()
                    except OwnerFailure:
                        provider.release_absence(absence);raise
                # The Provider inherits completion ownership, but only its pre-send
                # registration shares admission's deadline. Late paid results survive.
                if admission_deadline is None:
                    sent=await provider.send_verified_first(request,absence,record(work['intent']))
                else:
                    sent=await provider.send_verified_first(request,absence,record(work['intent']),admission_deadline=admission_deadline)
                if type(sent) not in (Committed,Receipt):
                    if type(sent) is MappingProxyType:
                        raise OwnerFailure('RESOURCE_BUSY','work','OWNER_ACTIVE',True)
                    raise LocalConfirmation(sent)
                registered=await provider.ledger.get('requests',request.request_id)
            if registered is None:raise OwnerFailure('STORAGE_FAILED','work','READ_FAILED',True)
            if registered['phase']=='REMOTE_RESULT_UNKNOWN' or registered['phase']=='TERMINAL' and registered['outcome']!='SUCCEEDED':
                kind='recover' if registered['phase']=='REMOTE_RESULT_UNKNOWN' else 'terminate'
                proof=await provider.ledger.operations[kind].read_receipt(identity('embedding-unknown' if kind=='recover' else 'embedding-terminate',request.request_id))
                if type(proof) is Failed:raise LocalConfirmation(proof)
                if type(proof) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
                reference=MappingProxyType({'request_id':request.request_id,'attempt_id':request.attempt_id})
                await self._commit('fail',identity('semantic-fail',work_id),MappingProxyType({'kind':'EMBED','work_id':work_id,
                    'expected_revision':work['revision'],'error':'KNOWN_PROVIDER_FAILURE','request_ref':reference,'terminal_receipt':provider.reference(proof.value)}))
                work=await self.work(work_id)
            elif registered['phase']!='TERMINAL':raise OwnerFailure('RESOURCE_BUSY','work','OWNER_ACTIVE',True)
        if work['state'] in ('PREPARED','BOUND') and work['intent'] is not None:
            request=provider.request(work,await self.owner.text(work),number(work['deadline_at']))
            received=await provider.recover_result(request.request_id);release_received=self.owner.hold_received(received)
            completion=CompletionScope()
            try:
                with completion:
                    await self._commit('record_result',identity('semantic-result',work_id),MappingProxyType({'work_id':work_id,
                        'expected_revision':work['revision'],'request_ref':MappingProxyType({'request_id':request.request_id,'attempt_id':request.attempt_id}),
                        'provider_completion':provider.reference(received.completion)}))
            finally:
                completion.when_ended(release_received)
            work=await self.work(work_id)
        if work['state']=='RESULT_STORED':
            if work['purpose']=='DOCUMENT':
                current,gap=await self.owner.memory.semantic_current(string(record(work['object_ref'])['object_id']))
                if current is not None and current['revision']==record(work['object_ref'])['revision']:
                    await self._commit('apply',identity('semantic-apply',work_id),MappingProxyType({'kind':'EMBED','work_id':work_id,
                        'expected_revision':work['revision'],'object_ref':work['object_ref'],'latest_seq':work['change_seq'],'artifact_id':work['artifact_id']}))
                elif gap is not None:
                    await self._commit('supersede',identity('semantic-supersede',work_id),MappingProxyType({'work_id':work_id,
                        'expected_revision':work['revision'],'current_object_revision':gap['object_revision'],'current_change_seq':gap['latest_change_seq']}))
            else:
                text=await self.owner.text(work);key=identity('semantic-cache-bind',work_id)
                payload=MappingProxyType({'cache_key':cache_key(self.owner.instance,string(work['partition_id']),self.owner.space,text),
                    'work_id':work_id,'expected_work_revision':work['revision'],'expires_at':time.time_ns()//1000+86400000000})
                observed=number(payload['expires_at'])-86400000000
                result=await self.execute('cache_bind',self.envelope('cache_bind',key,payload,observed),time.monotonic()+5)
                if type(result) is not Committed:raise LocalConfirmation(result)
            work=await self.work(work_id)
        if work['artifact_id'] is not None and work['cleanup_pending']:
            artifact=await self.owner.rows.read('embedding_artifact',string(work['artifact_id']));assert artifact is not None
            reference=record(work['request_ref']);rid=string(reference['request_id'])
            request_root=await provider.ledger.get('requests',rid);assert request_root is not None
            handoff=cast(Record,await provider.ledger.get('handoffs',cast(str,request_root['handoff_id'])));assert handoff is not None
            if record(handoff['embedding_cleanup'])['state']=='HELD':
                confirmed=await provider.execute('confirm_embedding_handoff',identity('embedding-confirm',rid),MappingProxyType({
                    'request_ref':reference,'receipt':artifact['completion_ref'],'artifact_id':artifact['artifact_id']}))
                if type(confirmed) is not Committed:raise LocalConfirmation(confirmed)
            while True:
                handoff=cast(Record,await provider.ledger.get('handoffs',cast(str,request_root['handoff_id'])));assert handoff is not None
                cleanup=record(handoff['embedding_cleanup'])
                if cleanup['state']=='RETIRED':break
                retired=await provider.execute('retire_embedding_handoff',identity('embedding-retire',rid,cleanup['retired_through']),MappingProxyType({
                    'request_ref':reference,'expected_handoff_revision':handoff['revision'],'after_ordinal':cleanup['retired_through']}))
                if type(retired) is not Committed:raise LocalConfirmation(retired)
            last_start=max(0,((number(record(handoff['embedding_payload'])['leaf_count'])-1)//8)*8)
            last=await provider.operations['retire_embedding_handoff'].read_receipt(identity('embedding-retire',rid,None if last_start==0 else last_start-1))
            if type(last) is Failed:raise LocalConfirmation(last)
            if type(last) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
            await self._commit('record_cleanup',identity('semantic-cleanup',work_id),MappingProxyType({'kind':'EMBED','work_id':work_id,
                'expected_revision':work['revision'],'request_ref':reference,'provider_receipt':provider.reference(last.value),'cleanup_pending':False}))
            if self.host.authorization is not None:self.host.authorization.complete(work_id)
        elif work['state'] in ('KNOWN_FAILED','NOT_SENT','REMOTE_UNKNOWN') and work['cleanup_pending']:
            rid=string(record(work['request_ref'])['request_id'])
            unknown=work['state']=='REMOTE_UNKNOWN'
            proof=await provider.ledger.operations['recover' if unknown else 'terminate'].read_receipt(identity('embedding-unknown' if unknown else 'embedding-terminate',rid))
            if type(proof) is Failed:raise LocalConfirmation(proof)
            if type(proof) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
            await self._commit('record_cleanup',identity('semantic-cleanup',work_id),MappingProxyType({'kind':'EMBED','work_id':work_id,
                'expected_revision':work['revision'],'request_ref':work['request_ref'],'provider_receipt':provider.reference(proof.value),'cleanup_pending':False}))
            if self.host.authorization is not None:self.host.authorization.complete(work_id)
        await provider.reconcile_network()
        return await self.work(work_id)

    @outcome
    async def publish(self,generation_id:str,captured_seq:int) -> Record:
        """Build and publish one complete generation from paid native artifacts."""
        return await self._local(lambda:self._publish(generation_id,captured_seq))

    @property
    def pending(self) -> bool:
        """Actual work and local file descendants retain their original owner."""
        return any(task is not None and not task.done() for task in (self._task,self._local_task))

    async def wait_actual(self,timeout:float) -> bool:
        tasks=tuple(task for task in (self._task,self._local_task) if task is not None and not task.done())
        if not tasks:return True
        _,pending=await asyncio.wait(tasks,timeout=timeout)
        return not pending

    async def _local[T](self,action:Callable[[],Awaitable[T]]) -> T:
        """One original five-second call, retaining all admitted descendant I/O."""
        if self._local_task is not None and not self._local_task.done():
            raise OwnerFailure('RESOURCE_BUSY','state','OWNER_ACTIVE',True)
        seconds=number(self.host.configuration.text.record('retrieval.semantic_storage')['local_step_ms'])/1000
        deadline=current_deadline(seconds)
        with DeadlineScope(deadline):
            self._local_task,result=start_owned(action())
            self._local_task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        done,_=await asyncio.wait((result,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return result.result()

    async def _publish(self,generation_id:str,captured_seq:int) -> Record:
        self.host.normal();owner=self.host.combination.generations;assert owner is not None
        control=await self.control()
        await self._commit('begin_generation',identity('semantic-generation-begin',generation_id),MappingProxyType({'generation_id':generation_id,
            'space_id':self.owner.space,'captured_seq':captured_seq,'expected_control_revision':control['revision']}))
        while True:
            check_deadline()
            generation=await owner.rows.read('semantic_generation',generation_id);assert generation is not None
            if generation['state']!='BUILDING':break
            page_no=number(generation['confirmed_pages'])
            if page_no==generation['page_count']:
                after=string(generation['build_cursor']) if generation['build_cursor'] else ''
                candidates=()
                while page:=await self.owner.memory.ack_page(after):
                    check_deadline();self.host.checkpoint()
                    candidates=tuple(v for v in page if v['artifact_id'] is not None)
                    if candidates:break
                    after=string(page[-1]['object_id'])
                if not candidates:break
                await self._commit('append_page',identity('semantic-generation-append',generation_id,page_no),MappingProxyType({'generation_id':generation_id,
                    'expected_revision':generation['revision'],'page_no':page_no,'after_object_id':generation['build_cursor']}))
            proof=await owner.write_page(generation_id,page_no)
            generation=await owner.rows.read('semantic_generation',generation_id);assert generation is not None
            await self._commit('confirm_page',identity('semantic-generation-confirm',generation_id,page_no),MappingProxyType({'generation_id':generation_id,
                'expected_revision':generation['revision'],'page_no':page_no,'page_digest':proof.page_digest}))
        sealed=await owner.seal_file(generation_id)
        generation=await owner.rows.read('semantic_generation',generation_id);assert generation is not None
        await self._commit('seal_generation',identity('semantic-generation-seal',generation_id),MappingProxyType({'generation_id':generation_id,
            'expected_revision':generation['revision'],'file_digest':sealed.file_digest,'file_bytes':sealed.file_bytes}))
        generation=await owner.rows.read('semantic_generation',generation_id);control=await self.control();assert generation is not None
        await self._commit('publish_generation',identity('semantic-generation-publish',generation_id),MappingProxyType({'generation_id':generation_id,
            'expected_revision':generation['revision'],'expected_control_revision':control['revision'],'file_digest':sealed.file_digest,'file_bytes':sealed.file_bytes}))
        result=await owner.rows.read('semantic_generation',generation_id);assert result is not None;return result

    @outcome
    async def rebuild(self,generation_id:str):
        """Explicit local reconstruction never requests a new embedding."""
        self.host.normal();owner=self.host.combination.generations;assert owner is not None
        return await self._local(lambda:owner.rebuild(generation_id))

    @outcome
    async def retire(self,generation_id:str) -> bool:
        """Retire at most one actual native page; held readers leave it intact."""
        return await self._local(lambda:self._retire(generation_id))

    async def _retire(self,generation_id:str) -> bool:
        self.host.checkpoint();owner=self.host.combination.generations;assert owner is not None
        generation=await owner.rows.read('semantic_generation',generation_id);assert generation is not None
        if generation['state']=='RETIRED':return True
        if not await owner.prepare_retirement(generation_id):return False
        control=await self.control()
        await self._commit('retire_page',identity('semantic-generation-retire',generation_id,generation['retired_cursor']),MappingProxyType({
            'generation_id':generation_id,'expected_revision':generation['revision'],'after_row_id':generation['retired_cursor'],'expected_control_revision':control['revision']}))
        generation=await owner.rows.read('semantic_generation',generation_id);assert generation is not None
        return generation['state']=='RETIRED'
