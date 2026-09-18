"""Persisted maintenance cut before configuration preparation, with live scheduler."""
import asyncio
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.notification_routes import NotificationRoutes
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.persistence import Committed,Ready
from companion_memory.memory.formats import record
from companion_memory.goals.communication_ledger import grace_from
from companion_memory.runtime.communication_gate import clock_from
from clients.iris_client import IrisClient
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application,enable_communication,confirm_original
from .test_business import controlled_resources


class ActivationClockRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_with_durable_pause_reopens_and_keeps_remaining_window(self):
        with TemporaryDirectory() as directory,responses((persona,)) as (provider_port,requests,failures):
            app,http=await ready_application(self,Path(directory)/'instance',provider_port,port=18190)
            now=[time.time_ns()]
            with patch('time.time_ns',side_effect=lambda:now[0]):
                try:
                    await enable_communication(self,app)
                    routes=NotificationRoutes(app.identity)
                    await confirm_original(self,lambda:routes.create('route','offline','host',('entry',),('goal.due',)))
                    await confirm_original(self,lambda:routes.enable('enable','offline',1,True))
                    _,token=await app.identity.create_token('goal-token','host',('entry',),('goal_write',),now[0]//1000+3600000000,route_ids=('offline',),event_types=('goal.due',))
                    assert token is not None
                    reply=await asyncio.to_thread(IrisClient('http://127.0.0.1:18190',token).request,'/api/host/goals/inject',{'entry_id':'entry','input':{
                        'operation_key':'original-goal','content':'激活中保留原窗口','subject_ids':['self'],'world_scope':'REAL',
                        'deadline':datetime.fromtimestamp(now[0]/1e9-1,timezone.utc).isoformat(),'reminder_lead_seconds':0,
                        'route_id':'offline','source_id':'source'}})
                    self.assertEqual(reply['outcome'],'COMMITTED',reply)
                    host=app.business.host;manager=app.business.configuration
                    assert host is not None and manager is not None
                    ledger=host.combination.communication_ledger;assert ledger is not None
                    grace=await ledger.rows.page('communication_grace','')
                    for _ in range(100):
                        grace=await ledger.rows.page('communication_grace','')
                        if grace: break
                        await asyncio.sleep(.1)
                    self.assertTrue(grace)
                    original=record(grace[0]['value']);window=grace_from(original)
                    status=await manager.status();revision=status['revision']
                    change: dict[str, object]={'platform':{'platforms.sample_platform.buffer.target_count':1}}
                    _,plan=await manager.preview(revision,change)
                    saved=await manager.save('same-activation',revision,change,plan['plan_digest'],actor='administrator',reason='合成持久切点')
                    self.assertIs(type(saved),Committed,saved);assert type(saved) is Committed
                    aid=str(record(saved.receipt.result)['activation_id'])
                    now[0]+=100000000000
                    await manager.maintenance_clock(aid,True)
                    self.assertTrue(await ledger.synchronize())
                    # The process can end after the durable gate while the
                    # candidate is still unprepared and no active pointer changed.
                    activation=await manager.versions.rows.read('managed_activations',aid)
                    assert activation is not None
                    self.assertEqual(activation['state'],'CANDIDATE')
                    settings=app.bootstrap.settings
                    await http.close()
                    while not await app.business.close(): await asyncio.sleep(.05)
                    await app.bootstrap.close()
                    now[0]+=400000000000
                    bootstrap=ManagedBootstrap(settings)
                    self.assertIs(type(await bootstrap.open()),Ready)
                    app=ManagedApplication(bootstrap,resource_factory=controlled_resources(provider_port))
                    await app.business.recover()
                    if app.business.task is not None: await app.business.task
                    self.assertTrue(await app.business.business_ready())
                    host=app.business.host;manager=app.business.configuration
                    assert host is not None and manager is not None
                    ledger=host.combination.communication_ledger;assert ledger is not None
                    self.assertTrue(await ledger.synchronize())
                    resumed=await ledger.rows.read('communication_grace',str(original['plan_id']))
                    assert resumed is not None
                    self.assertEqual(record(resumed['value']),original)
                    clock=await ledger.rows.read('communication_clock','communication-clock');assert clock is not None
                    self.assertEqual(window.remaining(clock_from(record(clock['value'])),now[0]//1000)[0],200000000)
                    self.assertEqual((await manager.coordinator.activate(aid,timeout_seconds=10))['state'],'APPLIED')
                    self.assertEqual((len(requests),failures),(1,[]))
                    print({'configuration_cut':'CANDIDATE_AFTER_DURABLE_PAUSE','downtime_seconds':400,
                        'remaining_us':200000000,'original_grace':'BYTE_EQUIVALENT','supplier_requests':0})
                finally:
                    await http.close()
                    while not await app.business.close(): await asyncio.sleep(.05)
                    await app.bootstrap.close()
