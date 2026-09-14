"""Atomic context publication with the frozen runtime work and live read rights.

The command receives bounded encoded material, while a separately retained native
read scope proves its source authority. No caller-supplied grant name or digest
can create that scope. Ordinary Provider association cannot bypass this write.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.cognition.text_context import FrozenContext,restore_context
from companion_memory.cognition.text_resources import learning_authorizations
from companion_memory.ingress.events import canonical_event
from companion_memory.memory.formats import record,sequence
from companion_memory.memory.sources import decode_source
from companion_memory.memory.service import MemoryReadPort,MemoryService
from companion_memory.persistence import ResultBoundCommandDefinition,RecordSchema,Field,BoundedTextSchema,SequenceSchema,UnitOfWork,Value
from companion_memory.persistence.content_codec import decode_content,encode_content
from companion_memory.persistence.text_records import ID,REVISION
from companion_memory.persistence.text_results import audits,result_schema,result,INTENT
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.self_model.repository import persona_catalog
from companion_memory.self_model.storage import PersonaStorage
from companion_memory.self_model.current import CurrentPersonaPort,Matched
if TYPE_CHECKING:
    from .content_assembly import ContentAssembly


class TextContextCommands:
    """One registered command and at most one separately owned context/read scope."""
    def __init__(self,assembly: ContentAssembly):
        if not assembly.text_format:raise InvalidValue()
        self.assembly=assembly;self.catalog=persona_catalog();self.persona:PersonaStorage|None=None
        self.current_persona:CurrentPersonaPort|None=None
        self._closed=False
        self._retained:tuple[FrozenContext,MemoryReadPort]|None=None
        requirements,bindings=audits('stage_learning_context',('cognition','runtime'))
        self.definition=ResultBoundCommandDefinition('runtime','stage_learning_context',1,
            RecordSchema((Field('operation_id',ID),Field('batch_id',ID),Field('expected_revision',REVISION),Field('generation',REVISION),
                Field('manifest',BoundedTextSchema(8192)),Field('leaves',SequenceSchema(BoundedTextSchema(8192),1,8)))),
            1,result_schema(('cognition','runtime'),('CONTEXT_STORED',)),assembly.repositories+(self.catalog.definition,),
            requirements,self.handle,INTENT,bindings)

    def bind(self) -> None:
        text=self.assembly.text_transactions
        self.persona=PersonaStorage(self.catalog,self.assembly.storage,text.configuration,self.assembly.instance_id)

    def retain(self,context: FrozenContext,read_port: MemoryReadPort) -> None:
        """Trusted coordinator retains its original material until actual cleanup."""
        if self._closed or type(context) is not FrozenContext or type(read_port) is not MemoryReadPort:raise InvalidValue()
        owner=MemoryReadPort._native(read_port)
        if type(owner) is not MemoryService or owner._owner is not self.assembly.memory:raise InvalidValue()
        if self._retained is not None and (self._retained[0]!=context or self._retained[1] is not read_port):
            raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING')
        self._retained=context,read_port

    def release_retained(self,context: FrozenContext) -> None:
        """Coordinator calls only after its original command's actual work ended."""
        if self._retained is not None and self._retained[0] is context:self._retained=None

    def handle(self,uow: UnitOfWork,values: MappingProxyType[str,Value]):
        a=self.assembly;text=a.text_transactions
        if self._closed or self.persona is None or self._retained is None:raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
        retained,port=self._retained
        mode=a._get('mode',uow,'mode_id','instance_mode')
        work=a._get('work',uow,'batch_id',values['batch_id']);batch=a._get('batches',uow,'batch_id',values['batch_id'])
        if (mode['state'] not in ('NORMAL','DRAINING') or batch['terminal']!='FROZEN' or work['phase']!='FROZEN'
                or work['revision']!=values['expected_revision'] or work['generation']!=values['generation']
                or work['model_binding'] is not None or work['provider_operation_key'] is not None):
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        source=decode_source(cast(str,batch['manifest']))
        context=restore_context(decode_content(cast(str,values['manifest']).encode(),8192),
            tuple(decode_content(cast(str,leaf).encode(),8192) for leaf in sequence(values['leaves'])),text.request_identity(source,work),text.chat)
        operation={'owner_namespace':'runtime','operation_kind':'stage_learning_context','scope_id':a.instance_id,'operation_key':values['operation_id']}
        if context!=retained or context.manifest['original_operation']!=operation or context.manifest['created_at_us']!=source['frozen_at_us']:raise InvalidValue()
        user=record(context.context['user'])
        if self.current_persona is None:
            if self.persona.current_projection(uow)!=user['persona']:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        else:
            selected=record(user['persona'])
            verified=self.current_persona.verify_current(uow,cast(str,selected['publication_id']),cast(int,selected['revision']))
            if type(verified) is not Matched or verified.value!=selected:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        if tuple(({'H':'HISTORY','T':'TARGET','R':'RECENT'}[cast(str,record(item)['role'])],record(item)['message_id'],record(item)['payload_digest'])
                for item in sequence(source['ordered_members']))!=tuple((record(item)['role'],record(item)['message_id'],record(item)['payload_digest'])
                for item in sequence(context.manifest['ordered_members'])):raise InvalidValue()
        for original,frozen in zip(sequence(source['ordered_members']),sequence(user['members']),strict=True):
            member=record(original);a.ingress.verify_member(uow,cast(str,source['entry_id']),member)
            payload=a.ingress.rows.stage('payload',uow,{'message_id':member['message_id']})
            if len(payload)!=1 or cast(str,payload[0]['body']).encode()!=canonical_event(record(record(frozen)['event'])):raise InvalidValue()
        owner=MemoryReadPort._native(port)
        if type(owner) is not MemoryService or owner._owner is not a.memory:raise InvalidValue()
        if any(record(reference)['grant_ref']!=owner.read_grant_reference(port) for reference in sequence(context.manifest['related_objects'])):
            raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
        roster=learning_authorizations(cast(str,context.context['system_text']),text.chat.requested_model)
        for raw in sequence(roster['subjects']):
            subject=record(raw);identity=cast(str,subject['subject_id'])
            if owner._authorize(port,identity,'read_subject') is not None:raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
            current=a.memory.subject(uow,identity)
            if current is None or any(current[name]!=subject[name] for name in ('subject_id','revision','kind')):raise InvalidValue()
        for raw in sequence(user['related']):
            related=record(raw);identity=cast(str,related['object_id'])
            if owner.usage_current(port,uow,identity,deep=False)!=related:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        uow.require_commit_permission(lambda:not self._closed and self._retained is not None and self._retained[0] is retained
            and owner._grants.get(id(port)) is port and not owner._closed and owner._gate() is None)
        text.contexts.stage(uow,context,text.chat)
        revision=cast(int,work['revision'])+1
        a.rows.stage('work_update',uow,{**work,'revision':revision,'model_binding':encode_content(text.work_binding(context),8192).decode()})
        context_ref={'kind':'CONTEXT','object_id':context.manifest['object_id'],'revision':1}
        work_ref={'kind':'WORK','object_id':values['batch_id'],'revision':revision}
        return result(cast(str,values['operation_id']),'CONTEXT_STORED',{
            'cognition':{'rows_changed':1+len(context.leaves),'references':(context_ref,)},
            'runtime':{'rows_changed':1,'references':(work_ref,)}},(context_ref,work_ref),
            ({'object_id':context.manifest['object_id'],'previous_revision':None,'revision':1},
             {'object_id':values['batch_id'],'previous_revision':work['revision'],'revision':revision}))

    def close(self) -> bool:
        self._closed=True
        return self.persona is None or self.persona.close()
