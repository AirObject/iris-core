"""Local initialization management with one retained operation and native proofs.

Only trusted startup issues this port. Human decisions carry the exact candidate
revision and digest; callers never supply candidate text or Provider evidence.
An explicit generate may dispatch only after its newly committed association.
Re-entry and recovery inspect the original request without a replacement send.
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass,replace
from typing import cast
from companion_memory.persistence import (Committed,Found,NotFound,NotCommitted,RecoveryHandle,ResultBoundCommand,Value,Receipt)
from companion_memory.persistence.completion import CompletionScope,retain_completion
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge,valid_identifier
from companion_memory.persistence.text_records import isolate_record,stable_identity
from companion_memory.provider import CancellationSource,Completed,Pending,Found as ProviderFound,ResultGrant
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.provider.unsent_evidence import VerifiedUnsent,UnsentVerified
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.service import OPTIONALS
from companion_memory.provider.values import as_record,freeze
from companion_memory.ingress.events import plain
from companion_memory.runtime.content_modes import ContentFocus
from companion_memory.runtime.content_assembly import stable
from .results import rejected,owner_failure
from .transactions import PersonaTransactions
from .focused_work import InitialPersonaWork,PersonaWorkPermit
from .formats import isolate_run
from .confirmation import ConfirmedAbsentResolution,confirm_absent


@dataclass(frozen=True,slots=True)
class SavedResolution:
    run_id: str
    request_id: str | None
    candidate_id: str
    receipt: Receipt
    cleanup_pending: bool


@dataclass(frozen=True,slots=True)
class RemoteUnknown:
    run_id: str
    request_id: str
    cleanup_pending: bool


@dataclass(frozen=True,slots=True)
class LocalUnconfirmed:
    run_id: str
    request_id: str | None
    reference: RecoveryHandle | None
    cleanup_pending: bool


@dataclass(frozen=True,slots=True,init=False)
class PersonaInitializationPort:
    """Local operator capability; ordinary HTTP and entry ports cannot construct it."""
    _owner: PersonaManagement
    def __init__(self):raise TypeError('Initialization management requires trusted local setup.')

    async def generate(self,run_id: str,generation: int,original_key: str,deadline: float):
        return await _call(self,'generate',original_key,{'run_id':run_id,'generation':generation},deadline)

    async def read_pending(self,run_id: str,deadline: float):
        return await _call(self,'read_pending','read-pending',{'run_id':run_id},deadline)

    async def register_initial_self(self,original_key: str,input_kind: str,body: str,input_origin: str,deadline: float):
        return await _call(self,'register_initial_self',original_key,{'input_kind':input_kind,'body':body,'input_origin':input_origin},deadline)

    async def register_initial_subjects(self,original_key: str,subjects: object,input_origin: str,deadline: float):
        """Register a bounded operator roster before first persona preparation."""
        return await _call(self,'register_initial_subjects',original_key,{'subjects':subjects,'input_origin':input_origin},deadline)

    async def prepare_initial_persona(self,original_key: str,input_id: str,expected_self_revision: int,expected_epoch: int,deadline: float):
        return await _call(self,'prepare_initial_persona',original_key,{'input_id':input_id,'expected_self_revision':expected_self_revision,'expected_epoch':expected_epoch},deadline)

    async def associate_initial_persona_request(self,original_key: str,run_id: str,expected_revision: int,generation: int,expected_epoch: int,deadline: float):
        return await _call(self,'associate_initial_persona_request',original_key,{'run_id':run_id,'expected_revision':expected_revision,'generation':generation,'expected_epoch':expected_epoch},deadline)

    async def confirm_initial_persona_request(self,original_key: str,run_id: str,expected_revision: int,generation: int,request_id: str,deadline: float):
        return await _call(self,'confirm_initial_persona_request',original_key,{'run_id':run_id,'expected_revision':expected_revision,'generation':generation,'request_id':request_id},deadline)

    async def record_initial_persona_resolution(self,original_key: str,run_id: str,expected_revision: int,generation: int,provider_reference: str,evidence_revision: int,deadline: float):
        return await _call(self,'record_initial_persona_resolution',original_key,{'run_id':run_id,'expected_revision':expected_revision,'generation':generation,
            'provider_reference':provider_reference,'evidence_revision':evidence_revision},deadline)

    async def review_initial_persona(self,original_key: str,run_id: str,expected_revision: int,candidate_id: str,candidate_revision: int,candidate_digest: str,decision: str,deadline: float):
        return await _call(self,'review_initial_persona',original_key,{'run_id':run_id,'expected_revision':expected_revision,'candidate_id':candidate_id,
            'candidate_revision':candidate_revision,'candidate_digest':candidate_digest,'decision':decision},deadline)

    async def retry_initial_persona(self,original_key: str,run_id: str,expected_revision: int,expected_generation: int,prior_resolution_id: str,expected_epoch: int,deadline: float):
        return await _call(self,'retry_initial_persona',original_key,{'run_id':run_id,'expected_revision':expected_revision,'expected_generation':expected_generation,
            'prior_resolution_id':prior_resolution_id,'expected_epoch':expected_epoch},deadline)

    async def publish_initial_persona(self,original_key: str,run_id: str,expected_revision: int,candidate_id: str,candidate_revision: int,candidate_digest: str,expected_epoch: int,deadline: float):
        return await _call(self,'publish_initial_persona',original_key,{'run_id':run_id,'expected_revision':expected_revision,'candidate_id':candidate_id,
            'candidate_revision':candidate_revision,'candidate_digest':candidate_digest,'expected_epoch':expected_epoch},deadline)


async def _call(port,kind,key,values,deadline):
    owner=getattr(port,'_owner',None)
    if type(port) is not PersonaInitializationPort or type(owner) is not PersonaManagement or owner.port is not port:
        return rejected(kind,'boundary')
    return await owner.call(kind,key,values,deadline)


class PersonaManagement:
    """One admitted manager operation, its original command and actual cleanup.

    A focus coordinator is required to prepare the run. Without one, already
    persisted focused work can still be inspected by trusted local recovery;
    this does not manufacture an ordinary-work cutoff or a READY transition.
    """
    def __init__(self,transactions: PersonaTransactions,focus: ContentFocus | None = None):
        if type(transactions) is not PersonaTransactions or transactions.gate is None or transactions.persona is None:raise InvalidValue()
        if focus is not None and (type(focus) is not ContentFocus or focus.runtime.assembly is not transactions.assembly
                or focus.runtime.gate is not transactions.gate):raise InvalidValue()
        self.transactions=transactions;self.focus=focus;self.work=InitialPersonaWork(transactions)
        self._task:asyncio.Task|None=None;self._closed=False;self._reference:RecoveryHandle|None=None
        self._permit:PersonaWorkPermit|None=None;self._result_owner=None;self._cancellation=CancellationSource()
        self._request_id:str|None=None
        self._unsent:VerifiedUnsent|None=None;self._absent_resolution:ConfirmedAbsentResolution|None=None
        port=object.__new__(PersonaInitializationPort);object.__setattr__(port,'_owner',self);self.port=port

    async def call(self,kind,key,values,deadline):
        t=self.transactions
        if self._closed:return rejected(kind,'state')
        if self.focus is not None and (self.focus.closed or self.focus.runtime.state!='READY'):return rejected(kind,'state')
        if self.focus is not None and self.focus.jobs:return rejected(kind,'busy',True)
        if self._task is not None:return rejected(kind,'busy',True)
        try:
            if not valid_identifier(key) or type(deadline) not in (int,float) or not time.monotonic()<deadline<float('inf'):raise InvalidValue()
            if kind in t.definitions:values=cast(dict,plain(isolate_record(t.definitions[kind].input_schema,{'operation_id':key,**values},8192)))
            elif kind=='register_initial_self':values=dict(isolate_record(t.initial_commands.commands[0].input_schema,{'operation_id':key,**values},8192))
            elif kind in ('generate','read_pending'):
                if not valid_identifier(values['run_id']) or kind=='generate' and (type(values['generation']) is not int or not 1<=values['generation']<=3):raise InvalidValue()
            else:raise InvalidValue()
        except (InvalidValue,KeyError,TypeError):return rejected(kind,'shape')
        self._reference=None;self._request_id=None
        outcome=asyncio.get_running_loop().create_future()
        outcome.add_done_callback(lambda future:None if future.cancelled() else future.exception())
        async def owned():
            with CompletionScope() as completion:
                try:
                    with DeadlineScope(deadline):
                        value=await self._perform(kind,key,values,deadline)
                        if type(value) in (SavedResolution,RemoteUnknown):
                            value=replace(value,cleanup_pending=completion.pending or self._permit is not None and not self._permit.work.consumers_ended())
                        outcome.set_result(value)
                except OwnerFailure as failure:outcome.set_result(owner_failure(kind,failure))
                except ValueTooLarge:outcome.set_result(rejected(kind,'shape'))
                except (InvalidValue,KeyError,IndexError):outcome.set_result(rejected(kind,'record'))
                finally:
                    await completion.wait()
                    permit=self._permit
                    if permit is not None:
                        while not permit.work.consumers_ended():await asyncio.sleep(.01)
                        self.work.release(permit);self._permit=None
                    if self._result_owner is not None:t.provider.revoke(self._result_owner);self._result_owner=None
                    # The registration seal outlives all transaction descendants,
                    # even when the public deadline ended before COMMIT cleanup.
                    self._unsent=None;self._absent_resolution=None
        task=asyncio.create_task(owned());self._task=task;retain_completion(task)
        if self.focus is not None:self.focus.jobs.add(task)
        def ended(job):
            if not job.cancelled():
                failure=job.exception()
                if failure is not None and not outcome.done():outcome.set_exception(failure)
            if self._task is job:self._task=None
            if self.focus is not None:self.focus.jobs.discard(job)
        task.add_done_callback(ended)
        done,_=await asyncio.wait((outcome,),timeout=max(0,deadline-time.monotonic()))
        if not done:return LocalUnconfirmed(cast(str,values.get('run_id',stable_identity('persona-run',t.assembly.configuration.database_id,t.assembly.instance_id))),
            self._request_id,self._reference,True)
        return outcome.result()

    async def _run(self,identity,deadline):
        t=self.transactions;assert t.persona is not None
        found=await t.persona.read_original('run',identity,deadline)
        if found is None:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        return isolate_run(found.value)

    async def _mode(self,deadline):
        if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        rows=await self.transactions.assembly.rows.read('mode_get',{'mode_id':'instance_mode'})
        if len(rows)!=1:raise InvalidValue()
        return rows[0]

    async def _publish_mode(self,deadline):
        mode=await self._mode(deadline);gate=self.transactions.gate;assert gate is not None
        if not self._closed:gate.resolve_cutoff(cast(str,mode['state']),cast(int,mode['epoch']))

    async def _issue(self,run,deadline,*,recovery=True):
        if self._permit is None:
            self._permit=await self.work.issue(cast(str,run['object_id']),cast(int,run['generation']),cast(str,run['provider_operation_key']),deadline,recovery=recovery)
        return self._permit

    async def _completion(self,run,deadline):
        permit=await self._issue(run,deadline);rid=cast(str,run['provider_request_id'])
        if not valid_identifier(rid):raise InvalidValue()
        if self._result_owner is None:self._result_owner=self.transactions.provider.bind_result_owner(ResultGrant('self_model',(rid,)))
        owner=self._result_owner
        original=as_record(freeze({**{name:permit.material.request.get(name) for name in OPTIONALS},**permit.material.request},131072,owned=True))
        terminal=await owner.verify_terminal(rid,original)
        if type(terminal) is not TerminalVerified:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        completion=await owner.confirm_completion(terminal.value)
        if type(completion) is not ConfirmedCompletion:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        return completion

    async def _absence(self,run,deadline) -> bool:
        permit=await self._issue(run,deadline)
        request={**cast(dict,plain(permit.material.request)),'deadline':deadline,'cancellation':self._cancellation.token}
        proof=await permit.work.verify_unsent('generate',request)
        if type(proof) is not UnsentVerified or proof.value.conclusion!='REGISTRATION_ABSENT':return False
        self._unsent=proof.value
        return True

    async def _execute(self,kind,key,values,deadline,*,completion=None):
        t=self.transactions
        if self._closed or time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        definition=t.definitions[kind];complete={'operation_id':key,**values}
        command=ResultBoundCommand(definition.command_version,complete,{r.event_slot:{'actor':t.actor} for r in definition.required_audits})
        operation=t.assembly.storage.bind_operation(definition,t.assembly.instance_id)
        handle=operation.recovery_handle(key,command)
        if type(handle) is not RecoveryHandle:return handle
        self._reference=handle
        original=await operation.resolve_operation(handle)
        if type(original) is not NotCommitted:return original
        if self._closed or time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        scope=t.retain(kind,complete,work=self._permit.work if self._permit is not None else None,result_owner=self._result_owner,
            completion=completion,unsent=self._unsent,absent_resolution=self._absent_resolution)
        with CompletionScope() as cleanup:
            try:return await operation.execute(key,command)
            finally:
                await cleanup.wait()
                t.release(scope)

    async def _perform(self,kind,key,values,deadline):
        t=self.transactions;gate=t.gate;assert gate is not None and t.persona is not None
        if kind in t.definitions:
            definition=t.definitions[kind]
            command=ResultBoundCommand(definition.command_version,{'operation_id':key,**values},
                {r.event_slot:{'actor':t.actor} for r in definition.required_audits})
            operation=t.assembly.storage.bind_operation(definition,t.assembly.instance_id)
            handle=operation.recovery_handle(key,command)
            if type(handle) is not RecoveryHandle:return handle
            self._reference=handle
            original=await operation.resolve_operation(handle)
            # Historical confirmation precedes current generation, evidence and
            # mode checks. It neither reacquires sending authority nor publishes
            # an old epoch over a later completed transition.
            if type(original) is not NotCommitted:return original
        if kind=='generate':return await self._generate(values['run_id'],values['generation'],key,deadline)
        if kind=='read_pending':
            with gate.lock:
                if gate.state!='DREAM_FOCUSED' or gate.integrity_pending():raise OwnerFailure('MODE_BLOCKED','state','DREAMING')
                epoch=gate.epoch
            run=await self._run(values['run_id'],deadline)
            found=None if run['resolution_id'] is None else await t.persona.read_original('candidate',cast(str,run['resolution_id']),deadline)
            if run['resolution_id'] is not None and found is None:raise InvalidValue()
            with gate.lock:
                if (self._closed or t._closed or time.monotonic()>=deadline or gate.state!='DREAM_FOCUSED'
                        or gate.epoch!=epoch or gate.integrity_pending()):raise OwnerFailure('MODE_BLOCKED','state','DREAMING')
                return NotFound() if found is None else Found(found.value)
        if kind=='register_initial_self':
            if gate.state!='NORMAL' or gate.integrity_pending():raise OwnerFailure('MODE_BLOCKED','state','DREAMING')
            return await t.initial_commands._register(key,values['input_kind'],values['body'],values['input_origin'])
        if kind=='register_initial_subjects':return await self._execute(kind,key,values,deadline)
        if kind=='prepare_initial_persona':
            if self.focus is None:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
            gate.close_ordinary()
            result=await self._execute(kind,key,values,deadline)
            if type(result) is Committed:
                await self._publish_mode(deadline)
                # The job remains in native focus ownership through this drain;
                # ordinary jobs retain their separate original cutoff entries.
                await self.focus.drain(stable_identity('persona-run',t.assembly.configuration.database_id,t.assembly.instance_id))
                await self._publish_mode(deadline)
            elif type(result) is NotCommitted:await self._publish_mode(deadline)
            return result
        run=await self._run(values['run_id'],deadline);completion=None
        if kind in ('confirm_initial_persona_request','record_initial_persona_resolution','retry_initial_persona','publish_initial_persona'):
            await self._issue(run,deadline)
        if kind=='retry_initial_persona' and run['provider_request_id'] is None:
            proposal=await t.persona.read_original('candidate',values['prior_resolution_id'],deadline)
            if proposal is None:raise InvalidValue()
            self._absent_resolution=await confirm_absent(t,proposal.value)
            if not await self._absence(run,deadline):raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        elif kind in ('retry_initial_persona','publish_initial_persona'):completion=await self._completion(run,deadline)
        if kind=='record_initial_persona_resolution':
            assert self._permit is not None
            if run['provider_request_id'] is None:
                if (values['provider_reference']!=run['provider_operation_key'] or values['evidence_revision']!=0
                        or not await self._absence(run,deadline)):raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
            else:
                request=await self._permit.work.get_request(values['provider_reference'])
                if type(request) is not ProviderFound:raise InvalidValue()
                if as_record(as_record(request.value)['request'])['phase']!='REMOTE_RESULT_UNKNOWN':completion=await self._completion(run,deadline)
        result=await self._execute(kind,key,values,deadline,completion=completion)
        if type(result) is Committed and kind in ('retry_initial_persona','publish_initial_persona'):
            await self._publish_mode(deadline)
            if kind=='publish_initial_persona' and self.focus is not None:await self.focus.transfer_staged(values['run_id'])
        return result

    async def _generate(self,run_id,generation,key,deadline):
        t=self.transactions;run=await self._run(run_id,deadline)
        if run['generation']!=generation or run['provider_operation_key']!=key:raise InvalidValue()
        send=False
        if run['state']=='PREPARED':
            mode=await self._mode(deadline)
            associated=await self._execute('associate_initial_persona_request',stable('persona_associate',run_id,generation),
                {'run_id':run_id,'expected_revision':run['revision'],'generation':generation,'expected_epoch':mode['epoch']},deadline)
            if type(associated) is not Committed:return associated
            send=associated.source=='NEW';run=await self._run(run_id,deadline)
        if run['state'] not in ('REQUEST_ASSOCIATED','REMOTE_UNKNOWN'):
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        permit=await self._issue(run,deadline,recovery=not send)
        request={**cast(dict,plain(permit.material.request)),'deadline':deadline,'cancellation':self._cancellation.token}
        response=await permit.work.generate(request) if send else await permit.work.lookup_request('generate',request)
        rid=response.record['object_id'] if type(response) is Completed else response.reference['request_id'] if type(response) is Pending else as_record(as_record(response.value)['request'])['object_id'] if type(response) is ProviderFound else None
        if rid is None:
            if run['provider_request_id'] is not None or not await self._absence(run,deadline):
                return LocalUnconfirmed(run_id,None,self._reference,not permit.work.consumers_ended())
            resolved=await self._execute('record_initial_persona_resolution',stable('persona_resolution',run_id,generation,0),
                {'run_id':run_id,'expected_revision':run['revision'],'generation':generation,'provider_reference':key,'evidence_revision':0},deadline)
            if type(resolved) is not Committed:return resolved
            current=await self._run(run_id,deadline)
            return SavedResolution(run_id,None,cast(str,current['resolution_id']),resolved.receipt,not permit.work.consumers_ended())
        self._request_id=cast(str,rid)
        if run['provider_request_id'] is None:
            confirmed=await self._execute('confirm_initial_persona_request',stable('persona_confirm',run_id,generation),
                {'run_id':run_id,'expected_revision':run['revision'],'generation':generation,'request_id':rid},deadline)
            if type(confirmed) is not Committed:return confirmed
            run=await self._run(run_id,deadline)
        if run['provider_request_id']!=rid:raise InvalidValue()
        found=await permit.work.get_request(rid)
        if type(found) is not ProviderFound:return LocalUnconfirmed(run_id,cast(str,rid),self._reference,not permit.work.consumers_ended())
        actual=as_record(as_record(found.value)['request'])
        if actual['phase'] not in ('TERMINAL','REMOTE_RESULT_UNKNOWN'):return RemoteUnknown(run_id,cast(str,rid),not permit.work.consumers_ended())
        completion=None if actual['phase']=='REMOTE_RESULT_UNKNOWN' else await self._completion(run,deadline)
        if run['state']=='REMOTE_UNKNOWN' and completion is None:return RemoteUnknown(run_id,cast(str,rid),not permit.work.consumers_ended())
        resolved=await self._execute('record_initial_persona_resolution',stable('persona_resolution',run_id,generation,cast(int,actual['revision'])),
            {'run_id':run_id,'expected_revision':run['revision'],'generation':generation,'provider_reference':rid,'evidence_revision':actual['revision']},deadline,completion=completion)
        if type(resolved) is not Committed:return resolved
        if completion is None:return RemoteUnknown(run_id,cast(str,rid),not permit.work.consumers_ended())
        current=await self._run(run_id,deadline)
        return SavedResolution(run_id,cast(str,rid),cast(str,current['resolution_id']),resolved.receipt,not permit.work.consumers_ended())

    async def close(self,deadline: float) -> bool:
        """Deny new actions and sending now; retain the sole job until actual cleanup."""
        self._closed=True;self.work.close();self._cancellation.cancel();self.transactions.close()
        if self._task is None:return True
        done,_=await asyncio.wait((self._task,),timeout=max(0,deadline-time.monotonic()))
        return bool(done)
