"""Independent real HTTP and RFC6455 clients against normal managed scheduling."""
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from clients.iris_client import IrisClient
from companion_memory.persistence import Committed
from companion_memory.management.notification_routes import NotificationRoutes
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application, enable_communication


class LiveCommunicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_and_route_scoped_takeover_with_actual_ack(self):
        with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
            app, http = await ready_application(self, Path(directory) / 'instance', provider_port)
            sockets = []
            try:
                business = app.business
                self.assertIsNotNone(business.goal_scheduler)
                assert business.goal_scheduler is not None
                self.assertFalse(business.goal_scheduler.closed)
                await enable_communication(self, app)
                routes = NotificationRoutes(app.identity)
                events = ('goal.upcoming', 'goal.due')
                for rid in ('route-a', 'route-b'):
                    self.assertIs(type(await routes.create('create-' + rid, rid, 'host', ('entry',), events)), Committed)
                    self.assertIs(type(await routes.enable('enable-' + rid, rid, 1, True)), Committed)
                operations = ('notifications', 'goal_write', 'goal_read', 'media_upload', 'media_inspect', 'accept', 'confirm')
                result, token = await app.identity.create_token('both-routes', 'host', ('entry',), operations,
                    time.time_ns() // 1000 + 120000000, route_ids=('route-a', 'route-b'), event_types=events)
                self.assertIs(type(result), Committed, result)
                assert token is not None
                client = IrisClient('http://127.0.0.1:18180', token)
                capabilities = await asyncio.to_thread(client.request, '/api/host/capabilities', {})
                self.assertEqual(capabilities['outcome'], 'OBSERVED', capabilities)
                from .test_protocol_resources import validate_http
                validate_http('/api/host/capabilities', capabilities)
                original = {'key': 'external-image', 'modality': 'IMAGE'}
                begun = await asyncio.to_thread(client.media, 'begin', 'entry', original)
                self.assertEqual(begun['outcome'], 'COMMITTED', begun)
                validate_http('/api/host/media/begin', begun)
                uid = begun['data']['receipt']['result']['upload_id']
                chunk = b'\x89PNG\r\n\x1a\nsynthetic independently uploaded attachment'
                progress = await asyncio.to_thread(client.chunk, 'entry', uid, 0, chunk)
                self.assertEqual(progress['data']['offset'], len(chunk), progress)
                repeated = await asyncio.to_thread(client.chunk, 'entry', uid, 0, chunk)
                self.assertEqual(repeated['data']['offset'], len(chunk), repeated)
                changed = await asyncio.to_thread(client.chunk, 'entry', uid, 0, b'changed')
                self.assertEqual(changed['outcome'], 'FAILED', changed)
                observed = await asyncio.to_thread(client.media, 'inspect', 'entry', original)
                validate_http('/api/host/media/inspect', observed)
                self.assertFalse(observed['data']['progress_durable'])
                self.assertEqual(observed['data']['volatile_offset'], len(chunk))
                finished = await asyncio.to_thread(client.media, 'finish', 'entry', {'upload_id': uid})
                self.assertEqual(finished['outcome'], 'COMMITTED', finished)
                confirmed = await asyncio.to_thread(client.media, 'resolve', 'entry', original)
                self.assertEqual(confirmed['data']['value']['commit_id'], finished['data']['receipt']['commit_id'])
                validate_http('/api/host/media/resolve', confirmed)
                cross_entry = await asyncio.to_thread(client.media, 'inspect', 'other-entry', original)
                self.assertEqual(cross_entry['outcome'], 'REJECTED', cross_entry)

                old = await asyncio.to_thread(client.websocket); sockets.append(old)
                self.assertEqual((await asyncio.to_thread(old.receive))['type'], 'ready')
                await asyncio.to_thread(old.send, {'version': 1, 'type': 'subscribe', 'request_id': 'both',
                    'route_ids': ['route-a', 'route-b'], 'event_types': list(events), 'takeover': False})
                self.assertEqual((await asyncio.to_thread(old.receive))['state'], 'SUBSCRIBED')
                _, rotated = await app.identity.create_token('only-a', 'host', ('entry',), ('notifications',),
                    time.time_ns() // 1000 + 120000000, route_ids=('route-a',), event_types=events)
                assert rotated is not None
                new = await asyncio.to_thread(IrisClient('http://127.0.0.1:18180', rotated).websocket); sockets.append(new)
                await asyncio.to_thread(new.receive)
                await asyncio.to_thread(new.send, {'version': 1, 'type': 'subscribe', 'request_id': 'take-a',
                    'route_ids': ['route-a'], 'event_types': list(events), 'takeover': True})
                self.assertEqual((await asyncio.to_thread(new.receive))['state'], 'SUBSCRIBED')
                self.assertEqual((await asyncio.to_thread(old.receive))['route_id'], 'route-a')
                self.assertEqual(len(business.communication.connections), 2)
                self.assertEqual(set(next(c for c in business.communication.connections.values()
                    if set(c.subscriptions) == {'route-b'}).subscriptions), {'route-b'})
                for rid, ws in (('route-b', old), ('route-a', new)):
                    injected = await asyncio.to_thread(client.request, '/api/host/goals/inject', {'entry_id': 'entry',
                        'input': {'operation_key': 'goal-' + rid, 'content': '实际通知 ' + rid, 'subject_ids': ['self'],
                            'world_scope': 'REAL', 'deadline': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                            'reminder_lead_seconds': 0, 'route_id': rid, 'source_id': 'source-' + rid}})
                    self.assertEqual(injected['outcome'], 'COMMITTED', injected)
                    notice = await asyncio.to_thread(ws.receive)
                    self.assertEqual(notice['event'], 'goal.due', notice)
                    self.assertEqual(notice['route_id'], rid)
                    await asyncio.to_thread(ws.send, {'version': 1, 'type': 'ack', 'route_id': rid,
                        'delivery_id': notice['delivery_id'], 'status': 'RECEIVED'})
                    ack = await asyncio.to_thread(ws.receive)
                    self.assertEqual(ack['outcome'], 'COMMITTED', ack)
                self.assertEqual(business.communication.ack_confirmed, 2)
                self.assertFalse(business.goal_scheduler.closed)
                self.assertEqual(len(requests), 1)
                self.assertFalse(failures)
                print({'actual_http_media': True, 'actual_ws_acks': 2, 'takeover_unaffected_route': 'route-b',
                    'normal_background_scheduling': True, 'real_supplier_requests': 0})
            finally:
                for ws in sockets: await asyncio.to_thread(ws.close)
                self.assertTrue(await http.close())
                self.assertTrue(await app.business.close())
                self.assertTrue(app.identity.close())
                self.assertTrue(await app.bootstrap.close())

    async def test_inflight_takeover_preserves_other_route_ack_and_rejects_partial_authority(self):
        with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
            app, http = await ready_application(self, Path(directory)/'instance', provider_port, port=18187)
            sockets=[]
            try:
                await enable_communication(self,app)
                routes=NotificationRoutes(app.identity)
                for rid in ('inflight-a','inflight-b'):
                    self.assertIs(type(await routes.create('create-'+rid,rid,'host',('entry',),('goal.due',))),Committed)
                    self.assertIs(type(await routes.enable('enable-'+rid,rid,1,True)),Committed)
                _, token=await app.identity.create_token('old-both','host',('entry',),('notifications','goal_write'),
                    time.time_ns()//1000+120000000,route_ids=('inflight-a','inflight-b'),event_types=('goal.due',))
                _, rotated=await app.identity.create_token('new-only-a','host',('entry',),('notifications',),
                    time.time_ns()//1000+120000000,route_ids=('inflight-a',),event_types=('goal.due',))
                assert token is not None and rotated is not None
                client=IrisClient('http://127.0.0.1:18187',token)
                old=await asyncio.to_thread(client.websocket);sockets.append(old)
                await asyncio.to_thread(old.receive)
                subscribe={'version':1,'type':'subscribe','request_id':'original','route_ids':['inflight-a','inflight-b'],
                    'event_types':['goal.due'],'takeover':False}
                await asyncio.to_thread(old.send,subscribe)
                self.assertEqual((await asyncio.to_thread(old.receive))['state'],'SUBSCRIBED')
                new=await asyncio.to_thread(IrisClient('http://127.0.0.1:18187',rotated).websocket);sockets.append(new)
                await asyncio.to_thread(new.receive)
                await asyncio.to_thread(new.send,subscribe|{'request_id':'partial-invalid','takeover':True})
                self.assertEqual((await asyncio.to_thread(new.receive))['reason'],'BINDING_MISMATCH')
                sessions=app.business.communication
                self.assertIs(sessions.consumers['inflight-a'].connection,sessions.consumers['inflight-b'].connection)
                due=(datetime.now(timezone.utc)+timedelta(seconds=3)).isoformat()
                for rid in ('inflight-a','inflight-b'):
                    result=await asyncio.to_thread(client.request,'/api/host/goals/inject',{'entry_id':'entry','input':{
                        'operation_key':'due-'+rid,'content':'在途并发 '+rid,'subject_ids':['self'],'world_scope':'REAL',
                        'deadline':due,'reminder_lead_seconds':0,'route_id':rid,'source_id':'source-'+rid}})
                    self.assertEqual(result['outcome'],'COMMITTED',result)
                notices={}
                for _ in range(2):
                    notice=await asyncio.to_thread(old.receive);notices[notice['route_id']]=notice
                self.assertEqual(set(notices),{'inflight-a','inflight-b'})
                self.assertEqual(len(sessions.deliveries),2)
                await asyncio.to_thread(new.send,subscribe|{'request_id':'only-a','route_ids':['inflight-a'],'takeover':True})
                self.assertEqual((await asyncio.to_thread(new.receive))['state'],'SUBSCRIBED')
                changed=await asyncio.to_thread(old.receive)
                self.assertEqual((changed['route_id'],changed['reason']),('inflight-a','TAKEN_OVER'))
                for rid,outcome in (('inflight-b','COMMITTED'),('inflight-a','BINDING_MISMATCH')):
                    await asyncio.to_thread(old.send,{'version':1,'type':'ack','route_id':rid,'delivery_id':notices[rid]['delivery_id'],'status':'RECEIVED'})
                    reply=await asyncio.to_thread(old.receive)
                    self.assertEqual(reply.get('outcome',reply.get('reason')),outcome,reply)
                # A second owner cannot oscillate the route within ten seconds.
                await asyncio.to_thread(old.send,subscribe|{'request_id':'too-fast','route_ids':['inflight-a'],'takeover':True})
                self.assertEqual((await asyncio.to_thread(old.receive))['reason'],'TAKEOVER_LIMIT')
                self.assertIn('inflight-b',next(c for c in sessions.connections.values() if c.principal.identity!=sessions.consumers['inflight-a'].connection.principal.identity).subscriptions)
                self.assertEqual(sessions.ack_confirmed,1)
                dispatcher=app.business.communication_dispatcher;assert dispatcher is not None
                self.assertEqual(dispatcher.terminals,{'ACKNOWLEDGED':1,'UNKNOWN':1,'NOT_SENT':0})
                self.assertEqual((len(requests),failures),(1,[]))
                print({'simultaneous_inflight':2,'takeover_a':'UNKNOWN','old_connection_b':'ACKNOWLEDGED',
                    'partial_authority':'ATOMIC_REFUSAL','takeover_rate':'REFUSED','supplier_requests':0})
            finally:
                for ws in sockets: await asyncio.to_thread(ws.close)
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()
