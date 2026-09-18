"""Actual host probe ACK and bounded management record reuse semantics."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from typing import cast
from unittest.mock import patch
from companion_memory.persistence import Rejected, PersistenceError
from clients.iris_client import IrisClient
from companion_memory.persistence import Committed
from companion_memory.management.notification_routes import NotificationRoutes
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application, enable_communication


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_probe_ack_duplicate_and_rate_bound(self):
        with TemporaryDirectory() as directory, responses((persona,)) as (port, requests, failures):
            app, http = await ready_application(self, Path(directory) / 'instance', port, port=18181)
            ws = None
            try:
                await enable_communication(self, app)
                routes = NotificationRoutes(app.identity)
                self.assertIs(type(await routes.create('probe-route-create', 'probe-route', 'host', ('entry',), ('connection.probe',))), Committed)
                self.assertIs(type(await routes.enable('probe-route-enable', 'probe-route', 1, True)), Committed)
                self.assertIs(type(await routes.create('other-create', 'other-route', 'host', ('entry',), ('connection.probe',))), Committed)
                self.assertIs(type(await routes.enable('other-enable', 'other-route', 1, True)), Committed)
                _, token = await app.identity.create_token('probe-token', 'host', ('entry',), ('notifications', 'probe'),
                    time.time_ns() // 1000 + 120000000, route_ids=('probe-route', 'other-route'), event_types=('connection.probe',))
                assert token is not None
                ws = await asyncio.to_thread(IrisClient('http://127.0.0.1:18181', token).websocket)
                await asyncio.to_thread(ws.receive)
                await asyncio.to_thread(ws.send, {'version': 1, 'type': 'subscribe', 'request_id': 'probe-sub',
                    'route_ids': ['probe-route', 'other-route'], 'event_types': ['connection.probe'], 'takeover': False})
                await asyncio.to_thread(ws.receive)
                for ordinal in range(3):
                    result = await app.business.communication.probes.run('probe-' + str(ordinal), 'probe-route')
                    self.assertIs(type(result['registration']), Committed)
                    message = await asyncio.to_thread(ws.receive)
                    self.assertEqual(message['event'], 'connection.probe')
                    ack = {'version': 1, 'type': 'ack', 'delivery_id': message['delivery_id'], 'route_id': 'probe-route', 'status': 'RECEIVED'}
                    if ordinal == 0:
                        original_write = app.identity.write
                        refused = [False]
                        async def reject_first_terminal(kind, *args, **kwargs):
                            if kind == 'finish_communication_probe' and not refused[0]:
                                refused[0] = True
                                return Rejected(PersistenceError('RESOURCE_BUSY', 'execute', 'resources', 'ADMISSION_BUSY', False))
                            return await original_write(kind, *args, **kwargs)
                        with patch.object(app.identity, 'write', side_effect=reject_first_terminal):
                            await asyncio.to_thread(ws.send, ack)
                            self.assertEqual((await asyncio.to_thread(ws.receive))['outcome'], 'UNCONFIRMED')
                        # The same ACK confirms its retained claim even after a
                        # background owner has durably settled it in between.
                    await asyncio.to_thread(ws.send, ack)
                    self.assertEqual((await asyncio.to_thread(ws.receive))['outcome'], 'COMMITTED')
                    await asyncio.to_thread(ws.send, ack)
                    self.assertEqual((await asyncio.to_thread(ws.receive))['outcome'], 'COMMITTED')
                repeated = await app.business.communication.probes.run('probe-0', 'probe-route')
                self.assertEqual(repeated['registration'].source, 'EXISTING')
                from companion_memory.persistence.owned_statements import OwnerFailure
                with self.assertRaises(OwnerFailure) as limited: await app.business.communication.probes.run('probe-4', 'other-route')
                self.assertEqual(limited.exception.reason, 'PROBE_RATE_LIMIT')
                rows = await app.identity.rows.page('communication_probes', '')
                self.assertEqual(sum(cast(int, row['registered_count']) for row in rows), 3)
                self.assertEqual(sum(cast(int, row['acknowledged_count']) for row in rows), 3)
                self.assertEqual(len(requests), 1)
                self.assertEqual(failures, [])
            finally:
                if ws is not None: await asyncio.to_thread(ws.close)
                self.assertTrue(await http.close())
                while not await app.business.close(): await asyncio.sleep(.05)
                self.assertTrue(await app.bootstrap.close())

    async def test_mode_hint_focus_suppression_and_resumed_snapshot(self):
        from companion_memory.information.management import HostIdentity
        from companion_memory.dream.port import OPERATIONS
        from companion_memory.persistence.owned_statements import OwnerFailure
        with TemporaryDirectory() as directory,responses((persona,)) as (port,requests,failures):
            app,http=await ready_application(self,Path(directory)/'instance',port,port=18181)
            ws=None
            try:
                await enable_communication(self,app)
                routes=NotificationRoutes(app.identity)
                events=('core.mode_changed','connection.probe')
                self.assertIs(type(await routes.create('mode-route','mode-route','host',('entry',),events)),Committed)
                self.assertIs(type(await routes.enable('enable-mode-route','mode-route',1,True)),Committed)
                _,token=await app.identity.create_token('mode-token','host',('entry',),('notifications','runtime_observe','probe'),
                    time.time_ns()//1000+120000000,route_ids=('mode-route',),event_types=events)
                assert token is not None
                client=IrisClient('http://127.0.0.1:18181',token)
                ws=await asyncio.to_thread(client.websocket)
                await asyncio.to_thread(ws.receive)
                await asyncio.to_thread(ws.send,{'version':1,'type':'subscribe','request_id':'mode','route_ids':['mode-route'],'event_types':list(events),'takeover':False})
                self.assertEqual((await asyncio.to_thread(ws.receive))['state'],'SUBSCRIBED')
                sessions=app.business.communication
                host=app.business.host
                assert host is not None and host.runtime is not None and host.combination.dream is not None
                registered=await sessions.probes.run('original-mode-probe','mode-route')
                self.assertIs(type(registered['registration']),Committed)
                notification=await asyncio.to_thread(ws.receive)
                self.assertEqual(notification['event'],'connection.probe')
                ack={'version':1,'type':'ack','route_id':'mode-route','delivery_id':notification['delivery_id'],'status':'RECEIVED'}
                await asyncio.to_thread(ws.send,ack)
                self.assertEqual((await asyncio.to_thread(ws.receive))['outcome'],'COMMITTED')
                dream=await host.bind_dream(HostIdentity('mode-dream','reviewer','host','entry',OPERATIONS,(),time.monotonic()+120))
                schedule=await host.combination.dream.schedule();assert schedule is not None
                focus=await dream.start_dream('mode-focus','mode-focus',cast(int,schedule['revision']),host.runtime.gate.epoch,mode='FOCUSED')
                self.assertIs(type(focus),Committed,focus)
                await sessions.observe()
                self.assertIsNone(sessions.mode_epoch)
                await asyncio.to_thread(ws.send,ack)
                self.assertEqual((await asyncio.to_thread(ws.receive))['outcome'],'COMMITTED')
                with self.assertRaises(OwnerFailure): await sessions.probes.run('forbidden-focused-probe','mode-route')
                state=await dream.inspect_dream('mode-focus');assert state is not None
                original_abort=(cast(int,state['revision']),cast(int,state['mode_epoch']))
                abort_outcomes=[]
                ended=None
                for _ in range(5):
                    ended=await dream.abort_dream('mode-abort','mode-focus',*original_abort)
                    abort_outcomes.append({'outcome':type(ended).__name__,'error':repr(getattr(ended,'error',None))})
                    if type(ended) is Committed: break
                    await asyncio.sleep(.1)
                self.assertIs(type(ended),Committed,abort_outcomes)
                print({'focused_exit_original_key':'mode-abort','outcomes':abort_outcomes})
                await sessions.observe()
                message=await asyncio.to_thread(ws.receive)
                self.assertEqual(message['event'],'core.mode_changed',message)
                self.assertEqual(message['hint'],'REFRESH_AVAILABILITY')
                self.assertNotIn('delivery_id',message)
                status=await asyncio.to_thread(client.request,'/api/host/notifications/status',{'route_ids':['mode-route']})
                self.assertEqual(status['data']['mode'],'NORMAL',status)
                probes=await app.identity.rows.page('communication_probes','')
                self.assertEqual(sum(cast(int,row['registered_count']) for row in probes),1)
                self.assertFalse(sessions.deliveries)
                self.assertEqual((len(requests),failures),(1,[]))
                print({'focused_probe':'REJECTED','focused_mode_hint':'SUPPRESSED','resumed_snapshot':'REFRESH_AVAILABILITY',
                    'query_corrects_lost_hint':True,'probe_records':1,'focused_original_ack':'COMMITTED','supplier_requests':0})
            finally:
                if ws is not None: await asyncio.to_thread(ws.close)
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()
