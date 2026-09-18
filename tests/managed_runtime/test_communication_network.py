"""Real verified WSS through NGINX: origin, peer, framing and idle lifecycle.

Requires the explicit local qualification proxy and synthetic CA. No external
model endpoint, uncontrolled forwarding header or disabled TLS check is used.
"""
import asyncio
import json
from pathlib import Path
import socket
import ssl
from tempfile import TemporaryDirectory
import time
import unittest
from clients.iris_client import IrisClient
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.management.managed_http import ManagedHTTP
from companion_memory.management.notification_routes import NotificationRoutes
from companion_memory.persistence import Committed
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application, enable_communication


class NetworkCommunicationTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(Path('/tmp/iris-test-ca.crt').is_file(), 'Requires explicit real TLS qualification proxy')
    async def test_real_wss_boundaries_frames_and_heartbeat(self):
        with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
            app, http = await ready_application(self, Path(directory)/'instance', provider_port, port=18184, origin='https://localhost:18444')
            extra = None;sockets = []
            try:
                await enable_communication(self, app)
                routes = NotificationRoutes(app.identity)
                self.assertIs(type(await routes.create('route', 'route', 'host', ('entry',), ('goal.due',))), Committed)
                _, token = await app.identity.create_token('network-token', 'host', ('entry',), ('notifications',),
                    time.time_ns()//1000+300000000, route_ids=('route',), event_types=('goal.due',))
                assert token is not None
                client = IrisClient('https://localhost:18444', token, ca_file='/tmp/iris-test-ca.crt')
                context = ssl.create_default_context(cafile='/tmp/iris-test-ca.crt')
                def upgrade(headers: bytes, host: str = 'localhost:18444', bearer: bool = True):
                    sock = context.wrap_socket(socket.create_connection(('localhost', 18444), 5), server_hostname='localhost')
                    sock.settimeout(10)
                    wire = (f'GET /api/host/ws HTTP/1.1\r\nHost: {host}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n'
                        'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Protocol: iris.communication.v1\r\n')
                    if bearer: wire += 'Authorization: Bearer '+token+'\r\n'
                    sock.sendall(wire.encode()+headers+b'\r\n')
                    response=bytearray()
                    while not response.endswith(b'\r\n\r\n'):
                        block=sock.recv(1)
                        if not block: break
                        response.extend(block)
                    return sock, int(bytes(response).split(b' ')[1]), bytes(response)
                for origin, expected in (('https://localhost:18444',101),('https://LOCALHOST:18444',101),
                    ('http://localhost:18444',403),('https://localhost',403),('null',400),('https://localhost:18444 https://evil.test',400)):
                    sock, code, _ = await asyncio.to_thread(upgrade, ('Origin: '+origin+'\r\n').encode())
                    sock.close();self.assertEqual(code,expected,origin)
                    await asyncio.sleep(.05)
                sock,code,_=await asyncio.to_thread(upgrade,b'Forwarded: host=localhost:18444;proto=https\r\nX-Forwarded-Host: localhost:18444\r\n','evil.test')
                sock.close();self.assertEqual(code,403)
                for headers in (b'Host: localhost:18444\r\n',b'Origin: https://localhost:18444\r\nOrigin: https://localhost:18444\r\n'):
                    sock,code,_=await asyncio.to_thread(upgrade,headers)
                    sock.close();self.assertIn(code,(400,403))
                sock,code,response=await asyncio.to_thread(upgrade,b'Origin: https://localhost:18444\r\nSec-WebSocket-Extensions: permessage-deflate\r\n',bearer=False)
                self.assertEqual(code,101);self.assertNotIn(b'Sec-WebSocket-Extensions',response)
                await asyncio.sleep(6)
                self.assertEqual(app.business.communication.authenticating,0)
                sock.close()
                ws=await asyncio.to_thread(client.websocket);sockets.append(ws)
                await asyncio.to_thread(ws.receive)
                subscription={'version':1,'type':'subscribe','request_id':'fragmented','route_ids':['route'],'event_types':['goal.due'],'takeover':False}
                encoded=json.dumps(subscription).encode()
                await asyncio.to_thread(ws.send_frame,encoded[:20],1,final=False)
                await asyncio.to_thread(ws.send_frame,b'fragment-ping',9)
                await asyncio.to_thread(ws.send_frame,encoded[20:],0)
                self.assertEqual((await asyncio.to_thread(ws.receive))['state'],'SUBSCRIBED')
                # A disabled route can be subscribed first, then enabled without
                # silently losing this original consumer during normal scans.
                await asyncio.sleep(2)
                self.assertIn('route',app.business.communication.consumers)
                self.assertIs(type(await routes.enable('enable-route','route',1,True)),Committed)
                await asyncio.sleep(2)
                self.assertIn('route',app.business.communication.consumers)
                reading=asyncio.create_task(asyncio.to_thread(ws.receive))
                await asyncio.sleep(65)
                self.assertFalse(reading.done(), 'Heartbeats must retain a real WSS connection past proxy idle timeout.')
                await asyncio.to_thread(ws.send, {'version':1,'type':'unsubscribe','request_id':'after-idle','route_ids':['route']})
                changed=await reading
                self.assertEqual(changed['type'],'subscription_changed',changed)
                self.assertEqual((await asyncio.to_thread(ws.receive))['state'],'UNSUBSCRIBED')
                for payload, opcode in ((b'binary',2),(b'x'*8193,1),(b'{"version":2,"type":"ack"}',1),
                        (b'{"version":1,"version":1,"type":"ack"}',1)):
                    bad=await asyncio.to_thread(client.websocket);sockets.append(bad)
                    await asyncio.to_thread(bad.receive)
                    if len(payload)>8192:
                        await asyncio.to_thread(bad.send_frame,payload[:4096],opcode,final=False)
                        await asyncio.to_thread(bad.send_frame,payload[4096:],0)
                    else: await asyncio.to_thread(bad.send_frame,payload,opcode)
                    with self.assertRaises((ConnectionError,OSError)): await asyncio.to_thread(bad.receive)
                    await asyncio.to_thread(bad.close)
                settings=resolve_deployment({'deployment.origin':'https://localhost:18445','deployment.port':18185,
                    'deployment.trusted_proxy_peers':'127.0.0.2'})
                extra=ManagedHTTP(settings,app.identity,app.dispatch,app.health,Path(directory)/'communication-static')
                await extra.start()
                untrusted=IrisClient('https://localhost:18445',token,ca_file='/tmp/iris-test-ca.crt')
                denied=await asyncio.to_thread(untrusted._request,'/api/host/capabilities',b'{}',
                    {'Content-Type':'application/json','X-Forwarded-For':'127.0.0.2','Forwarded':'for=127.0.0.2;proto=https'})
                self.assertEqual(denied['outcome'],'REJECTED',denied)
                self.assertEqual(len(requests),1);self.assertEqual(failures,[])
                print({'tls_verified':True,'proxy':'NGINX','idle_seconds':65,'cross_scheme':'REJECTED',
                    'nondefault_port':18444,'untrusted_actual_peer':'REJECTED','fragmented_control':'ACCEPTED',
                    'binary_oversize_duplicate_unknown_version':'CLOSED','supplier_requests':0})
            finally:
                for ws in sockets: await asyncio.to_thread(ws.close)
                if extra is not None: await extra.close()
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()

    @unittest.skipUnless(__import__('os').environ.get('IRIS_QUALIFY_PROXY_CONTROL')=='1',
        'Requires operator ownership of the explicit local qualification proxy')
    async def test_external_443_internal_8080_and_forced_proxy_restart(self):
        import os
        import signal
        config=Path('/evidence/nginx-validation.conf')
        original=config.read_text()
        async def nginx(*arguments):
            process=await asyncio.create_subprocess_exec('/usr/sbin/nginx','-c',str(config),*arguments)
            self.assertEqual(await process.wait(),0)
        config.write_text(original.replace('127.0.0.1:18080','127.0.0.1:8080'))
        await nginx('-s','reload')
        await asyncio.sleep(.3)
        sockets=[]
        with TemporaryDirectory() as directory,responses((persona,)) as (provider_port,requests,failures):
            app,http=await ready_application(self,Path(directory)/'instance',provider_port,port=8080,origin='https://localhost')
            try:
                await enable_communication(self,app)
                routes=NotificationRoutes(app.identity)
                self.assertIs(type(await routes.create('proxy-route','proxy-route','host',('entry',),('goal.due',))),Committed)
                _,token=await app.identity.create_token('proxy-token','host',('entry',),('notifications',),
                    time.time_ns()//1000+120000000,route_ids=('proxy-route',),event_types=('goal.due',))
                assert token is not None
                client=IrisClient('https://localhost',token,ca_file='/tmp/iris-test-ca.crt')
                for origin,expected in (('https://localhost','OBSERVED'),('https://localhost:443','OBSERVED'),('http://localhost','REJECTED')):
                    result=await asyncio.to_thread(client._request,'/api/host/capabilities',b'{}',{'Content-Type':'application/json','Origin':origin})
                    self.assertEqual(result['outcome'],expected,result)
                ws=await asyncio.to_thread(client.websocket);sockets.append(ws)
                await asyncio.to_thread(ws.receive)
                await asyncio.to_thread(ws.send,{'version':1,'type':'subscribe','request_id':'proxy-subscribe','route_ids':['proxy-route'],'event_types':['goal.due'],'takeover':False})
                self.assertEqual((await asyncio.to_thread(ws.receive))['state'],'SUBSCRIBED')
                os.kill(int(Path('/tmp/nginx.pid').read_text()),signal.SIGTERM)
                with self.assertRaises((ConnectionError,OSError)): await asyncio.to_thread(ws.receive)
                for _ in range(100):
                    if not app.business.communication.connections and not Path('/tmp/nginx.pid').exists(): break
                    await asyncio.sleep(.05)
                self.assertFalse(app.business.communication.connections)
                self.assertFalse(app.business.communication.consumers)
                await nginx()
                fresh=await asyncio.to_thread(client.websocket);sockets.append(fresh)
                self.assertEqual((await asyncio.to_thread(fresh.receive))['type'],'ready')
                self.assertFalse(app.business.communication.consumers)
                self.assertEqual((len(requests),failures),(1,[]))
                print({'external_port':443,'internal_port':8080,'explicit_default_origin':'ACCEPTED','http_origin':'REJECTED',
                    'forced_proxy_restart':'OLD_SOCKET_CLOSED','reconnect':'NO_RESTORED_SUBSCRIPTION','supplier_requests':0})
            finally:
                for ws in sockets: await asyncio.to_thread(ws.close)
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()
                config.write_text(original)
                await nginx(*(['-s','reload'] if Path('/tmp/nginx.pid').exists() else []))

    async def test_real_socket_queue_limit_and_half_open_timeout(self):
        with TemporaryDirectory() as directory,responses((persona,)) as (port,requests,failures):
            app,http=await ready_application(self,Path(directory)/'instance',port,port=18188)
            sockets=[]
            try:
                await enable_communication(self,app)
                routes=NotificationRoutes(app.identity)
                self.assertIs(type(await routes.create('queue-route','queue-route','host',('entry',),('goal.due',))),Committed)
                _,token=await app.identity.create_token('queue-token','host',('entry',),('notifications',),time.time_ns()//1000+120000000,route_ids=('queue-route',),event_types=('goal.due',))
                assert token is not None
                client=IrisClient('http://127.0.0.1:18188',token)
                ws=await asyncio.to_thread(client.websocket);sockets.append(ws)
                ready=await asyncio.to_thread(ws.receive)
                connection=app.business.communication.connections[ready['connection_id']]
                transport=connection.transport
                # Keep one event-loop turn atomic so an actual socket's finite
                # queue fills before its sender gets CPU; no send method mock.
                pending=[transport.enqueue('error',{'version':1,'type':'error','request_id':'queue-limit','code':'RESOURCE_BUSY','reason':'LIMIT_EXCEEDED','reconnect':False}) for _ in range(17)]
                self.assertFalse(pending[-1].result())
                self.assertLessEqual(len(transport.pending),16)
                self.assertLessEqual(transport.buffered_bytes,32768)
                await asyncio.gather(*pending)
                self.assertFalse(transport.pending)
                self.assertEqual(transport.buffered_bytes,0)
                silent=await asyncio.to_thread(client.websocket);sockets.append(silent)
                ready=await asyncio.to_thread(silent.receive)
                identity=ready['connection_id']
                # The peer remains TCP-connected but never reads/responds to
                # the real ping. Observe the ordinary 20+10 second lifecycle.
                await asyncio.sleep(32)
                self.assertNotIn(identity,app.business.communication.connections)
                self.assertEqual((len(requests),failures),(1,[]))
                print({'real_socket_queue_items':16,'seventeenth':'REFUSED_CLOSED','released_queue_bytes':0,
                    'half_open_no_pong_seconds':32,'connection':'RELEASED','supplier_requests':0})
            finally:
                for ws in sockets: await asyncio.to_thread(ws.close)
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()

    async def test_executable_client_bounded_reconnect_and_current_state_sync(self):
        import os
        import sys
        with TemporaryDirectory() as directory,responses((persona,)) as (port,requests,failures):
            app,http=await ready_application(self,Path(directory)/'instance',port,port=18193)
            child=None;reading=None;messages=[]
            try:
                await enable_communication(self,app)
                routes=NotificationRoutes(app.identity)
                self.assertIs(type(await routes.create('client-route','client-route','host',('entry',),('goal.due',))),Committed)
                _,token=await app.identity.create_token('client-token','host',('entry',),('notifications','goal_read'),
                    time.time_ns()//1000+120000000,route_ids=('client-route',),event_types=('goal.due',))
                assert token is not None
                child=await asyncio.create_subprocess_exec(sys.executable,'-u','-m','clients.iris_client',
                    '--origin','http://127.0.0.1:18193','--entry','entry','--route','client-route','--reconnect-attempts','2',
                    env=os.environ|{'IRIS_HOST_TOKEN':token},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                async def collect():
                    assert child is not None and child.stdout is not None
                    async for line in child.stdout: messages.append(json.loads(line))
                reading=asyncio.create_task(collect())
                async def wait_subscribed(count):
                    for _ in range(200):
                        if sum(m.get('state')=='SUBSCRIBED' for m in messages)>=count: return
                        await asyncio.sleep(.05)
                    self.fail('Actual client did not complete bounded reconnect: '+repr(messages))
                await wait_subscribed(1)
                sessions=app.business.communication
                old=sessions.consumers['client-route'].connection.connection_id
                sessions.connections[old].transport.writer.transport.abort()
                await wait_subscribed(2)
                self.assertNotEqual(sessions.consumers['client-route'].connection.connection_id,old)
                delays=[m['delay_seconds'] for m in messages if 'reconnect_attempt' in m]
                self.assertEqual(len(delays),1);self.assertGreaterEqual(delays[0],.8);self.assertLessEqual(delays[0],1.2)
                for _ in range(100):
                    if sum(m.get('data',{}).get('mode')=='NORMAL' for m in messages)>=2: break
                    await asyncio.sleep(.05)
                self.assertGreaterEqual(sum(m.get('data',{}).get('mode')=='NORMAL' for m in messages),2)
                self.assertFalse(sessions.deliveries)
                await sessions.retire(sessions.consumers['client-route'],'DISABLED')
                self.assertEqual(await asyncio.wait_for(child.wait(),5),0)
                await reading
                self.assertEqual((len(requests),failures),(1,[]))
                print({'actual_cli':'RECONNECTED_ONCE','jitter_delay_seconds':delays,'subscriptions':2,
                    'current_status_resynced':True,'business_replays':0,'withdrawal':'EXIT_0','supplier_requests':0})
            finally:
                if child is not None and child.returncode is None: child.terminate();await child.wait()
                if reading is not None: await reading
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()
