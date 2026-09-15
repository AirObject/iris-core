"""Isolated memory-coverage fixture using native leases and real SQLite UoWs.

Fixture-only commands install current rows to exercise the change-tracker port.
This is not fixed-set establishment or proof of paid artifact reception. Apply
uses the real semantic command identity, audits and receipt, with a test retrieval
participant updating its own control root in the same transaction.
"""
from pathlib import Path
from types import MappingProxyType
from hashlib import sha256
import json
from typing import cast
from companion_memory.persistence import (PersistenceService,DatabaseResources,Ready,CommandDefinition,LocalCommand,
    ResultBoundCommand,Committed,UnitOfWork,Value)
from companion_memory.persistence.text_records import extend_catalog
from companion_memory.persistence.semantic_records import ID,N,P,B,record,Record,identity,number,string
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_commands import SemanticCommands
from companion_memory.persistence.content_codec import encode_content
from companion_memory.configuration.semantic_persistence import SemanticConfigurationAssembly
from companion_memory.configuration.semantic_persistent_results import ConfigurationCommitted
from companion_memory.memory.information_repository import information_memory_catalog
from companion_memory.memory.initial_self import initial_self_catalog
from companion_memory.memory.semantic_repository import semantic_memory_catalog
from companion_memory.memory.transactions import MemoryTransactions
from companion_memory.memory.formats import isolate_object,isolate,TOMBSTONE_SCHEMA
from companion_memory.logging_service.object_history import history_catalog,HistoryBinding
from companion_memory.retrieval.semantic_repository import TABLES,semantic_retrieval_catalog
from companion_memory.cognition.fixed_memory_repository import fixed_memory_catalog
from companion_memory.provider.ledger import LedgerAssembly
from tests.semantic.configuration_support import candidate
from tests.persistence.support import Hooks


class UnusedSources:
    def verify_member(self,uow,entry_id,member):raise AssertionError('No source import in a tracker fixture.')
    def retain_source(self,uow,source_id,entry_id,members):raise AssertionError('No source import in a tracker fixture.')


def current(oid: str,revision: int) -> Record:
    return isolate_object({'object_version':2,'object_id':oid,'instance_id':'instance','kind':'MEMORY','revision':revision,
        'created_at_us':1,'modified_at_us':revision,'lifecycle':'ACTIVE','forgotten_since_us':None,'retention_policy_ref':'policy',
        'content':{'category':'FACT','body':'Fixture '+oid,'subject_ids':[],'speaker_subject_id':None,'stance':'ASSERTED',
            'world_scope':{'kind':'REAL','context_id':None},'occurred_range':None,'applicable_range':None},
        'scores':{'belief':50,'retention':50,'scale_id':'acceptance_100_v1','belief_reason':'Fixture','retention_reason':'Fixture','score_basis':[]},
        'origin':{'kind':'OPERATOR_INPUT','candidate_id':None,'batch_id':None,'actor_ref':'fixture','model_origin':'NONE','candidate_origin':'OPERATOR'}},text_format=True)


class CoverageFixture:
    def __init__(self,root: Path):
        self.root=root.resolve();self.candidate,self.supplied=candidate(self.root,offline=False)
        self.memory_catalog=extend_catalog(extend_catalog(information_memory_catalog(),initial_self_catalog(),3),semantic_memory_catalog(),4)
        self.retrieval_catalog=semantic_retrieval_catalog();self.history_catalog=history_catalog()
        self.config=SemanticConfigurationAssembly(self.memory_catalog,self.retrieval_catalog)
        repositories=self.config.repositories+(self.history_catalog.definition,fixed_memory_catalog().definition)+LedgerAssembly().repositories
        self.semantic_commands=SemanticCommands(repositories,self.semantic_handle)
        self.fixture_command=CommandDefinition('fixture','change',1,record(object_id=ID,revision=N,deleted=B),1,record(sequence=N),
            (self.memory_catalog.definition,),(),self.fixture_handle)
        self.storage=PersistenceService(repositories,self.config.commands+self.semantic_commands.commands+(self.fixture_command,),assembly_format='ASYNC_SEMANTIC_V1')
        self.hooks=Hooks()

    async def open(self,mode='CREATE_NEW'):
        path=self.root/'database'/'runtime.sqlite3'
        opened=await self.storage.initialize(self.candidate.foundation,DatabaseResources('text-database',lambda db,p:db=='text-database' and p==str(path),connect=self.hooks.connect),mode)
        assert type(opened) is Ready,opened
        self.binding=self.config.bind(self.storage,'instance',self.candidate)
        result=await self.binding.persist_semantic_configuration('initialize',self.candidate,actor='fixture',protected_directories=self.supplied[6])
        assert type(result) is ConfigurationCommitted and result.configuration is not None,result
        self.stored=result.configuration
        self.binding.release_bootstrap_writers()
        self.history=HistoryBinding(self.history_catalog,self.storage,'instance',8,8192,self.candidate.foundation,text_format=True)
        self.memory=MemoryTransactions(self.memory_catalog,self.storage,result.configuration,'instance',self.history,UnusedSources())
        self.information=self.memory.bind_information(result.configuration)
        assert self.memory.semantic is not None
        self.coverage=self.memory.semantic
        self.retrieval_lease=self.storage.claim_module_owner(self.retrieval_catalog.definition);assert self.retrieval_lease is not None
        self.retrieval=SemanticRecords(self.retrieval_catalog,TABLES,self.storage,'instance')
        self.control_id=identity('semantic-control','instance',self.coverage.space_id)
        if mode=='CREATE_NEW':await self.change('initialize-sequence',0)
        return self

    def fixture_handle(self,uow: UnitOfWork,values: Record):
        revision=number(values['revision']);oid=string(values['object_id'])
        if revision==0:
            self.information.initialize(uow,1)
            return {'sequence':0}
        previous=self.memory.current(uow,oid)
        if values['deleted']:
            assert previous is not None
            self.memory.rows.stage('objects_delete',uow,{'object_id':oid})
            tombstone=isolate(TOMBSTONE_SCHEMA,{'object_id':oid,'kind':'MEMORY','last_revision':number(previous['revision']),
                'deletion_revision':revision,'deleted_at_us':revision,'reason_code':'FIXTURE','operation_ref':'delete:'+oid},1024)
            self.memory.rows.stage('tombstones_insert',uow,{'object_id':oid,'body':encode_content(tombstone,1024).decode()})
        else:
            value=current(oid,revision);body=encode_content(value,4096)
            parameters={'object_id':oid,'kind':'MEMORY','revision':revision,'lifecycle':'ACTIVE','body':body.decode(),'digest':sha256(body).hexdigest()}
            if previous is not None:parameters['expected_revision']=number(previous['revision'])
            self.memory.rows.stage('objects_replace' if previous else 'objects_insert',uow,parameters)
        _,seq=self.information.changed(uow,oid,revision,values['deleted'] is True)
        return {'sequence':seq}

    def application_digest(self,object_id: str,revision: int) -> str:
        return 'a'*64

    def application_artifact(self,object_id: str,revision: int) -> str:
        return 'embedding-artifact:'+'a'*64

    def semantic_handle(self,kind: str,uow: UnitOfWork,envelope: Record,payload: Record) -> object:
        assert kind in ('apply','publish_generation')
        operation,fingerprint=self.storage.semantic_operation_context(uow,self.memory_catalog.definition)
        if kind=='apply':
            obj=cast(Record,payload['object_ref']);deleted=payload['kind']=='DELETE_LOCAL'
            ack,root=self.coverage.acknowledge(uow,string(obj['object_id']),number(obj['revision']),number(payload['latest_seq']),
                artifact_id=None if deleted else string(payload['artifact_id']),material_digest=None if deleted else self.application_digest(string(obj['object_id']),number(obj['revision'])),
                evidence=MappingProxyType({'kind':kind,'key':operation.operation_key,'fingerprint':fingerprint}),
                deletion_operation='delete:'+string(obj['object_id']) if deleted else None)
            targets=[{'object_id':ack['row_id'],'previous_revision':None if ack['revision']==1 else number(ack['revision'])-1,'revision':ack['revision']}]
        else:
            root=self.coverage.publish(uow,string(payload['generation_id']),number(payload['file_bytes']),number(envelope['observed_at']));targets=[]
        control=self.retrieval.get('semantic_control',uow,self.control_id);assert control is not None
        self.retrieval.write('semantic_control',uow,dict(control)|{'revision':number(control['revision'])+1,'operation_count':number(control['operation_count'])+1},expected_revision=number(control['revision']))
        targets.extend(({'object_id':root['row_id'],'previous_revision':number(root['revision'])-1,'revision':root['revision']},
            {'object_id':self.control_id,'previous_revision':control['revision'],'revision':number(control['revision'])+1}))
        counts=self.storage.transaction_row_changes(uow)
        facts={owner:{'rows_changed':counts[owner],'state':'APPLIED','references':[],'counts':[]} for owner in ('memory','retrieval')}
        facts['memory'].update(self.coverage.audit_fact(uow))
        return {'outcome':'APPLIED','targets':targets,'items':[],'facts':facts}

    async def change(self,oid: str,revision: int,deleted: bool=False):
        result=await self.storage.bind_operation(self.fixture_command,'instance').execute(f'change:{oid}:{revision}',LocalCommand(1,{'object_id':oid,'revision':revision,'deleted':deleted},{}))
        assert type(result) is Committed,result
        return result

    async def apply(self,oid: str,revision: int,seq: int,deleted: bool=False):
        definition=next(d for d in self.semantic_commands.commands if d.operation_kind=='apply')
        payload={'kind':'DELETE_LOCAL' if deleted else 'EMBED','work_id':oid,'expected_revision':1,'object_ref':{'object_id':oid,'revision':revision},'latest_seq':seq}
        if deleted:payload['deletion_ref']={'kind':'fixture','key':'delete:'+oid,'fingerprint':'a'*64}
        else:payload['artifact_id']=self.application_artifact(oid,revision)
        key=f'apply:{oid}:{revision}'
        command=ResultBoundCommand(1,{'binding_id':'fixture','request_key':key,'expected':[],'observed_at':1,
            'payload':json.dumps(payload,sort_keys=True,separators=(',',':'))},{r.event_slot:{'actor':'fixture'} for r in definition.required_audits})
        return await self.storage.bind_operation(definition,'instance').execute(key,command)

    async def view(self):
        root=await self.coverage.rows.read('semantic_publication',self.coverage.root_id);assert root is not None
        return root

    async def close(self):
        assert self.retrieval_lease is not None
        self.memory.close();self.history.close();self.retrieval_lease.release();self.binding.close();await self.storage.close()
