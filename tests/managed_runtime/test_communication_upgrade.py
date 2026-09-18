"""Populated predecessor upgrade with retained original business bytes and cuts."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import sys
import unittest
from typing import cast
from clients.iris_client import IrisClient
from companion_memory.persistence import Committed, Ready
from companion_memory.memory.formats import record
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_upgrade import CommunicationUpgrade
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.runtime.managed_resources import read_record
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from tests.runtime.configuration_support import event
from .communication_live_support import ready_application
from .test_business import controlled_resources


def rows(database: Path):
    with closing(sqlite3.connect(database)) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name!='application_metadata'")]
        return {name: tuple(db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')) for name in names}


class CommunicationUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_populated_current_format_and_interrupted_switch(self):
        for point in ('before_fence', 'after_fence', 'after_database', 'after_identity', 'after_active'):
            with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
                root = Path(directory) / 'instance'
                app, http = await ready_application(self, root, provider_port, communication_format=False, legacy_test_sink=True)
                async def cleanup(application=app, listener=http):
                    await listener.close()
                    while not await application.business.close(): await asyncio.sleep(.05)
                    await application.bootstrap.close()
                self.addAsyncCleanup(cleanup)
                settings = app.bootstrap.settings
                _, token = await app.identity.create_token('old-token', 'host', ('entry',), ('accept', 'confirm', 'goal_write', 'goal_read'),
                    time.time_ns() // 1000 + 300000000)
                assert token is not None
                client = IrisClient('http://127.0.0.1:18180', token)
                value = event('upgrade-event', '保留的存量原始业务输入。');value['event_version'] = 2
                original = {'entry_id': 'entry', 'input': {'key': 'old-input', 'event': value}}
                accepted = await asyncio.to_thread(client.request, '/api/host/accept', original)
                self.assertEqual(accepted['outcome'], 'COMMITTED', accepted)
                created = await asyncio.to_thread(client.request, '/api/host/goals/inject', {'entry_id': 'entry', 'input': {
                    'operation_key': 'old-goal', 'content': '存量目标，升级后保留。', 'subject_ids': ['self'],
                    'world_scope': 'REAL', 'deadline': None, 'reminder_lead_seconds': None, 'route_id': None, 'source_id': 'old-source'}})
                self.assertEqual(created['outcome'], 'COMMITTED', created)
                host = app.business.host
                assert host is not None
                from companion_memory.information.management import HostIdentity
                from companion_memory.information.records import identity as goal_identity
                assert host.goals is not None
                operations = frozenset(('goal_inject_external', 'goal_dedup_claim', 'goal_dedup_finish', 'goal_attempt_begin', 'goal_attempt_finish'))
                port = await host.bind_management(HostIdentity('old-unknown', 'old-host', 'host', 'entry', operations, ('old-route',), time.monotonic()+300))
                for number in range(2):
                    injected = await port.execute('goal_inject_external', 'old-deadline-'+str(number), {
                        'content': '待确认原目标 '+str(number), 'subject_ids': (), 'world_scope': 'REAL',
                        'deadline': time.time_ns()//1000-1000000, 'reminder_lead_seconds': 0,
                        'route_id': 'old-route', 'source_id': 'old-deadline-source-'+str(number)})
                    self.assertIs(type(injected), Committed, injected)
                    assert type(injected) is Committed
                    goal_id = str(record(record(record(injected.receipt.result)['facts'])['goals'])['object_id'])
                    task_id = goal_identity('goal_dedup', goal_id)
                    for kind, key, payload in (
                        ('goal_dedup_claim', 'old-claim-'+str(number), {'task_id': task_id, 'expected_revision': 1, 'owner_id': 'old-worker'}),
                        ('goal_dedup_finish', 'old-dedup-'+str(number), {'task_id': task_id, 'expected_revision': 2, 'owner_id': 'old-worker', 'status': 'DISTINCT'})):
                        self.assertIs(type(await port.execute(kind, key, payload)), Committed)
                    plan = next(p for p in await host.goals.due_plans(time.time_ns()//1000) if p['goal_id'] == goal_id)
                    registered, intent = await host.management.register_reminder(port, str(plan['plan_id']), cast(int, plan['revision']), 'old-attempt-'+str(number))
                    self.assertIs(type(registered), Committed, registered)
                    if number == 0:
                        self.assertIs(type(await port.execute('goal_attempt_finish', 'old-unknown-finish', {
                            'delivery_id': intent['delivery_id'], 'expected_revision': 1, 'state': 'UNKNOWN', 'reason': 'COMMIT_UNCONFIRMED'})), Committed)
                upload = host.media.bind_upload('entry')
                started = await upload.begin_upload('old-image', 'IMAGE')
                self.assertIs(type(started), Committed, started)
                assert type(started) is Committed
                uid = str(record(started.receipt.result)['upload_id'])
                await upload.append_upload(uid, 0, b'preserved synthetic original media')
                finished = await upload.finish_upload(uid)
                self.assertIs(type(finished), Committed, finished)
                self.assertTrue(await http.close())
                while not await app.business.close(): await asyncio.sleep(.05)
                self.assertTrue(await app.bootstrap.close())
                before = rows(root / 'db/memory.sqlite3')
                blob_hashes = {str(p.relative_to(root)): p.read_bytes() for p in (root / 'blobs').rglob('*') if p.is_file()}
                self.assertTrue(blob_hashes)
                upgrade = CommunicationUpgrade(settings)
                prepared = await asyncio.to_thread(upgrade.prepare, 'same-upgrade-key')
                self.assertEqual(prepared['state'], 'PREPARED')
                upgrade.close()
                child = await asyncio.create_subprocess_exec(sys.executable, '-m', 'tests.managed_runtime.upgrade_cut',
                    str(root), 'same-upgrade-key', point)
                self.assertEqual(await child.wait(), 86)
                state = read_record(root / 'bootstrap/communication-upgrade.json')['state']
                if state not in ('PREPARED', 'ACTIVE'):
                    with self.assertRaises(ValueError):
                        candidate = ManagedBootstrap(settings)
                        await candidate.open()
                upgrade = CommunicationUpgrade(settings)
                result = await asyncio.to_thread(upgrade.activate, 'same-upgrade-key')
                self.assertEqual(result['state'], 'ACTIVE')
                upgrade.close()
                after = rows(root / 'db/memory.sqlite3')
                for name, values in before.items(): self.assertEqual(after[name], values, name)
                self.assertEqual({name: (root / name).read_bytes() for name in blob_hashes}, blob_hashes)
                with self.assertRaises(ValueError):
                    await ManagedBootstrap(settings, communication_format=False).open()
                bootstrap = ManagedBootstrap(settings)
                self.assertIs(type(await bootstrap.open()), Ready)
                reopened = ManagedApplication(bootstrap, resource_factory=controlled_resources(provider_port))
                recovered = await reopened.business.recover()
                if reopened.business.task is not None: recovered = await reopened.business.task
                self.assertTrue(await reopened.business.business_ready(), recovered)
                self.assertFalse(reopened.business.communication.available())
                with closing(sqlite3.connect(root / 'db/memory.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT state FROM goals_attempt ORDER BY delivery_id').fetchall(), [('UNKNOWN',), ('UNKNOWN',)])
                original_again = await reopened.host_http.dispatch(await reopened.identity.authenticate(token, host=True), 'POST', '/api/host/accept/resolve', original)
                assert type(original_again) is Committed
                self.assertEqual(original_again.receipt.commit_id, accepted['data']['receipt']['commit_id'])
                self.assertEqual(len(requests), 1)
                self.assertEqual(failures, [])
                while not await reopened.business.close(): await asyncio.sleep(.05)
                self.assertTrue(await bootstrap.close())
                if point == 'after_active':
                    await self.check_new_data_backup_restore(settings, root, provider_port, token, original, accepted['data']['receipt']['commit_id'])
                    self.assertEqual(len(requests), 1)
                print({'cut': point, 'old_tables': len(before), 'preserved_rows': sum(map(len, before.values())),
                    'media_files': len(blob_hashes), 'original_commit': accepted['data']['receipt']['commit_id'],
                    'legacy_reopen': 'REFUSED', 'supplier_requests': 0, 'new_model_requests_after_upgrade': 0})

    async def check_new_data_backup_restore(self, settings, root, provider_port, token, original, original_commit):
        from companion_memory.persistence.managed_backup import ConsistentBackup
        from companion_memory.runtime.managed_restore import RestoreSwitch
        import os
        bootstrap=ManagedBootstrap(settings)
        self.assertIs(type(await bootstrap.open()),Ready)
        app=ManagedApplication(bootstrap,resource_factory=controlled_resources(provider_port))
        try:
            await app.business.recover()
            if app.business.task is not None: await app.business.task
            principal=await app.identity.authenticate(token,host=True)
            fresh={'entry_id':'entry','input':{'key':'after-upgrade-new-input','event':event('new-after-upgrade','升级后新增业务')|{'event_version':2}}}
            created=await app.host_http.dispatch(principal,'POST','/api/host/accept',fresh)
            self.assertIs(type(created),Committed,created)
            assert type(created) is Committed
            new_commit=created.receipt.commit_id
            while not await app.business.close(): await asyncio.sleep(.05)
            app.identity.close()
            resources=bootstrap.resources;assert resources is not None
            copier=ConsistentBackup(resources,lambda:bootstrap.assembly.storage.get_health().lifecycle=='CLOSED')
            completed=await asyncio.to_thread(copier.create,'post-upgrade-with-new-data')
            manifest=await asyncio.to_thread(copier.verify,completed)
            self.assertFalse(manifest['secrets_included'])
        finally:
            await app.business.close();await bootstrap.close()
        # A pre-upgrade backup is not a lossless code rollback after new writes.
        with self.assertRaises(ValueError): await ManagedBootstrap(settings,communication_format=False).open()
        target=root.parent/'restored'
        switch=RestoreSwitch(settings,bootstrap.assembly_digest)
        try:
            intent=await asyncio.to_thread(switch.prepare,'post-upgrade-with-new-data',target)
            await asyncio.to_thread(switch.activate,target,str(intent['switch_id']))
        finally: switch.close()
        os.rename(root,root.parent/'retired-upgraded');os.rename(target,root)
        restored=ManagedBootstrap(settings)
        self.assertIs(type(await restored.open()),Ready)
        app=ManagedApplication(restored,resource_factory=controlled_resources(provider_port))
        try:
            await app.business.recover()
            if app.business.task is not None: await app.business.task
            principal=await app.identity.authenticate(token,host=True)
            for payload,commit in ((original,original_commit),(fresh,new_commit)):
                confirmed=await app.host_http.dispatch(principal,'POST','/api/host/accept/resolve',payload)
                self.assertIs(type(confirmed),Committed,confirmed)
                assert type(confirmed) is Committed
                self.assertEqual(confirmed.receipt.commit_id,commit)
            host=app.business.host;assert host is not None
            from companion_memory.persistence import Found
            self.assertIs(type(await host.media.bind_upload('entry').resolve_upload('old-image','IMAGE')),Found)
            self.assertEqual(len(app.business.communication.connections),0)
            self.assertFalse(app.business.sends_enabled)
            print({'upgrade_backup_restore':'POPULATED_NEW_WRITES_PRESERVED','legacy_code_reopen':'REFUSED',
                'restored_sockets':0,'restored_tickets':len(app.business.communication.tickets)})
        finally:
            while not await app.business.close(): await asyncio.sleep(.05)
            await restored.close()

    async def test_preparation_process_cut_original_abort_and_compatible_restart(self):
        for point in ('after_backup','after_prepare'):
            with TemporaryDirectory() as directory,responses((persona,)) as (provider_port,requests,failures):
                root=Path(directory)/'instance'
                app,http=await ready_application(self,root,provider_port,communication_format=False,port=18188)
                settings=app.bootstrap.settings
                host=app.business.host;assert host is not None
                accepted=await host.bind_entry('entry').accept_event('retained-before-prepare',event('retained','准备中保留原数据')|{'event_version':2})
                self.assertIs(type(accepted),Committed,accepted)
                await http.close()
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()
                before=rows(root/'db/memory.sqlite3')
                child=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.managed_runtime.upgrade_cut',str(root),'prepare-original',point)
                self.assertEqual(await child.wait(),86)
                upgrade=CommunicationUpgrade(settings)
                try:
                    result=await asyncio.to_thread(upgrade.prepare,'prepare-original')
                    self.assertEqual(result['state'],'PREPARED')
                    self.assertEqual((await asyncio.to_thread(upgrade.abort,'prepare-original'))['state'],'ABORTED')
                finally: upgrade.close()
                old=ManagedBootstrap(settings,communication_format=False)
                self.assertIs(type(await old.open()),Ready)
                await old.close()
                self.assertEqual(rows(root/'db/memory.sqlite3'),before)
                self.assertEqual((len(requests),failures),(1,[]))
                print({'preparation_cut':point,'original_resume':'PREPARED','abort':'OLD_FORMAT_STARTABLE','business_rows':'UNCHANGED'})

    async def test_stale_preparation_rejects_native_commit_left_only_in_wal(self):
        from hashlib import sha256
        import json
        with TemporaryDirectory() as directory,responses((persona,)) as (provider_port,requests,failures):
            root=Path(directory)/'instance'
            app,http=await ready_application(self,root,provider_port,communication_format=False,port=18188)
            settings=app.bootstrap.settings
            await http.close()
            while not await app.business.close():await asyncio.sleep(.05)
            await app.bootstrap.close()
            upgrade=CommunicationUpgrade(settings)
            try:prepared=await asyncio.to_thread(upgrade.prepare,'wal-upgrade')
            finally:upgrade.close()
            database=root/'db/memory.sqlite3'
            digest=lambda:sha256(database.read_bytes()).hexdigest()
            self.assertEqual(digest(),prepared['source_sha256'])
            child=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.managed_runtime.upgrade_wal_child',str(root),stdout=asyncio.subprocess.PIPE)
            stdout,_=await child.communicate();self.assertEqual(child.returncode,86,stdout)
            commit=json.loads(stdout)['commit_id']
            self.assertGreater(Path(str(database)+'-wal').stat().st_size,32)
            self.assertEqual(digest(),prepared['source_sha256'],'Trigger requires an unchanged main file and new durable WAL frames.')
            upgrade=CommunicationUpgrade(settings)
            try:
                with self.assertRaisesRegex(ValueError,'Source changed'):
                    await asyncio.to_thread(upgrade.activate,'wal-upgrade')
                self.assertEqual(read_record(upgrade.intent_path)['state'],'PREPARED')
                self.assertNotEqual(read_record(root/'bootstrap/identity.json')['state'],'RETIRED')
                self.assertNotEqual(digest(),prepared['source_sha256'])
                self.assertFalse(Path(str(database)+'-wal').exists())
                await asyncio.to_thread(upgrade.abort,'wal-upgrade')
            finally:upgrade.close()
            old=ManagedBootstrap(settings,communication_format=False)
            self.assertIs(type(await old.open()),Ready)
            recovered=ManagedApplication(old,resource_factory=controlled_resources(provider_port))
            try:
                await recovered.business.recover()
                if recovered.business.task is not None:await recovered.business.task
                host=recovered.business.host;assert host is not None
                result=await host.bind_entry('entry').confirm_acceptance('wal-only-key',event('wal-only-event','崩溃前已提交且必须保留的输入')|{'event_version':2})
                self.assertIs(type(result),Committed,result)
                assert type(result) is Committed
                self.assertEqual(result.receipt.commit_id,commit)
            finally:
                while not await recovered.business.close():await asyncio.sleep(.05)
                await old.close()
            self.assertEqual((len(requests),failures),(1,[]))
            print({'native_child_exit':86,'wal_only_commit':commit,'stale_candidate':'REFUSED_BEFORE_FENCE','original_confirmation':'PRESERVED','supplier_requests':0})
