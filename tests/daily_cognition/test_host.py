"""Actual public daily host, original entry learning and local zero-send reopening."""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest
from companion_memory.runtime.daily_host import DailyCognitionHost,DailyHostResources
from companion_memory.persistence import DatabaseResources,Found,Committed
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.media.service import MediaResources
from companion_memory.cognition.fixed_memory import FixedReviewAuthority
from companion_memory.self_model.approved_import_evidence import verify_approved_persona
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import CredentialResolver,Available,CredentialLease
from companion_memory.provider.values import Record
from companion_memory.memory.formats import record
from companion_memory.persistence.content_codec import encode_content
from tests.runtime.configuration_support import event
from tests.semantic.test_semantic_host import material
from .configuration_support import candidate
from .test_reasoning import responses

def make_host(root:Path,port:int,credentials:list,*,configuration_input=None,entry_scope=None,scope_subjects=('self',),import_persona=True):
    if configuration_input is None:configuration,supplied=candidate(root)
    else:
        from companion_memory.configuration.daily_resolution import resolve_daily_configuration,DailyConfigurationOk
        supplied=configuration_input;parsed=resolve_daily_configuration(*supplied)
        if type(parsed) is not DailyConfigurationOk:raise AssertionError(parsed)
        configuration=parsed.value
    members=material()
    claims={'instance_id':'instance','set_id':'fixed-set','entry_id':'entry','review_ref':'fixture-review','review_digest':'a'*64,
        'manifest_digest':sha256(encode_content(tuple(m['content_digest'] for m in members),8192)).hexdigest(),'reviewed_by':'fixture-supervisor'}
    review=FixedReviewAuthority(lambda value:dict(value)==claims).grant(claims)
    def resolve(*args):
        lease=CredentialLease(b'synthetic-loopback-only');credentials.append(lease);return Available(lease)
    resolver=CredentialResolver(resolve)
    transports={cast(str,r['role']):ChatTransport.controlled_daily_loopback(r,resolver,time.monotonic,port) for r in cast(tuple[Record,...],configuration.text.record('provider.transport')['roles'])}
    embedding=ChatTransport.for_embedding(cast(Record,configuration.text.record('provider.embedding_transport')),resolver,'fixture_embedding',time.monotonic,loopback_port=port)
    directory=Path(__file__).parent/'fixtures'/'approved_persona'
    evidence=verify_approved_persona((directory/'review.md').read_bytes(),(directory/'publication.json').read_bytes(),(directory/'reconciliation.json').read_bytes())
    host=DailyCognitionHost(configuration,DailyHostResources(root,
        DatabaseResources('daily-host',lambda db,path:(db,path)==('daily-host',str(root/'database'/'runtime.sqlite3'))),
        MediaResources('daily-media',lambda media,db,path:(media,db,path)==('daily-media','daily-host',str(root/'media'))),
        'instance','configuration',cast(dict,supplied[6]),InitialSelfBinding('fixture-supervisor','self','Iris','SYNTHETIC_FIXTURE'),evidence if import_persona else None,review,embedding,transports,lambda key:True))
    host.configure_entry('entry','partition',scope_subjects,({'kind':'REAL','context_id':None},),**(entry_scope or {}))
    return host

class DailyHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_image_and_learning_share_one_host_and_complete_processing_cleanup(self):
        from io import BytesIO
        from PIL import Image
        from companion_memory.ingress.events import event_identity
        from companion_memory.ingress.media_events import isolate_media_event
        from companion_memory.media.service import identity
        image=BytesIO();Image.new('RGB',(48,32),(20,70,180)).save(image,format='PNG');raw_image=image.getvalue()
        outputs=({'schema_version':1,'text':'蓝色矩形。'},{'schema_version':1,'kind':'FINAL','actions':[]})
        credentials=[]
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);host=make_host(root,port,credentials)
            try:
                opened=await host.initialize('CREATE_NEW');self.assertIs(type(opened),Found,opened)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry');upload=host.media.bind_upload('entry')
                begun=await upload.begin_upload('image','IMAGE');self.assertIs(type(begun),Committed,begun)
                if type(begun) is not Committed:raise AssertionError(begun)
                uid=cast(str,record(begun.receipt.result)['upload_id'])
                appended=await upload.append_upload(uid,0,raw_image)
                finished=await upload.finish_upload(uid);self.assertIs(type(finished),Committed,(appended,finished))
                for n in range(3):
                    value=event('event-'+str(n),'看看这张合成色块。');value['event_version']=2
                    if n==0:
                        mid=event_identity(('instance','host','entry'),isolate_media_event(value,8192,occurrence_limit=2,text_limit=512))[0]
                        value['media']=[{'reference_id':uid,'occurrence_id':identity('occurrence',mid,0),'modality':'IMAGE','interpretation':None}]
                    accepted=await entry.accept_event('accept-'+str(n),value);self.assertIs(type(accepted),Committed,accepted)
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                learned=await entry.run_learning('learn')
                self.assertIs(type(learned),Committed,learned);self.assertEqual(len(requests),2);self.assertFalse(failures)
                first=json.loads(requests[0]);second=json.loads(requests[1]);import base64
                self.assertEqual(base64.b64decode(first['messages'][1]['content'][1]['image_url']['url'].split(',',1)[1]),raw_image)
                material=json.loads(second['messages'][1]['content'])
                versions=[json.loads(v) for member in material['members'] for v in member['interpretations']]
                self.assertEqual([v['text'] for v in versions],['蓝色矩形。'])
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],2)
                    self.assertEqual(db.execute("SELECT count(*) FROM media_references WHERE owner_kind='PROCESSING'").fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT count(*) FROM ingress_payload_holders WHERE owner_kind='PROCESSING'").fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_embedding_handoff_leaf').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'SUCCEEDED')
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                repeated=await entry.run_learning('learn');self.assertIs(type(repeated),Committed,repeated);self.assertEqual(len(requests),2)
            finally:self.assertTrue(await host.close())
            count=len(credentials);reopened=make_host(root,port,credentials)
            try:
                result=await reopened.initialize('OPEN_EXISTING');self.assertIs(type(result),Found,result)
                self.assertEqual(reopened.state,'READY');self.assertFalse(reopened.scheduling())
                self.assertEqual(len(requests),2);self.assertEqual(len(credentials),count)
            finally:self.assertTrue(await reopened.close())

    async def test_public_entry_formal_memory_exact_original_key_and_reopen_without_sends(self):
        class Outputs:
            requests:list[bytes]
            def __getitem__(self,index):
                original=json.loads(json.loads(self.requests[index])['messages'][1]['content']);source=original['source']
                member=next(m for m in source['ordered_members'] if m['role']=='T')
                return {'schema_version':1,'kind':'FINAL','actions':[{'action':'CREATE_MEMORY','local_ref':0,
                    'target_anchors':[{'message_id':member['message_id'],'part':'EVENT','item_index':None,'start_utf8':None,'end_utf8':None,'occurrence_id':None,'interpretation_id':None}],
                    'auxiliary_refs':[],'basis_refs':[],'category':'FACT','body':'杯子在展板旁。','subject_ids':[{'existing_id':'self'}],
                    'speaker_subject_id':None,'stance':'ASSERTED','world_scope':{'kind':'REAL','context_id':None},'occurred_range':None,'applicable_range':None,'belief':80,'belief_reason':'原目标事件直接陈述。'}]}
        outputs=Outputs();credentials=[]
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            outputs.requests=requests;root=Path(directory);host=make_host(root,port,credentials)
            try:
                opened=await host.initialize('CREATE_NEW');self.assertIs(type(opened),Found,opened)
                self.assertEqual(host.state,'READY');self.assertFalse(requests);self.assertFalse(credentials)
                assert host.runtime is not None
                self.assertIs(host.runtime.provider,host.provider);self.assertIs(host.runtime.embedding,host.provider)
                registered=await host.register_entry('entry','entry','host','sample_platform','conversation');self.assertIs(type(registered),Committed,registered)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'杯子在展板旁。');value['event_version']=2
                    accepted=await entry.accept_event('accept-'+str(n),value);self.assertIs(type(accepted),Committed,accepted)
                paused=await entry.run_learning('learn');self.assertIs(type(paused),Found,paused);self.assertFalse(requests)
                resumed=await host.resume_learning('resume');self.assertIs(type(resumed),Committed,resumed)
                learned=await entry.run_learning('learn');self.assertIs(type(learned),Committed,learned)
                repeated=await entry.run_learning('learn');self.assertIs(type(repeated),Committed,repeated)
                if type(learned) is Committed and type(repeated) is Committed:self.assertEqual(learned.receipt,repeated.receipt)
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'SUCCEEDED')
                    started=db.execute('SELECT started_at_us FROM runtime_content_preparations').fetchone()[0]
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.deadline_at_us') FROM cognition_reasoning_runs").fetchone()[0],started+1200000000)
            finally:self.assertTrue(await host.close())
            count=len(credentials);reopened=make_host(root,port,credentials)
            try:
                result=await reopened.initialize('OPEN_EXISTING');self.assertIs(type(result),Found,result)
                self.assertEqual(reopened.state,'READY');self.assertFalse(reopened.scheduling())
                assert reopened.network is not None
                self.assertEqual(len(requests),1);self.assertEqual(len(credentials),count)
                self.assertTrue(reopened.network.observation().paused)
            finally:self.assertTrue(await reopened.close())
