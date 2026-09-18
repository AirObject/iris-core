"""Actual long backup I/O, bounded host refusal and reminders after reopen."""
import asyncio
from datetime import datetime,timezone,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from clients.iris_client import IrisClient
from companion_memory.persistence import Committed
from companion_memory.persistence.managed_backup import ConsistentBackup
from companion_memory.management.notification_routes import NotificationRoutes
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application,enable_communication,confirm_original
from .test_developer_audit import wire


class CommunicationMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_actual_backup_refuses_promptly_and_resumes_original_goal(self):
        with TemporaryDirectory() as directory,responses((persona,)) as (port,requests,failures):
            app,http=await ready_application(self,Path(directory)/'instance',port,port=18194)
            http.identity_source=lambda:app.identity
            ws=None;backup=None;release=threading.Event();entered=threading.Event()
            try:
                await enable_communication(self,app)
                await confirm_original(self,lambda:app.identity.establish('backup-admin',app.resources.read_secret('bootstrap').decode(),'Synthetic-Backup-Only-123!'))
                _,session=await app.identity.login('backup-login','Synthetic-Backup-Only-123!');assert session is not None
                routes=NotificationRoutes(app.identity)
                await confirm_original(self,lambda:routes.create('backup-route','backup-route','host',('entry',),('goal.due',)))
                await confirm_original(self,lambda:routes.enable('enable-backup-route','backup-route',1,True))
                _,token=await app.identity.create_token('backup-token','host',('entry',),('notifications','goal_write','goal_read','query','confirm'),
                    time.time_ns()//1000+120000000,route_ids=('backup-route',),event_types=('goal.due',))
                assert token is not None
                client=IrisClient('http://127.0.0.1:18194',token)
                goal={'operation_key':'original-backup-goal','content':'合成备份跨期提醒','subject_ids':['self'],'world_scope':'REAL',
                    'deadline':(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat(),'reminder_lead_seconds':0,
                    'route_id':'backup-route','source_id':'backup-source'}
                created=await asyncio.to_thread(client.request,'/api/host/goals/inject',{'entry_id':'entry','input':goal})
                self.assertEqual(created['outcome'],'COMMITTED',created)
                copy=ConsistentBackup.create
                def controlled_copy(owner,backup_id,**kwargs):
                    entered.set()
                    if not release.wait(10): raise TimeoutError('Test did not release actual backup worker.')
                    return copy(owner,backup_id,**kwargs)
                observations=[]
                with patch.object(ConsistentBackup,'create',controlled_copy):
                    backup=asyncio.create_task(wire(http,'/api/backups/create',{'key':'long-original-backup'},
                        cookie='iris_session='+session['session'],csrf=session['csrf']))
                    self.assertTrue(await asyncio.to_thread(entered.wait,10))
                    for _ in range(3):
                        started=time.monotonic()
                        result=await asyncio.to_thread(client.request,'/api/host/goals',{'entry_id':'entry','input':{}})
                        duration=time.monotonic()-started
                        observations.append({'seconds':duration,'outcome':result['outcome'],'error':result.get('error')})
                        self.assertLess(duration,1)
                        self.assertNotEqual(result['outcome'],'OBSERVED')
                    await asyncio.sleep(2)
                    self.assertFalse(backup.done())
                    release.set()
                    _,result,_=await backup
                    self.assertEqual(result['data']['state'],'COMPLETE',result)
                self.assertTrue(await app.business.business_ready())
                self.assertIsNotNone(app.business.goal_scheduler)
                assert app.business.goal_scheduler is not None
                self.assertFalse(app.business.goal_scheduler.closed)
                started=time.monotonic()
                ws=await asyncio.to_thread(client.websocket)
                await asyncio.to_thread(ws.receive)
                await asyncio.to_thread(ws.send,{'version':1,'type':'subscribe','request_id':'after-backup','route_ids':['backup-route'],'event_types':['goal.due'],'takeover':False})
                self.assertEqual((await asyncio.to_thread(ws.receive))['state'],'SUBSCRIBED')
                notice=await asyncio.wait_for(asyncio.to_thread(ws.receive),20)
                self.assertEqual(notice['event'],'goal.due',notice)
                await asyncio.to_thread(ws.send,{'version':1,'type':'ack','route_id':'backup-route','delivery_id':notice['delivery_id'],'status':'RECEIVED'})
                self.assertEqual((await asyncio.to_thread(ws.receive))['outcome'],'COMMITTED')
                self.assertEqual((len(requests),failures),(1,[]))
                print({'actual_backup':'COMPLETE','controlled_io_hold_seconds':2,'host_during_backup':observations,
                    'reminder_after_reopen_seconds':time.monotonic()-started,'original_goal':'ACKNOWLEDGED','supplier_requests':0})
            finally:
                release.set()
                if backup is not None: await asyncio.gather(backup,return_exceptions=True)
                if ws is not None: await asyncio.to_thread(ws.close)
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()
