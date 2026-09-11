"""Explicit synthetic learning/publication owner for disposable integration assembly."""
import json
from typing import cast
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,AuditResultBinding,AuditFieldBinding,SequenceSchema,RepositoryDefinition,TableDefinition,StatementDefinition,RecordSchema,Field,ScalarSchema,BoundedTextSchema
from companion_memory.persistence.runtime_repositories import RuntimeRepository
from companion_memory.logging_service import AuditRequirement
from companion_memory.runtime.records import OwnedRows,DomainFailure,data,stable_id


class SyntheticParticipant:
    owner_module='synthetic_learning'
    def __init__(self):
        identifier=ScalarSchema('identifier');integer=ScalarSchema('integer');text=BoundedTextSchema(8192)
        row=RecordSchema(tuple(Field(k,identifier) for k in ('object_id','entry_id'))+(Field('sequence',integer),Field('state',identifier),Field('revision',integer),Field('body',text)))
        sql='CREATE TABLE synthetic_learning (scope_id TEXT NOT NULL, object_id TEXT NOT NULL, entry_id TEXT NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(scope_id,object_id))'
        get=StatementDefinition('SELECT object_id,entry_id,sequence,state,revision,body FROM synthetic_learning WHERE scope_id=:scope_id AND object_id=:object_id',RecordSchema((Field('object_id',identifier),)),row,False)
        insert=StatementDefinition('INSERT INTO synthetic_learning VALUES(:scope_id,:object_id,:entry_id,:sequence,:state,:revision,:body) RETURNING object_id,entry_id,sequence,state,revision,body',row,row,True)
        update=StatementDefinition('UPDATE synthetic_learning SET state=:state,revision=:revision,body=:body WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:expected_revision RETURNING object_id,entry_id,sequence,state,revision,body',RecordSchema((Field('object_id',identifier),Field('state',identifier),Field('revision',integer),Field('body',text),Field('expected_revision',integer))),row,True)
        definition=RepositoryDefinition(self.owner_module,1,(TableDefinition('synthetic_learning',sql),),(get,insert,update))
        self.repositories=(definition,)
        self.repository=RuntimeRepository(definition,(('candidate_get',get),('candidate_insert',insert),('candidate_update',update)))
        self.rows:OwnedRows | None=None
        self.fail_stage=False;self.fail_final=False
        self.publish_port=None
        fields=RecordSchema((Field('run_id',identifier),Field('config_snapshot_id',identifier),Field('publication_id',identifier),Field('exit_key',identifier),Field('exit_epoch',integer),Field('exit_actor',identifier)))
        targets=SequenceSchema(RecordSchema((Field('object_id',identifier),Field('previous_revision',integer,nullable=True),Field('revision',integer))),1,1)
        result=RecordSchema(fields.fields+(Field('targets',targets),))
        requirement=AuditRequirement(self.owner_module,'publication','PUBLICATION_COMPLETED',1,('PUBLISH',),fields)
        self.publication_command=ResultBoundCommandDefinition(self.owner_module,'publish',1,fields,1,result,self.repositories,(requirement,),self._publish,
            RecordSchema((Field('actor',identifier),)),(AuditResultBinding('publication',1,(
                AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),AuditFieldBinding('reason_code','CONSTANT',constant='PUBLISH'),
                AuditFieldBinding('target_refs','RESULT',('targets',)),AuditFieldBinding('change','INTENT',('publication',)),)),))
        # A finite intent carries the already-prepared publication identity; all
        # values are fixed before this independent synthetic publication command.
        from dataclasses import replace
        self.publication_command=replace(self.publication_command,audit_intent_schema=RecordSchema((Field('actor',identifier),Field('publication',fields))))
    def bind(self,storage,instance_id):
        self.rows=OwnedRows(self.repository,storage,instance_id)
        self.publish_port=storage.bind_operation(self.publication_command,instance_id)
    def prepare_outcome(self,batch_snapshot,provider_handoff):
        from companion_memory.buffers import decode_material
        _,members=decode_material(batch_snapshot['messages'])
        targets=[member.message_id for member in members if member.role=='T']
        if not targets:raise DomainFailure('PARTICIPANT_FAILED','participant','RESULT_INVALID')
        category=provider_handoff['outcome']
        if category=='SUCCEEDED':
            try:
                result=json.loads(provider_handoff['result']['text'])
                if type(result) is not dict or set(result)!= {'result_count'} or type(result['result_count']) is not int or not 0<=result['result_count']<=4:raise ValueError()
            except (ValueError,KeyError,TypeError):raise DomainFailure('PARTICIPANT_FAILED','participant','RESULT_INVALID') from None
            terminal='SUCCEEDED';count=result['result_count']
        else:
            terminal='SENSITIVE_DROPPED' if category=='SENSITIVE_REFUSAL' else 'FAILED_DROPPED';count=0
        candidate_id=stable_id('candidate',batch_snapshot['batch_id'])
        return {'batch_id':batch_snapshot['batch_id'],'config_snapshot_id':batch_snapshot['config_snapshot_id'],'request_id':provider_handoff['request_id'],
            'terminal':terminal,'result_count':count,'results':[{'object_id':stable_id('synthetic_result',candidate_id,index),'target_refs':[targets[index%len(targets)]]} for index in range(count)],
            'source_refs':[member.message_id for member in members] if count else []}
    def stage(self,uow,batch_id,candidate_id,outcome):
        assert self.rows is not None
        if self.fail_stage:raise DomainFailure('PARTICIPANT_FAILED','participant','PARTICIPANT_UNAVAILABLE')
        if set(outcome)!= {'request_id','outcome','result','candidate'} or outcome['candidate']['batch_id']!=batch_id:raise DomainFailure('PARTICIPANT_FAILED','participant','RESULT_INVALID')
        self.rows.insert('candidate',uow,candidate_id,batch_id,0,'CANDIDATE_STORED',outcome['candidate'])
    def finalize(self,uow,batch_id,candidate_id,members,protect):
        assert self.rows is not None
        candidate=self.rows.get('candidate',candidate_id,uow)
        if candidate is None or data(candidate)['batch_id']!=batch_id or candidate['state']!='CANDIDATE_STORED' or self.fail_final:raise DomainFailure('PARTICIPANT_FAILED','participant','RESULT_INVALID')
        values=data(candidate)
        targets={m['message_id'] for m in members if m['role']=='T'}
        if any(not item['target_refs'] or not set(cast(list[str],item['target_refs']))<=targets for item in cast(list[dict[str,object]],values['results'])):raise DomainFailure('PARTICIPANT_FAILED','participant','RESULT_INVALID')
        if values['result_count']:
            if values['source_refs']!=[m['message_id'] for m in members]:raise DomainFailure('PARTICIPANT_FAILED','participant','RESULT_INVALID')
            for item in cast(list[dict[str,object]],values['results']):
                self.rows.insert('candidate',uow,cast(str,item['object_id']),batch_id,0,'PUBLISHED',{'target_refs':item['target_refs'],'source_id':stable_id('synthetic_source',candidate_id),'synthetic':True})
            # Synthetic sources depend on the complete frozen material. The callback
            # binds the actual entry; this owner cannot invent an entry capability.
            for member in members:protect('',member['message_id'],member['entry_seq'],stable_id('synthetic_source',candidate_id))
        self.rows.update('candidate',uow,candidate,'TERMINAL',{'batch_id':batch_id,'terminal':values['terminal'],'result_count':values['result_count'],'request_id':values['request_id'],'config_snapshot_id':values['config_snapshot_id']})
        return cast(str,values['terminal']),cast(int,values['result_count'])
    async def recover(self,candidate_id):
        assert self.rows is not None
        candidate=await self.rows.load('candidate',candidate_id)
        return data(candidate) if candidate else None

    def _publish(self,uow,values):
        assert self.rows is not None
        self.rows.insert('candidate',uow,values['publication_id'],values['run_id'],0,'PUBLISHED',dict(values))
        return {**values,'targets':[{'object_id':values['publication_id'],'previous_revision':None,'revision':1}]}
    async def publish(self,run_id,configuration_id,exit_key='finish',exit_epoch=3,exit_actor='dream_coordinator'):
        assert self.publish_port is not None
        values={'run_id':run_id,'config_snapshot_id':configuration_id,'publication_id':stable_id('publication',run_id),'exit_key':exit_key,'exit_epoch':exit_epoch,'exit_actor':exit_actor}
        return await self.publish_port.execute(stable_id('publish',run_id),ResultBoundCommand(1,values,{'publication':{'actor':'publisher','publication':values}}))
    def verify(self,uow,run_id,publication_id,configuration_id):
        assert self.rows is not None
        row=self.rows.get('candidate',publication_id,uow)
        return row is not None and row['state']=='PUBLISHED' and all(data(row)[key]==value for key,value in {'run_id':run_id,'publication_id':publication_id,'config_snapshot_id':configuration_id}.items())
    async def recover_publication(self,run_id,configuration_id):
        assert self.rows is not None
        publication_id=stable_id('publication',run_id)
        row=await self.rows.load('candidate',publication_id)
        if row is not None and row['state']=='PUBLISHED' and all(data(row)[key]==value for key,value in {'run_id':run_id,'publication_id':publication_id,'config_snapshot_id':configuration_id}.items()):
            from companion_memory.runtime.assembly import PublicationRecovery
            values=data(row)
            return PublicationRecovery(run_id,configuration_id,publication_id,cast(str,values['exit_key']),cast(int,values['exit_epoch']),cast(str,values['exit_actor']))
        return None
