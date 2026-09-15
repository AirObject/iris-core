"""Native fixed-set fixture with real SQLite and public initial SELF registration.

The only fixture command initializes the old information sequence. All tested
memory/source writes go through the four real reviewed-material commands.
"""
from pathlib import Path
from types import MappingProxyType
from hashlib import sha256
import time
from companion_memory.persistence import (PersistenceService,DatabaseResources,Ready,CommandDefinition,LocalCommand,
    Committed,UnitOfWork,ResultBoundCommandDefinition)
from companion_memory.persistence.text_records import extend_catalog
from companion_memory.persistence.semantic_records import record,Record,identity,number
from companion_memory.persistence.semantic_commands import SemanticCommands
from companion_memory.persistence.content_codec import encode_content
from companion_memory.configuration.semantic_persistence import SemanticConfigurationAssembly
from companion_memory.configuration.semantic_persistent_results import ConfigurationCommitted
from companion_memory.memory.information_repository import information_memory_catalog
from companion_memory.memory.initial_self import initial_self_catalog
from companion_memory.memory.initial_self_commands import InitialSelfCommands
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.memory.semantic_repository import semantic_memory_catalog
from companion_memory.memory.transactions import MemoryTransactions
from companion_memory.memory.formats import isolate_object
from companion_memory.logging_service.object_history import history_catalog,HistoryBinding
from companion_memory.retrieval.semantic_repository import semantic_retrieval_catalog
from companion_memory.cognition.fixed_memory_repository import fixed_memory_catalog
from companion_memory.cognition.fixed_memory import FixedMemorySets,FixedReviewAuthority
from companion_memory.cognition.fixed_memory_port import FixedMemoryPort
from companion_memory.provider.ledger import LedgerAssembly
from tests.semantic.configuration_support import candidate
from tests.semantic.coverage_support import current,UnusedSources
from tests.persistence.support import Hooks


def material():
    members=[]
    for ordinal in range(12):
        mid='member:'+str(ordinal);oid=identity('fixed-memory','instance','fixed-set',mid)
        original=current(oid,1)
        value=isolate_object(dict(original)|{'origin':{'kind':'OPERATOR_INPUT','candidate_id':None,'batch_id':None,
            'actor_ref':'reviewer','model_origin':'NONE','candidate_origin':'OPERATOR'}},text_format=True)
        event=MappingProxyType({'event_version':1,'event_kind':'MESSAGE','client_event_key':'event:'+str(ordinal),
            'sender':MappingProxyType({'subject_id':'reviewer','role':'AUTHOR','display_name':'Reviewer','identity_source':'SYNTHETIC'}),
            'body':'Fixed fixture '+str(ordinal),'quotation':(),'media':(),'extensions':MappingProxyType({}),'correlation':None})
        event_json=encode_content(event,2048).decode();memory_json=encode_content(value,4096).decode()
        members.append(MappingProxyType({'ordinal':ordinal,'member_id':mid,'event_json':event_json,'memory_json':memory_json,
            'content_digest':sha256(encode_content((event_json,memory_json),8192)).hexdigest()}))
    return tuple(members)


class FixedFixture:
    def __init__(self,root: Path):
        self.root=root.resolve();self.candidate,self.supplied=candidate(self.root,offline=False)
        self.memory_catalog=extend_catalog(extend_catalog(information_memory_catalog(semantic_format=True),initial_self_catalog(),3),semantic_memory_catalog(),4)
        self.retrieval_catalog=semantic_retrieval_catalog();self.history_catalog=history_catalog();self.fixed_catalog=fixed_memory_catalog()
        self.config=SemanticConfigurationAssembly(self.memory_catalog,self.retrieval_catalog)
        repositories=self.config.repositories+(self.history_catalog.definition,self.fixed_catalog.definition)+LedgerAssembly().repositories
        self.semantic_commands=SemanticCommands(repositories,self.handle)
        self.initial=InitialSelfCommands(self.memory_catalog,lambda:1)
        self.fixture_command=CommandDefinition('fixture','initialize_sequence',1,record(),1,record(),(self.memory_catalog.definition,),(),self.initialize_sequence)
        self.storage=PersistenceService(repositories,self.config.commands+self.semantic_commands.commands+self.initial.commands+(self.fixture_command,),assembly_format='ASYNC_SEMANTIC_V1')
        self.hooks=Hooks();self.members=material();self.fixed: FixedMemorySets|None=None

    def initialize_sequence(self,uow: UnitOfWork,values: Record):
        assert self.memory.information is not None
        self.memory.information.initialize(uow,1)
        return {}

    def handle(self,kind: str,uow: UnitOfWork,envelope: Record,payload: Record):
        assert self.fixed is not None
        return self.fixed.handle(kind,uow,envelope,payload)

    async def open(self,mode='CREATE_NEW'):
        path=self.root/'database'/'runtime.sqlite3'
        opened=await self.storage.initialize(self.candidate.foundation,DatabaseResources('text-database',lambda db,p:db=='text-database' and p==str(path),connect=self.hooks.connect),mode)
        assert type(opened) is Ready,opened
        self.binding=self.config.bind(self.storage,'instance',self.candidate)
        result=await self.binding.persist_semantic_configuration('initialize',self.candidate,actor='fixture',protected_directories=self.supplied[6])
        assert type(result) is ConfigurationCommitted and result.configuration is not None,result
        self.stored=result.configuration;self.binding.release_bootstrap_writers()
        self.history=HistoryBinding(self.history_catalog,self.storage,'instance',8,8192,self.candidate.foundation,text_format=True)
        self.memory=MemoryTransactions(self.memory_catalog,self.storage,self.stored,'instance',self.history,UnusedSources())
        self.memory.bind_information(self.stored)
        self.cognition_lease=self.storage.claim_module_owner(self.fixed_catalog.definition);assert self.cognition_lease is not None
        claims={'instance_id':'instance','set_id':'fixed-set','entry_id':'entry','review_ref':'fixture-review','review_digest':'a'*64,
            'manifest_digest':sha256(encode_content(tuple(m['content_digest'] for m in self.members),8192)).hexdigest(),'reviewed_by':'fixture-supervisor'}
        self.grant=FixedReviewAuthority(lambda received:dict(received)==claims).grant(claims)
        establishment=next(d for d in self.semantic_commands.commands if d.operation_kind=='fixed_establish')
        self.fixed=FixedMemorySets(self.fixed_catalog,self.storage,self.stored,self.memory,self.grant,establishment,lambda:None)
        self.port=FixedMemoryPort(self.fixed,self.semantic_commands.commands,lambda:None)
        initial_port=self.initial.bind(self.memory,InitialSelfBinding('fixture-supervisor','self','Fixture','SYNTHETIC_FIXTURE'))
        if mode=='CREATE_NEW':
            assert type(await self.storage.bind_operation(self.fixture_command,'instance').execute('sequence',LocalCommand(1,{},{}))) is Committed
            assert type(await initial_port.register_initial_self('self','NO_PRESET','Synthetic self.','SYNTHETIC_FIXTURE')) is Committed
        return self

    def envelope(self,kind: str,key: str,payload: dict) -> Record:
        return self.port.envelope(kind,key,MappingProxyType(payload),1)

    async def prepare(self):
        config={'database_id':self.stored.database_id,'instance_id':'instance','snapshot_id':self.stored.snapshot_id}
        start=self.envelope('fixed_begin','begin',{'set_id':'fixed-set','config':config,
            **{k:self.grant.claims[k] for k in ('manifest_digest','review_ref','review_digest')}})
        result=await self.port.begin(start,time.monotonic()+5);assert type(result) is Committed,result
        for ordinal,member in enumerate(self.members):
            env=self.envelope('fixed_add_member','add:'+str(ordinal),{'set_id':'fixed-set','expected_revision':ordinal+1,**member})
            result=await self.port.add_member(env,time.monotonic()+5);assert type(result) is Committed,result
        env=self.envelope('fixed_seal','seal',{'set_id':'fixed-set','expected_revision':13})
        result=await self.port.seal(env,time.monotonic()+5);assert type(result) is Committed,result

    def establishment(self,ordinal: int,key: str|None=None) -> Record:
        return self.envelope('fixed_establish',key or 'establish:'+str(ordinal),{'set_id':'fixed-set','expected_revision':14+ordinal,
            'ordinal':ordinal,'expected_member_revision':1})

    async def close(self):
        self.port.close();self.initial.close();self.memory.close();self.history.close()
        assert self.cognition_lease is not None
        self.cognition_lease.release();self.binding.close();await self.storage.close()
