"""Retrieval-owned query cache with exact original text and absolute expiry.

Cache operations never dispatch Provider work and never destroy paid artifacts.
Bindings verify complete input and vector leaves inside the original transaction.
Expiration removes hit eligibility; bounded collection is a separate command.
"""
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
from types import MappingProxyType
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import RLock
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration,stored_semantic_configuration_issue
from companion_memory.persistence import PersistenceService,UnitOfWork,ResultBoundCommandDefinition
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_records import Record,identity,number,string,isolate,RECEIPT
from companion_memory.memory.formats import record,sequence
from .semantic_repository import TABLES
from .semantic_payloads import restore_input,restore_vector
from .semantic_material import cache_key


class SemanticQueryCache:
    """One native retrieval owner's cache participant, with no independent lease."""
    def __init__(self,catalog: StatementCatalog,storage: PersistenceService,configuration: StoredSemanticConfiguration | StoredCognitionConfiguration,instance: str,
                 reception: ResultBoundCommandDefinition):
        from companion_memory.persistence.semantic_commands import declared
        if (stored_cognition_configuration_issue(configuration,storage=storage) if (type(configuration) is StoredDailyConfiguration or type(configuration) is StoredDreamConfiguration or type(configuration) is StoredManagedConfiguration) else stored_semantic_configuration_issue(configuration)) is not None or catalog.definition.owner_module!='retrieval':
            raise ValueError('The native semantic retrieval configuration is required.')
        if not declared(reception) or reception.operation_kind!='record_result' or reception.owner_namespace!='retrieval':
            raise ValueError('The original artifact reception command is required.')
        self.reception=reception
        self.catalog=catalog;self.storage=storage;self.configuration=configuration;self.instance=instance
        self.space=string(configuration.candidate.text.record('retrieval.semantic')['space_id'])
        self.rows=SemanticRecords(catalog,TABLES,storage,instance)
        self.control_id=identity('semantic-control',instance,self.space)
        self.hits=0;self.misses=0
        self._lock=RLock();self._consumers: dict[str,int]={};self._closing=False

    def _id(self,key: str,partition: str) -> str:
        return identity('query-cache',self.instance,self.space,partition,key)

    def _control(self,uow: UnitOfWork) -> Record:
        value=self.rows.get('semantic_control',uow,self.control_id)
        if value is None:raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
        return value

    def _finish(self,uow: UnitOfWork,control: Record,targets: list[Record],items: tuple[str,...],**changes) -> object:
        revision=number(control['revision'])
        self.rows.write('semantic_control',uow,dict(control)|{'revision':revision+1,
            'operation_count':number(control['operation_count'])+1,**changes},expected_revision=revision)
        targets.append(MappingProxyType({'object_id':control['row_id'],'previous_revision':revision,'revision':revision+1}))
        count=self.storage.transaction_row_changes(uow).get('retrieval',0)
        return {'outcome':'APPLIED','targets':tuple(targets),'items':items,'facts':{'retrieval':{
            'rows_changed':count,'state':'APPLIED','references':[],'counts':[]}}}

    def handle(self,kind: str,uow: UnitOfWork,envelope: Record,payload: Record) -> object:
        """Execute only a cache command issued in this native assembly and scope."""
        operation,fingerprint=self.storage.semantic_operation_context(uow,self.catalog.definition)
        if operation.operation_kind!=kind or operation.scope_id!=self.instance or kind not in ('cache_bind','cache_expire','gc_page'):
            raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        control=self._control(uow);observed=number(envelope['observed_at'])
        if kind=='cache_bind':
            work=self.rows.get('embedding_work',uow,string(payload['work_id']))
            if work is None or work['kind']!='EMBED' or work['purpose']!='QUERY' or work['state']!='RESULT_STORED' or work['revision']!=payload['expected_work_revision']:
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            artifact=self.rows.get('embedding_artifact',uow,string(work['artifact_id']))
            if artifact is None or any(artifact[key]!=work[key] for key in ('config','space_id','purpose','partition_id','material_digest')):
                raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
            proof=record(work['completion_ref'])
            received=self.storage.confirm_prior_operation(uow,self.reception,string(proof['key']))
            if (received is None or proof['kind']!='record_result' or received.fingerprint!=proof['fingerprint']
                    or artifact['artifact_id'] not in sequence(record(received.result)['items'])):
                raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
            configuration=record(artifact['config'])
            if (configuration['database_id']!=self.configuration.database_id or configuration['instance_id']!=self.instance
                    or configuration['snapshot_id']!=self.configuration.snapshot_id or artifact['space_id']!=self.space):
                raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
            input_rows=tuple(self.rows.get('embedding_input_leaf',uow,identity('embedding-input-leaf',work['work_id'],n)) for n in range(number(work['input_leaf_count'])))
            text=restore_input(string(work['work_id']),'QUERY',number(work['input_bytes']),string(work['material_digest']),input_rows)
            vectors=tuple(self.rows.get('embedding_vector_leaf',uow,identity('embedding-vector-leaf',artifact['artifact_id'],n)) for n in range(2))
            restore_vector(string(artifact['artifact_id']),string(artifact['vector_digest']),vectors)
            key=cache_key(self.instance,string(work['partition_id']),self.space,text)
            if key!=payload['cache_key'] or payload['expires_at']!=observed+86400000000:
                raise OwnerFailure('PRECONDITION_FAILED','query','REVISION_CONFLICT')
            row_id=self._id(key,string(work['partition_id']));old=self.rows.get('query_embedding_cache',uow,row_id)
            if old is not None and (old['state']=='ACTIVE' and number(old['expires_at'])>observed):
                if old['artifact_id']!=artifact['artifact_id']:raise OwnerFailure('PRECONDITION_FAILED','query','NO_CHANGE')
                proof=isolate(RECEIPT,{'kind':kind,'key':operation.operation_key,'fingerprint':fingerprint})
                updated=self.rows.write('embedding_work',uow,dict(work)|{'state':'APPLIED','revision':number(work['revision'])+1,'completion_ref':proof},expected_revision=number(work['revision']))
                return self._finish(uow,control,[MappingProxyType({'object_id':updated['row_id'],'previous_revision':work['revision'],'revision':updated['revision']})],(string(artifact['artifact_id']),))
            count=self.rows.statements.stage('query_embedding_cache_count',uow,{})[0]['count']
            if old is None and number(count)>=128:raise OwnerFailure('RESOURCE_BUSY','query','CAPACITY_REACHED')
            revision=number(old['revision'])+1 if old else 1
            cached=self.rows.write('query_embedding_cache',uow,{'v':1,'revision':revision,'row_id':row_id,'cache_key':key,
                'space_id':self.space,'partition_id':work['partition_id'],'material_digest':work['material_digest'],
                'artifact_id':artifact['artifact_id'],'state':'ACTIVE','bound_at':observed,'expires_at':payload['expires_at']},
                expected_revision=number(old['revision']) if old else None)
            proof=isolate(RECEIPT,{'kind':kind,'key':operation.operation_key,'fingerprint':fingerprint})
            updated=self.rows.write('embedding_work',uow,dict(work)|{'state':'APPLIED','revision':number(work['revision'])+1,'completion_ref':proof},expected_revision=number(work['revision']))
            targets=[MappingProxyType({'object_id':cached['row_id'],'previous_revision':number(old['revision']) if old else None,'revision':revision}),
                MappingProxyType({'object_id':updated['row_id'],'previous_revision':work['revision'],'revision':updated['revision']})]
            return self._finish(uow,control,targets,(string(cached['artifact_id']),))
        if kind=='cache_expire':
            if payload['observed_at']!=envelope['observed_at']:raise OwnerFailure('PRECONDITION_FAILED','query','REVISION_CONFLICT')
            found=self.rows.statements.stage('semantic_cache_key',uow,{'space_id':self.space,'cache_key':payload['cache_key']})
            if len(found)!=1:raise OwnerFailure('PRECONDITION_FAILED','query','SOURCE_CHANGED')
            old=self.rows.decode('query_embedding_cache',found[0])
            if old['revision']!=payload['expected_revision'] or old['state']!='ACTIVE' or number(old['expires_at'])>observed:
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            updated=self.rows.write('query_embedding_cache',uow,dict(old)|{'revision':number(old['revision'])+1,'state':'EXPIRED'},expected_revision=number(old['revision']))
            return self._finish(uow,control,[MappingProxyType({'object_id':updated['row_id'],'previous_revision':old['revision'],'revision':updated['revision']})],())
        if payload['space_id']!=self.space or payload['expected_control_revision']!=control['revision']:
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        after=payload['after_cache_key']
        if after!=control['gc_cursor']:raise OwnerFailure('PRECONDITION_FAILED','query','REVISION_CONFLICT')
        found=self.rows.statements.stage('semantic_cache_gc',uow,{'space_id':self.space,'after':after or ''})
        if not found:
            if after is None:raise OwnerFailure('PRECONDITION_FAILED','query','NO_CHANGE')
            return self._finish(uow,control,[],(),gc_cursor=None,last_cleanup_at=observed)
        removed=[]
        for value in found:
            cache=self.rows.decode('query_embedding_cache',value)
            active=self.rows.statements.stage('semantic_cache_consumers',uow,{'artifact_id':cache['artifact_id']})[0]['count']
            with self._lock:
                if cache['state']=='EXPIRED' and not active and not self._consumers.get(string(cache['cache_key'])):
                    self.rows.remove('query_embedding_cache',uow,string(cache['row_id']),number(cache['revision']));removed.append(string(cache['row_id']))
        return self._finish(uow,control,[],tuple(removed),gc_cursor=self.rows.decode('query_embedding_cache',found[-1])['cache_key'],last_cleanup_at=observed)

    async def lookup(self,query_text: str,partition: str,observed_at: int) -> Record | None:
        """Return eligible metadata; permission and revision checks still follow."""
        key=cache_key(self.instance,partition,self.space,query_text)
        value=await self.rows.read('query_embedding_cache',self._id(key,partition))
        if value is None or value['state']!='ACTIVE' or number(value['expires_at'])<=observed_at:
            self.misses=min(2**63-1,self.misses+1);return None
        self.hits=min(2**63-1,self.hits+1)
        return value

    async def reusable(self,query_text:str,partition:str) -> Record|None:
        """Find a retained paid QUERY artifact without extending cache expiry."""
        from hashlib import sha256
        from companion_memory.persistence import Found
        found=await self.rows.statements.read('semantic_artifact_matching',{'space_id':self.space,'purpose':'QUERY',
            'partition_id':partition,'material_digest':sha256(query_text.encode()).hexdigest()})
        if not found:return None
        artifact=self.rows.decode('embedding_artifact',found[0]);config=record(artifact['config'])
        if config!={'database_id':self.configuration.database_id,'instance_id':self.instance,'snapshot_id':self.configuration.snapshot_id}:
            raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
        proof=record(artifact['completion_ref'])
        original=await self.storage.bind_operation(self.reception,self.instance).read_receipt(string(proof['key']))
        if (proof['kind']!='record_result' or type(original) is not Found or original.value.fingerprint!=proof['fingerprint']
                or artifact['artifact_id'] not in sequence(record(original.value.result)['items'])):raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
        return artifact

    @asynccontextmanager
    async def borrow(self,query_text: str,partition: str,observed_at: int) -> AsyncIterator[Record | None]:
        """Retain the exact cache key through the caller's final delivery check.

        Reservation precedes the asynchronous read, so collection cannot race
        between observing a cache hit and retaining its in-process consumer.
        Misses and cancellation release the same bounded reservation.
        """
        key=cache_key(self.instance,partition,self.space,query_text)
        with self._lock:
            if self._closing or sum(self._consumers.values())>=2:
                raise OwnerFailure('RESOURCE_BUSY','query','CAPACITY_REACHED')
            self._consumers[key]=self._consumers.get(key,0)+1
        try:
            yield await self.lookup(query_text,partition,observed_at)
        finally:
            with self._lock:
                self._consumers[key]-=1
                if not self._consumers[key]:del self._consumers[key]

    def close(self) -> bool:
        """Stop reader admission; report completion only after real users exit."""
        with self._lock:
            self._closing=True
            return not self._consumers
