"""Daily image result reception through native media and Provider participants."""
from __future__ import annotations
import asyncio
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,Found,NotCommitted,Committed
from companion_memory.persistence.daily_records import ID,REVISION,DIGEST,Field,RecordSchema,identity,enum
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.owned_statements import OwnerFailure,BoundStatements
from companion_memory.persistence.schema import InvalidValue
from companion_memory.memory.formats import record
from companion_memory.provider.daily_service import DailyResult
from companion_memory.provider.values import as_record
from .daily_image import DailyImages

class DailyImageWork:
    """Share media ownership; receive no arbitrary model data through commands."""
    def __init__(self,media,content,provider_repository):
        self.media=media;self.content=content;self.bound=False;self.closed=False;self._result=None;self._task:asyncio.Task|None=None
        self._cleanup=None;self.images:DailyImages|None=None
        from companion_memory.persistence.owned_statements import OwnerCauses
        self.causes=OwnerCauses()
        participants=content.repositories+(provider_repository,)
        self.commands=[]
        for kind,owners in (('associate_daily_image',('media',)),('store_daily_image',('media',)),('retire_daily_image_processing',('media','ingress')),('reject_daily_image_input',('media','ingress'))):
            shape=RecordSchema((Field('operation_id',ID),Field('work_id',ID),Field('request_id',ID),Field('request_digest',DIGEST),Field('expected_revision',REVISION)))
            if kind=='reject_daily_image_input':shape=RecordSchema((Field('operation_id',ID),Field('work_id',ID),Field('expected_revision',REVISION),Field('reason',enum('INPUT_FORMAT_UNSUPPORTED','IMAGE_LIMIT_EXCEEDED','CONTENT_CORRUPT'))))
            required,bindings=audits(kind,owners)
            def handle(uow,value,action=kind):
                try:return self.handle(action,uow,value)
                except OwnerFailure as failure:
                    self.causes.record(action,value,failure)
                    raise
            self.commands.append(ResultBoundCommandDefinition('media',kind,1,shape,1,result_schema(owners,('ASSOCIATED','RESULT_STORED','PROCESSING_RELEASED','REMOTE_UNKNOWN','INPUT_FORMAT_UNSUPPORTED','IMAGE_LIMIT_EXCEEDED','CONTENT_CORRUPT')),
                participants,required,handle,INTENT,bindings))
        self.commands=tuple(self.commands)

    def bind(self,provider):
        if self.bound or not self.media.matches_provider(provider.storage,provider.configuration.database_id,provider.instance):raise InvalidValue()
        self.provider=provider;self.storage=provider.storage;self.configuration=provider.configuration
        self.views=BoundStatements(self.media.catalog,self.storage,'provider')
        self.operations={d.operation_kind:self.storage.bind_operation(d,self.configuration.scope_id) for d in self.commands};self.bound=True

    def key(self,kind,*parts):return identity(kind,self.configuration.database_id,self.configuration.scope_id,*parts)

    def handle(self,kind,uow,v):
        if self.closed or not self.bound:raise InvalidValue()
        m=self.media;work=m.rows.stage('work_get',uow,{'work_id':v['work_id']})[0]
        if work['revision']!=v['expected_revision']:raise OwnerFailure('PRECONDITION_FAILED','revision','WORK_FENCED')
        if kind=='reject_daily_image_input':return self.reject_input(uow,v,work)
        request=self.provider.participate_original(uow,cast(str,v['request_id']),role='MEDIA',owner_ref=cast(str,work['work_id']),
            operation_key=cast(str,work['original_operation_key']),request_digest=cast(str,v['request_digest']))
        self.provider.verify_original_receipt(uow,request)
        before=cast(int,work['revision']);now=time.time_ns()//1000;targets={}
        if kind=='associate_daily_image':
            if work['phase']!='READY_TO_REQUEST' or work['provider_request_id'] is not None:raise InvalidValue()
            m.rows.stage('work_update',uow,dict(work)|{'revision':before+1,'provider_request_id':v['request_id'],'phase':'REQUEST_ASSOCIATED','request_association_state':'ID_CONFIRMED'})
            state='ASSOCIATED'
        elif kind=='store_daily_image':
            if work['provider_request_id']!=request['object_id']:raise InvalidValue()
            if request['phase']=='REMOTE_RESULT_UNKNOWN':
                m.rows.stage('work_update',uow,dict(work)|{'revision':before+1,'phase':'REMOTE_UNKNOWN','request_association_state':'ID_CONFIRMED'})
                state='REMOTE_UNKNOWN'
            else:
                output=None
                if request['outcome']=='SUCCEEDED':
                    received=self._result
                    if type(received) is not DailyResult:raise InvalidValue()
                    self.provider.verify_daily_result(uow,received,role='MEDIA',owner_ref=cast(str,work['work_id']),operation_key=cast(str,work['original_operation_key']),request_digest=cast(str,v['request_digest']))
                    output=as_record(received.value['output'])
                m.work.store_daily(uow,cast(str,work['work_id']),before,request,output,now);state='RESULT_STORED'
        else:
            if self._cleanup!=(work['work_id'],request['object_id']) or work['phase']!='RESULT_STORED':raise InvalidValue()
            old=self.content.ingress.event(uow,cast(str,work['message_id']))
            m.work.release_processing(uow,self.content.ingress,cast(str,work['work_id']),now)
            # A real progress revision names retirement, independently of the
            # already received immutable interpretation and original request.
            m.rows.stage('work_update',uow,dict(work)|{'revision':before+1,'request_association_state':'PROCESSING_RELEASED'})
            changed=self.content.ingress.event(uow,cast(str,work['message_id']))
            targets['ingress']=(target(cast(str,work['message_id']),cast(int,changed['references_revision']),cast(int,old['references_revision'])),);state='PROCESSING_RELEASED'
        targets['media']=(target(cast(str,work['work_id']),before+1,before),)
        counts=self.storage.transaction_row_changes(uow)
        return result(cast(str,v['operation_id']),state,{owner:{'rows_changed':counts[owner],'targets':refs} for owner,refs in targets.items()})

    def reject_input(self,uow,v,work):
        """Release only proven unsent input work; no interpretation or Provider fact."""
        if self.images is None or work['phase']!='READY_TO_REQUEST' or work['provider_request_id'] is not None:raise InvalidValue()
        self.images.verify_rejection(uow,cast(str,work['work_id']),cast(str,v['reason']))
        if not self.provider.participate_unsent_daily(uow,cast(str,work['original_operation_key']),cast(str,work['work_id'])):
            raise OwnerFailure('RESULT_UNCONFIRMED','request','COMMIT_UNCONFIRMED',True)
        m=self.media;now=time.time_ns()//1000;old=self.content.ingress.event(uow,cast(str,work['message_id']))
        m.rows.stage('admissions_insert',uow,{'work_id':work['work_id'],'admission_generation':work['admission_generation'],'preparation_id':work['admission_preparation_id'],
            'operation_key':work['original_operation_key'],'request_id':None,'descriptor':work['original_request_descriptor'],'conclusion':'REGISTRATION_ABSENT','closed_at_us':now})
        m.rows.stage('work_update',uow,dict(work)|{'revision':cast(int,work['revision'])+1,'phase':'PARKED','request_association_state':'INPUT_REJECTED'})
        m.work.release_unsent_processing(uow,self.content.ingress,work,now)
        changed=self.content.ingress.event(uow,cast(str,work['message_id']));counts=self.storage.transaction_row_changes(uow)
        return result(cast(str,v['operation_id']),cast(str,v['reason']),{
            'media':{'rows_changed':counts['media'],'targets':(target(cast(str,work['work_id']),cast(int,work['revision'])+1,cast(int,work['revision'])),)},
            'ingress':{'rows_changed':counts['ingress'],'targets':(target(cast(str,work['message_id']),cast(int,changed['references_revision']),cast(int,old['references_revision'])),)}})

    def rejected_preparation(self,uow,members):
        """Runtime may invalidate a window only from the original media receipt."""
        from companion_memory.memory.formats import sequence
        from .service import identity as media_identity
        definition=next(d for d in self.commands if d.operation_kind=='reject_daily_image_input')
        for raw in members:
            for selected in sequence(record(raw)['media']):
                occurrence=self.media.rows.stage('occurrences_get',uow,{'occurrence_id':record(selected)['occurrence_id']})[0]
                wid=media_identity('occurrence_work',self.configuration.database_id,occurrence['occurrence_id'],'DESCRIBE')
                work=self.media.rows.stage('work_get',uow,{'work_id':wid})
                if not work or work[0]['request_association_state']!='INPUT_REJECTED':continue
                proof=self.storage.confirm_prior_operation(uow,definition,self.key('image-input-reject',wid))
                if proof is None:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
                return True
        return False

    def verify_received(self,uow,request,handoff,proof):
        if request['task_role']!='MEDIA' or proof['kind']!='store_daily_image':return False
        wid=cast(str,as_record(request['attribution'])['run_id']);rows=self.views.stage('daily_provider_work',uow,{'caller_scope':self.configuration.scope_id,'work_id':wid})
        if len(rows)!=1 or rows[0]['provider_request_id']!=request['object_id'] or rows[0]['phase']!='RESULT_STORED':return False
        work=rows[0];versions=self.views.stage('daily_provider_interpretations',uow,{'caller_scope':self.configuration.scope_id,'interpretation_id':work['interpretation_id']})
        if len(versions)!=1:return False
        from .interpretations import decode_interpretation
        value=decode_interpretation(cast(str,versions[0]['body']).encode())
        if value['provider_request_id']!=request['object_id'] or value['source_ref']!=handoff['object_id']:return False
        definition=next(d for d in self.commands if d.operation_kind=='store_daily_image')
        original=self.storage.confirm_cognition_consumer_operation(uow,definition,proof['key'],cast(str,request['object_id']))
        return original is not None and original.fingerprint==proof['fingerprint'] and proof['key']==self.key('image-result',wid,request['object_id'])

    async def execute(self,kind,key,payload):
        if self.closed or self._task is not None:raise OwnerFailure('RESOURCE_BUSY','media','CLEANUP_PENDING',True)
        definition=next(d for d in self.commands if d.operation_kind==kind)
        command=ResultBoundCommand(1,{'operation_id':key,**payload},{a.event_slot:{'actor':'daily_image'} for a in definition.required_audits})
        async def write():
            outcome,cause=await self.causes.execute_original(self.operations[kind],definition,key,command)
            if type(outcome) is NotCommitted and cause is not None:
                from companion_memory.runtime.results import NotCommitted as DomainNotCommitted,RuntimeError
                return DomainNotCommitted(RuntimeError(cause.code,kind,cause.field,cause.reason,cause.cleanup_pending or outcome.error is not None and outcome.error.cleanup_pending))
            return outcome
        task,logical=start_owned(write());self._task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=5)
        if not done:raise OwnerFailure('TIMEOUT','media','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def receive(self,work,request,digest,received):
        if self._result is not None:raise InvalidValue()
        self._result=received
        try:return await self.execute('store_daily_image',self.key('image-result',work['work_id'],request['object_id']),
            {'work_id':work['work_id'],'expected_revision':work['revision'],'request_id':request['object_id'],'request_digest':digest})
        finally:
            if self._task is not None:await asyncio.wait((self._task,))
            self._result=None

    async def retire(self,work,request,digest):
        self._cleanup=work['work_id'],request['object_id']
        try:return await self.execute('retire_daily_image_processing',self.key('image-retire',work['work_id'],request['object_id']),
            {'work_id':work['work_id'],'expected_revision':work['revision'],'request_id':request['object_id'],'request_digest':digest})
        finally:
            if self._task is not None:await asyncio.wait((self._task,))
            self._cleanup=None

    def close(self):
        self.closed=True
        return self._task is None and self._result is None and self._cleanup is None

    async def wait_actual(self):
        """Join this media coordinator's original command and its actual I/O tail."""
        if self._task is not None:await asyncio.wait((self._task,))
