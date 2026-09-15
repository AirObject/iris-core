"""Controlled artifact fixture for the real generation transaction/file owners.

Only the fixture apply boundary fabricates deterministic vectors. Tests exercise
the complete index lifecycle, without claiming Provider or source establishment.
"""
from hashlib import sha256
from types import MappingProxyType
import json
from companion_memory.persistence import Committed,ResultBoundCommand
from companion_memory.persistence.semantic_records import Record,number,string,identity
from companion_memory.retrieval.semantic_generation import SemanticGenerations
from companion_memory.retrieval.semantic_material import render_document
from companion_memory.retrieval.semantic_payloads import artifact_leaves
from companion_memory.retrieval.semantic_binary import vector_bytes
from companion_memory.memory.formats import record
from tests.semantic.coverage_support import CoverageFixture,current
from tests.semantic.test_files_ranking import owner


class GenerationFixture(CoverageFixture):
    async def open(self,mode='CREATE_NEW'):
        await super().open(mode)
        self.files=owner(self.root/'vectors')
        self.generations=SemanticGenerations(self.retrieval_catalog,self.storage,self.stored,'instance',self.coverage,self.files,lambda:None)
        return self

    def application_digest(self,object_id: str,revision: int) -> str:
        return sha256(render_document(current(object_id,revision))).hexdigest()

    def application_artifact(self,object_id: str,revision: int) -> str:
        return identity('embedding-artifact',object_id,revision)

    def semantic_handle(self,kind,uow,envelope,payload):
        if kind!='apply':return self.generations.handle(kind,uow,envelope,payload)
        if payload['kind']=='EMBED':
            obj=record(payload['object_ref']);artifact_id=string(payload['artifact_id'])
            if self.retrieval.get('embedding_artifact',uow,artifact_id) is None:
                operation,fingerprint=self.storage.semantic_operation_context(uow,self.retrieval_catalog.definition)
                proof=MappingProxyType({'kind':kind,'key':operation.operation_key,'fingerprint':fingerprint})
                vector=(1.0,)+(0.0,)*1023
                config={'database_id':self.stored.database_id,'instance_id':'instance','snapshot_id':self.stored.snapshot_id}
                self.retrieval.write('embedding_artifact',uow,{'v':1,'revision':1,'row_id':artifact_id,'artifact_id':artifact_id,
                    'config':config,'space_id':self.coverage.space_id,'purpose':'DOCUMENT','partition_id':'fixture',
                    'material_digest':self.application_digest(string(obj['object_id']),number(obj['revision'])),
                    'request_ref':{'request_id':'request:'+string(obj['object_id'])+':'+str(obj['revision']),'attempt_id':'controlled-attempt'},
                    'dimension':1024,'vector_digest':sha256(vector_bytes(vector)).hexdigest(),'vector_leaf_count':2,
                    'usage_ref':proof,'completion_ref':proof,'received_at':1})
                for leaf in artifact_leaves(artifact_id,{'vectors':(vector,),'dimensions':1024,'space_id':self.coverage.space_id,
                    'model_id':'doubao-embedding-vision','input_items':1},space_id=self.coverage.space_id,model_id='doubao-embedding-vision'):
                    self.retrieval.write('embedding_vector_leaf',uow,leaf)
        return super().semantic_handle(kind,uow,envelope,payload)

    async def command(self,kind: str,key: str,payload: object):
        definition=next(d for d in self.semantic_commands.commands if d.operation_kind==kind)
        values={'binding_id':'fixture','request_key':key,'expected':[],'observed_at':100,
            'payload':json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))}
        return await self.storage.bind_operation(definition,'instance').execute(key,ResultBoundCommand(1,values,
            {audit.event_slot:{'actor':'fixture'} for audit in definition.required_audits}))

    async def row(self,generation: str) -> Record:
        value=await self.retrieval.read('semantic_generation',generation);assert value is not None
        return value

    async def control(self) -> Record:
        value=await self.retrieval.read('semantic_control',self.control_id);assert value is not None
        return value

    async def build(self,generation: str,sequence: int,nonempty=True):
        result=await self.command('begin_generation','begin:'+generation,{'generation_id':generation,'space_id':self.coverage.space_id,
            'captured_seq':sequence,'expected_control_revision':(await self.control())['revision']});assert type(result) is Committed,result
        if nonempty:
            result=await self.command('append_page','append:'+generation,{'generation_id':generation,'expected_revision':1,'page_no':0,'after_object_id':None});assert type(result) is Committed,result
            proof=await self.generations.write_page(generation,0)
            result=await self.command('confirm_page','confirm:'+generation,{'generation_id':generation,'expected_revision':2,'page_no':0,
                'page_digest':proof.page_digest});assert type(result) is Committed,result
        sealed=await self.generations.seal_file(generation)
        result=await self.command('seal_generation','seal:'+generation,{'generation_id':generation,'expected_revision':(await self.row(generation))['revision'],
            'file_digest':sealed.file_digest,'file_bytes':sealed.file_bytes});assert type(result) is Committed,result
        return sealed

    async def publish(self,generation: str):
        value=await self.row(generation)
        return await self.command('publish_generation','publish:'+generation,{'generation_id':generation,'expected_revision':value['revision'],
            'file_digest':value['file_digest'],'file_bytes':value['file_bytes'],'expected_control_revision':(await self.control())['revision']})

    async def close(self):
        assert self.files.close()
        await super().close()
