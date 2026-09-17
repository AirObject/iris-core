"""One public daily host learns, embeds, publishes and serves native HTTP queries."""
import asyncio
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from types import MappingProxyType
import unittest
from companion_memory.persistence import Found,Committed,Receipt
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.semantic_records import identity,isolate
from companion_memory.runtime.semantic_authorization import SemanticActivationAuthority,DAILY_BINDING
from companion_memory.retrieval.semantic_material import render_document
from companion_memory.information.management import HostIdentity
from companion_memory.information.index_worker import LocalIndexWorker
from companion_memory.memory.formats import record
from tests.information.publication_support import index_identity
from tests.information.test_queries import query
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_protocol import response

@contextmanager
def mixed_responses():
    requests=[];failures=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                raw=self.rfile.read(int(self.headers['Content-Length']));request=json.loads(raw);requests.append(request)
                if 'messages' in request:
                    context=json.loads(request['messages'][1]['content']);member=next(m for m in context['source']['ordered_members'] if m['role']=='T')
                    common={'target_anchors':[{'message_id':member['message_id'],'part':'EVENT','item_index':None,'start_utf8':None,'end_utf8':None,'occurrence_id':None,'interpretation_id':None}],'auxiliary_refs':[],'basis_refs':[]}
                    value=response({'schema_version':1,'kind':'FINAL','actions':[
                        {'action':'REGISTER_SUBJECT','local_ref':0,**common,'subject_kind':'THING','label':'合成展板','platform_id':None,'external_subject_id':None},
                        {'action':'CREATE_MEMORY','local_ref':1,**common,'category':'FACT','body':'合成展板是红色矩形。','subject_ids':[{'local_ref':0}],
                        'speaker_subject_id':None,'stance':'ASSERTED','world_scope':{'kind':'REAL','context_id':None},'occurred_range':None,'applicable_range':None,'belief':80,'belief_reason':'原目标事件直接陈述。'},
                        {'action':'CREATE_GOAL','local_ref':2,**common,'content':'核对合成展板颜色。','subject_refs':[{'local_ref':0}],'world_scope':'REAL','deadline':None,
                        'reminder_lead_seconds':None,'route_id':None,'basis_action_refs':[1]}]})
                else:value=json.dumps({'id':'fixture-response','created':1,'model':'doubao-embedding-vision','object':'list',
                    'data':[{'index':0,'object':'embedding','embedding':[1.0]+[0.0]*1023}],
                    'usage':{'prompt_tokens':10,'total_tokens':10}},separators=(',',':')).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
            except (KeyError,ValueError,OSError) as failure:failures.append(type(failure).__name__)
        def log_message(self,format,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01));worker.start()
    try:yield server.server_port,requests,failures
    finally:server.shutdown();server.server_close();worker.join(5)

class DailySemanticHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_learning_document_query_publication_http_and_zero_send_reopen(self):
        with TemporaryDirectory() as directory,mixed_responses() as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                from .trial_support import controlled_activation
                trial_grant=controlled_activation(host);trial=host.bind_trial_activation(trial_grant)
                self.assertFalse(trial.active);self.assertFalse(trial.entries);self.assertFalse(requests)
                await trial.resume()
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                from companion_memory.ingress.events import event_identity
                from companion_memory.ingress.media_events import isolate_media_event
                from companion_memory.media.service import identity as media_identity,VolatileProgress
                from .materials import png_bytes
                upload=host.media.bind_upload('entry');begun=await upload.begin_upload('image','IMAGE')
                if type(begun) is not Committed:raise AssertionError(begun)
                uid=record(begun.receipt.result)['upload_id']
                self.assertIs(type(await upload.append_upload(uid,0,png_bytes('A'))),VolatileProgress)
                self.assertIs(type(await upload.finish_upload(uid)),Committed)
                for ordinal in range(3):
                    value=event('event-'+str(ordinal),'合成展板是红色矩形，需要核对颜色。');value['event_version']=2
                    if ordinal==0:
                        mid=event_identity(('instance','host','entry'),isolate_media_event(value,8192,occurrence_limit=2,text_limit=512))[0]
                        value['media']=[{'reference_id':uid,'occurrence_id':media_identity('occurrence',mid,0),'modality':'IMAGE',
                            'interpretation':{'status':'COMPLETE','text':'左红矩形右蓝圆。','source_ref':'synthetic-external-observer','coverage':'COMPLETE'}}]
                    self.assertIs(type(await entry.accept_event('accept-'+str(ordinal),value)),Committed)
                self.assertIs(type(await host.resume_learning('learn-resume')),Committed)
                learned=await entry.run_learning('learn');self.assertIs(type(learned),Committed,learned)
                if host.semantic is None or host.stored is None or host.network is None or host.runtime is None or host.retrieval is None or host.queries is None or host.queries.semantic is None:raise AssertionError('Native owners missing')
                semantic=host.semantic
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    oid=db.execute('SELECT object_id FROM memory_objects').fetchone()[0]
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_semantic_gap').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subject_origins').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_goal').fetchone()[0],1)
                    self.assertEqual(db.execute("SELECT count(*) FROM memory_source_holders WHERE owner_kind='SUBJECT'").fetchone()[0],1)
                current,gap=await semantic.owner.memory.semantic_current(oid)
                if current is None or gap is None:raise AssertionError('Actual learned memory missing')
                query_port=await host.bind_query(HostIdentity('query','principal','host','entry',frozenset(('search_memory','resolve_recall')),(),time.monotonic()+300))
                partition=host.queries.semantic.partition(query_port)
                documents=({'object_id':oid,'revision':current['revision'],'material_digest':sha256(render_document(current)).hexdigest(),'partition_id':partition},)
                queries=({'query_id':'query-real','text':'合成展板有哪些颜色和形状？','partition_id':partition},
                    {'query_id':'query-fiction','text':'雾港故事里的展板有什么图形？','partition_id':'unused-fiction-partition'})
                actual=sha256(Path(__file__).read_bytes()).hexdigest()
                assert host.resources.review is not None
                activation={'format':'DAILY_SEMANTIC_AUTH_V1','package_id':'daily-fixture','set_id':'fixed-set','instance_id':'instance',
                    'database_id':host.stored.database_id,'config_snapshot_id':host.stored.snapshot_id,'code_digest':actual,
                    'material_digest':sha256(json.dumps((documents,queries),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'review_digest':host.resources.review.claims['review_digest'],
                    'protocol_digest':actual,'sdk_digest':actual,'decision_ref':'controlled-fixture','execution':'CONTROLLED',
                    'account_evidence_ref':'synthetic-loopback','input_evidence_ref':'native-learning-result','server_evidence_ref':'controlled-http',
                    'document_ids':(oid,),'documents':documents,'queries':queries,'expires_at':time.time_ns()//1000+3600000000,
                    'verification_mode':'USER_ALLOCATED_USAGE_TRIAL'}
                expected=isolate(DAILY_BINDING,activation,1048576)
                host.bind_activation(SemanticActivationAuthority(lambda v:v==expected).grant(activation))
                self.assertIs(type(await semantic.resume('semantic-resume')),Receipt)
                await host.network.wait_quiet(time.monotonic()+60)
                wid=await semantic.prepare_document(oid,'document',partition)
                if type(wid) is not str:raise AssertionError(wid)
                result=await semantic.run_work(wid,slot_id=identity('semantic-slot','daily-fixture','DOCUMENT',oid))
                if type(result) is not MappingProxyType:raise AssertionError(result)
                self.assertEqual(result['state'],'APPLIED');self.assertFalse(result['cleanup_pending'])
                publication=await semantic.owner.memory.rows.read('semantic_publication',semantic.owner.memory.root_id)
                if publication is None:raise AssertionError('Publication watermark missing')
                seq=publication['material_seq']
                if type(seq) is not int:raise AssertionError(seq)
                published=await semantic.publish(identity('semantic-generation','daily',1),seq)
                if type(published) is not MappingProxyType:raise AssertionError(published)
                self.assertEqual(published['state'],'PUBLISHED')
                native=await host.bind_management(index_identity())
                begun=await native.execute('index_begin','index-begin',{'expected_generation':None})
                if type(begun) is not Committed:raise AssertionError(begun)
                gid=record(record(record(begun.receipt.result)['facts'])['retrieval'])['object_id']
                if type(gid) is not str:raise AssertionError(gid)
                worker=LocalIndexWorker(host.runtime,host.retrieval,native,'daily-index')
                for _ in range(10):
                    if type(await worker.run(gid)) is Found:break
                lexical,_=await host.retrieval.work_page(gid)
                self.assertIs(type(await native.execute('index_publish','index-publish',{'generation_id':gid,'expected_revision':lexical['revision']})),Committed)
                await host.network.wait_quiet(time.monotonic()+60)
                qid=await semantic.prepare_query(queries[0]['text'],'query-real',partition)
                if type(qid) is not str:raise AssertionError(qid)
                result=await semantic.run_work(qid,slot_id=identity('semantic-slot','daily-fixture','QUERY','query-real'))
                if type(result) is not MappingProxyType:raise AssertionError(result)
                self.assertEqual(result['state'],'APPLIED');self.assertFalse(result['cleanup_pending'])
                searched=await query_port.search_memory(query('search',query_text=queries[0]['text'],retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False))
                if type(searched) is not Found:raise AssertionError(searched)
                self.assertEqual(searched.value['response_version'],2);self.assertEqual(searched.value['actual_mode'],'HYBRID')
                self.assertEqual(searched.value['decision'],'MATCH');self.assertEqual(len(requests),3)
                business=await host.bind_business(HostIdentity('http','principal','host','entry',frozenset(('search_memory',)),(),time.monotonic()+60))
                if host.http is None:raise AssertionError('HTTP missing')
                token=host.http.issue_test_session(business,time.monotonic()+60);address,http_port=await host.http.start()
                reader,writer=await asyncio.open_connection(address,http_port)
                body=json.dumps(query('http-search',query_text=queries[0]['text'],retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False)).encode()
                writer.write(('POST /api/host/memory/search HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+'\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body)
                await writer.drain();raw=await reader.read();writer.close();await writer.wait_closed()
                self.assertTrue(raw.startswith(b'HTTP/1.1 200'),raw[:80]);self.assertEqual(json.loads(raw.split(b'\r\n\r\n',1)[1])['value']['response_version'],2)
                self.assertEqual(len(requests),3);self.assertFalse(failures)
                self.assertEqual(json.loads(requests[0]['messages'][1]['content'])['request_constraints'],{'memory_target_limit':2})
                repeated=await semantic.run_work(wid)
                if type(repeated) is not MappingProxyType:raise AssertionError(repeated)
                self.assertEqual(repeated['state'],'APPLIED')
                await trial.verify_native()
                self.assertEqual(set(trial.entries),{'learning:0','embedding_document:4','embedding_query:0'})
                self.assertTrue(all(e['state']=='REGISTERED' for e in trial.entries.values()))
                journal=(root/'daily-trial-authorization.jsonl').read_bytes()
            finally:self.assertTrue(await host.close())
            resolutions=len(credentials);reopened=make_host(root,port,credentials)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')),Found)
                reopened_trial=reopened.bind_trial_activation(trial_grant)
                self.assertFalse(reopened_trial.active);await reopened_trial.verify_native()
                self.assertEqual((root/'daily-trial-authorization.jsonl').read_bytes(),journal)
                self.assertFalse(reopened.scheduling());self.assertEqual(len(requests),3);self.assertEqual(len(credentials),resolutions)
                if reopened.semantic is None:raise AssertionError('Semantic owner missing')
                self.assertEqual((await reopened.semantic.work(wid))['state'],'APPLIED')
                self.assertEqual((await reopened.semantic.work(qid))['state'],'APPLIED')
            finally:self.assertTrue(await reopened.close())
