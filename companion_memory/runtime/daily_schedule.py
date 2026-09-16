"""Durable explicit daily scheduling with FIFO entry coverage and original keys.

Constructing, reopening or polling this owner never enables dispatch. Admission
is a separate volatile switch which only an explicit successful resume opens.
The scheduler stores work references; it neither calls Provider nor owns media.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable
from dataclasses import replace
import time
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,PersistenceService,UnitOfWork,Found,NotFound,NotCommitted,Committed,Field,RecordSchema,StatementDefinition
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.schema import BoundedTextSchema,SequenceSchema,InvalidValue,Value
from companion_memory.persistence.daily_records import DailyRows,ID,UINT,REVISION,identity,enum,Record
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure
from .daily_schedule_records import TABLES,TRIGGER

def schedule_result(kind):
    shape=result_schema(('runtime',),('PAUSED','ENABLED','QUEUED','CLAIMED','TERMINAL','RETIRED'))
    if kind=='retire_trigger_page':return RecordSchema(shape.fields+(Field('retired',SequenceSchema(RecordSchema((Field('object_id',ID),Field('revision',REVISION))),1,4)),))
    return RecordSchema(shape.fields+(Field('trigger',TRIGGER),)) if kind in ('enqueue_learning','complete_trigger') else shape

class DailySchedule:
    """Finite runtime commands share the runtime owner's repository and gate."""
    def __init__(self,catalog:StatementCatalog,content=None):
        if catalog.definition.owner_module!='runtime' or catalog.definition.schema_version!=5:raise InvalidValue()
        self.catalog=catalog;self.content=content;self._bound=False;self._closed=False;self.enabled=False;self._task:asyncio.Task|None=None
        self.authorize:Callable[[str],bool]=lambda entry:False
        self._command_gate=asyncio.Lock();self._waiters=0
        from companion_memory.persistence.owned_statements import OwnerCauses
        self.causes=OwnerCauses()
        self.checkpoint:Callable[[],None]|None=None
        layouts={
            'initialize_daily_schedule':(),
            'resume_learning':(Field('expected_revision',REVISION),Field('mode_epoch',UINT)),
            'pause_learning':(Field('expected_revision',REVISION),Field('mode_epoch',UINT)),
            'enqueue_learning':(Field('entry_id',ID),Field('reason',enum('THRESHOLD','FOCUS','ACTIVE')),Field('target_through_seq',UINT)),
            'claim_learning':(Field('trigger_id',ID),Field('expected_revision',REVISION),Field('schedule_revision',REVISION),Field('batch_id',ID)),
            'complete_trigger':(Field('trigger_id',ID),Field('expected_revision',REVISION),Field('batch_id',ID)),
            'retire_trigger_page':(Field('references',SequenceSchema(RecordSchema((Field('object_id',ID),Field('revision',REVISION))),1,4)),),
        }
        commands=[]
        for name,fields in layouts.items():
            requirements,bindings=audits(name,('runtime',))
            def handle(uow:UnitOfWork,values:Record,kind=name):
                try:return self._apply(kind,uow,values)
                except OwnerFailure as failure:
                    self.causes.record(kind,values,failure)
                    raise
            commands.append(ResultBoundCommandDefinition('runtime',name,1,RecordSchema((Field('operation_id',ID),)+fields),1,
                schedule_result(name), (catalog.definition,) if content is None else content.repositories,requirements,handle,INTENT,bindings))
        self.commands=tuple(commands)

    def bind(self,storage:PersistenceService,configuration:StoredDailyConfiguration,checkpoint:Callable[[],None]):
        """Borrow only runtime-native statements; reopening leaves dispatch disabled."""
        if self._bound or stored_daily_configuration_issue(configuration) is not None or not callable(checkpoint):raise InvalidValue()
        self.configuration=configuration;self.storage=storage;self.checkpoint=checkpoint
        self.rows=DailyRows(self.catalog,TABLES,storage,configuration.database_id,configuration.scope_id,configuration.snapshot_id)
        self.operations={d.operation_kind:storage.bind_operation(d,configuration.scope_id) for d in self.commands}
        self.schedule_id=identity('learning-schedule',configuration.database_id,configuration.scope_id)
        self._bound=True;self.enabled=False

    async def observation(self,after:str='') -> Record:
        """Return a bounded owner projection without event text or write authority."""
        if not self._bound or self._closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        if type(after) is not str or len(after.encode())>128:raise InvalidValue()
        schedule=await self.rows.read('learning_schedule',self.schedule_id)
        page=await self.rows.page('daily_learning_triggers',after)
        keys=('object_id','revision','entry_id','reason','phase','batch_id','target_through_seq')
        return MappingProxyType({'scheduler':schedule['state'] if schedule else None,'sending_enabled':self.enabled,
            'items':tuple(MappingProxyType({k:r[k] for k in keys}) for r in page),
            'next_after':page[-1]['object_id'] if len(page)==4 else None,'cleanup_pending':self._task is not None})

    def _operation(self,uow:UnitOfWork):
        op=self.storage.daily_operation_context(uow,self.catalog.definition)
        return {name:getattr(op,name) for name in ('owner_namespace','operation_kind','scope_id','operation_key')}

    def _base(self,object_id:str,now:int):
        return {'format_version':1,'object_id':object_id,'revision':1,'database_id':self.configuration.database_id,
            'instance_id':self.configuration.scope_id,'config_snapshot_id':self.configuration.snapshot_id,'created_at_us':now,'updated_at_us':now}

    def _apply(self,kind:str,uow:UnitOfWork,v:Record):
        if not self._bound or self._closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        if self.checkpoint is None:raise InvalidValue()
        self.checkpoint();uow.require_commit_permission(lambda:not self._closed)
        operation=self._operation(uow)
        if operation['operation_key']!=v['operation_id']:raise InvalidValue()
        now=time.time_ns()//1000;targets=[]
        receipt_trigger=None;retired=None
        if kind=='retire_trigger_page':
            from companion_memory.memory.formats import record as native_record,sequence
            page=self.rows.rows.stage('daily_terminal_page',uow,{})
            originals={row['object_id']:self.rows.decode('daily_learning_triggers',row) for row in page}
            refs=tuple(native_record(ref) for ref in sequence(v['references']))
            if len({cast(str,ref['object_id']) for ref in refs})!=len(refs):raise InvalidValue()
            for ref in refs:
                original=originals.get(ref['object_id'])
                if original is None or original['revision']!=ref['revision']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
                for field in ('original_operation','terminal_operation'):
                    proof_operation=native_record(original[field]);definition=next(d for d in self.commands if d.operation_kind==proof_operation['operation_kind'])
                    proof=self.storage.confirm_prior_operation(uow,definition,cast(str,proof_operation['operation_key']))
                    if proof is None or native_record(proof.result)['trigger']!=original and field=='terminal_operation':
                        raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
                deleted=self.rows.rows.stage('daily_delete_terminal',uow,{'object_id':original['object_id'],'revision':original['revision']})
                if len(deleted)!=1:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            current=self.rows.get('learning_schedule',uow,self.schedule_id)
            if current is None:raise InvalidValue()
            changed=self.rows.write('learning_schedule',uow,dict(current)|{'revision':cast(int,current['revision'])+1,'updated_at_us':now,'last_operation':operation},cast(int,current['revision']))
            targets.append(target(self.schedule_id,cast(int,changed['revision']),cast(int,current['revision'])))
            retired=refs
            state='RETIRED'
        elif kind=='initialize_daily_schedule':
            if self.rows.get('learning_schedule',uow,self.schedule_id) is not None:raise InvalidValue()
            record=self.rows.write('learning_schedule',uow,{**self._base(self.schedule_id,now),'state':'PAUSED','last_entry_id':None,
                'last_kind':None,'mode_epoch':0,'last_operation':operation})
            targets.append(target(self.schedule_id,1));state='PAUSED'
        elif kind in ('pause_learning','resume_learning'):
            current=self.rows.get('learning_schedule',uow,self.schedule_id)
            if current is None or current['revision']!=v['expected_revision']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            state='ENABLED' if kind=='resume_learning' else 'PAUSED'
            record=self.rows.write('learning_schedule',uow,{**current,'revision':cast(int,current['revision'])+1,'updated_at_us':now,
                'state':state,'mode_epoch':v['mode_epoch'],'last_operation':operation},cast(int,current['revision']))
            targets.append(target(self.schedule_id,cast(int,record['revision']),cast(int,current['revision'])))
        elif kind=='enqueue_learning':
            # Entry authorization and the observed target sequence belong to the
            # ingress/buffer ports which issue the host invocation.
            trigger=identity('learning-trigger',self.configuration.database_id,self.configuration.scope_id,cast(str,v['operation_id']))
            if self.rows.get('daily_learning_triggers',uow,trigger) is not None:raise InvalidValue()
            count=self.rows.rows.stage('daily_trigger_counts',uow,{})[0]['active']
            if count is not None and cast(int,count)>=128:raise OwnerFailure('RESOURCE_BUSY','resource','CAPACITY_REACHED')
            batch=None
            if self.content is not None:
                if not self.authorize(cast(str,v['entry_id'])):raise OwnerFailure('ACCESS_DENIED','entry','BINDING_MISMATCH')
                state=self.content.buffers.current(uow,cast(str,v['entry_id']))
                if not 0<cast(int,v['target_through_seq'])<cast(int,state['next_sequence']):raise InvalidValue()
                a=self.content;entry=a.ingress.rows.stage('entry',uow,{'entry_id':v['entry_id']})[0]
                platform=a.configuration.candidate.platform(cast(str,entry['platform_id']));count=platform.count('target_count');recent=platform.count('recent_context_count')
                positions=a.buffers.rows.stage('fifo',uow,{'entry_id':v['entry_id'],'state':'NORMAL','limit':count+recent})
                if len(positions)<count+recent or cast(int,positions[count-1]['entry_seq'])>cast(int,v['target_through_seq']):raise OwnerFailure('PRECONDITION_FAILED','source','WINDOW_CHANGED')
                covered=[]
                for raw in self.rows.rows.stage('daily_trigger_coverage',uow,{'entry_id':v['entry_id'],'target_through_seq':positions[count-1]['entry_seq']}):
                    original=self.rows.decode('daily_learning_triggers',raw)
                    previous=a.rows.stage('batches_get',uow,{'batch_id':original['batch_id']})
                    if not previous or previous[0]['terminal']=='FROZEN':covered.append(raw);break
                from .content_assembly import stable
                batch=self.rows.decode('daily_learning_triggers',covered[0])['batch_id'] if covered else stable('batch',stable('preparation',self.configuration.database_id,v['entry_id'],trigger))
            record=self.rows.write('daily_learning_triggers',uow,{**self._base(trigger,now),'entry_id':v['entry_id'],'request_key':v['operation_id'],
                'reason':v['reason'],'target_through_seq':v['target_through_seq'],'phase':'QUEUED','batch_id':batch,
                'original_operation':operation,'terminal_operation':None})
            targets.append(target(trigger,1));state='QUEUED'
            receipt_trigger=record
        else:
            trigger=self.rows.get('daily_learning_triggers',uow,cast(str,v['trigger_id']))
            if trigger is None or trigger['revision']!=v['expected_revision']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            if kind=='claim_learning':
                schedule=self.rows.get('learning_schedule',uow,self.schedule_id)
                if (not self.enabled or schedule is None or schedule['state']!='ENABLED' or schedule['revision']!=v['schedule_revision']
                        or trigger['phase']!='QUEUED'):raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
                if trigger['batch_id'] is not None and trigger['batch_id']!=v['batch_id']:raise InvalidValue()
                uow.require_commit_permission(lambda:self.enabled and not self._closed)
                # One unique partial index in the runtime catalog arbitrates all
                # competing claims for this same entry inside SQLite.
                changed=self.rows.write('learning_schedule',uow,{**schedule,'revision':cast(int,schedule['revision'])+1,'updated_at_us':now,
                    'last_entry_id':trigger['entry_id'],'last_kind':'LEARNING','last_operation':operation},cast(int,schedule['revision']))
                targets.append(target(self.schedule_id,cast(int,changed['revision']),cast(int,schedule['revision'])))
                state='CLAIMED';terminal=None
            else:
                if trigger['phase'] not in (('CLAIMED','QUEUED') if self.content is not None else ('CLAIMED',)) or trigger['batch_id']!=v['batch_id']:raise InvalidValue()
                if self.content is not None:
                    rows=self.content.rows.stage('batches_get',uow,{'batch_id':v['batch_id']})
                    if rows:
                        if len(rows)!=1 or rows[0]['entry_id']!=trigger['entry_id'] or rows[0]['terminal']=='FROZEN':raise InvalidValue()
                    else:
                        preparations=self.content.rows.stage('daily_preparation_for_batch',uow,{'batch_id':v['batch_id']})
                        if preparations:
                            if len(preparations)!=1 or preparations[0]['phase'] not in ('EXPIRED','INVALIDATED'):raise InvalidValue()
                        else:
                            for queue_state in ('NORMAL','STAGED'):
                                pending=self.content.buffers.rows.stage('fifo',uow,{'entry_id':trigger['entry_id'],'state':queue_state,'limit':1})
                                if pending and cast(int,pending[0]['entry_seq'])<=cast(int,trigger['target_through_seq']):raise InvalidValue()
                state='TERMINAL';terminal=operation
            changed=self.rows.write('daily_learning_triggers',uow,{**trigger,'revision':cast(int,trigger['revision'])+1,'updated_at_us':now,
                'phase':state,'batch_id':v['batch_id'],'terminal_operation':terminal},cast(int,trigger['revision']))
            targets.append(target(cast(str,trigger['object_id']),cast(int,changed['revision']),cast(int,trigger['revision'])))
            if kind=='complete_trigger':receipt_trigger=changed
        value=result(cast(str,v['operation_id']),state,{'runtime':{'rows_changed':self.storage.transaction_row_changes(uow)['runtime'],'targets':targets}})
        if retired is not None:return MappingProxyType(dict(value)|{'retired':retired})
        return MappingProxyType(dict(value)|{'trigger':receipt_trigger}) if receipt_trigger is not None else value

    async def execute(self,kind:str,key:str,values:dict[str,object],actor:str):
        """Serialize finite local control/trigger commands without releasing actual work."""
        if self._closed or self._waiters>=128:raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
        if kind=='pause_learning':self.enabled=False
        self._waiters+=1
        acquired=False
        try:
            try:await asyncio.wait_for(self._command_gate.acquire(),5)
            except TimeoutError:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED') from None
            acquired=True
            return await self._execute(kind,key,values,actor)
        finally:
            if acquired:self._command_gate.release()
            self._waiters-=1

    async def _execute(self,kind:str,key:str,values:dict[str,object],actor:str):
        """Confirm original commands before constructing any new local transaction."""
        if not self._bound or kind not in self.operations:raise InvalidValue()
        if self._closed or self._task is not None:raise OwnerFailure('RESOURCE_BUSY','state','CLEANUP_PENDING',self._task is not None)
        definition=next(d for d in self.commands if d.operation_kind==kind)
        command=ResultBoundCommand(1,{'operation_id':key,**values},{r.event_slot:{'actor':actor} for r in definition.required_audits})
        if kind=='pause_learning':self.enabled=False
        async def apply():
            operation=self.operations[kind]
            outcome,cause=await self.causes.execute_original(operation,definition,key,command)
            if type(outcome) is NotCommitted and cause is not None:
                from .results import NotCommitted as DomainNotCommitted,RuntimeError
                return DomainNotCommitted(RuntimeError(cause.code,kind,cause.field,cause.reason,cause.cleanup_pending or outcome.error is not None and outcome.error.cleanup_pending))
            if type(outcome) is Committed and outcome.source=='NEW' and kind=='resume_learning' and not self._closed:self.enabled=True
            return outcome
        task,logical=start_owned(apply());self._task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=5)
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def current(self):
        """Observe the original schedule; no idle checkpoint writes are created."""
        return await self.rows.read('learning_schedule',self.schedule_id)

    async def confirm_control(self,kind:str,key:str):
        """Return the original control receipt without changing volatile admission."""
        if not self._bound or self._closed or kind not in ('resume_learning','pause_learning'):raise InvalidValue()
        original=await self.operations[kind].read_receipt(key)
        if type(original) is Found:return Committed(original.value,'EXISTING')
        if type(original) is NotFound:return None
        return original

    async def queued(self,after:str=''):
        """One fixed page supports fair host selection without unbounded loading."""
        rows=await self.rows.rows.read('daily_queued_page',{'after':after})
        return tuple(self.rows.decode('daily_learning_triggers',row) for row in rows)

    async def claimed(self):
        """Return one finite original claimed page without acquiring work."""
        rows=await self.rows.rows.read('daily_claimed_page',{})
        return tuple(self.rows.decode('daily_learning_triggers',row) for row in rows)

    async def retire_page(self):
        """Delete at most four terminal roots only after both original receipts exist."""
        page=await self.rows.rows.read('daily_terminal_page',{})
        if not page:return Found(MappingProxyType({'state':'EMPTY','rows_changed':0}))
        refs=tuple({'object_id':row['object_id'],'revision':row['revision']} for row in page)
        from hashlib import sha256
        from companion_memory.persistence.content_codec import encode_content
        from companion_memory.provider.values import freeze
        digest=sha256(encode_content(cast(Value,freeze(refs,2048,owned=True)),2048)).hexdigest()
        return await self.execute('retire_trigger_page',identity('trigger-retire',self.configuration.database_id,self.configuration.scope_id,digest),{'references':refs},'daily_scheduler')

    def close(self):
        """Admission closes immediately; storage retains actual original command work."""
        self.enabled=False;self._closed=True
        return self._task is None and self._waiters==0
