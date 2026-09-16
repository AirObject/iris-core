"""Actual rejected image bytes release only unsent work and preserve input."""
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from PIL import Image
from companion_memory.persistence import Committed,Found
from companion_memory.memory.formats import record
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import identity,VolatileProgress
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses

class ImageRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversize_original_is_unsent_other_entry_finishes_and_reopen_is_local(self):
        image=BytesIO();Image.new('RGB',(2049,1),(12,34,56)).save(image,format='PNG')
        credentials=[]
        with TemporaryDirectory() as directory,responses(({'schema_version':1,'kind':'FINAL','actions':[]},)) as (port,requests,failures):
            root=Path(directory);host=make_host(root,port,credentials)
            host.configure_entry('entry-b','partition',('self',),({'kind':'REAL','context_id':None},))
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                for name in ('entry','entry-b'):
                    self.assertIs(type(await host.register_entry(name,name,'host','sample_platform',name)),Committed)
                upload=host.media.bind_upload('entry')
                begun=await upload.begin_upload('oversize','IMAGE');self.assertIs(type(begun),Committed,begun)
                if type(begun) is not Committed:raise AssertionError(begun)
                uid=cast(str,record(begun.receipt.result)['upload_id'])
                self.assertIs(type(await upload.append_upload(uid,0,image.getvalue())),VolatileProgress)
                self.assertIs(type(await upload.finish_upload(uid)),Committed)
                for name in ('entry','entry-b'):
                    entry=host.bind_entry(name)
                    for n in range(3):
                        value=event(name+'-'+str(n),'合成图片与文字。');value['event_version']=2
                        if name=='entry' and n==0:
                            mid=event_identity(('instance','host',name),isolate_media_event(value,8192,occurrence_limit=2,text_limit=512))[0]
                            value['media']=[{'reference_id':uid,'occurrence_id':identity('occurrence',mid,0),'modality':'IMAGE','interpretation':None}]
                        self.assertIs(type(await entry.accept_event('accept-'+str(n),value)),Committed)
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                if host.dispatch is None:raise AssertionError()
                await host.dispatch.wait_actual()
                self.assertIsNone(host.dispatch.last_failure)
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                self.assertEqual(json.loads(json.loads(requests[0])['messages'][1]['content'])['source']['entry_id'],'entry-b')
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT phase,request_association_state,provider_request_id FROM media_work').fetchone(),('PARKED','INPUT_REJECTED',None))
                    self.assertEqual(db.execute('SELECT count(*) FROM media_interpretations').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT count(*) FROM media_references WHERE owner_kind='PROCESSING'").fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT count(*) FROM ingress_payload_holders WHERE owner_kind IN ('PROCESSING','PREPARATION')").fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT phase FROM runtime_content_preparations WHERE entry_id='entry'").fetchone()[0],'INVALIDATED')
                    self.assertEqual(db.execute("SELECT count(*) FROM runtime_content_batches WHERE entry_id='entry'").fetchone()[0],0)
            finally:self.assertTrue(await host.close())
            count=len(credentials);reopened=make_host(root,port,credentials)
            reopened.configure_entry('entry-b','partition',('self',),({'kind':'REAL','context_id':None},))
            try:
                opened=await reopened.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                self.assertEqual(len(credentials),count);self.assertEqual(len(requests),1)
                self.assertFalse(reopened.scheduling())
            finally:self.assertTrue(await reopened.close())
