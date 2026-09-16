"""Complete public business material at simultaneous count and text boundaries."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found,Value
from companion_memory.ingress.events import canonical_event,event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import identity
from companion_memory.memory.formats import record
from companion_memory.provider.values import freeze
from tests.runtime.configuration_support import event
from .materials import png_bytes
from .test_host import make_host
from .test_reasoning import responses

def full_event(number,upload_ids):
    value=event('event-'+str(number),'正文');value['event_version']=2
    mid=event_identity(('instance','host','entry'),isolate_media_event(value,8192,occurrence_limit=2,text_limit=512))[0]
    value['media']=[{'reference_id':upload_ids[ordinal],'occurrence_id':identity('occurrence',mid,ordinal),'modality':'IMAGE',
        'interpretation':{'status':'COMPLETE','text':'外部合成观察：'+'x'*(512-len('外部合成观察：'.encode())),'source_ref':'s'*128,'coverage':'COMPLETE'}} for ordinal in range(2)]
    current=canonical_event(cast(Value,freeze(value,16384,owned=True)))
    remaining=8192-len(current)
    value['body']=cast(str,value['body'])+'\\'*(remaining//2)+'x'*(remaining%2)
    frozen=isolate_media_event(value,8192,occurrence_limit=2,text_limit=512)
    if len(canonical_event(frozen))!=8192:raise AssertionError('Complete maximum event differs')
    return value

def common(request,local):
    source=json.loads(request['messages'][1]['content'])['source']
    targets=[m for m in source['ordered_members'] if m['role']=='T']
    return {'local_ref':local,'target_anchors':[{'message_id':m['message_id'],'part':'EVENT','item_index':None,'start_utf8':None,'end_utf8':None,
        'occurrence_id':None,'interpretation_id':None} for m in targets],'auxiliary_refs':[],'basis_refs':[]}

def subjects(start,count):
    def output(request):
        return {'schema_version':1,'kind':'FINAL','actions':[{'action':'REGISTER_SUBJECT',**common(request,n),'subject_kind':'THING',
            'label':('subject-'+str(start+n)+'-').ljust(256,'x'),'platform_id':None,'external_subject_id':None} for n in range(count)]}
    return output

def objects_and_goals(request):
    actions=[]
    for n in range(4):
        actions.append({'action':'CREATE_MEMORY',**common(request,n),'category':'FACT','body':('合成展板'+str(n)).ljust(1016,'x'),
            'subject_ids':[{'existing_id':'self'}],'speaker_subject_id':None,'stance':'ASSERTED','world_scope':{'kind':'REAL','context_id':None},
            'occurred_range':None,'applicable_range':None,'belief':80,'belief_reason':'b'*256})
    for n in range(4):
        actions.append({'action':'CREATE_GOAL',**common(request,n+4),'content':('检查'+str(n)).ljust(2044,'g'),'subject_refs':[{'existing_id':'self'}],
            'world_scope':'REAL','deadline':None,'reminder_lead_seconds':None,'route_id':None,'basis_action_refs':[n]})
    return {'schema_version':1,'kind':'FINAL','actions':actions}

class CapacityHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_events_images_roster_related_objects_and_tool_loop_use_actual_storage(self):
        outputs:list[object]=[subjects(0,8),subjects(8,7),objects_and_goals]
        with TemporaryDirectory() as directory,responses(outputs) as (http_port,requests,failures):
            root=Path(directory);credentials=[];roster=('self',);related=();sequence=0;host=None
            try:
                for stage in range(4):
                    host=make_host(root,http_port,credentials,scope_subjects=roster,entry_scope={'related':related})
                    opened=await host.initialize('CREATE_NEW' if stage==0 else 'OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                    if stage==0:
                        self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                    if stage==3:
                        outputs.extend((
                            {'schema_version':1,'kind':'TOOL','tools':[{'name':'read_memories','arguments':{'refs':[{'object_id':oid,'expected_revision':1} for oid in related]}},
                                {'name':'read_subjects','arguments':{'subject_ids':list(roster[1:5])}}]},
                            {'schema_version':1,'kind':'TOOL','tools':[{'name':'list_goals','arguments':{'world_scope':'REAL','limit':4}},
                                {'name':'search_memories','arguments':{'query':'q'*512,'world_scope':{'kind':'REAL','context_id':None},'limit':4}}]},
                            {'schema_version':1,'kind':'FINAL','actions':[]}))
                    entry=host.bind_entry('entry')
                    upload=host.media.bind_upload('entry')
                    for _ in range(3 if stage==0 else 2):
                        upload_ids=[]
                        for occurrence in range(2):
                            begun=await upload.begin_upload('image-'+str(sequence)+'-'+str(occurrence),'IMAGE')
                            if type(begun) is not Committed:raise AssertionError(begun)
                            uid=record(begun.receipt.result)['upload_id'];upload_ids.append(uid)
                            await upload.append_upload(uid,0,png_bytes('A'))
                            self.assertIs(type(await upload.finish_upload(uid)),Committed)
                        value=full_event(sequence,upload_ids)
                        accepted=await entry.accept_event('accept-'+str(sequence),value)
                        self.assertIs(type(accepted),Committed,accepted)
                        sequence+=1
                    self.assertIs(type(await host.resume_learning('resume-'+str(stage))),Committed)
                    learned=await entry.run_learning('learn-'+str(stage));self.assertIs(type(learned),Committed,learned)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT count(*) FROM media_references WHERE owner_kind='PROCESSING'").fetchone()[0],0)
                        if stage==1:
                            roster=tuple(row[0] for row in db.execute('SELECT subject_id FROM memory_subjects ORDER BY subject_id'))
                            self.assertEqual(len(roster),16)
                        if stage==2:
                            related=tuple(row[0] for row in db.execute('SELECT object_id FROM memory_objects ORDER BY object_id'))
                            self.assertEqual(len(related),4);self.assertEqual(db.execute('SELECT count(*) FROM goals_goal').fetchone()[0],4)
                        if stage==3:
                            materials=[json.loads(json.loads(raw)['messages'][1]['content']) for raw in requests[-3:]]
                            self.assertEqual(len(materials[0]['members']),4);self.assertEqual(len(materials[0]['subjects']),16)
                            self.assertEqual(len(materials[0]['related']),4);self.assertEqual(sum(len(m['interpretations']) for m in materials[0]['members']),8)
                            self.assertTrue(all(len(m['payload'].encode())==8192 for m in materials[0]['members']))
                            self.assertEqual(db.execute('SELECT count(*) FROM cognition_reasoning_tools').fetchone()[0],4)
                            roots=[json.loads(row[0]) for row in db.execute('SELECT body FROM cognition_learning_contexts')]
                            self.assertTrue(all(r['state']=='RELEASED' and r['byte_count']<=262144 and len(r['leaf_refs'])<=48 for r in roots))
                            print(json.dumps({'acceptance':'complete_business_capacity','events':4,'event_bytes':8192,'interpretations':8,'subjects':16,'related_objects':4,
                                'tools':4,'generation_requests':len(requests),'maximum_material_bytes':max(r['byte_count'] for r in roots),'maximum_material_leaves':max(len(r['leaf_refs']) for r in roots),
                                'wire_bytes':[len(raw) for raw in requests],'provider_attempts':db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0]},sort_keys=True))
                    self.assertTrue(await host.close());host=None
                self.assertEqual(len(requests),6);self.assertFalse(failures)
            finally:
                if host is not None:self.assertTrue(await host.close())
