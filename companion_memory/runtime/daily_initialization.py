"""Finite native startup intent and receipt-backed continuation for one database.

The intent is written before configuration or business initialization. Reopening
must match the complete trusted binding; completed steps must retain their exact
receipts. Only an unfinished intent authorizes missing original steps. No model
permission is issued, and a completed database is never repaired from absence.
"""
import time
from types import MappingProxyType
from dataclasses import replace
from typing import cast
from companion_memory.persistence import Field,RecordSchema,ResultBoundCommandDefinition,ResultBoundCommand,Committed,Found,NotFound,PersistenceService,UnitOfWork,Receipt
from companion_memory.persistence.daily_records import BASE,DailyTable,DailyRows,ID,UINT,DIGEST,Record,enum,daily_catalog,identity
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.schema import SequenceSchema,InvalidValue
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure

STEPS=('configuration','roots','media','runtime','information','schedule','persona')
PROOF=RecordSchema((Field('owner',ID),Field('kind',ID),Field('key',ID),Field('commit_id',ID),Field('fingerprint',DIGEST)))
BODY=RecordSchema(BASE+(Field('binding',DIGEST),Field('state',enum('INITIALIZING','COMPLETE')),
    Field('receipts',SequenceSchema(PROOF,0,7))))
TABLES=(DailyTable('daily_initialization',(BODY,),8192,True),)
DREAM_BODY=RecordSchema(tuple(Field('receipts',SequenceSchema(PROOF,0,8)) if f.name=='receipts' else f for f in BODY.fields))
DREAM_TABLES=(replace(TABLES[0],schemas=(DREAM_BODY,)),)

def initialization_catalog(*,dream_format:bool=False):
    """Declare bounded runtime-owned progress without extending configuration writes."""
    return daily_catalog('runtime',5,DREAM_TABLES if dream_format else TABLES)

class DailyInitialization:
    """Trusted startup coordinator; progress records prove original committed work."""
    def __init__(self,catalog:StatementCatalog):
        self.catalog=catalog;self.bound=False;self.lease=None;self.closed=False;self.current:Record|None=None
        commands=[]
        for kind,fields in (('begin_daily_initialization',(Field('started_at_us',UINT),)),('record_daily_initialization',(Field('proof',PROOF),)),('finish_daily_initialization',())):
            required,bindings=audits(kind,('runtime',))
            def handler(uow,values,name=kind):return self.apply(name,uow,values)
            commands.append(ResultBoundCommandDefinition('runtime',kind,1,RecordSchema((Field('operation_id',ID),Field('binding',DIGEST))+fields),1,
                result_schema(('runtime',),('INITIALIZING','COMPLETE')),(catalog.definition,),required,handler,INTENT,bindings))
        self.commands=tuple(commands)

    def bind(self,storage:PersistenceService,database:str,instance:str,binding:str,definitions:tuple,*,create:bool,persona:bool,configuration_key:str,dream_format:bool=False):
        """Acquire the runtime bootstrap lease before any content owner is bound."""
        if self.bound:raise InvalidValue()
        self.storage=storage;self.database=database;self.instance=instance;self.binding=binding;self.create=create
        self.begin_time=time.time_ns()//1000
        self.steps=(STEPS if persona else STEPS[:-1])+(('dream',) if dream_format else ())
        from .content_assembly import stable
        self.expected=(('configuration','initialize_dream_configuration' if dream_format else 'initialize_daily_configuration',configuration_key),
            ('runtime','initialize_daily_roots',stable('initialize_daily_roots',configuration_key)),
            ('media','initialize_media_root','media-root'),
            ('runtime','information_initialize_content_runtime',stable('initialize_runtime',instance)),
            ('information','initialize_information_owners',stable('initialize_information',instance)),
            ('runtime','initialize_daily_schedule',stable('initialize_daily_schedule',instance)),
            ('self_model','import_approved_persona_with_self',stable('approved_persona_import',instance)))[:7 if persona else 6]
        if dream_format:self.expected+= (('dream','initialize_dream_control',stable('initialize_dream_control',instance)),)
        self.lease=storage.claim_module_owner(self.catalog.definition)
        if self.lease is None or self.lease.database_id!=database:raise InvalidValue()
        self.rows=DailyRows(self.catalog,DREAM_TABLES if dream_format else TABLES,storage,database,instance,'initialization-intent')
        self.row_id=identity('daily-initialization',database,instance)
        self.operations={d.operation_kind:storage.bind_operation(d,instance) for d in self.commands}
        self.definitions={(d.owner_namespace,d.operation_kind):d for d in definitions}
        self.bound=True

    @property
    def unfinished(self):
        return self.current is not None and self.current['state']=='INITIALIZING'

    @property
    def original_time(self)->int:
        if self.current is None:raise InvalidValue()
        return cast(int,self.current['created_at_us'])

    def release_bootstrap(self):
        if self.lease is not None:
            if not self.lease.release():return False
            self.lease=None
        return True

    async def load(self):
        """Reject missing intent on reopen and missing receipts even during continuation."""
        value=await self.rows.read('daily_initialization',self.row_id)
        if value is None:
            if not self.create:raise OwnerFailure('STORAGE_FAILED','initialization','INTEGRITY_FAILURE')
            outcome=await self.execute('begin_daily_initialization',self.row_id,{'started_at_us':self.begin_time})
            if type(outcome) is not Committed:return outcome
            value=await self.rows.read('daily_initialization',self.row_id)
        if value is None or value['binding']!=self.binding:raise OwnerFailure('ACCESS_DENIED','initialization','BINDING_MISMATCH')
        proofs=cast(tuple[Record,...],value['receipts'])
        if len(proofs)>len(self.steps) or value['state']=='COMPLETE' and len(proofs)!=len(self.steps):raise InvalidValue()
        if value['revision']!=1+len(proofs)+int(value['state']=='COMPLETE'):raise InvalidValue()
        await self.confirm_metadata('begin_daily_initialization',self.row_id,{'started_at_us':value['created_at_us']})
        for index,proof in enumerate(proofs):
            self.check_proof(index,proof)
            await self.confirm_metadata('record_daily_initialization',identity('initialization-step',self.database,self.instance,self.steps[index]),{'proof':dict(proof)})
            definition=self.definitions.get((proof['owner'],proof['kind']))
            if definition is None:raise InvalidValue()
            found=await self.storage.bind_operation(definition,self.instance).read_receipt(cast(str,proof['key']))
            if type(found) is not Found or self.proof(found.value)!=proof:raise OwnerFailure('STORAGE_FAILED','initialization','INTEGRITY_FAILURE')
        if value['state']=='COMPLETE':await self.confirm_metadata('finish_daily_initialization',identity('initialization-complete',self.database,self.instance),{})
        self.current=value
        return Found(value)

    def check_proof(self,index:int,proof):
        if (proof['owner'],proof['kind'],proof['key'])!=self.expected[index]:raise InvalidValue()

    def proof(self,receipt:Receipt):
        return MappingProxyType({'owner':receipt.identity.owner_namespace,'kind':receipt.identity.operation_kind,
            'key':receipt.identity.operation_key,'commit_id':receipt.commit_id,'fingerprint':receipt.fingerprint})

    async def step(self,name:str,receipt:Receipt):
        """Advance only after an original native receipt is available; replay writes nothing."""
        if self.current is None or name not in self.steps:raise InvalidValue()
        index=self.steps.index(name);proof=self.proof(receipt);old=cast(tuple[Record,...],self.current['receipts'])
        self.check_proof(index,proof)
        if index<len(old):
            if old[index]!=proof:raise InvalidValue()
            return Committed(receipt,'EXISTING')
        if index!=len(old) or not self.unfinished:raise InvalidValue()
        outcome=await self.execute('record_daily_initialization',identity('initialization-step',self.database,self.instance,name),{'proof':dict(proof)})
        if type(outcome) is Committed:await self.load()
        return outcome

    async def finish(self):
        if self.current is None:raise InvalidValue()
        if not self.unfinished:return Found(self.current)
        value=await self.execute('finish_daily_initialization',identity('initialization-complete',self.database,self.instance),{})
        if type(value) is Committed:await self.load()
        return value

    async def receipt(self,owner:str,kind:str,key:str):
        return await self.storage.bind_operation(self.definitions[(owner,kind)],self.instance).read_receipt(key)

    def command(self,kind:str,key:str,values:dict):
        definition=next(d for d in self.commands if d.operation_kind==kind)
        return ResultBoundCommand(1,{'operation_id':key,'binding':self.binding,**values},
            {a.event_slot:{'actor':'daily_initialization'} for a in definition.required_audits})

    async def confirm_metadata(self,kind:str,key:str,values:dict):
        port=self.operations[kind]
        original=await port.resolve_operation(port.recovery_handle(key,self.command(kind,key,values)))
        if type(original) is not Committed:raise OwnerFailure('STORAGE_FAILED','initialization','INTEGRITY_FAILURE')

    async def execute(self,kind:str,key:str,values:dict):
        return await self.operations[kind].execute(key,self.command(kind,key,values))

    def apply(self,kind:str,uow:UnitOfWork,values:Record):
        if not self.bound or self.closed or values['binding']!=self.binding:raise InvalidValue()
        old=self.rows.get('daily_initialization',uow,self.row_id);now=time.time_ns()//1000
        if kind=='begin_daily_initialization':
            if not self.create or old is not None:raise InvalidValue()
            now=cast(int,values['started_at_us'])
            value={'format_version':1,'object_id':self.row_id,'revision':1,'database_id':self.database,'instance_id':self.instance,
                'config_snapshot_id':'initialization-intent','created_at_us':now,'updated_at_us':now,'binding':self.binding,'state':'INITIALIZING','receipts':()}
            previous=None
        else:
            if old is None or old['binding']!=self.binding or old['state']!='INITIALIZING':raise InvalidValue()
            previous=cast(int,old['revision']);value=dict(old)|{'revision':previous+1,'updated_at_us':now}
            proofs=cast(tuple[Record,...],old['receipts'])
            if kind=='record_daily_initialization':
                proof=cast(Record,values['proof']);definition=self.definitions.get((proof['owner'],proof['kind']))
                if len(proofs)>=len(self.steps) or definition is None:raise InvalidValue()
                self.check_proof(len(proofs),proof)
                receipt=self.storage.confirm_prior_operation(uow,definition,cast(str,proof['key']))
                if receipt is None or self.proof(receipt)!=proof:raise InvalidValue()
                value['receipts']=proofs+(proof,)
            else:
                if len(proofs)!=len(self.steps):raise InvalidValue()
                value['state']='COMPLETE'
        written=self.rows.write('daily_initialization',uow,value,previous)
        return result(cast(str,values['operation_id']),cast(str,written['state']),{'runtime':{'rows_changed':1,'targets':[target(self.row_id,cast(int,written['revision']),previous)]}})

    def permits_media_continuation(self,configuration)->bool:
        """Only original pre-media intent permits an unfinished physical directory setup."""
        return self.bound and not self.closed and self.unfinished and self.current is not None and len(cast(tuple,self.current['receipts']))==2 and configuration.database_id==self.database and configuration.scope_id==self.instance

    def close(self):
        self.closed=True
        return self.release_bootstrap()
