"""Bounded native reads for the four closed daily reasoning tools.

Tool arguments cannot create authority. The cognition owner retains one issued
read grant and one actual operation. Results contain complete selected current
values and explicit omissions; neither recalls nor model requests are created.
"""
from __future__ import annotations
import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from hashlib import sha256
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Found,NotFound
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge,valid_identifier
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope,check_deadline
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import Record
from companion_memory.memory.service import MemoryService,MemoryReadPort
from companion_memory.memory.formats import record,sequence,check_world
from companion_memory.retrieval.index import LocalIndex
from companion_memory.retrieval.semantic_query import SemanticQuery
from companion_memory.goals.service import GoalsService
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration
from .daily_output import isolate_tool

@dataclass(frozen=True,slots=True,init=False)
class DailyReadGrant:
    """One immutable native current-read scope; it is not model-supplied data."""
    owner:DailyReadTools
    memory:MemoryReadPort
    entry_id:str
    partition_id:str
    subject_ids:frozenset[str]
    worlds:tuple[Record,...]
    digest:str
    writable_objects:frozenset[str]
    route_ids:tuple[str,...]
    def __init__(self):raise TypeError('Cognition setup issues bounded read grants.')

class DailyReadTools:
    """One actual local read slot sharing native memory, goals and retrieval owners."""
    def __init__(self,memory:MemoryService,index:LocalIndex,semantic:SemanticQuery,goals:GoalsService,checkpoint):
        if (type(memory) is not MemoryService or type(index) is not LocalIndex or type(semantic) is not SemanticQuery or type(goals) is not GoalsService
                or type(index.configuration) is not StoredDailyConfiguration or index.configuration is not goals.configuration
                or index.configuration is not semantic.cache.configuration or not memory.matches_configuration(index.configuration,semantic.work.storage)):
            raise InvalidValue()
        self.memory=memory;self.index=index;self.semantic=semantic;self.goals=goals;self.checkpoint=checkpoint
        self._grant:DailyReadGrant|None=None;self._task:asyncio.Task|None=None;self.closed=False
    def bind(self,memory_port:MemoryReadPort,entry_id:str,partition_id:str,subjects:tuple[str,...],worlds:tuple[Record,...],*,writable_objects:tuple[str,...]=(),route_ids:tuple[str,...]=()) -> DailyReadGrant:
        """Trusted setup binds the exact frozen roster and native original memory port."""
        if self.closed or self._grant is not None or not all(valid_identifier(v) for v in (entry_id,partition_id,*subjects)) or len(subjects)>16 or len(set(subjects))!=len(subjects) or not 1<=len(worlds)<=16:raise InvalidValue()
        if len(writable_objects)>16 or len(route_ids)>16 or len(set(writable_objects))!=len(writable_objects) or len(set(route_ids))!=len(route_ids) or not all(valid_identifier(v) for v in (*writable_objects,*route_ids)):raise InvalidValue()
        for oid in writable_objects:self.memory.verify_scope_member(memory_port,oid)
        checksum=self.scope_digest(memory_port,entry_id,partition_id,subjects,worlds,writable_objects,route_ids)
        for world in worlds:check_world(world)
        if len({encode_content(world,512) for world in worlds})!=len(worlds):raise InvalidValue()
        grant=object.__new__(DailyReadGrant)
        for key,value in dict(owner=self,memory=memory_port,entry_id=entry_id,partition_id=partition_id,subject_ids=frozenset(subjects),worlds=worlds,writable_objects=frozenset(writable_objects),route_ids=route_ids,digest=checksum).items():object.__setattr__(grant,key,value)
        self._grant=grant;return grant
    def scope_digest(self,memory_port,entry_id,partition_id,subjects,worlds,writable_objects,route_ids):
        """Describe an issued current scope without creating or widening a grant."""
        reference=self.memory.read_grant_reference(memory_port)
        data=MappingProxyType({'memory_ref':reference,'entry_id':entry_id,'partition_id':partition_id,'subjects':subjects,'worlds':worlds,'writable_objects':writable_objects,'route_ids':route_ids})
        return sha256(encode_content(data,16384)).hexdigest()
    def release(self,grant:DailyReadGrant) -> None:
        """A late actual read keeps its grant until its own completion."""
        if grant is not self._grant or self._task is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',self._task is not None)
        self._grant=None
    def _check(self,grant:DailyReadGrant):
        if self.closed or type(grant) is not DailyReadGrant or grant is not self._grant or grant.owner is not self:raise OwnerFailure('ACCESS_DENIED','capability','TOOL_NOT_GRANTED')
        self.memory.read_grant_reference(grant.memory);self.checkpoint();check_deadline()
    @staticmethod
    def _world_id(world:Record) -> str:
        return 'REAL' if world['kind']=='REAL' else cast(str,world['kind'])+':'+cast(str,world['context_id'])
    async def read(self,grant:DailyReadGrant,request:object,deadline:float) -> Record:
        self._check(grant);tool=isolate_tool(request)
        if self._task is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        if type(deadline) not in (float,int) or not time.monotonic()<deadline<float('inf'):raise InvalidValue()
        async def run():
            started=time.time_ns()//1000
            with DeadlineScope(deadline):
                try:return await self._read(grant,tool)
                except OwnerFailure as failure:
                    # Only a known local admission failure is a resumable read
                    # result. Permission, corruption, modes and actual pending
                    # I/O retain their original stopping semantics.
                    if failure.code!='RESOURCE_BUSY' or failure.cleanup_pending or failure.reason not in ('ADMISSION_FULL','OWNER_ACTIVE'):raise
                    self._check(grant)
                    result=MappingProxyType({'schema_version':1,'name':tool['name'],'arguments':tool['arguments'],
                        'grant_digest':grant.digest,'state':'FAILED','started_at_us':started,'ended_at_us':time.time_ns()//1000,
                        'items':(),'missing':(),'omitted':(),'read_refs':(),'truncated':True,'reasons':('TOOL_FAILED',),
                        'failure':MappingProxyType({'code':failure.code,'operation':tool['name'],'field':failure.field,'reason':failure.reason})})
                    encode_content(result,8192);return result
        task,logical=start_owned(run());self._task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','resource','DEADLINE_EXCEEDED',True)
        return logical.result()
    async def _read(self,grant:DailyReadGrant,tool:Record) -> Record:
        self._check(grant);started=time.time_ns()//1000;args=record(tool['arguments']);name=tool['name'];items=[];missing=[];reasons=[]
        if name=='search_memories':
            world=record(args['world_scope'])
            if world not in grant.worlds:raise OwnerFailure('ACCESS_DENIED','world','TOOL_NOT_GRANTED')
            async with AsyncExitStack() as resources:
                from companion_memory.persistence.deadlines import current_deadline
                deadline=current_deadline(1200)
                if deadline is None:raise InvalidValue()
                selected,coverage,limitations,detail=await self.semantic.select_readonly(self.memory,grant.memory,self.index,grant.partition_id,cast(str,args['query']),world,resources,deadline)
                items.extend(selected[:cast(int,args['limit'])]);reasons.extend(limitations)
                if len(selected)>cast(int,args['limit']):reasons.append('RESULT_LIMIT')
        elif name=='read_memories':
            for ref in sequence(args['refs']):
                self._check(grant);ref=record(ref);oid=cast(str,ref['object_id'])
                value=await grant.memory.get_current(oid)
                if type(value) is NotFound:missing.append(MappingProxyType({'object_id':oid,'reason':'NOT_FOUND'}));continue
                current=self._found(value)
                if record(record(current)['content'])['world_scope'] not in grant.worlds:raise OwnerFailure('ACCESS_DENIED','world','TOOL_NOT_GRANTED')
                if current['revision']!=ref['expected_revision']:missing.append(MappingProxyType({'object_id':oid,'reason':'REVISION_CHANGED'}));continue
                items.append(await self.memory.query_projection(grant.memory,current,deep=False))
        elif name=='read_subjects':
            for sid in sequence(args['subject_ids']):
                self._check(grant)
                if sid not in grant.subject_ids:raise OwnerFailure('ACCESS_DENIED','subject','TOOL_NOT_GRANTED')
                value=await grant.memory.read_subject(sid)
                if type(value) is NotFound:missing.append(MappingProxyType({'object_id':sid,'reason':'NOT_FOUND'}));continue
                current=self._found(value)
                metadata=self._found(await self.memory.subject_relationships(grant.memory,cast(str,sid),grant.subject_ids,grant.worlds))
                items.append(MappingProxyType(dict(current)|{'relations':metadata['relations'],'relations_truncated':metadata['truncated']}))
                if metadata['truncated']:reasons.append('RESULT_LIMIT')
        elif name=='list_goals':
            if args['world_scope'] not in tuple(self._world_id(world) for world in grant.worlds):raise OwnerFailure('ACCESS_DENIED','world','TOOL_NOT_GRANTED')
            selected,truncated=await self.goals.read_learning_goals(grant.entry_id,cast(str,args['world_scope']),cast(int,args['limit']))
            items.extend(selected)
            if truncated:reasons.append('RESULT_LIMIT')
        else:raise InvalidValue()
        self._check(grant)
        if missing:reasons.append('MISSING_ITEMS')
        accepted=[];omitted=[]
        def identity(value):return next(cast(str,value[key]) for key in ('object_id','subject_id','goal_id') if key in value)
        read_refs={identity(item):MappingProxyType({'object_id':identity(item),'revision':item['revision']}) for item in items}
        for item in items:
            for relation in sequence(item.get('relations',())):
                relation=record(relation);read_refs[cast(str,relation['object_id'])]=MappingProxyType({'object_id':relation['object_id'],'revision':relation['revision']})
        # The full result envelope, arguments and metadata share the same bound.
        ended=time.time_ns()//1000
        def result():
            return MappingProxyType({'schema_version':1,'name':name,'arguments':args,'grant_digest':grant.digest,'state':'RESULT_STORED',
                'started_at_us':started,'ended_at_us':ended,'items':tuple(accepted),'missing':tuple(missing),'omitted':tuple(omitted),
                'read_refs':tuple(read_refs.values()),
                'truncated':bool(reasons),'reasons':tuple(dict.fromkeys(reasons))})
        for item in items:
            accepted.append(item)
            try:encode_content(result(),8192)
            except ValueTooLarge:
                accepted.pop();omitted.append(identity(item));reasons.append('RESULT_LIMIT')
        value=result();encode_content(value,8192);return value
    @staticmethod
    def _found(value):
        if type(value) is Found:return record(value.value)
        code=getattr(value,'code','STORAGE_FAILED');reason=getattr(value,'reason','INTEGRITY_FAILURE')
        raise OwnerFailure(code,'tool',reason,bool(getattr(value,'cleanup_pending',False)))
    @property
    def pending(self) -> bool:
        return self._task is not None and not self._task.done()

    async def wait_actual(self) -> None:
        """Retain the original grant until this read's actual completion."""
        task=self._task
        if task is not None:await asyncio.wait((task,))

    def close(self) -> bool:
        self.closed=True
        return self._task is None
