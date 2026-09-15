"""Actual public work races and independent remote-result/metering outcomes."""
from types import MappingProxyType
import asyncio
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from companion_memory.persistence import Committed
from companion_memory.memory.formats import record
from companion_memory.persistence.semantic_records import identity,number
from tests.semantic.public_support import establish,activate


class PublicFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_result_preserves_new_delete_gap_and_complete_paid_artifact(self):
        arrived=threading.Event();release=threading.Event();calls=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(self.rfile.read(int(self.headers['Content-Length'])));arrived.set()
                if not release.wait(10):self.close_connection=True;return
                body=json.dumps({'id':'fixture-response','created':1,'model':'doubao-embedding-vision','object':'list',
                    'data':[{'index':0,'object':'embedding','embedding':[1.0]+[0.0]*1023}],
                    'usage':{'prompt_tokens':10,'total_tokens':10}},separators=(',',':')).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01));worker.start()
        try:
            with TemporaryDirectory() as directory:
                host,oid=await establish(Path(directory).resolve(),server.server_port)
                try:
                    activate(host);assert host.semantic is not None and host.runtime is not None and host.embedding is not None
                    await host.semantic.resume('resume')
                    work_id=await host.semantic.prepare_document(oid,'original-document','partition');assert type(work_id) is str,work_id
                    running=asyncio.create_task(host.semantic.run_work(work_id,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid)))
                    self.assertTrue(await asyncio.to_thread(arrived.wait,5))
                    deleted=await host.runtime.maintenance.bind((oid,)).delete_object('delete-during-wire',oid,1)
                    self.assertIs(type(deleted),Committed,deleted);assert type(deleted) is Committed
                    release.set();late=await running;assert type(late) is MappingProxyType,late
                    self.assertEqual(late['state'],'SUPERSEDED');self.assertIsNotNone(late['artifact_id']);self.assertFalse(late['cleanup_pending'])
                    current,gap=await host.semantic.owner.memory.semantic_current(oid);self.assertIsNone(current);assert gap is not None
                    self.assertEqual(gap['object_revision'],2);self.assertEqual(gap['action'],'DELETE')
                    local=await host.semantic.prepare_delete(oid,2,number(gap['latest_change_seq']),host.embedding.reference(deleted.receipt));assert type(local) is str,local
                    result=await host.semantic.run_work(local);assert type(result) is MappingProxyType,result;self.assertEqual(result['state'],'LOCAL_APPLIED')
                    self.assertEqual(len(calls),1)
                finally:release.set();self.assertTrue(await host.close())
        finally:release.set();server.shutdown();worker.join();server.server_close()

    async def test_known_invalid_result_keeps_known_usage_and_unknown_keeps_reservation(self):
        for unknown in (False,True):
            with self.subTest(unknown=unknown):
                calls=[]
                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        calls.append(self.rfile.read(int(self.headers['Content-Length'])))
                        if unknown:self.close_connection=True;return
                        body=json.dumps({'id':'fixture-response','created':1,'model':'doubao-embedding-vision','object':'list',
                            'data':[{'index':0,'object':'embedding','embedding':[0.0]*1024}],
                            'usage':{'prompt_tokens':10,'total_tokens':10}},separators=(',',':')).encode()
                        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                    def log_message(self,format,*args):pass
                server=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01));worker.start()
                try:
                    with TemporaryDirectory() as directory:
                        host,oid=await establish(Path(directory).resolve(),server.server_port)
                        try:
                            activate(host);assert host.semantic is not None and host.embedding is not None
                            await host.semantic.resume('resume');work=await host.semantic.prepare_document(oid,'original-document','partition');assert type(work) is str,work
                            result=await host.semantic.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid));assert type(result) is MappingProxyType,result
                            self.assertEqual(result['state'],'REMOTE_UNKNOWN' if unknown else 'KNOWN_FAILED')
                            self.assertFalse(result['cleanup_pending']);self.assertEqual(len(calls),1)
                            budget=await host.embedding.ledger.get('budget_windows',identity('embedding-budget','fixture_embedding','fixture_window'));assert budget is not None
                            self.assertEqual(budget['known_subtotal_atoms'],0 if unknown else 7)
                            held=budget['held_atoms'];assert type(held) is int
                            self.assertGreater(held,0) if unknown else self.assertEqual(held,0)
                            replay=await host.semantic.run_work(work);assert type(replay) is MappingProxyType,replay;self.assertEqual(replay,result);self.assertEqual(len(calls),1)
                        finally:self.assertTrue(await host.close())
                finally:server.shutdown();worker.join();server.server_close()
