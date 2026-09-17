"""Transactional frozen generations and independent file-owner confirmation.

Only current memory acknowledgments and complete retained artifacts supply
members. Publication rechecks the entire authoritative set in one UoW. File
preparation runs outside transactions and never advances publication itself.
"""
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
import asyncio
import time
from collections.abc import Callable
from typing import cast
from companion_memory.persistence.deadlines import DeadlineScope,current_deadline,check_deadline
from companion_memory.persistence.completion import retain_completion
from hashlib import sha256
from types import MappingProxyType
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration,stored_semantic_configuration_issue
from companion_memory.memory.semantic_tracking import MemorySemanticCoverage
from companion_memory.persistence import PersistenceService,UnitOfWork,Value
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_records import Record,identity,number,string
from .semantic_repository import TABLES
from .semantic_files import VectorFiles,WrittenVectorPage,SealedGeneration
from .semantic_binary import VectorMember,InvalidVectorFile
from .semantic_material import render_document
from .semantic_payloads import restore_vector


class SemanticGenerations:
    """One retrieval owner's bounded index participant and retained file proofs."""
    def __init__(self,catalog: StatementCatalog,storage: PersistenceService,configuration: StoredSemanticConfiguration | StoredCognitionConfiguration,
                 instance: str,memory: MemorySemanticCoverage,files: VectorFiles,checkpoint: Callable[[],None]):
        if ((stored_cognition_configuration_issue(configuration,storage=storage) if (type(configuration) is StoredDailyConfiguration or type(configuration) is StoredDreamConfiguration or type(configuration) is StoredManagedConfiguration) else stored_semantic_configuration_issue(configuration)) is not None or catalog.definition.owner_module!='retrieval'
                or type(memory) is not MemorySemanticCoverage or type(files) is not VectorFiles
                or memory.objects.storage is not storage or memory.objects.instance_id!=instance
                or memory.objects.configuration is not configuration):
            raise ValueError('Native semantic generation owners are required.')
        self.catalog=catalog;self.storage=storage;self.memory=memory;self.files=files;self._observe=checkpoint
        self.local_seconds=number(configuration.candidate.text.record('retrieval.semantic_storage')['local_step_ms'])
        self._io:asyncio.Task[object]|None=None
        self.rows=SemanticRecords(catalog,TABLES,storage,instance);self.instance=instance;self.space=memory.space_id
        self.config=MappingProxyType({'database_id':configuration.database_id,'instance_id':instance,'snapshot_id':configuration.snapshot_id})
        if configuration.candidate.text.record('retrieval.semantic')['space_id']!=self.space:raise ValueError('Embedding space differs.')
        self.control_id=identity('semantic-control',instance,self.space)
        self._page: WrittenVectorPage | None=None;self._sealed: SealedGeneration | None=None
        self._retirement: tuple[str,int,bool] | None=None

    def checkpoint(self) -> None:
        """Resource observation and the same inherited absolute local budget."""
        check_deadline();self._observe()

    @property
    def io_pending(self) -> bool:
        return self._io is not None and not self._io.done()

    async def _file[T](self, action: Callable[[],T]) -> T:
        """Return by the original deadline while retaining actual file I/O."""
        self.checkpoint()
        if self.io_pending:raise OwnerFailure('RESOURCE_BUSY','state','OWNER_ACTIVE',True)
        task=asyncio.create_task(asyncio.to_thread(action))
        self._io=cast(asyncio.Task[object],task);retain_completion(task)
        task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        done,_=await asyncio.wait((task,),timeout=max(0,current_deadline(self.local_seconds/1000)-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        result=task.result();self.checkpoint();return result

    def _required(self,name: str,uow: UnitOfWork,key: str) -> Record:
        value=self.rows.get(name,uow,key)
        if value is None:raise OwnerFailure('PRECONDITION_FAILED','index','SOURCE_CHANGED')
        return value

    def _change(self,name: str,uow: UnitOfWork,old: Record,targets: list[Record],**changes: Value) -> Record:
        value=self.rows.write(name,uow,dict(old)|{'revision':number(old['revision'])+1,**changes},expected_revision=number(old['revision']))
        targets.append(MappingProxyType({'object_id':value['row_id'],'previous_revision':old['revision'],'revision':value['revision']}))
        return value

    def _finish(self,uow: UnitOfWork,control: Record,targets: list[Record],items: tuple[str,...],memory: bool=False,/,**changes: Value) -> object:
        self._change('semantic_control',uow,control,targets,operation_count=number(control['operation_count'])+1,**changes)
        counts=self.storage.transaction_row_changes(uow)
        facts: dict[str,object]={'retrieval':{'rows_changed':counts.get('retrieval',0),'state':'APPLIED','references':[],'counts':[]}}
        if memory:facts['memory']={'rows_changed':counts.get('memory',0),'state':'APPLIED','references':[],'counts':[],**self.memory.audit_fact(uow)}
        return {'outcome':'APPLIED','targets':tuple(targets),'items':items,'facts':facts}

    def _covered(self,uow: UnitOfWork,captured: Value) -> None:
        coverage=self.memory.coverage(uow)
        if coverage['pending_count'] or coverage['captured_seq']!=captured:
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')

    def _vector(self,uow: UnitOfWork,current: Record,ack: Record) -> VectorMember:
        artifact=self._required('embedding_artifact',uow,string(ack['artifact_id']))
        digest=sha256(render_document(current)).hexdigest()
        if (artifact['config']!=self.config or artifact['space_id']!=self.space or artifact['purpose']!='DOCUMENT'
                or artifact['material_digest']!=digest or ack['material_digest']!=digest):
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        leaves=tuple(self.rows.get('embedding_vector_leaf',uow,identity('embedding-vector-leaf',artifact['artifact_id'],n)) for n in range(2))
        raw=restore_vector(string(artifact['artifact_id']),string(artifact['vector_digest']),leaves)
        return VectorMember(string(current['object_id']),number(current['revision']),number(ack['revision']),
            number(ack['applied_seq']),string(artifact['artifact_id']),raw)

    def _members(self,uow: UnitOfWork,generation: str,ordinal: int) -> tuple[Record,...]:
        values=self.rows.statements.stage('semantic_generation_members',uow,{'generation_id':generation,'ordinal':ordinal})
        return tuple(self.rows.decode('semantic_member',value) for value in values)

    def handle(self,kind: str,uow: UnitOfWork,envelope: Record,payload: Record) -> object:
        """Execute one native index command; payload never supplies member data."""
        operation,_=self.storage.semantic_operation_context(uow,self.catalog.definition)
        if operation.operation_kind!=kind or operation.scope_id!=self.instance:
            raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        self.checkpoint();control=self._required('semantic_control',uow,self.control_id);targets: list[Record]=[]
        generation_id=string(payload['generation_id'])
        if kind=='begin_generation':
            if (payload['space_id']!=self.space or payload['expected_control_revision']!=control['revision']
                    or control['building_generation'] is not None or control['retiring_generation'] is not None):
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            self._covered(uow,payload['captured_seq'])
            # The binary owner validates the full domain-separated identity even
            # before a generation row can consume a build slot.
            VectorFiles._name(generation_id)
            created=self.rows.write('semantic_generation',uow,{'v':1,'revision':1,'row_id':generation_id,
                'generation_id':generation_id,'space_id':self.space,'config':self.config,'state':'BUILDING',
                'captured_seq':payload['captured_seq'],'member_count':0,'page_count':0,'confirmed_pages':0,
                'file_name':generation_id,'file_bytes':0,'file_digest':None,'member_digest':None,'build_cursor':None,
                'retired_cursor':None,'error':'NONE','created_at':envelope['observed_at'],'published_at':None})
            targets.append(MappingProxyType({'object_id':created['row_id'],'previous_revision':None,'revision':1}))
            return self._finish(uow,control,targets,(generation_id,),building_generation=generation_id)
        generation=self._required('semantic_generation',uow,generation_id)
        if (generation['revision']!=payload['expected_revision'] or generation['config']!=self.config or generation['space_id']!=self.space):
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        if kind in ('append_page','confirm_page','seal_generation') and (generation['state']!='BUILDING' or control['building_generation']!=generation_id):
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        if kind=='append_page':
            self._covered(uow,generation['captured_seq'])
            if (payload['page_no']!=generation['page_count'] or payload['after_object_id']!=generation['build_cursor']
                    or number(generation['member_count'])%8):raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            originals=self.memory.index_members(uow,string(payload['after_object_id']) if payload['after_object_id'] is not None else '')
            if not originals:raise OwnerFailure('PRECONDITION_FAILED','index','NO_CHANGE')
            vectors=tuple(self._vector(uow,current,ack) for current,ack in originals)
            if number(generation['member_count'])+len(vectors)>4096:raise OwnerFailure('RESOURCE_BUSY','index','CAPACITY_REACHED')
            ids=[]
            for offset,member in enumerate(vectors):
                ordinal=number(generation['member_count'])+offset;row_id=identity('semantic-member',generation_id,ordinal)
                self.rows.write('semantic_member',uow,{'v':1,'revision':1,'row_id':row_id,'generation_id':generation_id,
                    'ordinal':ordinal,'object_id':member.object_id,'object_revision':member.object_revision,'ack_revision':member.ack_revision,
                    'applied_seq':member.applied_seq,'artifact_id':member.artifact_id,'vector_digest':member.vector_digest})
                targets.append(MappingProxyType({'object_id':row_id,'previous_revision':None,'revision':1}));ids.append(row_id)
            page_id=identity('semantic-page',generation_id,payload['page_no']);raw=b''.join(member.encode() for member in vectors)
            self.rows.write('semantic_page',uow,{'v':1,'revision':1,'row_id':page_id,'generation_id':generation_id,'page_no':payload['page_no'],
                'first_ordinal':generation['member_count'],'count':len(vectors),'members_digest':sha256(raw).hexdigest(),
                'state':'STAGED','byte_offset':4096+8384*number(generation['member_count']),'byte_count':len(raw)})
            targets.append(MappingProxyType({'object_id':page_id,'previous_revision':None,'revision':1}))
            self._change('semantic_generation',uow,generation,targets,member_count=number(generation['member_count'])+len(vectors),
                page_count=number(generation['page_count'])+1,build_cursor=vectors[-1].object_id)
            return self._finish(uow,control,targets,tuple(ids))
        if kind=='confirm_page':
            page=self._required('semantic_page',uow,identity('semantic-page',generation_id,payload['page_no']));proof=self._page
            if (page['state']!='STAGED' or proof is None or (proof.generation_id,proof.page_no,proof.offset,proof.member_count,proof.page_digest)
                    !=(generation_id,payload['page_no'],page['byte_offset'],page['count'],payload['page_digest'])
                    or page['members_digest']!=proof.page_digest):raise OwnerFailure('PRECONDITION_FAILED','index','REVISION_CONFLICT')
            self.files.confirm_written_page(proof,self.checkpoint)
            self._change('semantic_page',uow,page,targets,state='CONFIRMED')
            self._change('semantic_generation',uow,generation,targets,confirmed_pages=number(generation['confirmed_pages'])+1)
            return self._finish(uow,control,targets,())
        if kind in ('seal_generation','publish_generation'):
            sealed=self._sealed
            if (sealed is None or (sealed.generation_id,sealed.file_digest,sealed.file_bytes)!=(generation_id,payload['file_digest'],payload['file_bytes'])
                    or sealed.header.captured_seq!=generation['captured_seq'] or sealed.header.member_count!=generation['member_count']
                    or generation['confirmed_pages']!=generation['page_count']):raise OwnerFailure('PRECONDITION_FAILED','index','REVISION_CONFLICT')
            verified=self.files.verify(self.space,generation_id,self.checkpoint)
            if verified!=sealed:raise InvalidVectorFile('Retained sealed evidence differs.')
            page_count=0
            while pages:=self.rows.statements.stage('semantic_generation_pages',uow,{'generation_id':generation_id,'page_no':page_count}):
                self.checkpoint()
                for row in pages:
                    page=self.rows.decode('semantic_page',row)
                    if page['page_no']!=page_count or page['state']!='CONFIRMED':
                        raise OwnerFailure('PRECONDITION_FAILED','index','REVISION_CONFLICT')
                    members=self._members(uow,generation_id,8*page_count)
                    raw=b''.join(self._stored_vector(uow,value).encode() for value in members)
                    if len(members)!=page['count'] or len(raw)!=page['byte_count'] or sha256(raw).hexdigest()!=page['members_digest']:
                        raise OwnerFailure('PRECONDITION_FAILED','index','REVISION_CONFLICT')
                    page_count+=1
            if page_count!=generation['page_count']:raise OwnerFailure('PRECONDITION_FAILED','index','REVISION_CONFLICT')
            # Compare all persisted bindings to the complete file binding digest.
            bindings=sha256(b'[');count=0
            while values:=self._members(uow,generation_id,count):
                self.checkpoint()
                for value in values:
                    if value['ordinal']!=count:raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
                    member=self._stored_vector(uow,value)
                    if count:bindings.update(b',')
                    bindings.update(member.binding_bytes());count+=1
            bindings.update(b']')
            if count!=generation['member_count'] or bindings.digest()!=sealed.header.member_digest:
                raise OwnerFailure('PRECONDITION_FAILED','index','REVISION_CONFLICT')
            if kind=='seal_generation':
                self._change('semantic_generation',uow,generation,targets,state='READY',file_bytes=sealed.file_bytes,
                    file_digest=sealed.file_digest,member_digest=sealed.header.member_digest.hex())
                return self._finish(uow,control,targets,(generation_id,))
            if generation['state']!='READY' or control['building_generation']!=generation_id or control['revision']!=payload['expected_control_revision']:
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            self._covered(uow,generation['captured_seq']);after='';count=0
            while originals:=self.memory.index_members(uow,after):
                self.checkpoint();stored=self._members(uow,generation_id,count)
                if len(stored)!=len(originals):raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
                for (current,ack),value in zip(originals,stored):
                    if self._vector(uow,current,ack)!=self._stored_vector(uow,value):
                        raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
                    count+=1;after=string(current['object_id'])
            if count!=generation['member_count']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            old_id=control['current_generation']
            if old_id is not None:
                old=self._required('semantic_generation',uow,string(old_id))
                if old['state']!='PUBLISHED':raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
                self._change('semantic_generation',uow,old,targets,state='RETIRING')
            self._change('semantic_generation',uow,generation,targets,state='PUBLISHED',published_at=envelope['observed_at'])
            root=self.memory.publish(uow,generation_id,number(generation['captured_seq']),number(envelope['observed_at']))
            targets.append(MappingProxyType({'object_id':root['row_id'],'previous_revision':number(root['revision'])-1,'revision':root['revision']}))
            return self._finish(uow,control,targets,(generation_id,),True,current_generation=generation_id,
                building_generation=None,retiring_generation=old_id)
        if kind=='generation_fail':
            if generation['state'] not in ('BUILDING','READY') or payload['error']=='NONE':
                raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
            self._change('semantic_generation',uow,generation,targets,state='FAILED',error=payload['error'])
            return self._finish(uow,control,targets,(generation_id,))
        if kind=='retire_page':
            if (generation['state'] not in ('RETIRING','FAILED') or payload['expected_control_revision']!=control['revision']
                    or payload['after_row_id']!=generation['retired_cursor'] or control['current_generation']==generation_id
                    or self._retirement is None or self._retirement[:2]!=(generation_id,number(generation['revision']))):
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            if not self.files.retirement_ready(generation_id):raise OwnerFailure('RESOURCE_BUSY','index','CAPACITY_REACHED')
            values=self.rows.statements.stage('semantic_retirement_rows',uow,{'generation_id':generation_id,'after':generation['retired_cursor'] or ''})
            if values:
                for value in values:self.rows.remove(string(value['table_name']),uow,string(value['row_id']),number(value['revision']))
                self._change('semantic_generation',uow,generation,targets,retired_cursor=values[-1]['row_id'])
                return self._finish(uow,control,targets,tuple(string(value['row_id']) for value in values))
            if not self._retirement[2]:raise OwnerFailure('PRECONDITION_FAILED','index','REVISION_CONFLICT')
            # Confirmed file deletion is separate from any row-deletion page.
            # Original-key receipt lookup never re-enters this handler.
            self._change('semantic_generation',uow,generation,targets,state='RETIRED')
            changes: dict[str,Value]={}
            if control['retiring_generation']==generation_id:changes['retiring_generation']=None
            if control['building_generation']==generation_id:changes['building_generation']=None
            return self._finish(uow,control,targets,(),**changes)
        raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')

    def _stored_vector(self,uow: UnitOfWork,value: Record) -> VectorMember:
        leaves=tuple(self.rows.get('embedding_vector_leaf',uow,identity('embedding-vector-leaf',value['artifact_id'],n)) for n in range(2))
        vector=restore_vector(string(value['artifact_id']),string(value['vector_digest']),leaves)
        return VectorMember(string(value['object_id']),number(value['object_revision']),number(value['ack_revision']),
            number(value['applied_seq']),string(value['artifact_id']),vector)

    async def write_page(self,generation_id: str,page_no: int) -> WrittenVectorPage:
        """Write exactly one committed original page outside the storage UoW."""
        with DeadlineScope(current_deadline(self.local_seconds/1000)):
            self.checkpoint()
            generation=await self.rows.read('semantic_generation',generation_id)
            page=await self.rows.read('semantic_page',identity('semantic-page',generation_id,page_no))
            if generation is None or page is None or generation['state'] not in ('BUILDING','READY','PUBLISHED'):raise InvalidVectorFile('No original build page.')
            values=await self.rows.statements.read('semantic_generation_members',{'generation_id':generation_id,'ordinal':8*page_no})
            members=[]
            for row in values[:number(page['count'])]:
                value=self.rows.decode('semantic_member',row)
                leaves=tuple([await self.rows.read('embedding_vector_leaf',identity('embedding-vector-leaf',value['artifact_id'],n)) for n in range(2)])
                vector=restore_vector(string(value['artifact_id']),string(value['vector_digest']),leaves)
                members.append(VectorMember(string(value['object_id']),number(value['object_revision']),number(value['ack_revision']),
                    number(value['applied_seq']),string(value['artifact_id']),vector))
            proof=await self._file(lambda:self.files.stage_page(self.space,generation_id,number(generation['captured_seq']),page_no,tuple(members),self.checkpoint))
            if proof.page_digest!=page['members_digest'] or proof.member_count!=page['count']:raise InvalidVectorFile('Committed page differs.')
            self._page=proof;return proof

    async def seal_file(self,generation_id: str) -> SealedGeneration:
        """Finish a fully confirmed build from retained original artifact bytes."""
        with DeadlineScope(current_deadline(self.local_seconds/1000)):
            self.checkpoint()
            generation=await self.rows.read('semantic_generation',generation_id)
            if generation is None or generation['state'] not in ('BUILDING','READY','PUBLISHED') or generation['page_count']!=generation['confirmed_pages']:
                raise InvalidVectorFile('Not every original page is confirmed.')
            digest=sha256(b'[');ordinal=0
            while values:=await self.rows.statements.read('semantic_generation_members',{'generation_id':generation_id,'ordinal':ordinal}):
                self.checkpoint()
                for row in values:
                    value=self.rows.decode('semantic_member',row)
                    if value['ordinal']!=ordinal:raise InvalidVectorFile('Original member extent differs.')
                    from companion_memory.persistence.content_codec import encode_content
                    binding=tuple(value[key] for key in ('object_id','object_revision','ack_revision','applied_seq','artifact_id','vector_digest'))
                    if ordinal:digest.update(b',')
                    digest.update(encode_content(binding,2048));ordinal+=1
            digest.update(b']')
            if ordinal!=generation['member_count']:raise InvalidVectorFile('Original member count differs.')
            sealed=await self._file(lambda:self.files.seal_staged(self.space,generation_id,number(generation['captured_seq']),ordinal,digest.hexdigest(),self.checkpoint))
            self._sealed=sealed;return sealed

    async def rebuild(self,generation_id:str) -> SealedGeneration:
        """Reconstruct the exact published file from fully verified paid artifacts."""
        with DeadlineScope(current_deadline(self.local_seconds/1000)):
            self.checkpoint()
            self.checkpoint();generation=await self.rows.read('semantic_generation',generation_id)
            control=await self.rows.read('semantic_control',self.control_id)
            if (generation is None or control is None or generation['state']!='PUBLISHED' or control['current_generation']!=generation_id
                    or generation['config']!=self.config or generation['confirmed_pages']!=generation['page_count']):raise InvalidVectorFile('No original published generation.')
            from .semantic_binary import VectorHeader
            records=sha256();bindings=sha256(b'[');ordinal=0
            while values:=await self.rows.statements.read('semantic_generation_members',{'generation_id':generation_id,'ordinal':ordinal}):
                self.checkpoint()
                for row in values:
                    value=self.rows.decode('semantic_member',row)
                    if value['ordinal']!=ordinal:raise InvalidVectorFile('Missing original member.')
                    leaves=tuple([await self.rows.read('embedding_vector_leaf',identity('embedding-vector-leaf',value['artifact_id'],n)) for n in range(2)])
                    raw=restore_vector(string(value['artifact_id']),string(value['vector_digest']),leaves)
                    member=VectorMember(string(value['object_id']),number(value['object_revision']),number(value['ack_revision']),number(value['applied_seq']),string(value['artifact_id']),raw)
                    records.update(member.encode())
                    if ordinal:bindings.update(b',')
                    bindings.update(member.binding_bytes());ordinal+=1
            bindings.update(b']')
            header=VectorHeader(self.space,generation_id,number(generation['captured_seq']),ordinal,bindings.digest(),records.digest())
            if ordinal!=generation['member_count'] or bindings.hexdigest()!=generation['member_digest']:raise InvalidVectorFile('Original complete artifact extent differs.')
            if await self.rows.read('semantic_generation',generation_id)!=generation:raise InvalidVectorFile('Generation changed during reconstruction.')
            if not await self._file(lambda:self.files._reset_damaged(self.space,generation_id,string(generation['file_digest']),self.checkpoint)):
                return await self._file(lambda:self.files.verify(self.space,generation_id,self.checkpoint))
            for page in range(number(generation['page_count'])):await self.write_page(generation_id,page)
            sealed=await self.seal_file(generation_id)
            if sealed.file_digest!=generation['file_digest'] or sealed.header!=header:raise InvalidVectorFile('Rebuilt file differs from original publication.')
            return sealed

    async def prepare_retirement(self,generation_id: str) -> bool:
        """Confirm persisted intent, stop readers, then unlink only after all rows.

        A crash after unlink retains the original database intent; a new owner
        can reconfirm actual absence and finish its final local command.
        """
        with DeadlineScope(current_deadline(self.local_seconds/1000)):
            self.checkpoint()
            self.checkpoint();generation=await self.rows.read('semantic_generation',generation_id)
            control=await self.rows.read('semantic_control',self.control_id)
            if (generation is None or control is None or generation['state'] not in ('RETIRING','FAILED')
                    or control['current_generation']==generation_id):raise InvalidVectorFile('No retained retirement intent.')
            if not await self._file(lambda:self.files.retirement_ready(generation_id)):return False
            remaining=await self.rows.statements.read('semantic_retirement_rows',{'generation_id':generation_id,'after':''})
            removed=False
            if not remaining:
                self.checkpoint();removed=await self._file(lambda:self.files.retire(generation_id))
                if not removed:return False
            self._retirement=(generation_id,number(generation['revision']),removed)
            return True
