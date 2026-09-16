"""Real memory and ingress owners atomically retain a subject's frozen source."""
from pathlib import Path
import sqlite3
import json
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.persistence import Field,RecordSchema,BoundedTextSchema,ResultBoundCommandDefinition,ResultBoundCommand,Committed,NotCommitted
from companion_memory.persistence.daily_records import ID,identity
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.memory.formats import record,sequence
from companion_memory.memory.sources import decode_source
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.transactions import ApplyScope
from .test_persona_import import ImportFixture
from tests.runtime.configuration_support import event

class SourceFixture(ImportFixture):
    def extra_commands(self):
        catalogs={c.definition.owner_module:c for c in self.content.catalogs}
        requirements,bindings=audits('apply_internal_subject',('memory','ingress'))
        self.subject_command=ResultBoundCommandDefinition('memory','apply_internal_subject',1,
            RecordSchema((Field('operation_id',ID),Field('source',BoundedTextSchema(8192)),Field('change',BoundedTextSchema(8192)))),1,
            result_schema(('memory','ingress'),('SUBJECT_CREATED',)),(catalogs['memory'].definition,catalogs['ingress'].definition),
            requirements,self.apply,INTENT,bindings)
        return (self.subject_command,)
    def apply(self,uow,values):
        source=decode_source(values['source']);change=isolate_change(decode_content(values['change'].encode(),8192),8192,text_format=True,daily_format=True);sid=cast(str,change['target_id'])
        scope=ApplyScope('instance','candidate',cast(str,source['batch_id']),frozenset(),frozenset(),frozenset((sid,)),frozenset())
        members=tuple(record(m) for m in sequence(source['ordered_members']))
        before={cast(str,m['message_id']):self.content.ingress.event(uow,cast(str,m['message_id']))['references_revision'] for m in members}
        applied=self.content.memory.apply_change_set(uow,scope,(change,),source,None,1000000,values['operation_id'],'LEARNING')
        if applied.subjects!=1 or applied.source_holders_acquired!=1 or applied.objects:raise AssertionError(applied)
        counts=self.storage.transaction_row_changes(uow)
        source_id=cast(str,source['source_id'])
        origins=identity('subject-origin',self.content.configuration.database_id,'instance',sid)
        source_row=self.content.memory.source(uow,source_id)[0]
        ingress_targets=[]
        for m in members:
            mid=cast(str,m['message_id']);after=self.content.ingress.event(uow,mid)['references_revision']
            ingress_targets.append(target(mid,cast(int,after),cast(int,before[mid])))
        return result(values['operation_id'],'SUBJECT_CREATED',{
            'memory':{'rows_changed':counts['memory'],'targets':[target(sid,1),target(origins,1),target(source_id,cast(int,source_row['references_revision']))]},
            'ingress':{'rows_changed':counts['ingress'],'targets':ingress_targets}})
    async def execute(self,kind,key,values):
        definition=self.content.command_definition(kind)
        return await self.content.operations[kind].execute(key,ResultBoundCommand(1,{'operation_id':key,**values},
            {r.event_slot:{'actor':'scheduler'} for r in definition.required_audits}))
    async def freeze(self):
        steps=[('initialize_content_runtime','initialize',{}),('register_content_entry','register',{'entry_id':'entry','host_id':'host',
            'platform_id':'sample_platform','external_entry_id':'conversation'})]
        for index in range(3):
            raw=event('input:'+str(index),'原始展板旁的杯子。');raw['event_version']=2
            steps.append(('accept_media_event','input:'+str(index),{'entry_id':'entry','event':json.dumps(raw,ensure_ascii=False,separators=(',', ':'))}))
        steps.extend((('select_content_preparation','select',{'entry_id':'entry','preparation_id':'preparation','batch_id':'batch','run_id':'run'}),
            ('claim_content_preparation','claim',{'preparation_id':'preparation','expected_revision':1,'owner_generation':1}),
            ('complete_content_preparation','prepare',{'preparation_id':'preparation','expected_revision':2,'owner_generation':1}),
            ('freeze_content_batch','freeze',{'preparation_id':'preparation','expected_revision':3,'owner_generation':1})))
        for kind,key,values in steps:
            outcome=await self.execute(kind,key,values)
            if type(outcome) is not Committed:raise AssertionError((kind,outcome))
        batch=(await self.content.rows.read('batches_get',{'batch_id':'batch'}))[0]
        return decode_source(cast(str,batch['manifest']))

class SubjectSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_subject_only_source_is_real_and_invalid_anchor_rolls_back_all_owners(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);f=await SourceFixture(root).open()
            try:
                source=await f.freeze();member=next(record(m) for m in sequence(source['ordered_members']) if record(m)['role']=='T')
                anchor={'message_id':member['message_id'],'part':'BODY','item_index':None,'start_utf8':None,'end_utf8':None,'occurrence_id':None,'interpretation_id':None}
                subject={'subject_version':1,'subject_id':'cup','instance_id':'instance','kind':'THING','platform_id':None,'external_subject_id':None,'label':'杯子','revision':1}
                def change(a):
                    return {'change_version':1,'action':'REGISTER_SUBJECT','target_id':'cup','expected_revision':None,'proposed_value':subject,
                        'links':{'sources':[{'object_id':'cup','object_revision':1,'source_id':source['source_id'],'link_role':'DIRECT','target_anchors':[a],'auxiliary_refs':[]}],'bases':[]}}
                operation=f.storage.bind_operation(f.subject_command,'instance')
                async def apply(a):
                    return await operation.execute('subject',ResultBoundCommand(1,{'operation_id':'subject','source':encode_content(source,8192).decode(),'change':json.dumps(change(a),ensure_ascii=False,separators=(',', ':'))},
                        {r.event_slot:{'actor':'scheduler'} for r in f.subject_command.required_audits}))
                failed=await apply({**anchor,'message_id':'foreign-message'})
                self.assertIs(type(failed),NotCommitted)
                with sqlite3.connect(f.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subject_origins').fetchone()[0],0)
                done=await apply(anchor)
                if type(done) is not Committed:raise AssertionError(done)
                origin=await f.content.memory.verify_subject_origin('cup',cast(str,source['source_id']))
                self.assertIsNotNone(origin)
                with sqlite3.connect(f.path) as db:
                    self.assertEqual(db.execute('SELECT owner_kind,owner_id FROM memory_source_holders').fetchall(),[('SUBJECT','cup')])
                    self.assertEqual(db.execute('SELECT holder_count,state FROM memory_sources').fetchall(),[(1,'RETAINED')])
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                again=await apply(anchor)
                if type(again) is not Committed:raise AssertionError(again)
                self.assertEqual(again.receipt,done.receipt)
            finally:await f.close()
            f=await SourceFixture(root,'OPEN_EXISTING').open()
            try:
                observed=await f.content.memory.verify_subject_origin('cup',cast(str,source['source_id']))
                self.assertEqual(observed,origin)
            finally:await f.close()
