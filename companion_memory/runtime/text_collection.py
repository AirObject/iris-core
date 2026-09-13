"""Complete original learning context collected through finite native read rights.

Trusted entry setup chooses permitted identities, worlds and up to two related
objects. The model and entry request cannot replace that scope. Every body comes
from its current owner, then the stage transaction rechecks the same authority
and revisions before it publishes the entire frozen context.
"""
from __future__ import annotations
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.cognition.text_context import freeze_context
from companion_memory.cognition.text_resources import render_learning_instructions
from companion_memory.memory.formats import record,sequence,WORLD
from companion_memory.memory.service import MemoryReadPort
from companion_memory.persistence import Found,Value
from companion_memory.persistence.schema import InvalidValue,SequenceSchema,freeze_value,valid_identifier
from companion_memory.persistence.content_codec import decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.text_records import digest
from companion_memory.provider.service import derived_id
from companion_memory.self_model.current import CurrentPersonaPort,Available,Unavailable
from companion_memory.self_model.results import Failed as PersonaFailed
if TYPE_CHECKING:
    from .content_service import ContentRuntimeService,ContentEntryPort


@dataclass(frozen=True,slots=True)
class EntryContextScope:
    port: MemoryReadPort
    subjects: tuple[str,...]
    related: tuple[str,...]
    worlds: tuple[Value,...]


class TextContextCollection:
    """Finite per-entry authority, without lookup expansion or implicit Top-K."""
    def __init__(self,runtime: ContentRuntimeService,persona: CurrentPersonaPort):
        from .content_service import ContentRuntimeService
        if (type(runtime) is not ContentRuntimeService or not runtime.assembly.text_format or type(persona) is not CurrentPersonaPort
                or persona._owner.transactions.assembly is not runtime.assembly):raise InvalidValue()
        self.runtime=runtime;self.persona=persona;self._scopes:dict[str,EntryContextScope]={}
        runtime.assembly.text_commands.current_persona=persona

    def bind_entry(self,entry: ContentEntryPort,port: MemoryReadPort,subjects: tuple[str,...],related: tuple[str,...],worlds: object) -> None:
        """Trusted setup binds exact existing rights; model output cannot call this."""
        from .content_service import ContentEntryPort
        r=self.runtime
        if (type(entry) is not ContentEntryPort or entry._runtime is not r or r._entries.get(id(entry)) is not entry
                or type(port) is not MemoryReadPort or port._service is not r.memory or r.memory._grants.get(id(port)) is not port
                or type(subjects) is not tuple or len(subjects)>16 or type(related) is not tuple or len(related)>2
                or len(set(subjects))!=len(subjects) or len(set(related))!=len(related)
                or any(not valid_identifier(value) for value in (*subjects,*related))):raise InvalidValue()
        frozen=cast(tuple[Value,...],freeze_value(SequenceSchema(WORLD,1,16),worlds,owned=True))
        if any(r.memory._authorize(port,sid,'read_subject') is not None for sid in subjects) or any(
                r.memory._authorize(port,oid,'get_current') is not None for oid in related):raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        if entry._entry not in self._scopes and len(self._scopes)>=r.settings.integer('runtime.max_active_entries'):
            raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
        self._scopes[entry._entry]=EntryContextScope(port,subjects,related,frozen)

    async def collect(self,source: MappingProxyType[str,Value],work: MappingProxyType[str,Value],key: str,deadline: float):
        r=self.runtime;a=r.assembly;text=a.text_transactions
        scope=self._scopes.get(cast(str,source['entry_id']))
        if scope is None:raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        current=await self.require_current(deadline)
        subjects=[];related=[];members=[]
        for identity in scope.subjects:
            found=await scope.port.read_subject(identity)
            if type(found) is not Found:raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
            value=record(found.value);subjects.append({name:value[name] for name in ('subject_id','revision','kind')})
        for identity in scope.related:
            found=await scope.port.get_current(identity)
            if type(found) is not Found:raise OwnerFailure('PRECONDITION_FAILED','object','BASIS_UNAVAILABLE')
            related.append(record(found.value))
        for raw in sequence(source['ordered_members']):
            member=record(raw)
            payload=await a.ingress.rows.read('payload',{'message_id':member['message_id']})
            if len(payload)!=1:raise OwnerFailure('INTEGRITY_FAILURE','storage','CONTEXT_UNRECOVERABLE')
            members.append({'member':{'role':{'H':'HISTORY','T':'TARGET','R':'RECENT'}[cast(str,member['role'])],
                'message_id':member['message_id'],'payload_digest':member['payload_digest']},
                'event':decode_content(cast(str,payload[0]['body']).encode(),r.settings.integer('ingress.event_max_bytes'))})
        if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        resources=text.configuration.candidate.text.record('provider.generation')
        context={'context_version':1,'system_text':render_learning_instructions(subjects,scope.worlds),
            'user':{'members':members,'persona':current.value,'related':related,'identity':{'database_id':text.configuration.database_id,'instance_id':a.instance_id,
                **{name:source[name] for name in ('batch_id','run_id','source_id','config_snapshot_id')}}},
            'resources':{name:resources[name] for name in ('prompt_ref','prompt_digest','schema_ref','schema_digest','transform_ref','transform_digest')},
            'model_binding':{'profile_id':text.profile['profile_id'],'config_snapshot_id':text.configuration.snapshot_id,
                'profile_revision':derived_id('profile',text.configuration.snapshot_id,cast(str,text.profile['profile_id'])),
                'price_revision':record(text.account['price'])['revision_ref'],'protocol':'OPENAI_CHAT_COMPLETIONS','model_id':text.profile['model_id'],
                'capability_evidence_ref':resources['capability_evidence_ref'],'billing_evidence_ref':resources['billing_evidence_ref']}}
        operation={'owner_namespace':'runtime','operation_kind':'stage_learning_context','scope_id':a.instance_id,'operation_key':key}
        reference=r.memory.read_grant_reference(scope.port)
        frozen=freeze_context(context,text.request_identity(source,work),tuple({'object_id':value['object_id'],'revision':value['revision'],
            'grant_ref':reference,'snapshot_digest':digest(value)} for value in related),
            operation,cast(int,source['frozen_at_us']),cast(int,resources['reservation_input_bound']),text.chat)
        return frozen,scope.port

    async def require_current(self,deadline: float) -> Available:
        """Distinguish missing publication from occupied or damaged local owners."""
        current=await self.persona.read_current(deadline)
        if type(current) is Available:return current
        if type(current) is Unavailable:raise OwnerFailure('PRECONDITION_FAILED','state','PERSONA_REQUIRED')
        if type(current) is PersonaFailed:
            error=current.error
            raise OwnerFailure(error.code,error.field,error.reason,current.cleanup_pending)
        raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
