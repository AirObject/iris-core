"""Cognition-owned page persistence and complete immutable material observation.

The owner freezes an invocation before paging. SQLite handlers accept only that
exact invocation and compare existing leaves before sealing the root. A stored
root is never exposed with missing text or a caller-supplied replacement body.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
import asyncio
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,PersistenceService,UnitOfWork,Field,RecordSchema,SequenceSchema,BoundedTextSchema,Found,NotFound,Committed
from companion_memory.persistence.daily_records import DailyRows,ID,Record
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.schema import InvalidValue,Value
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.owned_statements import StatementCatalog,BoundStatements,OwnerFailure
from .daily_material import FrozenDailyMaterial,restore_material,TABLES,ROOT_TABLE,DREAM_TABLES,DREAM_ROOT_TABLE
from .managed_material_versions import ManagedMaterialVersions, TABLE as VERSION_TABLE
from companion_memory.configuration.execution_versions import ExecutionVersion

@dataclass(frozen=True,slots=True,init=False)
class DailyMaterialReadLease:
    """Native retained complete material, bound to its actual business owner.

    Possession is read authority only; the Provider must separately recheck the
    frozen native run or goal and first-send admission before registration.
    """
    issuer:DailyMaterialStorage
    material:FrozenDailyMaterial
    deadline:float
    execution_version:ExecutionVersion|None


class DailyMaterialStorage:
    """A bounded local component of the cognition owner, never a second host."""
    def __init__(self,catalog:StatementCatalog, *, dream_format: bool = False, managed_format: bool = False):
        if catalog.definition.owner_module!='cognition' or catalog.definition.schema_version!=5:raise InvalidValue()
        self.dream_format=dream_format
        self.managed_format=managed_format
        if managed_format and not dream_format:raise ValueError("Managed storage requires dream owners.")
        self.tables=(DREAM_TABLES if dream_format else TABLES) + ((VERSION_TABLE,) if managed_format else ())
        self.versions:ManagedMaterialVersions|None=None
        self.root_table=DREAM_ROOT_TABLE if dream_format else ROOT_TABLE
        self.catalog=catalog;self._bound=False;self._closed=False
        self._active:FrozenDailyMaterial|None=None;self._task:asyncio.Task|None=None
        self._readers:set[asyncio.Task]=set();self._leases:dict[int,DailyMaterialReadLease]={}
        self._reader_contexts:dict[asyncio.Task,str]={};self._retiring:str|None=None
        layouts={'stage_material_page':(Field('context_id',ID),Field('leaves',SequenceSchema(BoundedTextSchema(8192),1,4))),
            'seal_material':(Field('manifest',BoundedTextSchema(16384)),)}
        commands=[]
        for name,fields in layouts.items():
            requirements,bindings=audits(name,('cognition',))
            def handle(uow:UnitOfWork,v:Record,kind=name):return self._apply(kind,uow,v)
            commands.append(ResultBoundCommandDefinition('cognition',name,1,RecordSchema((Field('operation_id',ID),)+fields),1,
                result_schema(('cognition',),('MATERIAL_PAGE_STORED','MATERIAL_STORED')),(catalog.definition,),requirements,handle,INTENT,bindings))
        self.commands=tuple(commands)

    def reader_active(self,lease:DailyMaterialReadLease) -> bool:
        """Observe only this owner's native complete-material lease lifetime."""
        if type(lease) is not DailyMaterialReadLease or lease.issuer is not self:raise InvalidValue()
        return self._leases.get(id(lease)) is lease

    def participate_manifest(self,uow:UnitOfWork,context_id:str,owner_ref:str):
        """Read only an original owner's manifest in an existing native transaction."""
        if self._closed or not self._bound:raise InvalidValue()
        root=self.rows.get('learning_contexts',uow,context_id)
        if root is None or root['owner_ref']!=owner_ref:raise OwnerFailure('PRECONDITION_FAILED','material','BINDING_MISMATCH')
        return root

    async def read_manifest(self,context_id:str,owner_ref:str):
        """Observe original metadata without granting result or publication authority."""
        if self._closed or not self._bound:raise InvalidValue()
        root=await self.rows.read('learning_contexts',context_id)
        if root is None:return None
        if root['owner_ref']!=owner_ref:raise OwnerFailure('ACCESS_DENIED','material','BINDING_MISMATCH')
        return root

    def bind(self,storage:PersistenceService,configuration:StoredCognitionConfiguration):
        """Bind only one full native configuration; the main cognition owner retains the lease."""
        if self._bound or type(configuration) is not (StoredManagedConfiguration if self.managed_format else StoredDreamConfiguration if self.dream_format else StoredDailyConfiguration) or stored_cognition_configuration_issue(configuration,storage=storage) is not None:raise InvalidValue()
        self.storage=storage;self.configuration=configuration
        self.rows=DailyRows(self.catalog,self.tables,storage,configuration.database_id,configuration.scope_id,configuration.snapshot_id)
        self.operations={d.operation_kind:storage.bind_operation(d,configuration.scope_id) for d in self.commands}
        self._provider_read=BoundStatements(self.catalog,storage,'provider')
        self._bound=True

    def _apply(self,kind:str,uow:UnitOfWork,v:Record):
        active=self._active
        if self._closed or active is None:raise OwnerFailure('ACCESS_DENIED','material','BINDING_MISMATCH')
        uow.require_commit_permission(lambda:not self._closed)
        targets=[];root=active.manifest
        version_target = self.versions.stage(uow, cast(str, root['object_id']), cast(str, root['owner_ref'])) if self.versions is not None else None
        if kind=='stage_material_page':
            if v['context_id']!=root['object_id'] or self.rows.get('learning_contexts',uow,cast(str,root['object_id'])) is not None:raise InvalidValue()
            values=cast(tuple[str,...],v['leaves'])
            ordinals=[]
            for raw in values:
                leaf=decode_content(raw.encode(),8192)
                if type(leaf) is not dict or type(leaf.get('ordinal')) is not int:raise InvalidValue()
                ordinal=cast(int,leaf['ordinal'])
                if not 0<=ordinal<len(active.leaves) or encode_content(active.leaves[ordinal],8192).decode()!=raw:raise InvalidValue()
                ordinals.append(ordinal)
                stored=self.rows.write('learning_context_leaves',uow,leaf)
                targets.append(target(cast(str,stored['object_id']),1))
            if ordinals!=list(range(ordinals[0],ordinals[0]+len(ordinals))):raise InvalidValue()
            state='MATERIAL_PAGE_STORED'
        else:
            if v['manifest']!=encode_content(root,16384).decode():raise InvalidValue()
            leaves=[]
            for raw_ref in cast(tuple[Record,...],root['leaf_refs']):
                leaf=self.rows.get('learning_context_leaves',uow,cast(str,raw_ref['object_id']))
                if leaf is None:raise OwnerFailure('PRECONDITION_FAILED','material','MATERIAL_MISSING')
                leaves.append(leaf)
            if restore_material(root,leaves,dream_format=self.dream_format)!=active:raise InvalidValue()
            self.rows.write('learning_contexts',uow,root)
            targets.append(target(cast(str,root['object_id']),1));state='MATERIAL_STORED'
        return result(cast(str,v['operation_id']),state,{'cognition':{'rows_changed':len(targets) + int(version_target is not None),'targets':targets}})

    def stage_complete(self,uow:UnitOfWork,material:FrozenDailyMaterial) -> dict[str,object]:
        """Atomically store one complete owner material with its enclosing business root.

        The caller must be a declared participant of this same daily assembly.
        Returning this fact does not commit and grants no Provider admission.
        """
        if self._closed or not self._bound or type(material) is not FrozenDailyMaterial:raise InvalidValue()
        operation=self.storage.cognition_operation_context(uow,self.catalog.definition)
        if operation.scope_id!=self.configuration.scope_id:raise InvalidValue()
        checked=restore_material(material.manifest,material.leaves,dream_format=self.dream_format)
        if checked!=material:raise InvalidValue()
        version_target = self.versions.stage(uow, cast(str, material.manifest['object_id']), cast(str, material.manifest['owner_ref'])) if self.versions is not None else None
        for leaf in material.leaves:self.rows.write('learning_context_leaves',uow,leaf)
        root=self.rows.write('learning_contexts',uow,material.manifest)
        return {'rows_changed':len(material.leaves)+1+int(version_target is not None),'targets':(target(cast(str,root['object_id']),1),)}

    def participate_material(self,uow:UnitOfWork,context_id:str,digest:str,owner_ref:str) -> FrozenDailyMaterial:
        """Read the exact complete durable material in another owner's atomic command.

        The static command must declare cognition as a participant. This grants
        neither write access nor a path, and a released or partial root fails.
        """
        operation=self.storage.cognition_operation_context(uow,self.catalog.definition)
        def read(name:str,key:str):
            if operation.scope_id!='provider':return self.rows.get(name,uow,key)
            if operation.owner_namespace!='provider' or operation.operation_kind not in ('register_daily_request','confirm_daily_handoff'):raise InvalidValue()
            values=self._provider_read.stage('provider_read_'+name,uow,{'caller_scope':self.configuration.scope_id,'object_id':key})
            return self.rows.decode(name,values[0]) if len(values)==1 else None
        root=read('learning_contexts',context_id)
        if root is None or root['state']!='STORED' or root['payload_digest']!=digest or root['owner_ref']!=owner_ref:
            raise OwnerFailure('PRECONDITION_FAILED','material','MATERIAL_CHANGED')
        leaves=[]
        for ref in cast(tuple[Record,...],root['leaf_refs']):
            leaf=read('learning_context_leaves',cast(str,ref['object_id']))
            if leaf is None:raise OwnerFailure('STORAGE_FAILED','material','INTEGRITY_FAILURE')
            leaves.append(leaf)
        return restore_material(root,leaves,dream_format=self.dream_format)

    async def persist(self,material:FrozenDailyMaterial,deadline:float):
        """Store finite original pages and then seal; continuation reuses original receipts."""
        if self.versions is not None:
            version = await self.versions.existing(cast(str, material.manifest['object_id']), cast(str, material.manifest['owner_ref']))
            with self.versions.versions.use(version or self.versions.versions.current):
                return await self._persist_selected(material, deadline)
        return await self._persist_selected(material, deadline)

    async def _persist_selected(self,material:FrozenDailyMaterial,deadline:float):
        if (not self._bound or self._closed or type(material) is not FrozenDailyMaterial
                or type(deadline) not in (float,int) or not time.monotonic()<deadline<float('inf')):raise InvalidValue()
        verified=restore_material(material.manifest,material.leaves,dream_format=self.dream_format)
        if verified!=material or any(material.manifest[name]!=expected for name,expected in (
                ('database_id',self.configuration.database_id),('instance_id',self.configuration.scope_id),('config_snapshot_id',self.configuration.snapshot_id))):raise InvalidValue()
        if self._task is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        self._active=material
        task,logical=start_owned(self._persist(deadline));self._task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None;self._active=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','material','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def _persist(self,deadline:float):
        active=self._active
        if active is None:raise InvalidValue()
        cid=cast(str,active.manifest['object_id'])
        operations=[]
        for offset in range(0,len(active.leaves),4):
            operations.append(('stage_material_page','material-page:'+cid.removeprefix('learning-context:')+':'+str(offset),
                {'context_id':cid,'leaves':tuple(encode_content(leaf,8192).decode() for leaf in active.leaves[offset:offset+4])}))
        operations.append(('seal_material','material-seal:'+cid.removeprefix('learning-context:'),{'manifest':encode_content(active.manifest,16384).decode()}))
        with DeadlineScope(deadline):
            outcome=None
            for kind,key,payload in operations:
                if self._closed or time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','material','DEADLINE_EXCEEDED')
                definition=next(d for d in self.commands if d.operation_kind==kind)
                outcome=await self.operations[kind].execute(key,ResultBoundCommand(1,{'operation_id':key,**payload},
                    {r.event_slot:{'actor':'daily_cognition'} for r in definition.required_audits}))
                if type(outcome) is not Committed:return outcome
            return outcome

    async def borrow(self,context_id:str,digest:str,owner_ref:str,deadline:float) -> DailyMaterialReadLease:
        """Retain one of two complete native readers until its consumer releases it."""
        if len(self._readers)+len(self._leases)>=2:raise OwnerFailure('RESOURCE_BUSY','material','ADMISSION_FULL')
        found=await self.read(context_id,deadline)
        if type(found) is not Found or found.value.manifest['payload_digest']!=digest or found.value.manifest['owner_ref']!=owner_ref:
            raise OwnerFailure('PRECONDITION_FAILED','material','MATERIAL_CHANGED')
        if self._closed or time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','material','DEADLINE_EXCEEDED')
        version = await self.versions.required(context_id, owner_ref) if self.versions is not None else None
        lease=object.__new__(DailyMaterialReadLease)
        for name,value in (('issuer',self),('material',found.value),('deadline',deadline),('execution_version',version)):object.__setattr__(lease,name,value)
        self._leases[id(lease)]=lease
        return lease

    def verify_lease(self,lease:DailyMaterialReadLease,uow:UnitOfWork|None=None) -> FrozenDailyMaterial:
        """Require the exact live reader; an atomic consumer also rereads all bytes."""
        if (type(lease) is not DailyMaterialReadLease or lease.issuer is not self or self._leases.get(id(lease)) is not lease
                or self._closed or time.monotonic()>=lease.deadline):raise OwnerFailure('ACCESS_DENIED','material','BINDING_MISMATCH')
        root=lease.material.manifest
        if uow is not None and self.participate_material(uow,cast(str,root['object_id']),cast(str,root['payload_digest']),cast(str,root['owner_ref']))!=lease.material:
            raise OwnerFailure('PRECONDITION_FAILED','material','MATERIAL_CHANGED')
        return lease.material

    def release_reader(self,lease:DailyMaterialReadLease) -> None:
        """Release this actual consumer independently of durable material cleanup."""
        if type(lease) is not DailyMaterialReadLease or self._leases.get(id(lease)) is not lease:raise InvalidValue()
        del self._leases[id(lease)]

    async def read(self,context_id:str,deadline:float):
        """Keep the physical read slot until all SQLite tails have actually ended."""
        if self._retiring==context_id:raise OwnerFailure('PRECONDITION_FAILED','material','MATERIAL_RELEASED')
        if self._closed or len(self._readers)+len(self._leases)>=2:raise OwnerFailure('RESOURCE_BUSY','material','ADMISSION_FULL')
        task,logical=start_owned(self._read_complete(context_id,deadline));self._readers.add(task);self._reader_contexts[task]=context_id
        def ended(job):
            if not job.cancelled():job.exception()
            self._readers.discard(job)
            self._reader_contexts.pop(job,None)
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','material','DEADLINE_EXCEEDED',True)
        return logical.result()

    def begin_retirement(self,context_id:str):
        """Fence new readers after this exact material's actual consumers ended."""
        if self._closed or self._retiring is not None:raise OwnerFailure('RESOURCE_BUSY','material','CLEANUP_PENDING',True)
        if context_id in self._reader_contexts.values() or any(lease.material.manifest['object_id']==context_id for lease in self._leases.values()):
            raise OwnerFailure('RESOURCE_BUSY','material','CLEANUP_PENDING',True)
        self._retiring=context_id

    def end_retirement(self,context_id:str):
        if self._retiring!=context_id:raise InvalidValue()
        self._retiring=None

    def retire_page(self,uow:UnitOfWork,context_id:str,owner_ref:str,offset:int):
        """Delete at most four original leaves and revise the actual retained root."""
        if self._retiring!=context_id or context_id in self._reader_contexts.values() or any(lease.material.manifest['object_id']==context_id for lease in self._leases.values()):raise InvalidValue()
        root=self.rows.get('learning_contexts',uow,context_id)
        if root is None or root['owner_ref']!=owner_ref or offset%4 or not 0<=offset<len(cast(tuple,root['leaf_refs'])):raise InvalidValue()
        refs=cast(tuple[Record,...],root['leaf_refs']);count=0
        for ref in refs[offset:offset+4]:
            old=self.rows.get('learning_context_leaves',uow,cast(str,ref['object_id']))
            if old is None or old['digest']!=ref['digest'] or old['context_id']!=context_id:raise OwnerFailure('STORAGE_FAILED','material','INTEGRITY_FAILURE')
            removed=self.rows.rows.stage('daily_context_leaf_delete',uow,{'object_id':ref['object_id'],'context_id':context_id})
            if len(removed)!=1:raise InvalidValue()
            count+=1
        operation=self.storage.cognition_operation_context(uow,self.catalog.definition)
        changed=self.rows.write('learning_contexts',uow,dict(root)|{'state':'RELEASED','revision':cast(int,root['revision'])+1,
            'updated_at_us':max(time.time_ns()//1000,cast(int,root['updated_at_us'])),
            'terminal_operation':{name:getattr(operation,name) for name in ('owner_namespace','operation_kind','scope_id','operation_key')}},cast(int,root['revision']))
        return {'rows_changed':count+1,'targets':(target(context_id,cast(int,changed['revision']),cast(int,root['revision'])),)}

    async def _read_complete(self,context_id:str,deadline:float):
        """Return complete verified bytes only; released material has no success value."""
        if not self._bound or self._closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        with DeadlineScope(deadline):
            root=await self.rows.read('learning_contexts',context_id)
            if root is None:return NotFound()
            if root['state']!='STORED':raise OwnerFailure('PRECONDITION_FAILED','material','MATERIAL_RELEASED')
            leaves=[]
            for ref in cast(tuple[Record,...],root['leaf_refs']):
                if self._closed or time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','material','DEADLINE_EXCEEDED')
                leaf=await self.rows.read('learning_context_leaves',cast(str,ref['object_id']))
                if leaf is None:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
                leaves.append(leaf)
            if await self.rows.read('learning_contexts',context_id)!=root:raise OwnerFailure('PRECONDITION_FAILED','material','MATERIAL_CHANGED')
            return Found(restore_material(root,leaves,dream_format=self.dream_format))

    def close(self) -> bool:
        """Refuse new pages while the current actual storage consumer retains its slot."""
        self._closed=True
        return self._task is None and not self._readers and not self._leases and self._retiring is None
