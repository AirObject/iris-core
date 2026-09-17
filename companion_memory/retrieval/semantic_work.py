"""Native work coordination, immutable artifact reception and local deletion.

All memory revision checks and acknowledgments participate in the same original
transaction. The deletion branch has no Provider access, request descriptor or
input leaf. Late paid results remain artifacts without clearing newer gaps.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
from collections.abc import Callable
from hashlib import sha256
from types import MappingProxyType
from typing import cast
import time
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.memory.semantic_tracking import MemorySemanticCoverage
from companion_memory.memory.formats import record,sequence
from companion_memory.persistence import PersistenceService,UnitOfWork,Value,ResultBoundCommandDefinition
from companion_memory.persistence.definitions import CommandSpec
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_records import Record,identity,isolate,number,string,RECEIPT
from companion_memory.provider.daily_service import DailyProvider
from companion_memory.provider.embedding_service import EmbeddingProvider,EmbeddingRequest,EmbeddingResult
from companion_memory.provider.values import as_record
from .semantic_repository import TABLES
from .semantic_payloads import input_leaves,restore_input,artifact_leaves,restore_vector
from .semantic_material import render_document


class SemanticWork:
    """One retrieval owner for prepared work, application and bounded recovery."""
    def __init__(self,catalog: StatementCatalog,storage: PersistenceService,configuration: StoredSemanticConfiguration | StoredCognitionConfiguration,instance: str,
                 memory: MemorySemanticCoverage,provider: EmbeddingProvider,definitions: tuple[CommandSpec,...],
                 checkpoint: Callable[[],None],normal: Callable[[],None],authorization: Callable[[str],bool]):
        if (type(memory) is not MemorySemanticCoverage or type(provider) is not (DailyProvider if (type(configuration) is StoredDailyConfiguration or type(configuration) is StoredDreamConfiguration or type(configuration) is StoredManagedConfiguration) else EmbeddingProvider) or memory.objects.storage is not storage
                or memory.objects.configuration is not configuration or provider.configuration is not configuration
                or provider.instance!=instance or memory.objects.instance_id!=instance):raise ValueError('Native semantic owners must share one instance.')
        self.catalog=catalog;self.storage=storage;self.configuration=configuration;self.instance=instance;self.memory=memory;self.provider=provider
        self.checkpoint=checkpoint;self.normal=normal;self.authorization=authorization
        self.space=memory.space_id;self.rows=SemanticRecords(catalog,TABLES,storage,instance)
        self.config=MappingProxyType({'database_id':configuration.database_id,'instance_id':instance,'snapshot_id':configuration.snapshot_id})
        self.control_id=identity('semantic-control',instance,self.space)
        self.definitions={d.operation_kind:d for d in definitions}
        self._binding: tuple[EmbeddingRequest,Record]|None=None;self._result: EmbeddingResult|None=None
        self._local_failure: tuple[str,str]|None=None;self._not_sent:tuple[str,str]|None=None
        provider.bind_retrieval(catalog)

    def hold_binding(self,request:EmbeddingRequest,intent:Record) -> Callable[[],None]:
        """Retain a native first-request binding through actual transaction end."""
        if self._binding is not None:raise OwnerFailure('RESOURCE_BUSY','state','CLEANUP_PENDING',True)
        binding=(request,intent);self._binding=binding
        def release():
            if self._binding is binding:self._binding=None
        return release

    def hold_received(self,result:EmbeddingResult) -> Callable[[],None]:
        """Retain complete Provider material until its reception transaction ends."""
        if self._result is not None:raise OwnerFailure('RESOURCE_BUSY','state','CLEANUP_PENDING',True)
        self._result=result
        def release():
            if self._result is result:self._result=None
            self.provider.release_result(result)
        return release

    def hold_unsent(self,work_id:str,error:str) -> Callable[[],None]:
        """Supply local absence while the Provider's native absence lease is held."""
        if self._not_sent is not None:raise OwnerFailure('RESOURCE_BUSY','state','CLEANUP_PENDING',True)
        proof=(work_id,error);self._not_sent=proof
        def release():
            if self._not_sent is proof:self._not_sent=None
        return release

    @staticmethod
    def work_id(payload: Record,instance: str) -> str:
        """Separate local deletion proof from every network request identity."""
        if payload['kind']=='DELETE_LOCAL':
            return identity('semantic-delete-work',instance,payload['space_id'],payload['object_ref'],payload['change_seq'],payload['deletion_ref'])
        digest=sha256(string(payload['rendered_text']).encode()).hexdigest()
        return identity('embedding-work',instance,payload['space_id'],payload['purpose'],payload['object_ref'],payload['change_seq'],
            payload['partition_id'],digest,payload['original_request_key'])

    def _required(self,name: str,uow: UnitOfWork,key: str) -> Record:
        value=self.rows.get(name,uow,key)
        if value is None:raise OwnerFailure('PRECONDITION_FAILED','object','SOURCE_CHANGED')
        return value

    def _change(self,name: str,uow: UnitOfWork,old: Record,targets: list[Record],**changes: Value) -> Record:
        updated=self.rows.write(name,uow,dict(old)|{'revision':number(old['revision'])+1,**changes},expected_revision=number(old['revision']))
        targets.append(MappingProxyType({'object_id':old['row_id'],'previous_revision':old['revision'],'revision':updated['revision']}))
        return updated

    def _finish(self,uow: UnitOfWork,control: Record,targets: list[Record],items: tuple[str,...],*,memory: bool=False,**changes: Value) -> object:
        self._change('semantic_control',uow,control,targets,operation_count=number(control['operation_count'])+1,**changes)
        counts=self.storage.transaction_row_changes(uow)
        facts: dict[str,object]={'retrieval':{'rows_changed':counts.get('retrieval',0),'state':'APPLIED','references':[],'counts':[]}}
        if memory:facts['memory']={'rows_changed':counts.get('memory',0),'state':'APPLIED','references':[],'counts':[],**self.memory.audit_fact(uow)}
        return {'outcome':'APPLIED','targets':tuple(targets),'items':items,'facts':facts}

    def _deletion(self,uow: UnitOfWork,reference: Record,object_ref: Record,seq: int) -> str:
        current,gap=self.memory.current(uow,string(object_ref['object_id']))
        tombstone=self.memory.deletion(uow,string(object_ref['object_id']))
        definition=self.definitions.get(string(reference['kind']))
        maintenance=tuple('information_apply_memory_'+mask for mask in ('none','ingress','media','ingress_media'))
        candidates=tuple('information_apply_candidate_changes'+suffix for suffix in ('','_with_media','_with_goals','_with_media_and_goals'))
        if definition is None or definition.operation_kind not in (*maintenance,*candidates):
            raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        receipt=self.storage.confirm_prior_operation(uow,definition,string(reference['key']))
        if (current is not None or gap is None or gap['action']!='DELETE' or gap['object_revision']!=object_ref['revision'] or gap['latest_change_seq']!=seq
                or tombstone['deletion_revision']!=object_ref['revision'] or tombstone['kind']!='MEMORY'
                or receipt is None or receipt.fingerprint!=reference['fingerprint']):raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        references=sequence(record(record(record(receipt.result)['facts'])['memory'])['references'])
        root_matches=(any(record(ref)['name']=='root' and record(ref)['object_id']==tombstone['operation_ref'] for ref in references)
            if definition.operation_kind in maintenance else record(receipt.result)['operation_id']==tombstone['operation_ref'])
        if not root_matches:
            raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        if not any(record(t)['object_id']==object_ref['object_id'] and record(t)['revision']==object_ref['revision'] for t in sequence(record(receipt.result)['targets'])):
            raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        return string(tombstone['operation_ref'])

    def _artifact(self,uow: UnitOfWork,work: Record,artifact_id: str) -> Record:
        artifact=self._required('embedding_artifact',uow,artifact_id)
        if any(artifact[k]!=work[k] for k in ('config','space_id','purpose','partition_id','material_digest')):raise ValueError('Artifact material differs.')
        reference=record(artifact['completion_ref']);definition=self.definitions['record_result']
        receipt=self.storage.confirm_prior_operation(uow,definition,string(reference['key']))
        if reference['kind']!='record_result' or receipt is None or receipt.fingerprint!=reference['fingerprint'] or artifact_id not in sequence(record(receipt.result)['items']):
            raise ValueError('Original artifact reception differs.')
        leaves=tuple(self.rows.get('embedding_vector_leaf',uow,identity('embedding-vector-leaf',artifact_id,n)) for n in range(2))
        restore_vector(artifact_id,string(artifact['vector_digest']),leaves)
        return artifact

    def handle(self,kind: str,uow: UnitOfWork,envelope: Record,payload: Record) -> object:
        """Apply only original native work commands with current owner evidence."""
        operation,fingerprint=self.storage.semantic_operation_context(uow,self.catalog.definition)
        if operation.operation_kind!=kind or operation.scope_id!=self.instance or envelope['binding_id']!=self.instance:raise ValueError('Semantic work scope differs.')
        self.checkpoint();uow.require_commit_permission(self._commit_allowed)
        completion=isolate(RECEIPT,{'kind':kind,'key':operation.operation_key,'fingerprint':fingerprint})
        control=self._required('semantic_control',uow,self.control_id);targets: list[Record]=[]
        if kind in ('pause','resume'):
            if payload['space_id']!=self.space or payload['expected_revision']!=control['revision']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            if kind=='pause':
                if control['scheduler']=='PAUSED' and control['pause_reason']==payload['reason']:raise OwnerFailure('PRECONDITION_FAILED','state','NO_CHANGE')
                return self._finish(uow,control,targets,(),scheduler='PAUSED',pause_reason=payload['reason'])
            self.normal()
            if (control['scheduler']!='PAUSED' and (type(self.configuration) is not StoredDailyConfiguration and type(self.configuration) is not StoredDreamConfiguration and type(self.configuration) is not StoredManagedConfiguration) or control['pause_reason'] in ('UNKNOWN','INTEGRITY') or not self.authorization(string(payload['authorization_digest']))):
                raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
            return self._finish(uow,control,targets,(),scheduler='ENABLED',pause_reason='NONE',authorization_digest=payload['authorization_digest'])
        if kind=='prepare':
            if payload['config']!=self.config or payload['space_id']!=self.space or payload['work_id']!=self.work_id(payload,self.instance):raise ValueError('Preparation identity differs.')
            if number(self.rows.statements.stage('semantic_work_active_count',uow,{})[0]['count'])>=64:raise OwnerFailure('RESOURCE_BUSY','state','CAPACITY_REACHED')
            local=payload['kind']=='DELETE_LOCAL'
            if local:self._deletion(uow,record(payload['deletion_ref']),record(payload['object_ref']),number(payload['change_seq']))
            else:
                self.normal()
                if control['scheduler']!='ENABLED':raise OwnerFailure('MODE_BLOCKED','state','NOT_READY')
                if payload['purpose']=='DOCUMENT':
                    ref=record(payload['object_ref']);current,gap=self.memory.current(uow,string(ref['object_id']))
                    if (current is None or gap is None or current['revision']!=ref['revision'] or gap['object_revision']!=ref['revision']
                            or gap['action']!='UPSERT' or gap['latest_change_seq']!=payload['change_seq'] or render_document(current).decode()!=payload['rendered_text']):
                        raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            work={'v':1,'revision':1,'row_id':payload['work_id'],'work_id':payload['work_id'],'config':self.config,'space_id':self.space,
                'object_ref':payload['object_ref'],'change_seq':payload['change_seq'],'superseded_by_revision':None,'error':'NONE','cleanup_pending':False,
                'created_at':envelope['observed_at'],'completion_ref':None,'kind':payload['kind'],'state':'LOCAL_PREPARED' if local else 'PREPARED'}
            if local:work['deletion_ref']=payload['deletion_ref'];leaves=()
            else:
                text=string(payload['rendered_text']);leaves=input_leaves(string(payload['work_id']),text,string(payload['purpose']))
                work.update({'purpose':payload['purpose'],'partition_id':payload['partition_id'],'material_digest':sha256(text.encode()).hexdigest(),
                    'input_leaf_count':len(leaves),'input_bytes':len(text.encode()),'original_request_key':payload['original_request_key'],
                    'intent':None,'request_ref':None,'artifact_id':None,'deadline_at':None})
                matches=self.rows.statements.stage('semantic_artifact_matching',uow,{k:work[k] for k in ('space_id','purpose','partition_id','material_digest')})
                if matches:
                    original=self.rows.decode('embedding_artifact',matches[0])
                    self._artifact(uow,cast(Record,MappingProxyType(work)),string(original['artifact_id']))
                    work.update(state='RESULT_STORED',artifact_id=original['artifact_id'],completion_ref=original['completion_ref'])
            created=self.rows.write('embedding_work',uow,work)
            targets.append(MappingProxyType({'object_id':created['row_id'],'previous_revision':None,'revision':1}))
            for leaf in leaves:
                self.rows.write('embedding_input_leaf',uow,leaf);targets.append(MappingProxyType({'object_id':leaf['row_id'],'previous_revision':None,'revision':1}))
            return self._finish(uow,control,targets,(string(created['work_id']),))
        work=self._required('embedding_work',uow,string(payload['work_id']))
        if work['revision']!=payload['expected_revision'] or work['config']!=self.config or work['space_id']!=self.space:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        local=work['kind']=='DELETE_LOCAL'
        if kind=='bind':
            self.normal();binding=self._binding
            if local or work['state']!='PREPARED' or work['intent'] is not None or control['scheduler']!='ENABLED' or binding is None:raise ValueError('Original unbound work required.')
            request,intent=binding
            if (request.description['work_id']!=work['work_id'] or intent!=payload['intent'] or request.description['deadline_at']!=payload['deadline_at']
                    or not self.provider.permit(request.description,intent) or intent['request_digest']!=request.fingerprint):raise ValueError('Original sending intention differs.')
            self._change('embedding_work',uow,work,targets,intent=intent,deadline_at=payload['deadline_at'])
            return self._finish(uow,control,targets,(string(work['work_id']),))
        if kind=='record_result':
            result=self._result
            if local or work['state'] not in ('PREPARED','BOUND','REMOTE_UNKNOWN') or result is None:raise ValueError('Original complete Provider result required.')
            self.provider.verify_result(uow,result,work)
            if payload['provider_completion']!=self.provider.reference(result.completion) or payload['request_ref']!={'request_id':result.request['object_id'],'attempt_id':result.attempt['object_id']}:
                raise ValueError('Original completion binding differs.')
            if number(self.rows.statements.stage('semantic_artifact_count',uow,{})[0]['count'])>=8192:raise OwnerFailure('RESOURCE_BUSY','state','CAPACITY_REACHED')
            artifact_id=cast(str,result.handoff['artifact_id']);model=as_record(as_record(result.request['execution_evidence'])['profile'])['model_id']
            leaves=artifact_leaves(artifact_id,result.value,space_id=self.space,model_id=cast(str,model))
            from companion_memory.persistence.semantic_records import leaf_bytes
            digest=sha256(b''.join(leaf_bytes(leaf,maximum=4096,exact=4096) for leaf in leaves)).hexdigest()
            artifact=self.rows.write('embedding_artifact',uow,{'v':1,'revision':1,'row_id':artifact_id,'artifact_id':artifact_id,
                **{k:work[k] for k in ('config','space_id','purpose','partition_id','material_digest')},'request_ref':payload['request_ref'],
                'dimension':1024,'vector_digest':digest,'vector_leaf_count':2,'usage_ref':payload['provider_completion'],
                'completion_ref':completion,'received_at':envelope['observed_at']})
            targets.append(MappingProxyType({'object_id':artifact_id,'previous_revision':None,'revision':1}))
            for leaf in leaves:
                self.rows.write('embedding_vector_leaf',uow,leaf);targets.append(MappingProxyType({'object_id':leaf['row_id'],'previous_revision':None,'revision':1}))
            self._change('embedding_work',uow,work,targets,state='RESULT_STORED',artifact_id=artifact['artifact_id'],request_ref=payload['request_ref'],completion_ref=completion,cleanup_pending=True)
            return self._finish(uow,control,targets,(artifact_id,))
        if kind=='apply':
            if payload['kind']!=work['kind'] or payload['object_ref']!=work['object_ref'] or payload['latest_seq']!=work['change_seq']:raise ValueError('Application revision differs.')
            ref=record(work['object_ref'])
            deletion_operation=None
            if local:
                if work['state']!='LOCAL_PREPARED' or payload['deletion_ref']!=work['deletion_ref']:raise ValueError('Original local deletion required.')
                deletion_operation=self._deletion(uow,record(work['deletion_ref']),ref,number(work['change_seq']));artifact_id=None;digest=None
            else:
                if work['state']!='RESULT_STORED' or work['purpose']!='DOCUMENT' or payload['artifact_id']!=work['artifact_id']:raise ValueError('Complete document artifact required.')
                artifact=self._artifact(uow,work,string(payload['artifact_id']));artifact_id=string(artifact['artifact_id']);digest=string(artifact['material_digest'])
                current,_=self.memory.current(uow,string(ref['object_id']))
                if current is None or sha256(render_document(current)).hexdigest()!=digest:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            ack,publication=self.memory.acknowledge(uow,string(ref['object_id']),number(ref['revision']),number(work['change_seq']),
                artifact_id=artifact_id,material_digest=digest,evidence=completion,
                deletion_operation=deletion_operation)
            targets.extend(MappingProxyType({'object_id':v['row_id'],'previous_revision':None if number(v['revision'])==1 else number(v['revision'])-1,'revision':v['revision']}) for v in (ack,publication))
            self._change('embedding_work',uow,work,targets,state='LOCAL_APPLIED' if local else 'APPLIED',completion_ref=completion)
            return self._finish(uow,control,targets,(string(ref['object_id']),),memory=True)
        if kind=='supersede':
            ref=record(work['object_ref']);current,gap=self.memory.current(uow,string(ref['object_id']))
            revision=number(current['revision']) if current is not None else number(self.memory.deletion(uow,string(ref['object_id']))['deletion_revision'])
            if (gap is None or revision<=number(ref['revision']) or payload['current_object_revision']!=revision or payload['current_change_seq']!=gap['latest_change_seq']):
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            if local:
                if work['state']!='LOCAL_PREPARED':raise ValueError('Local work is already terminal.')
                state='LOCAL_SUPERSEDED'
            elif work['state']=='REMOTE_UNKNOWN':state='REMOTE_UNKNOWN'
            elif work['state'] in ('RESULT_STORED','NOT_SENT','KNOWN_FAILED'):state='SUPERSEDED'
            else:raise ValueError('No reliable original terminal for supersession.')
            self._change('embedding_work',uow,work,targets,state=state,superseded_by_revision=revision,completion_ref=completion)
            return self._finish(uow,control,targets,(string(work['work_id']),))
        if kind=='fail':
            if payload['kind']!=work['kind'] or payload['error']=='NONE':raise ValueError('Invalid failure evidence.')
            if local:
                if work['state']!='LOCAL_PREPARED' or self._local_failure!=(work['work_id'],payload['error']):raise ValueError('Native deterministic local rejection required.')
                changes={'state':'LOCAL_FAILED','completion_ref':completion}
            else:
                request=self.provider.work_request(uow,work)
                if request is None:
                    if (work['state']!='PREPARED' or payload['request_ref'] is not None or payload['terminal_receipt'] is not None
                            or self._not_sent!=(work['work_id'],payload['error'])):raise ValueError('Native reliable absence required.')
                    self._change('embedding_work',uow,work,targets,state='NOT_SENT',error=payload['error'],completion_ref=completion,cleanup_pending=False)
                    return self._finish(uow,control,targets,(string(work['work_id']),),last_cleanup_at=envelope['observed_at'])
                if request['phase'] not in ('TERMINAL','REMOTE_RESULT_UNKNOWN'):raise ValueError('Original Provider failure evidence required.')
                attempt=self.provider.verify_failure(uow,work,record_reference=record(payload['terminal_receipt']),request_ref=payload['request_ref'])
                changes={'state':'REMOTE_UNKNOWN' if request['phase']=='REMOTE_RESULT_UNKNOWN' else 'NOT_SENT' if attempt['state']=='NOT_SENT' else 'KNOWN_FAILED','completion_ref':completion,
                    'request_ref':payload['request_ref'],'cleanup_pending':True}
            self._change('embedding_work',uow,work,targets,error=payload['error'],**changes)
            return self._finish(uow,control,targets,(string(work['work_id']),))
        if kind=='record_cleanup':
            if payload['kind']!=work['kind'] or payload['cleanup_pending'] is not False:raise ValueError('Actual ended cleanup evidence is required.')
            if local:
                if work['state'] not in ('LOCAL_APPLIED','LOCAL_SUPERSEDED','LOCAL_FAILED') or payload['completion_receipt']!=work['completion_ref']:raise ValueError('Original local completion required.')
                proof=record(work['completion_ref']);receipt=self.storage.confirm_prior_operation(uow,self.definitions[string(proof['kind'])],string(proof['key']))
                if receipt is None or receipt.fingerprint!=proof['fingerprint']:raise ValueError('Original local commit is unconfirmed.')
            else:
                if payload['request_ref']!=work['request_ref']:raise ValueError('Cleanup request identity differs.')
                self.provider.verify_cleanup(uow,work,record(payload['provider_receipt']))
            self._change('embedding_work',uow,work,targets,cleanup_pending=False)
            return self._finish(uow,control,targets,(string(work['work_id']),),last_cleanup_at=envelope['observed_at'])
        raise ValueError('Unsupported semantic work command.')

    def _commit_allowed(self) -> bool:
        try:self.checkpoint();return True
        except Exception:return False

    async def text(self,work: Record) -> str:
        """Recover only complete original input bytes; DELETE_LOCAL has no text."""
        if work['kind']!='EMBED':raise ValueError('Local deletion has no input.')
        leaves=tuple([await self.rows.read('embedding_input_leaf',identity('embedding-input-leaf',work['work_id'],n)) for n in range(number(work['input_leaf_count']))])
        return restore_input(string(work['work_id']),string(work['purpose']),number(work['input_bytes']),string(work['material_digest']),leaves)
