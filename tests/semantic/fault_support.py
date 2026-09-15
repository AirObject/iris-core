"""Controlled loopback responses and real public index setup for fault tests."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import threading
from companion_memory.persistence import Committed,Found
from companion_memory.memory.formats import record
from companion_memory.persistence.semantic_records import string
from companion_memory.information.index_worker import LocalIndexWorker
from tests.information.publication_support import index_identity


@contextmanager
def server(before=lambda:None,*,unknown=False,axis=lambda body:0,usage=lambda:{"prompt_tokens":10,"total_tokens":10},model="doubao-embedding-vision"):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers['Content-Length'])))
            before()
            if unknown:self.close_connection=True;return
            raw=json.dumps({'id':'fixture-response','created':1,'model':model,'object':'list',
                'data':[{'index':0,'object':'embedding','embedding':[1.0 if n==axis(calls[-1]) else 0.0 for n in range(1024)]}],
                'usage':usage()},separators=(',',':')).encode()
            try:
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
        def log_message(self,format,*args):pass
    http=HTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=lambda:http.serve_forever(poll_interval=.01));worker.start()
    try:yield http.server_port,calls
    finally:http.shutdown();worker.join();http.server_close()


async def lexical(host,key='lexical',port=None):
    if port is None:port=await host.bind_management(index_identity())
    coordinator,_=await host.retrieval.query_generation()
    if coordinator['active_generation'] is not None:
        worker=LocalIndexWorker(host.runtime,host.retrieval,port,key)
        for _ in range(10):
            progress=await worker.run(string(coordinator['active_generation']))
            if type(progress) is Found:return port
            assert type(progress) is Committed,progress
        raise AssertionError('Original active index did not catch up.')
    result=await port.execute('index_begin',key+':begin',{'expected_generation':coordinator['active_generation']})
    assert type(result) is Committed,result
    generation=string(record(record(record(result.receipt.result)['facts'])['retrieval'])['object_id'])
    worker=LocalIndexWorker(host.runtime,host.retrieval,port,key)
    for _ in range(10):
        progress=await worker.run(generation)
        if type(progress) is Found:break
        assert type(progress) is Committed,progress
    original,_=await host.retrieval.work_page(generation)
    result=await port.execute('index_publish',key+':publish',{'generation_id':generation,'expected_revision':original['revision']})
    assert type(result) is Committed,result
    return port
