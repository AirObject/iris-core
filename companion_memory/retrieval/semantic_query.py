"""Hybrid candidate selection retaining native readers until final delivery.

This reader delegates only explicitly granted cold queries to their host owner. Paid vectors
are considered only with their current authoritative revision, permission and
structural filters, before absolute admission and reciprocal rank fusion.
"""
from __future__ import annotations
from contextlib import AsyncExitStack
import heapq
import time
from types import MappingProxyType
from typing import TYPE_CHECKING,cast,BinaryIO
from companion_memory.persistence import Found,NotFound,Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import Record,identity,string,number
from companion_memory.memory.formats import record
from .semantic_binary import read_members,cosine,InvalidVectorFile
from .semantic_payloads import restore_vector
from .semantic_ranking import lexical_admitted,fuse
from .lexical import normalize_material,object_text,matches_structure,StructuralFilter,rank_key
if TYPE_CHECKING:
    from .query_service import QueryService,QueryPort
    from .semantic_work import SemanticWork
    from .semantic_cache import SemanticQueryCache
    from .semantic_generation import SemanticGenerations
    from companion_memory.runtime.semantic_cold_query import ControlledColdQueries

REASONS=('QUERY_VECTOR_MISSING','BUDGET_PAUSED','PROVIDER_UNKNOWN','PROVIDER_KNOWN_FAILURE','DEADLINE','INDEX_NOT_READY',
    'INDEX_LAG_PARTIAL','INDEX_FORMAT_LIMIT','SEMANTIC_CAPACITY','CANDIDATE_LIMIT','PERMISSION_CHANGED','REVISION_CHANGED',
    'SECTION_LIMIT','RESOURCE_BUSY','INTEGRITY_UNAVAILABLE')


class SemanticQuery:
    """Native semantic owners attached to the existing query delivery authority."""
    def __init__(self,work:SemanticWork,cache:SemanticQueryCache,generations:SemanticGenerations):
        if work.storage is not cache.storage or work.storage is not generations.storage:raise ValueError('Query owners differ.')
        self.work=work;self.cache=cache;self.generations=generations
        self.cold:ControlledColdQueries|None=None

    @staticmethod
    def partition(port:QueryPort) -> str:
        """Cache identity includes the actual restricted principal binding."""
        return port._authority.principal_binding_id

    async def select(self,service:QueryService,port:QueryPort,query:Record,selected:StructuralFilter,deep:bool,
                     resources:AsyncExitStack,deadline:float) -> tuple[tuple[Record,...],Record,tuple[str,...],Record]:
        reasons=[];now=time.time_ns()//1000
        def checkpoint():
            self.work.checkpoint()
            if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','query','DEADLINE_EXCEEDED')
        checkpoint();text=string(query['query_text'])
        material=normalize_material(text,byte_limit=8192,term_limit=4096)
        structural=not material.terms or bool(selected.object_ids)
        coordinator,lexical_generation=await service.index.query_generation()
        lexical_coverage=await service.index.memory.coverage_view()
        lexical_coverage=MappingProxyType(dict(lexical_coverage)|{'generation':lexical_generation['generation_id'] if lexical_generation else None,
            'preprocess_id':'LOCAL_LEXICAL_V1','unicode_version':material.unicode_version,'posting_visits':0,'candidate_count':0,'observed_at':now})
        ids:dict[str,None]={oid:None for oid in selected.object_ids}
        if not structural:
            if lexical_generation is None:reasons.append('INDEX_NOT_READY')
            elif (lexical_coverage['pending_count'] or lexical_coverage['contiguous_seq']!=lexical_coverage['captured_seq']
                    or lexical_generation['pending_count'] or lexical_generation['captured_seq']!=lexical_coverage['captured_seq']
                    or lexical_generation['contiguous_seq']!=lexical_coverage['captured_seq']):
                reasons.append('INDEX_LAG_PARTIAL')
        if not selected.object_ids:
            if not structural and lexical_generation is not None:
                candidates,visits,exhausted=await service.index.posting_candidates(string(lexical_generation['generation_id']),material.terms)
                ids.update((oid,None) for oid in candidates[:128])
                lexical_coverage=MappingProxyType(dict(lexical_coverage)|{'posting_visits':visits})
                if exhausted:reasons.append('CANDIDATE_LIMIT')
            else:
                after=''
                for _ in range(16):
                    checkpoint();page=await service.index.memory.current_page(after,8)
                    for value in page:after=string(value['object_id']);ids[after]=None
                    if len(page)<8:break
                else:reasons.append('CANDIDATE_LIMIT')
            if not structural:
                gaps=await service.index.memory.gaps(128)
                for gap in gaps:
                    if gap['action']=='UPSERT' and len(ids)<128:ids[string(gap['object_id'])]=None
                if len(gaps)==128:reasons.append('INDEX_LAG_PARTIAL')
        async def current(oid:str) -> Record|None:
            checkpoint()
            if not service.runtime.memory.query_allowed(port._authority.memory_port,oid,deep=deep):
                if oid in selected.object_ids:raise OwnerFailure('ACCESS_DENIED','object','OPERATION_NOT_GRANTED')
                return None
            result=await (port._authority.memory_port.get_for_deep_read(oid) if deep else port._authority.memory_port.get_current(oid))
            if type(result) is NotFound:return None
            if type(result) is not Found:raise OwnerFailure('STORAGE_FAILED','storage','READ_FAILED')
            value=record(result.value)
            return value if value['kind']=='MEMORY' and matches_structure(value,selected) else None
        lexical=[];explicit=[];objects:dict[str,Record]={}
        for oid in ids:
            value=await current(oid)
            if value is None:continue
            normalized=normalize_material(object_text(value),byte_limit=8192,term_limit=4096)
            if oid in selected.object_ids:explicit.append(oid);objects[oid]=value
            elif structural or lexical_admitted(material,normalized,600000):
                lexical.append((rank_key(oid,normalized,material,selected),oid));objects[oid]=value
        lexical.sort();lexical_ids=tuple(oid for _,oid in lexical[:64])
        objects={oid:value for oid,value in objects.items() if oid in lexical_ids or oid in explicit}
        publication=await self.work.memory.rows.read('semantic_publication',self.work.memory.root_id)
        floor=(await self.work.memory.rows.statements.read('semantic_floor',{'space_id':self.work.space}))[0]
        control=await self.work.rows.read('semantic_control',self.work.control_id)
        generation_id=None if control is None else control['current_generation'];scanned=0;semantic_ids:tuple[str,...]=()
        semantic_coverage:dict[str,Value]={'state':'NOT_APPLICABLE' if structural else 'NOT_READY','space_id':self.work.space,'generation_id':generation_id,
            'captured_seq':None,'material_seq':publication['material_seq'] if publication else None,'published_seq':publication['published_seq'] if publication else None,
            'first_uncovered_seq':floor['minimum'],'pending_count':floor['count'],'scanned_count':0,'observed_at':now}
        vector_meta:Record=MappingProxyType({'state':'NOT_REQUIRED' if structural else 'UNAVAILABLE','space_id':None if structural else self.work.space,
            'artifact_id':None,'request_ref':None})
        if not structural:
            cache=await resources.enter_async_context(self.cache.borrow(text,self.partition(port),now))
            vector=None;remote_result=False
            artifact=await self.cache.reusable(text,self.partition(port)) if cache is None else await self.work.rows.read('embedding_artifact',string(cache['artifact_id']))
            if artifact is None and cache is None and self.cold is not None:
                cold=await self.cold.obtain(text,self.partition(port),deadline)
                artifact=cold.artifact;remote_result=artifact is not None
                if cold.reason is not None:reasons.append(cold.reason)
            if artifact is None:
                if cache is not None:raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
                reasons.append('QUERY_VECTOR_MISSING')
            else:
                leaves=tuple([await self.work.rows.read('embedding_vector_leaf',identity('embedding-vector-leaf',artifact['artifact_id'],n)) for n in range(2)])
                vector=restore_vector(string(artifact['artifact_id']),string(artifact['vector_digest']),leaves)
                vector_meta=MappingProxyType({'state':'REMOTE_RESULT' if remote_result else 'CACHE_HIT' if cache is not None else 'REUSED_ARTIFACT','space_id':self.work.space,'artifact_id':artifact['artifact_id'],'request_ref':artifact['request_ref']})
            if generation_id is None:reasons.append('INDEX_NOT_READY')
            elif vector is not None and self.generations.io_pending:
                semantic_coverage['state']='UNAVAILABLE';reasons.append('RESOURCE_BUSY')
            elif vector is not None:
                try:
                    generation=await self.work.rows.read('semantic_generation',string(generation_id))
                    if generation is None or generation['state']!='PUBLISHED':raise InvalidVectorFile('Published metadata missing.')
                    sealed=self.generations.files.verify(self.work.space,string(generation_id),checkpoint)
                    if sealed.file_digest!=generation['file_digest'] or sealed.file_bytes!=generation['file_bytes']:raise InvalidVectorFile('Published file differs.')
                    stream=resources.enter_context(self.generations.files.reader(sealed));ranked=[]
                    for member in read_members(cast(BinaryIO,stream),sealed.header,checkpoint):
                        scanned+=1;score=cosine(member.vector,vector)
                        if score<.7:continue
                        value=await current(member.object_id)
                        if value is None:continue
                        if value['revision']!=member.object_revision:reasons.append('REVISION_CHANGED');continue
                        ranked.append((-score,member.object_id));ranked=heapq.nsmallest(64,ranked)
                        objects[member.object_id]=value
                        retained=set(lexical_ids)|set(explicit)|{oid for _,oid in ranked}
                        objects={oid:obj for oid,obj in objects.items() if oid in retained}
                    semantic_ids=tuple(oid for _,oid in sorted(ranked))
                    lag=bool(floor['count']) or sealed.header.captured_seq!=lexical_coverage['captured_seq'] or publication is None or publication['published_seq']!=sealed.header.captured_seq
                    semantic_coverage.update(state='PARTIAL' if lag or 'REVISION_CHANGED' in reasons else 'COMPLETE',
                        captured_seq=sealed.header.captured_seq,scanned_count=scanned)
                    if lag:reasons.append('INDEX_LAG_PARTIAL')
                except (InvalidVectorFile,OSError):semantic_coverage['state']='UNAVAILABLE';reasons.append('INTEGRITY_UNAVAILABLE')
        order=fuse(lexical_ids,semantic_ids,tuple(explicit),rrf_k=60,combined_limit=128)[:8]
        projections=tuple([await service.runtime.memory.query_projection(port._authority.memory_port,objects[oid],deep=deep) for oid in order])
        lexical_coverage=MappingProxyType(dict(lexical_coverage)|{'candidate_count':len(ids)})
        detail=MappingProxyType({'actual_mode':'STRUCTURAL_ONLY' if structural else 'HYBRID' if vector_meta['state'] in ('CACHE_HIT','REUSED_ARTIFACT','REMOTE_RESULT') and semantic_coverage['state'] in ('COMPLETE','PARTIAL') else 'LEXICAL_ONLY',
            'query_vector':vector_meta,'admission':MappingProxyType({'policy':'ABSOLUTE_COSINE_LEXICAL_V1','semantic_min_millionths':700000,
                'lexical_min_millionths':600000,'semantic_accepted':len(semantic_ids),'lexical_accepted':len(lexical_ids),'explicit_accepted':len(explicit)})})
        coverage=MappingProxyType({'lexical':lexical_coverage,'semantic':MappingProxyType(semantic_coverage)})
        return projections,coverage,tuple(reason for reason in REASONS if reason in reasons),detail
