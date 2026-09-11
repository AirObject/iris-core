"""Disposable READY media ownership; no files, decoding or production media service.

Each event retains an explicit occurrence manifest, including empty manifests.
Consumption preserves the owner until every enclosing source/buffer releases it.
"""
from typing import cast
from companion_memory.persistence import (
    RepositoryDefinition,TableDefinition,StatementDefinition,RecordSchema,Field,
    ScalarSchema,BoundedTextSchema,ResultBoundCommandDefinition,ResultBoundCommand,
    AuditResultBinding,AuditFieldBinding,
)
from companion_memory.persistence.runtime_repositories import RuntimeRepository
from companion_memory.logging_service import AuditRequirement
from companion_memory.runtime.records import OwnedRows,DomainFailure,data,stable_id
from companion_memory.runtime.transaction_schema import RESULT,CHANGE,facts


class SyntheticMedia:
    owner_module='synthetic_media'
    def __init__(self):
        identifier=ScalarSchema('identifier');integer=ScalarSchema('integer')
        row=RecordSchema((Field('object_id',identifier),Field('entry_id',identifier),Field('sequence',integer),Field('state',identifier),Field('revision',integer),Field('body',BoundedTextSchema(8192))))
        columns='object_id,entry_id,sequence,state,revision,body'
        table=TableDefinition('synthetic_media','CREATE TABLE synthetic_media(scope_id TEXT NOT NULL,object_id TEXT NOT NULL,entry_id TEXT NOT NULL,sequence INTEGER NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(scope_id,object_id))')
        get=StatementDefinition('SELECT '+columns+' FROM synthetic_media WHERE scope_id=:scope_id AND object_id=:object_id',RecordSchema((Field('object_id',identifier),)),row,False)
        insert=StatementDefinition('INSERT INTO synthetic_media VALUES(:scope_id,:object_id,:entry_id,:sequence,:state,:revision,:body) RETURNING '+columns,row,row,True)
        update=StatementDefinition('UPDATE synthetic_media SET state=:state,revision=:revision,body=:body WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:expected_revision RETURNING '+columns,RecordSchema((Field('object_id',identifier),Field('state',identifier),Field('revision',integer),Field('body',BoundedTextSchema(8192)),Field('expected_revision',integer))),row,True)
        definition=RepositoryDefinition(self.owner_module,1,(table,),(get,insert,update))
        self.repositories=(definition,);self.repository=RuntimeRepository(definition,(('manifest_get',get),('manifest_insert',insert),('manifest_update',update)))
        self.rows:OwnedRows | None=None;self.port=None;self.fail_retain=False
        requirement=AuditRequirement(self.owner_module,'ready','MEDIA_READY',1,('READY',),CHANGE)
        self.command=ResultBoundCommandDefinition(self.owner_module,'ready',1,RecordSchema((Field('reference_id',identifier),Field('entry_id',identifier),Field('modality',ScalarSchema('enum',choices=('IMAGE','AUDIO','VIDEO'))))),1,RESULT,self.repositories,(requirement,),self._ready,RecordSchema((Field('actor',identifier),)),(AuditResultBinding('ready',1,(
            AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),AuditFieldBinding('reason_code','CONSTANT',constant='READY'),AuditFieldBinding('target_refs','RESULT',('targets',)),AuditFieldBinding('change','RESULT',('change',)),)),))
    def bind(self,storage,instance_id):
        self.rows=OwnedRows(self.repository,storage,instance_id);self.port=storage.bind_operation(self.command,instance_id)
    def _ready(self,uow,values):
        assert self.rows is not None
        self.rows.insert('manifest',uow,values['reference_id'],values['entry_id'],0,'READY',{'modality':values['modality']})
        return facts(values['reference_id'],values['entry_id'],'READY',config_snapshot_id='synthetic_media',run_id='media_setup',revision=1)
    async def prepare(self,reference_id,entry_id,modality):
        assert self.port is not None
        return await self.port.execute(reference_id,ResultBoundCommand(1,{'reference_id':reference_id,'entry_id':entry_id,'modality':modality},{'ready':{'actor':'media_setup'}}))
    def retain_event(self,uow,entry_id,message_id,media):
        assert self.rows is not None
        if self.fail_retain:raise DomainFailure('PARTICIPANT_FAILED','participant','PARTICIPANT_UNAVAILABLE')
        occurrences=[]
        for item in media:
            row=self.rows.get('manifest',item['reference_id'],uow)
            if row is None or row['entry_id']!=entry_id or row['state']!='READY' or data(row)['modality']!=item['modality']:
                raise DomainFailure('ACCESS_DENIED','event','BINDING_MISMATCH')
            occurrence=item['occurrence_id']
            if occurrence in occurrences:raise DomainFailure('INVALID_INPUT','event','INVALID_SHAPE')
            occurrences.append(occurrence)
        self.rows.insert('manifest',uow,stable_id('media_event',message_id),entry_id,0,'HELD',{'message_id':message_id,'references':[dict(m) for m in media]})
    def settle_event(self,uow,message_id):
        assert self.rows is not None
        row=self.rows.get('manifest',stable_id('media_event',message_id),uow)
        if row is None or row['state']!='HELD':raise DomainFailure('STORAGE_FAILED','event','INTEGRITY_FAILURE')
        self.rows.update('manifest',uow,row,'CONSUMED',data(row))
    def release_event(self,uow,message_id):
        assert self.rows is not None
        row=self.rows.get('manifest',stable_id('media_event',message_id),uow)
        if row is None:raise DomainFailure('STORAGE_FAILED','event','INTEGRITY_FAILURE')
        self.rows.update('manifest',uow,row,'RELEASED',{'message_id':message_id,'references':[]})
    async def inspect(self,message_id):
        assert self.rows is not None
        return await self.rows.load('manifest',stable_id('media_event',message_id))
