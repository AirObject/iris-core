"""Real ACK loss and durable route-scoped pages survive an independent process."""
import asyncio
from contextlib import nullcontext
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from clients.iris_client import IrisClient
from companion_memory.management.notification_routes import NotificationRoutes
from companion_memory.persistence import Committed
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application,enable_communication,confirm_original
from .test_protocol_resources import validate_http


class DeliveryResultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_terminal_pages_after_ack_loss_and_process_restart(self):
        with TemporaryDirectory() as directory,responses((persona,)) as (provider_port,requests,failures):
            root=Path(directory)/'instance';app,http=await ready_application(self,root,provider_port,port=18194)
            child=None;sockets=[]
            try:
                await enable_communication(self,app)
                routes=NotificationRoutes(app.identity)
                for route in ('visible','hidden'):
                    await confirm_original(self,lambda route=route:routes.create('create-'+route,route,'host',('entry',),('goal.due',)))
                    await confirm_original(self,lambda route=route:routes.enable('enable-'+route,route,1,True))
                _,token=await app.identity.create_token('result-reader','host',('entry',),('notifications','goal_write'),time.time_ns()//1000+300000000,route_ids=('visible',),event_types=('goal.due',))
                assert token is not None
                client=IrisClient('http://127.0.0.1:18194',token)
                async def connect():
                    ws=await asyncio.to_thread(client.websocket);sockets.append(ws);await asyncio.to_thread(ws.receive)
                    await asyncio.to_thread(ws.send,{'version':1,'type':'subscribe','request_id':'read-current','route_ids':['visible'],'event_types':['goal.due'],'takeover':True})
                    self.assertEqual((await asyncio.to_thread(ws.receive))['state'],'SUBSCRIBED');return ws
                ws=await connect()
                host=app.business.host;assert host is not None and host.goals is not None
                dispatcher=app.business.communication_dispatcher;assert dispatcher is not None
                for number in range(5):
                    async def disable_before_write(*args):
                        await confirm_original(self,lambda:routes.enable('disable-before-first-write','visible',2,False))
                        return await original_permit(*args)
                    original_permit=host.goals.permit_reminder
                    context=patch.object(host.goals,'permit_reminder',side_effect=disable_before_write) if number==4 else nullcontext()
                    with context:
                        payload={'entry_id':'entry','input':{'operation_key':'result-goal-'+str(number),'content':'独立结果 '+str(number),
                            'subject_ids':['self'],'world_scope':'REAL','deadline':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),
                            'reminder_lead_seconds':0,'route_id':'visible','source_id':'source-'+str(number)}}
                        reply=await asyncio.to_thread(client.request,'/api/host/goals/inject',payload)
                        self.assertEqual(reply['outcome'],'COMMITTED',reply)
                        if number<4:
                            notice=await asyncio.to_thread(ws.receive);self.assertEqual(notice['event'],'goal.due')
                            if number<3:
                                await asyncio.to_thread(ws.send,{'version':1,'type':'ack','route_id':'visible','delivery_id':notice['delivery_id'],'status':'RECEIVED'})
                                # Do not read ack_result: lose the connection only after durable settlement.
                                for _ in range(120):
                                    if dispatcher.terminals['ACKNOWLEDGED']>=number+1:break
                                    await asyncio.sleep(.05)
                                self.assertEqual(dispatcher.terminals['ACKNOWLEDGED'],number+1)
                            await asyncio.to_thread(ws.close)
                        for _ in range(160):
                            if sum(dispatcher.terminals.values())>=number+1:break
                            await asyncio.sleep(.05)
                        self.assertEqual(sum(dispatcher.terminals.values()),number+1)
                    if number<4:ws=await connect()
                self.assertEqual(dispatcher.terminals,{'ACKNOWLEDGED':3,'UNKNOWN':1,'NOT_SENT':1})
                async def pages():
                    after='';items=[];sizes=[]
                    for _ in range(4):
                        response=await asyncio.to_thread(client.request,'/api/host/notifications/results',{'route_id':'visible','after':after})
                        self.assertEqual(response['outcome'],'OBSERVED',response);validate_http('/api/host/notifications/results',response)
                        data=response['data'];sizes.append(len(data['items']));items.extend(data['items']);after=data['after']
                        if not after:break
                    self.assertEqual(sizes,[4,1,0]);return items
                before=await pages()
                self.assertEqual(len({r['plan_id'] for r in before}),5)
                denied=await asyncio.to_thread(client.request,'/api/host/notifications/results',{'route_id':'hidden','after':''})
                self.assertEqual(denied['outcome'],'REJECTED',denied)
                invalid=await asyncio.to_thread(client.request,'/api/host/notifications/results',{'route_id':'visible','after':'x'*129})
                self.assertEqual(invalid['outcome'],'REJECTED',invalid)
                for ws in sockets:await asyncio.to_thread(ws.close)
                await http.close()
                while not await app.business.close():await asyncio.sleep(.05)
                await app.bootstrap.close()
                child=await asyncio.create_subprocess_exec(sys.executable,'-u','-m','tests.managed_runtime.communication_reopen',str(root),'18194',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                assert child.stdout is not None
                ready=await asyncio.wait_for(child.stdout.readline(),30);self.assertEqual(json.loads(ready)['state'],'READY')
                self.assertEqual(await pages(),before)
                # Reconnect does not resend any of the five finalized attempts.
                ws=await connect();await asyncio.sleep(1)
                response=await asyncio.to_thread(client.request,'/api/host/notifications/status',{'route_ids':['visible']})
                self.assertEqual(response['data']['deliveries'],[])
                child.terminate();stdout,stderr=await asyncio.wait_for(child.communicate(),20)
                self.assertEqual(child.returncode,0,stderr)
                cleanup=json.loads(stdout);self.assertEqual(cleanup['new_writes'],0);self.assertEqual(cleanup['storage'],'CLOSED')
                self.assertEqual((len(requests),failures),(1,[]))
                print({'durable_results':{'ACKNOWLEDGED':3,'UNKNOWN':1,'NOT_SENT':1},'page_sizes':[4,1,0],
                    'ack_receipts_intentionally_unread':3,'new_process':'EXACT_MATCH','foreign_route':'REFUSED','replays':0,'cleanup':cleanup})
            finally:
                if child is not None and child.returncode is None:child.terminate();await child.wait()
                for ws in sockets:await asyncio.to_thread(ws.close)
                await http.close()
                while not await app.business.close():await asyncio.sleep(.05)
                await app.bootstrap.close()
