"""Public small-profile assembly with native SQLite, files and loopback wire.

Fixture credentials and vectors have no supplier or semantic-quality meaning.
No business rows are initialized through SQL or test-only command declarations.
"""
import asyncio
from hashlib import sha256
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import sys
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.runtime.semantic_host import SemanticHost,SemanticHostResources
from companion_memory.runtime.semantic_authorization import SemanticActivationAuthority,BINDING
from companion_memory.cognition.fixed_memory import FixedReviewAuthority
from companion_memory.media.service import MediaResources
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.persistence.resources import ConnectionFactory
from companion_memory.persistence import DatabaseResources,Committed,Found,Receipt
from companion_memory.memory.formats import record
from companion_memory.information.management import HostIdentity
from companion_memory.information.index_worker import LocalIndexWorker
from tests.information.test_queries import query
from tests.information.publication_support import index_identity
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.semantic_records import identity,Record,number,string,isolate
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import CredentialResolver,CredentialLease,Available
from companion_memory.provider.values import Record as ModelRecord
from tests.semantic.configuration_support import candidate
from tests.semantic.fixed_support import material as fixed_material


def material():
    """Distinct synthetic documents exercise every approved fixture purpose slot."""
    from companion_memory.persistence.content_codec import decode_content
    from companion_memory.memory.formats import isolate_object
    values=[]
    for member in fixed_material():
        raw=decode_content(string(member['memory_json']).encode(),4096);assert type(raw) is dict
        raw['content']['body']='Fixed fixture fact number '+str(member['ordinal'])+'.'
        body=encode_content(isolate_object(raw,text_format=True),4096).decode()
        values.append(MappingProxyType(dict(member)|{'memory_json':body,'content_digest':sha256(encode_content((member['event_json'],body),8192)).hexdigest()}))
    return tuple(values)


def make_host(root:Path,port:int,*,connect:ConnectionFactory=sqlite3.connect,usage_only:bool=False) -> SemanticHost:
    """Bind only explicit fixture authority and the actual loopback adapter."""
    configuration,supplied=candidate(root,offline=False,usage_only=usage_only);members=material()
    claims={'instance_id':'instance','set_id':'fixed-set','entry_id':'entry','review_ref':'fixture-review','review_digest':'a'*64,
        'manifest_digest':sha256(encode_content(tuple(m['content_digest'] for m in members),8192)).hexdigest(),'reviewed_by':'fixture-supervisor'}
    review=FixedReviewAuthority(lambda value:dict(value)==claims).grant(claims)
    transport=ChatTransport.for_embedding(cast(ModelRecord,configuration.text.record('provider.embedding_transport')),
        CredentialResolver(lambda secret,revision,account:Available(CredentialLease(b'synthetic-loopback-only'))),
        'fixture_embedding',time.monotonic,loopback_port=port)
    return SemanticHost(configuration,SemanticHostResources(root,
        DatabaseResources('text-database',lambda db,path:(db,path)==('text-database',str(root/'database'/'runtime.sqlite3')),connect=connect),
        MediaResources('text-media',lambda media,db,path:(media,db,path)==('text-media','text-database',str(root/'media'))),
        'instance','configuration',cast(dict[str,tuple[str,...]|list[str]],supplied[6]),InitialSelfBinding('fixture-supervisor','self','Fixture','SYNTHETIC_FIXTURE'),review,transport))


async def opened(host:SemanticHost,mode:str) -> object:
    result:object=None
    for _ in range(10):
        result=await host.initialize(mode)
        if type(result) is Found:return result
        await asyncio.sleep(.01)
    return result


class SemanticHostTests(unittest.IsolatedAsyncioTestCase):
    document_count=1
    query_count=1
    async def test_public_initialization_fixed_embedding_and_original_reopen(self):
        requests=[];steps=[]
        from tests.semantic.public_evidence import operation_count,save
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']));requests.append(json.loads(raw))
                result={'id':'fixture-response','created':1,'model':'doubao-embedding-vision','object':'list',
                    'data':[{'index':0,'object':'embedding','embedding':[1.0]+[0.0]*1023}],
                    'usage':{'prompt_tokens':10,'total_tokens':10}}
                body=json.dumps(result,separators=(',',':')).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01));worker.start()
        try:
            with TemporaryDirectory() as directory:
                root=Path(directory).resolve();host=make_host(root,server.server_port)
                try:
                    result=await opened(host,'CREATE_NEW');self.assertIs(type(result),Found,result)
                    self.assertEqual(requests,[])
                    assert host.initial is not None and host.fixed is not None and host.stored is not None
                    initial=await host.initial.register_initial_self('initial-self','NO_PRESET','Synthetic fixture self.','SYNTHETIC_FIXTURE')
                    self.assertIs(type(initial),Committed,initial)
                    entry=await host.register_entry('entry-registration','entry','host','sample_platform','external-entry')
                    self.assertIs(type(entry),Committed,entry)
                    members=material();claims=host.resources.review.claims
                    config={'database_id':host.stored.database_id,'instance_id':'instance','snapshot_id':host.stored.snapshot_id}
                    fixed=host.fixed
                    def env(kind,key,payload):return fixed.envelope(kind,key,MappingProxyType(payload),1)
                    result=await host.fixed.begin(env('fixed_begin','begin',{'set_id':'fixed-set','config':config,
                        **{k:claims[k] for k in ('manifest_digest','review_ref','review_digest')}}),time.monotonic()+5)
                    self.assertIs(type(result),Committed,result)
                    for ordinal,member in enumerate(members):
                        result=await host.fixed.add_member(env('fixed_add_member','add:'+str(ordinal),{'set_id':'fixed-set','expected_revision':ordinal+1,**member}),time.monotonic()+5)
                        self.assertIs(type(result),Committed,result)
                    self.assertIs(type(await host.fixed.seal(env('fixed_seal','seal',{'set_id':'fixed-set','expected_revision':13}),time.monotonic()+5)),Committed)
                    for ordinal in range(self.document_count):
                        result=await host.fixed.establish_one(env('fixed_establish','establish:'+str(ordinal),{'set_id':'fixed-set','expected_revision':14+ordinal,
                            'ordinal':ordinal,'expected_member_revision':1}),time.monotonic()+5)
                        self.assertIs(type(result),Committed,result)
                    document_ids=tuple(identity('fixed-memory','instance','fixed-set',m['member_id']) for m in members)
                    activation={'format':'SEMANTIC_TRIAL_AUTH_V1','package_id':'fixture-package','set_id':'fixed-set',
                        'instance_id':'instance','database_id':host.stored.database_id,'config_snapshot_id':host.stored.snapshot_id,
                        'code_digest':'b'*64,'material_digest':'c'*64,'review_digest':'a'*64,'protocol_digest':'d'*64,'sdk_digest':'e'*64,
                        'decision_ref':'fixture-decision','execution':'CONTROLLED','account_evidence_ref':'fixture-account',
                        'input_evidence_ref':'fixture-input','server_evidence_ref':'fixture-server','document_ids':document_ids,
                        'queries':tuple({'query_id':'query:'+str(n),'text':'query '+str(n)} for n in range(6)),'expires_at':time.time_ns()//1000+3600000000}
                    from companion_memory.ingress.events import plain
                    expected=isolate(BINDING,activation,1048576)
                    grant=SemanticActivationAuthority(lambda v:v==expected).grant(activation)
                    host.bind_activation(grant);assert host.semantic is not None
                    self.assertIs(type(await host.semantic.resume('resume')),Receipt)
                    before=operation_count(host)
                    work_id=await host.semantic.prepare_document(document_ids[0],'document:0','fixture-partition');assert type(work_id) is str,work_id
                    completed=await host.semantic.run_work(work_id,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',document_ids[0]));assert type(completed) is MappingProxyType,completed
                    self.assertEqual(completed['state'],'APPLIED');self.assertFalse(completed['cleanup_pending']);self.assertEqual(len(requests),1)
                    steps.append(operation_count(host)-before);self.assertLessEqual(steps[-1],16)
                    work=await host.semantic.work(work_id);self.assertEqual(completed,work)
                    async def wait_dispatch():
                        assert host.semantic is not None
                        control=await host.semantic.control();earliest=number(control['last_cleanup_at'])+30000000
                        while time.time_ns()//1000<earliest:await asyncio.sleep(min(.5,(earliest-time.time_ns()//1000)/1000000))
                    for ordinal in range(1,self.document_count):
                        await wait_dispatch()
                        before=operation_count(host)
                        next_work=await host.semantic.prepare_document(document_ids[ordinal],'document:'+str(ordinal),'fixture-partition');assert type(next_work) is str,next_work
                        completed=await host.semantic.run_work(next_work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',document_ids[ordinal]));assert type(completed) is MappingProxyType,completed
                        self.assertEqual(completed['state'],'APPLIED');self.assertFalse(completed['cleanup_pending'])
                        steps.append(operation_count(host)-before);self.assertLessEqual(steps[-1],16)
                    publication=await host.semantic.owner.memory.rows.read('semantic_publication',host.semantic.owner.memory.root_id)
                    assert publication is not None
                    generation=await host.semantic.publish(identity('semantic-generation','fixture',1),number(publication['material_seq']));assert type(generation) is MappingProxyType,generation
                    self.assertEqual(generation['state'],'PUBLISHED')
                    native=await host.bind_management(index_identity());assert host.runtime is not None and host.retrieval is not None
                    begun=await native.execute('index_begin','lexical-begin',{'expected_generation':None});self.assertIs(type(begun),Committed,begun)
                    assert type(begun) is Committed
                    gid=string(record(record(record(begun.receipt.result)['facts'])['retrieval'])['object_id'])
                    index_worker=LocalIndexWorker(host.runtime,host.retrieval,native,'fixture-index')
                    for _ in range(10):
                        progress=await index_worker.run(gid)
                        if type(progress) is Found:break
                        self.assertIs(type(progress),Committed,progress)
                    lex,_=await host.retrieval.work_page(gid)
                    self.assertIs(type(await native.execute('index_publish','lexical-publish',{'generation_id':gid,'expected_revision':lex['revision']})),Committed)
                    port=await host.bind_query(HostIdentity('fixture-query','principal','host','entry',frozenset(('search_memory','resolve_recall')),(),time.monotonic()+300))
                    assert host.queries is not None and host.queries.semantic is not None
                    partition=host.queries.semantic.partition(port)
                    control=await host.semantic.control();earliest=number(control['last_cleanup_at'])+30000000
                    while time.time_ns()//1000<earliest:await asyncio.sleep(min(.5,(earliest-time.time_ns()//1000)/1000000))
                    before=operation_count(host)
                    query_work=await host.semantic.prepare_query('query 0','query:0',partition);assert type(query_work) is str,query_work
                    result=await host.semantic.run_work(query_work,slot_id=identity('semantic-slot','fixture-package','QUERY','query:0'));assert type(result) is MappingProxyType,result
                    self.assertEqual(result['state'],'APPLIED');self.assertFalse(result['cleanup_pending'])
                    steps.append(operation_count(host)-before);self.assertLessEqual(steps[-1],16)
                    for ordinal in range(1,self.query_count):
                        await wait_dispatch()
                        before=operation_count(host)
                        next_work=await host.semantic.prepare_query('query '+str(ordinal),'query:'+str(ordinal),partition);assert type(next_work) is str,next_work
                        completed=await host.semantic.run_work(next_work,slot_id=identity('semantic-slot','fixture-package','QUERY','query:'+str(ordinal)));assert type(completed) is MappingProxyType,completed
                        self.assertEqual(completed['state'],'APPLIED');self.assertFalse(completed['cleanup_pending'])
                        steps.append(operation_count(host)-before);self.assertLessEqual(steps[-1],16)
                    response=await port.search_memory(query('hybrid',query_text='query 0',retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False))
                    self.assertIs(type(response),Found,response);assert type(response) is Found
                    value=record(response.value);self.assertEqual(value['response_version'],2);self.assertEqual(value['actual_mode'],'HYBRID')
                    self.assertEqual(value['decision'],'MATCH');self.assertEqual(value['availability'],'COMPLETE')
                    self.assertEqual(len(requests),self.document_count+self.query_count)
                    for ordinal in range(1,self.query_count):
                        formal=await port.search_memory(query('formal:'+str(ordinal),query_text='query '+str(ordinal),retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False))
                        self.assertIs(type(formal),Found,formal);assert type(formal) is Found
                        self.assertEqual(record(formal.value)['availability'],'COMPLETE')
                    no_match=await port.search_memory(query('no-match',query_text='query 0',category='EVENT',retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False))
                    self.assertIs(type(no_match),Found,no_match);assert type(no_match) is Found
                    self.assertEqual(record(no_match.value)['decision'],'NO_MATCH');self.assertIsNone(record(no_match.value)['recall_id'])
                    from tests.semantic.public_retirement import verify
                    await verify(self,host,partition,self.document_count+self.query_count)
                    business=await host.bind_business(HostIdentity('http-query','principal','host','entry',frozenset(('search_memory',)),(),time.monotonic()+300))
                    assert host.http is not None
                    token=host.http.issue_test_session(business,time.monotonic()+300);address,http_port=await host.http.start()
                    reader,writer=await asyncio.open_connection(address,http_port)
                    body=json.dumps(query('http-search',query_text='query 0',retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False)).encode()
                    writer.write(('POST /api/host/memory/search HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+'\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body)
                    await writer.drain();raw=await reader.read();writer.close();await writer.wait_closed()
                    self.assertTrue(raw.startswith(b'HTTP/1.1 200'));self.assertEqual(json.loads(raw.split(b'\r\n\r\n',1)[1])['value']['response_version'],2)
                    self.assertEqual(len(requests),self.document_count+self.query_count)
                    self.assertTrue(await host.close())
                    child=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.semantic.host_reopen',str(root),str(server.server_port),work_id,
                        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    stdout,stderr=await child.communicate();self.assertEqual(child.returncode,0,(stdout,stderr))
                    self.assertEqual(json.loads(stdout)['sends'],0);self.assertEqual(len(requests),self.document_count+self.query_count)
                    save(root,requests,steps,self.document_count,self.query_count)
                finally:
                    for _ in range(20):
                        if await host.close():break
                        await asyncio.sleep(.01)
                    self.assertEqual(host.state,'CLOSED')
        finally:
            server.shutdown();worker.join();server.server_close()


class FullSemanticHostTests(SemanticHostTests):
    """The complete small package, using actual unshortened dispatch intervals."""
    document_count=12
    query_count=6
