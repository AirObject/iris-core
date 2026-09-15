"""Bounded native recovery of original work, paid artifacts and publications.

Read cursors advance only after an entire record and its original receipt have
been checked. Re-entry retains the cursor and never creates network work or
changes an absolute deadline. Artifact bytes remain independently recoverable
when a derived index file is absent.
"""
from __future__ import annotations
import time
from companion_memory.persistence import Found
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import Record,string,number,identity
from companion_memory.memory.formats import record,sequence
from .semantic_work import SemanticWork
from .semantic_generation import SemanticGenerations
from .semantic_payloads import restore_vector


class SemanticRecovery:
    """Retained pagination over the actual semantic owners, without activation."""
    def __init__(self,work:SemanticWork,generations:SemanticGenerations):
        self.work=work;self.generations=generations;self.phase=0;self.after='';self.complete=False
        self.tables=('embedding_artifact','embedding_work','query_embedding_cache','semantic_generation','semantic_gap','semantic_ack')

    async def receipt(self,reference:Record,item:str|None=None) -> None:
        definition=self.work.definitions.get(string(reference['kind']))
        if definition is None:raise ValueError('Unknown original semantic operation.')
        result=await self.work.storage.bind_operation(definition,self.work.instance).read_receipt(string(reference['key']))
        if (type(result) is not Found or result.value.fingerprint!=reference['fingerprint']
                or item is not None and item not in sequence(record(result.value.result)['items'])):
            raise ValueError('Original semantic receipt differs.')

    async def run(self,deadline:float) -> None:
        while self.phase<len(self.tables):
            self.work.checkpoint()
            if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
            name=self.tables[self.phase];rows=self.work.memory.rows if name in ('semantic_gap','semantic_ack') else self.work.rows
            page=await rows.page(name,self.after)
            if not page:self.phase+=1;self.after='';continue
            for value in page:
                if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
                if 'config' in value and value['config']!=self.work.config or value['space_id']!=self.work.space:
                    raise ValueError('Recovered semantic configuration differs.')
                if name=='embedding_artifact':
                    await self.receipt(record(value['completion_ref']),string(value['artifact_id']))
                    leaves=tuple([await rows.read('embedding_vector_leaf',identity('embedding-vector-leaf',value['artifact_id'],n)) for n in range(2)])
                    restore_vector(string(value['artifact_id']),string(value['vector_digest']),leaves)
                    request=await self.work.provider.ledger.get('requests',string(record(value['request_ref'])['request_id']))
                    if request is None or request['phase']!='TERMINAL' or request['outcome']!='SUCCEEDED':raise ValueError('Paid artifact has no successful original request.')
                elif name=='embedding_work':
                    if value['kind']=='EMBED':
                        text=await self.work.text(value)
                        if value['intent'] is not None:
                            request=self.work.provider.request(value,text,number(value['deadline_at']))
                            root=await self.work.provider.ledger.get('requests',request.request_id)
                            if root is not None and root['fingerprint']!=request.fingerprint:raise ValueError('Original request fingerprint differs.')
                            if value['request_ref'] is not None and (root is None or record(value['request_ref'])['request_id']!=request.request_id):raise ValueError('Original request binding is missing.')
                        if value['artifact_id'] is not None:
                            artifact=await rows.read('embedding_artifact',string(value['artifact_id']))
                            if artifact is None or any(artifact[k]!=value[k] for k in ('config','space_id','purpose','partition_id','material_digest')):
                                raise ValueError('Recovered work artifact differs.')
                    if value['completion_ref'] is not None:await self.receipt(record(value['completion_ref']))
                elif name=='query_embedding_cache':
                    artifact=await rows.read('embedding_artifact',string(value['artifact_id']))
                    if artifact is None or artifact['purpose']!='QUERY' or any(artifact[k]!=value[k] for k in ('space_id','partition_id','material_digest')):raise ValueError('Recovered query artifact differs.')
                elif name=='semantic_generation' and value['state']=='PUBLISHED':
                    await self.generations.rebuild(string(value['generation_id']))
                elif name=='semantic_gap':
                    current,gap=await self.work.memory.semantic_current(string(value['object_id']))
                    if gap!=value or value['action']=='UPSERT' and (current is None or current['revision']!=value['object_revision']):raise ValueError('Current semantic gap differs.')
                    if value['action']=='DELETE' and current is not None:raise ValueError('Deleted semantic gap has a current object.')
                elif name=='semantic_ack':await self.receipt(record(value['evidence_receipt']))
                self.after=string(value['row_id'])
        publication=await self.work.memory.rows.read('semantic_publication',self.work.memory.root_id)
        control=await self.work.rows.read('semantic_control',self.work.control_id)
        floor=(await self.work.memory.rows.statements.read('semantic_floor',{'space_id':self.work.space}))[0]
        coverage=await self.work.memory.information.coverage_view()
        material=coverage['captured_seq'] if floor['minimum'] is None else number(floor['minimum'])-1
        if publication is None or control is None or publication['material_seq']!=material or publication['generation_id']!=control['current_generation']:
            raise ValueError('Semantic coverage and publication disagree.')
        self.complete=True
