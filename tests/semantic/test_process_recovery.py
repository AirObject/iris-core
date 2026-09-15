"""An interrupted actual Provider request remains UNKNOWN after process exit."""
import asyncio
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest


class ProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_request_recovers_original_key_without_resending(self):
        received=threading.Event();release=threading.Event();calls=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(self.rfile.read(int(self.headers['Content-Length'])))
                received.set();release.wait(20)
            def log_message(self,format,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with TemporaryDirectory() as directory:
                root=Path(directory).resolve()
                child=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.semantic.host_interrupted','send',str(root),str(server.server_port),
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                try:
                    assert child.stdout is not None
                    line=await asyncio.wait_for(child.stdout.readline(),10)
                    if not line:self.fail(str(await child.communicate()))
                    work=json.loads(line)['work_id']
                    self.assertTrue(await asyncio.to_thread(received.wait,10))
                finally:
                    if child.returncode is None:child.kill()
                    await child.communicate();release.set()
                recover=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.semantic.host_interrupted','recover',str(root),str(server.server_port),work,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                out,err=await asyncio.wait_for(recover.communicate(),15)
                self.assertEqual(recover.returncode,0,(out,err));self.assertEqual(json.loads(out)['sends'],0);self.assertEqual(len(calls),1)
        finally:release.set();server.shutdown();thread.join();server.server_close()
