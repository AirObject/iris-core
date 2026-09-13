"""First-persona command declarations and fixed same-transaction owner effects.

A separately retained native local invocation must match the complete command.
The eventual management coordinator owns that invocation until actual cleanup;
ordinary operation ports, role names and serialized results cannot create it.
"""
from __future__ import annotations
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.persistence import (Field,RecordSchema,ScalarSchema,ResultBoundCommandDefinition,UnitOfWork,Value)
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.text_records import ID,UINT,REVISION,DIGEST,isolate_record,stable_identity
from companion_memory.persistence.text_results import audits,result_schema,result,INTENT
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.initial_self_storage import InitialSelfStorage,InitialSelfBinding
from companion_memory.memory.initial_self_commands import InitialSelfCommands
from companion_memory.provider import ProviderService,WorkPort,ResultOwnerPort,Found
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.provider.unsent_evidence import VerifiedUnsent
from companion_memory.provider.values import as_record
from companion_memory.runtime.content_gate import ContentGate
from companion_memory.runtime.initial_persona_mode import InitialPersonaMode
from .formats import isolate_run,GENERATION
from .storage import PersonaStorage
from .preparation import prepare_run,associate_run,confirm_run,retained_material,generation_key
from .request_material import request_material
from .resolution import resolve_known,verify_resolved,resolve_unsent,verify_unsent_resolution
from .confirmation import ConfirmedAbsentResolution,confirmed_absent
from .transitions import review,next_generation
from .publication import publish_approved
if TYPE_CHECKING:
    from companion_memory.runtime.content_assembly import ContentAssembly


@dataclass(frozen=True,slots=True,init=False)
class PersonaInvocation:
    """One retained invocation, issued only by its trusted local transaction owner."""
    kind: str
    values: MappingProxyType[str,Value]
    work: WorkPort | None
    result_owner: ResultOwnerPort | None
    completion: ConfirmedCompletion | None
    unsent: VerifiedUnsent | None
    absent_resolution: ConfirmedAbsentResolution | None
    def __init__(self):raise TypeError('Invocation ownership is issued by local persona coordination.')


class PersonaTransactions:
    """Seven fixed commands plus the memory-owned initial registration command.

    This participant supplies no automatic generation loop. Trusted management
    must close ordinary admission before prepare and independently join actual
    Provider/storage cleanup before releasing an invocation or retrying.
    """
    def __init__(self,assembly: ContentAssembly,provider: ProviderService):
        if not assembly.text_format or assembly._bound or type(provider) is not ProviderService or not provider._assembly.text_generation:raise InvalidValue()
        self.assembly=assembly;self.provider=provider;self.mode=InitialPersonaMode(assembly)
        memory=next(c for c in assembly.catalogs if c.definition.owner_module=='memory')
        self.initial_commands=InitialSelfCommands(memory,assembly.utc_now_us)
        self.persona:PersonaStorage|None=None;self.initial:InitialSelfStorage|None=None;self.gate:ContentGate|None=None
        self._scope:PersonaInvocation|None=None;self._closed=False;self.actor=''
        common=(Field('run_id',ID),Field('expected_revision',REVISION))
        layouts=(
            ('prepare_initial_persona',(Field('input_id',ID),Field('expected_self_revision',REVISION),Field('expected_epoch',REVISION)),('PREPARED',),('self_model','runtime')),
            ('associate_initial_persona_request',common+(Field('generation',GENERATION),Field('expected_epoch',REVISION)),('REQUEST_ASSOCIATED',),('self_model',)),
            ('confirm_initial_persona_request',common+(Field('generation',GENERATION),Field('request_id',ID)),('REQUEST_ASSOCIATED',),('self_model',)),
            ('record_initial_persona_resolution',common+(Field('generation',GENERATION),Field('provider_reference',ID),Field('evidence_revision',UINT)),('WAITING_REVIEW','KNOWN_FAILED','REMOTE_UNKNOWN'),('self_model',)),
            ('review_initial_persona',common+(Field('candidate_id',ID),Field('candidate_revision',REVISION),Field('candidate_digest',DIGEST),Field('decision',ScalarSchema('enum',choices=('APPROVE','REJECT')))),('APPROVED','USER_REJECTED'),('self_model',)),
            ('retry_initial_persona',common+(Field('expected_generation',ScalarSchema('integer',1,2)),Field('prior_resolution_id',ID),Field('expected_epoch',REVISION)),('PREPARED',),('self_model','runtime')),
            ('publish_initial_persona',common+(Field('candidate_id',ID),Field('candidate_revision',REVISION),Field('candidate_digest',DIGEST),Field('expected_epoch',REVISION)),('PUBLISHED',),('self_model','runtime')),
        )
        self.definitions={}
        participants=tuple(r for r in assembly.repositories if r.owner_module in ('self_model','memory','runtime'))+provider._assembly.repositories
        for kind,fields,states,owners in layouts:
            requirements,bindings=audits(kind,owners)
            def handle(uow,values,operation=kind):return self.handle(operation,uow,values)
            self.definitions[kind]=ResultBoundCommandDefinition('self_model',kind,1,RecordSchema((Field('operation_id',ID),)+fields),1,
                result_schema(owners,states),participants,requirements,handle,INTENT,bindings)
        self.commands=self.initial_commands.commands+tuple(self.definitions.values())
        original_mode=assembly.command_definition('change_content_mode')
        if original_mode.command_version!=1:raise InvalidValue()
        assembly.commands=tuple(self.mode.definition if command is original_mode else command for command in assembly.commands)+self.commands
        assembly._semantic_definitions['change_content_mode']=self.mode.definition

    def bind(self,binding: InitialSelfBinding,gate: ContentGate):
        """Bind the actual same-instance owners after configuration confirmation."""
        if self.persona is not None or not self.assembly._bound or type(gate) is not ContentGate or type(binding) is not InitialSelfBinding:raise InvalidValue()
        self.persona=self.assembly.text_commands.persona
        if self.persona is None:raise InvalidValue()
        self.mode.bind(self.persona);self.mode.gate=gate;self.gate=gate;self.actor=binding.actor_ref
        registration=self.initial_commands.bind(self.assembly.memory,binding)
        self.initial=self.initial_commands._owner
        return registration

    def retain(self,kind: str,values: object,*,work: WorkPort|None=None,result_owner: ResultOwnerPort|None=None,
               completion: ConfirmedCompletion|None=None,unsent: VerifiedUnsent|None=None,
               absent_resolution: ConfirmedAbsentResolution|None=None) -> PersonaInvocation:
        """Trusted coordination retains exactly one complete operation and its evidence."""
        if self._closed or self.persona is None or self._scope is not None or kind not in self.definitions:
            raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING')
        isolated=isolate_record(self.definitions[kind].input_schema,values,8192)
        scope=object.__new__(PersonaInvocation)
        for name,value in (('kind',kind),('values',isolated),('work',work),('result_owner',result_owner),('completion',completion),
                           ('unsent',unsent),('absent_resolution',absent_resolution)):
            object.__setattr__(scope,name,value)
        self._scope=scope
        return scope

    def release(self,scope: PersonaInvocation) -> None:
        """Call only after the original storage and Provider consumers actually end."""
        if self._scope is scope:self._scope=None

    def _guard(self,uow: UnitOfWork,kind: str,values: MappingProxyType[str,Value]) -> PersonaInvocation:
        scope=self._scope;gate=self.gate
        if (self._closed or scope is None or scope.kind!=kind or scope.values!=values or gate is None
                or gate.state not in ('DREAM_PREPARING','DREAM_FOCUSED') or gate.integrity_pending()):
            raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
        epoch=gate.epoch
        uow.require_commit_permission(lambda:not self._closed and self._scope is scope and self.gate is gate and gate.epoch==epoch
            and gate.state in ('DREAM_PREPARING','DREAM_FOCUSED') and not gate.integrity_pending())
        return scope

    def handle(self,kind: str,uow: UnitOfWork,values: MappingProxyType[str,Value]):
        scope=self._guard(uow,kind,values)
        assert self.persona is not None and self.initial is not None
        owner=self.persona;a=self.assembly;now=a.utc_now_us()
        key={'owner_namespace':'self_model','operation_kind':kind,'scope_id':a.instance_id,'operation_key':values['operation_id']}
        changed: list[tuple[str,MappingProxyType[str,Value]|None,MappingProxyType[str,Value]]]=[]
        mode_before=None;mode_after=None
        if kind=='prepare_initial_persona':
            initial=self.initial.read_initial(uow,cast(str,values['input_id']))
            if initial is None:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
            run_id=stable_identity('persona-run',a.configuration.database_id,a.instance_id)
            if owner.read(uow,'run',run_id) is not None or owner.current_publication(uow) is not None:
                raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
            mode_before=self.mode.current(uow,run_id,cast(int,values['expected_epoch']),enter=True)
            mode_after=self.mode.enter(uow,run_id,cast(int,values['expected_epoch']),now)
            after=prepare_run(a.text_transactions.configuration,initial.input,cast(int,values['expected_self_revision']),cast(int,mode_after['epoch']),key,now)
            owner.stage_run(uow,after);changed.append(('RUN',None,after))
        else:
            found=owner.read(uow,'run',cast(str,values['run_id']))
            if found is None or found.value['revision']!=values['expected_revision']:
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            before=found.value;initial=self.initial.read_initial(uow,cast(str,before['input_id']))
            if initial is None:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            material=retained_material(a.text_transactions.configuration,initial.input,before)
            mode=a._get('mode',uow,'mode_id','instance_mode')
            if (mode['run_id']!=before['object_id'] or mode['state']!='DREAM_FOCUSED' or mode['publication_id'] is not None
                    or self.gate is None or self.gate.epoch!=mode['epoch']):
                raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
            if 'generation' in values and values['generation']!=before['generation']:raise InvalidValue()
            if 'expected_epoch' in values and values['expected_epoch']!=mode['epoch']:raise InvalidValue()
            if kind=='associate_initial_persona_request':
                after=associate_run(a.text_transactions.configuration,initial.input,before,cast(int,values['expected_revision']),cast(int,values['generation']),cast(int,mode['epoch']),key,now)
                owner.update_run(uow,before,after)
            elif kind=='confirm_initial_persona_request':
                request=self._request(uow,scope,material.request,cast(str,values['request_id']))
                after=confirm_run(before,cast(int,values['expected_revision']),cast(int,values['generation']),cast(str,request['object_id']),key,now)
                owner.update_run(uow,before,after)
            elif kind=='record_initial_persona_resolution':
                if before['state'] not in ('REQUEST_ASSOCIATED','REMOTE_UNKNOWN'):raise InvalidValue()
                if before['provider_request_id'] is None:
                    if (values['provider_reference']!=before['provider_operation_key'] or values['evidence_revision']!=0
                            or before['state']!='REQUEST_ASSOCIATED'):raise InvalidValue()
                    self._unsent(uow,scope,material.request)
                    resolved=resolve_unsent(a.text_transactions.configuration,initial.input,before,scope.unsent,key,now)
                    after=resolved.run;owner.stage_resolution(uow,before,after,resolved.candidate)
                    changed.append(('RESOLUTION',None,resolved.candidate))
                else:
                    request=self._request(uow,scope,material.request,cast(str,values['provider_reference']))
                    if request['object_id']!=before['provider_request_id'] or request['revision']!=values['evidence_revision']:raise InvalidValue()
                    if request['phase']=='REMOTE_RESULT_UNKNOWN':
                        if before['state']=='REMOTE_UNKNOWN':raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
                        after=isolate_run({**before,'revision':cast(int,before['revision'])+1,'state':'REMOTE_UNKNOWN','last_operation':key,'updated_at_us':now})
                        owner.update_run(uow,before,after)
                    else:
                        completion=self._completion(uow,scope)
                        resolved=resolve_known(a.text_transactions.configuration,initial.input,before,completion,key,now)
                        after=resolved.run;owner.stage_resolution(uow,before,after,resolved.candidate)
                        changed.append(('RESOLUTION',None,resolved.candidate))
            else:
                candidate_id=values['prior_resolution_id'] if kind=='retry_initial_persona' else values['candidate_id']
                proposal=owner.read(uow,'candidate',cast(str,candidate_id))
                if proposal is None:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
                candidate=proposal.value
                if kind=='review_initial_persona':
                    reviewed=review(before,candidate,cast(int,values['expected_revision']),cast(int,values['candidate_revision']),cast(str,values['candidate_digest']),cast(str,values['decision']),self.actor,key,now)
                    owner.stage_review(uow,before,candidate,reviewed);after=reviewed.run
                    changed.append(('RESOLUTION',candidate,reviewed.candidate))
                elif kind=='retry_initial_persona':
                    if candidate['provider_request_id'] is None:
                        self._unsent(uow,scope,material.request,retry=True)
                        if not confirmed_absent(self,scope.absent_resolution,candidate):raise InvalidValue()
                        uow.require_commit_permission(lambda:confirmed_absent(self,scope.absent_resolution,candidate))
                        verify_unsent_resolution(a.text_transactions.configuration,initial.input,before,candidate,scope.unsent,now)
                    else:
                        completion=self._completion(uow,scope,retry=True)
                        verify_resolved(a.text_transactions.configuration,initial.input,before,candidate,completion,now)
                        if candidate['terminal_receipt']!=completion.operation or candidate['provider_request_id']!=completion.terminal.request['object_id']:raise InvalidValue()
                    mode_before=self.mode.current(uow,cast(str,before['object_id']),cast(int,mode['epoch']))
                    mode_after=self.mode.retry(uow,cast(str,before['object_id']),cast(int,mode['epoch']))
                    generation=cast(int,before['generation'])+1
                    new_material=request_material(a.text_transactions.configuration,initial.input,cast(str,before['object_id']),generation,
                        generation_key(a.configuration.database_id,a.instance_id,cast(str,before['object_id']),generation))
                    after=next_generation(before,candidate,initial.input,cast(int,values['expected_revision']),cast(int,values['expected_generation']),cast(str,values['prior_resolution_id']),cast(int,mode_after['epoch']),key,now,new_material)
                    owner.update_run(uow,before,after)
                elif kind=='publish_initial_persona':
                    completion=self._completion(uow,scope)
                    published=publish_approved(a.text_transactions.configuration,initial.input,before,candidate,cast(int,values['expected_revision']),cast(int,values['candidate_revision']),cast(str,values['candidate_digest']),completion,key,now)
                    after=published.run;owner.stage_publication(uow,before,after,published.publication)
                    mode_before=self.mode.current(uow,cast(str,before['object_id']),cast(int,mode['epoch']))
                    mode_after=self.mode.publish(uow,cast(str,before['object_id']),cast(int,mode['epoch']),cast(str,published.publication['object_id']))
                    changed.append(('PUBLICATION',None,published.publication))
                else:raise InvalidValue()
            changed.append(('RUN',before,after))
        references=tuple({'kind':label,'object_id':current['object_id'],'revision':current['revision']} for label,_,current in changed)
        targets=tuple({'object_id':current['object_id'],'previous_revision':previous['revision'] if previous is not None else None,'revision':current['revision']} for _,previous,current in changed)
        facts:dict[str,object]={'self_model':{'rows_changed':len(changed),'references':references}}
        if mode_after is not None:
            assert mode_before is not None
            ref={'kind':'MODE','object_id':'instance_mode','revision':mode_after['epoch']}
            facts['runtime']={'rows_changed':1,'references':(ref,)};references+=(ref,)
            targets+=({'object_id':'instance_mode','previous_revision':mode_before['epoch'],'revision':mode_after['epoch']},)
        return result(cast(str,values['operation_id']),cast(str,after['state']),facts,references,targets)

    def _request(self,uow: UnitOfWork,scope: PersonaInvocation,original: MappingProxyType[str,Value],identity: str):
        if type(scope.work) is not WorkPort or scope.work._native() is not self.provider:raise InvalidValue()
        from companion_memory.provider.service import OPTIONALS
        value={**{name:original.get(name) for name in OPTIONALS},**original}
        verified=scope.work.verify_request_in_transaction(uow,identity,value)
        if type(verified) is not Found:raise OwnerFailure('INTEGRITY_FAILURE','identity','BINDING_MISMATCH')
        request=as_record(verified.value)
        if (request['caller_module']!='self_model' or request['caller_scope']!=self.assembly.instance_id
                or request['task_role']!='PERSONA' or request['result_owner']!='self_model'):
            raise OwnerFailure('INTEGRITY_FAILURE','identity','BINDING_MISMATCH')
        return request

    def _completion(self,uow: UnitOfWork,scope: PersonaInvocation,*,retry: bool = False) -> ConfirmedCompletion:
        if type(scope.result_owner) is not ResultOwnerPort or scope.result_owner._native() is not self.provider or type(scope.completion) is not ConfirmedCompletion:
            raise OwnerFailure('INTEGRITY_FAILURE','identity','BINDING_MISMATCH')
        if retry and type(scope.work) is not WorkPort:raise InvalidValue()
        verified=scope.result_owner.verify_completion_in_transaction(uow,scope.completion,retry_work=scope.work if retry else None)
        if type(verified) is not Found:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        return scope.completion

    def _unsent(self,uow: UnitOfWork,scope: PersonaInvocation,original: MappingProxyType[str,Value],*,retry: bool = False) -> None:
        if type(scope.work) is not WorkPort or scope.work._native() is not self.provider:raise InvalidValue()
        from companion_memory.provider.service import OPTIONALS
        value={**{name:original.get(name) for name in OPTIONALS},**original}
        verified=scope.work.verify_unsent_in_transaction(uow,scope.unsent,value,retry=retry)
        if type(verified) is not Found:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')

    def close(self) -> None:
        """Close new management writes without releasing an unfinished invocation."""
        self._closed=True;self.mode.close();self.initial_commands.close()
