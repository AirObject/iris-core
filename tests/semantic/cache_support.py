"""Native cache transaction fixture with a controlled result-owner boundary.

The fixture receiver supplies one deterministic vector in lieu of a Provider
result. It still commits an actual original reception receipt and complete leaves.
No test here claims paid Provider execution or the full semantic host workflow.
"""
import json
from pathlib import Path
from hashlib import sha256
from types import MappingProxyType
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,ResultBoundCommand
from companion_memory.persistence.semantic_records import Record,identity,number,string
from companion_memory.persistence.semantic_commands import SemanticCommands
from companion_memory.configuration.semantic_persistence import SemanticConfigurationAssembly
from companion_memory.configuration.semantic_persistent_results import ConfigurationCommitted
from companion_memory.provider.ledger import LedgerAssembly
from companion_memory.logging_service.object_history import history_catalog
from companion_memory.cognition.fixed_memory_repository import fixed_memory_catalog
from companion_memory.retrieval.semantic_cache import SemanticQueryCache
from companion_memory.retrieval.semantic_payloads import input_leaves,artifact_leaves
from companion_memory.retrieval.semantic_binary import vector_bytes
from companion_memory.retrieval.semantic_material import cache_key
from tests.semantic.configuration_support import candidate


class CacheFixture:
    def __init__(self,root: Path):
        self.root=root.resolve();self.candidate,self.supplied=candidate(self.root,offline=False)
        self.configuration=SemanticConfigurationAssembly()
        repositories=self.configuration.repositories+LedgerAssembly().repositories+(history_catalog().definition,fixed_memory_catalog().definition)
        self.commands=SemanticCommands(repositories,self.handle)
        self.storage=PersistenceService(repositories,self.configuration.commands+self.commands.commands,assembly_format='ASYNC_SEMANTIC_V1')
        self.text='原完整查询';self.partition='partition';self.work_id='query-work';self.artifact_id=identity('embedding-artifact','controlled-fixture')
        self.space=self.candidate.text.record('retrieval.semantic')['space_id']
        self.key=cache_key('instance',self.partition,string(self.space),self.text)

    async def open(self):
        path=self.root/'database'/'runtime.sqlite3'
        ready=await self.storage.initialize(self.candidate.foundation,DatabaseResources('text-database',lambda db,p:db=='text-database' and p==str(path)),'CREATE_NEW');assert type(ready) is Ready,ready
        self.binding=self.configuration.bind(self.storage,'instance',self.candidate)
        result=await self.binding.persist_semantic_configuration('initialize',self.candidate,actor='fixture',protected_directories=self.supplied[6])
        assert type(result) is ConfigurationCommitted and result.configuration is not None,result
        self.bound=result.configuration;assert self.binding.release_bootstrap_writers()
        catalog=self.configuration.retrieval.catalog
        self.lease=self.storage.claim_module_owner(catalog.definition);assert self.lease is not None
        self.cache=SemanticQueryCache(catalog,self.storage,self.bound,'instance',next(d for d in self.commands.commands if d.operation_kind=='record_result'))
        return self

    def handle(self,kind,uow,envelope,payload):
        if kind in ('cache_bind','cache_expire','gc_page'):return self.cache.handle(kind,uow,envelope,payload)
        assert kind in ('prepare','record_result')
        rows=self.cache.rows;control=self.cache._control(uow);targets=[]
        operation,fingerprint=self.storage.semantic_operation_context(uow,self.cache.catalog.definition)
        proof=MappingProxyType({'kind':kind,'key':operation.operation_key,'fingerprint':fingerprint})
        if kind=='prepare':
            leaves=input_leaves(self.work_id,self.text,'QUERY')
            config=MappingProxyType({'database_id':self.bound.database_id,'instance_id':'instance','snapshot_id':self.bound.snapshot_id})
            work=rows.write('embedding_work',uow,{'v':1,'revision':1,'row_id':self.work_id,
                'work_id':self.work_id,'config':config,'space_id':self.space,'kind':'EMBED','state':'PREPARED','object_ref':None,
                'change_seq':None,'superseded_by_revision':None,'error':'NONE','cleanup_pending':False,'created_at':1,'completion_ref':None,
                'purpose':'QUERY','partition_id':self.partition,'material_digest':sha256(self.text.encode()).hexdigest(),'input_leaf_count':len(leaves),
                'input_bytes':len(self.text.encode()),'original_request_key':'original-provider-key','intent':None,'request_ref':None,'artifact_id':None,'deadline_at':None})
            for leaf in leaves:rows.write('embedding_input_leaf',uow,leaf)
            targets.append(MappingProxyType({'object_id':work['row_id'],'previous_revision':None,'revision':1}));items=(self.work_id,)
        else:
            work=rows.get('embedding_work',uow,self.work_id);assert work is not None
            vector=(1.0,)+(0.0,)*1023
            material={'vectors':(vector,),'dimensions':1024,'space_id':self.space,'model_id':'doubao-embedding-vision','input_items':1}
            leaves=artifact_leaves(self.artifact_id,material,space_id=string(self.space),model_id='doubao-embedding-vision')
            request={'request_id':'controlled-request','attempt_id':'controlled-attempt'}
            artifact=rows.write('embedding_artifact',uow,{'v':1,'revision':1,'row_id':self.artifact_id,'artifact_id':self.artifact_id,
                'config':work['config'],'space_id':self.space,'purpose':'QUERY','partition_id':self.partition,'material_digest':work['material_digest'],
                'request_ref':request,'dimension':1024,'vector_digest':sha256(vector_bytes(vector)).hexdigest(),'vector_leaf_count':2,
                'usage_ref':proof,'completion_ref':proof,'received_at':1})
            for leaf in leaves:rows.write('embedding_vector_leaf',uow,leaf)
            updated=rows.write('embedding_work',uow,dict(work)|{'revision':2,'state':'RESULT_STORED','request_ref':request,'artifact_id':self.artifact_id,'completion_ref':proof},expected_revision=1)
            targets.extend((MappingProxyType({'object_id':updated['row_id'],'previous_revision':1,'revision':2}),MappingProxyType({'object_id':artifact['row_id'],'previous_revision':None,'revision':1})));items=(self.artifact_id,)
        return self.cache._finish(uow,control,targets,items)

    async def execute(self,kind,key,payload,observed=100):
        definition=next(d for d in self.commands.commands if d.operation_kind==kind)
        values={'binding_id':'fixture','request_key':key,'expected':[],'observed_at':observed,'payload':json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))}
        return await self.storage.bind_operation(definition,'instance').execute(key,ResultBoundCommand(1,values,{r.event_slot:{'actor':'fixture'} for r in definition.required_audits}))

    async def receive(self):
        from companion_memory.persistence import Committed
        p={'kind':'EMBED','work_id':self.work_id,'config':{'database_id':self.bound.database_id,'instance_id':'instance','snapshot_id':self.bound.snapshot_id},
           'space_id':self.space,'purpose':'QUERY','object_ref':None,'change_seq':None,'partition_id':self.partition,'rendered_text':self.text,'original_request_key':'original-provider-key'}
        result=await self.execute('prepare','prepare',p);assert type(result) is Committed,result
        result=await self.execute('record_result','receive',{'work_id':self.work_id,'expected_revision':1,'request_ref':{'request_id':'controlled-request','attempt_id':'controlled-attempt'},
            'provider_completion':{'kind':'fixture','key':'result','fingerprint':'a'*64}});assert type(result) is Committed,result

    async def close(self):
        assert self.lease is not None
        self.lease.release();self.binding.close();await self.storage.close()
