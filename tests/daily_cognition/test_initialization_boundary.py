"""Full SQLite host keeps configuration publication separate from business roots."""
import asyncio
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.configuration.daily_persistence import DailyConfigurationBinding
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.command_capacity import declared_capacity
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_assembly import stable
from .test_host import make_host


class InitializationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_interpreter_confirms_roots_after_process_exit_before_owner_binding(self):
        script = r'''
import asyncio,os,sys
from pathlib import Path
from companion_memory.configuration.daily_persistence import DailyConfigurationBinding
from tests.daily_cognition.test_host import make_host
original=DailyConfigurationBinding.initialize_business_roots
async def boundary(self,key,stored):
 if sys.argv[2]=='after_roots':
  await original(self,key,stored)
 os._exit(23)
DailyConfigurationBinding.initialize_business_roots=boundary
asyncio.run(make_host(Path(sys.argv[1]),9,[]).initialize('CREATE_NEW'))
'''
        for window in ('before_roots', 'after_roots'):
            with self.subTest(window=window), TemporaryDirectory() as directory:
                root = Path(directory)
                child = await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(root), window,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(child.communicate(), 30)
                self.assertEqual(child.returncode, 23, (out, err))
                with sqlite3.connect(root / 'database' / 'runtime.sqlite3') as db:
                    configuration_receipt = db.execute("SELECT receipt FROM operation_receipts WHERE operation_kind='initialize_daily_configuration'").fetchone()
                    self.assertIsNotNone(configuration_receipt)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_semantic_publication').fetchone()[0], int(window == 'after_roots'))
                credentials = []; host = make_host(root, 9, credentials)
                try:
                    result = await host.initialize('OPEN_EXISTING')
                    self.assertEqual(host.state, 'READY', (result,host.phase))
                    self.assertFalse(credentials)
                    with sqlite3.connect(root / 'database' / 'runtime.sqlite3') as db:
                        self.assertEqual(db.execute("SELECT receipt FROM operation_receipts WHERE operation_kind='initialize_daily_configuration'").fetchone(), configuration_receipt)
                        self.assertEqual(db.execute('SELECT count(*) FROM memory_semantic_publication').fetchone()[0], 1)
                        self.assertEqual(db.execute('SELECT count(*) FROM retrieval_semantic_control').fetchone()[0], 1)
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0], 0)
                finally:
                    self.assertTrue(await host.close())

    async def test_configuration_only_audit_and_real_ordinary_roots_original_receipts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); credentials = []; original = []
            for mode in ('CREATE_NEW', 'OPEN_EXISTING'):
                host = make_host(root, 9, credentials)
                try:
                    opened=await host.initialize(mode)
                    self.assertIs(type(opened), Found, (opened,host.phase))
                    config = host.combination.configuration.commands[0]
                    roots = host.combination.configuration.root_commands[0]
                    self.assertEqual([r.owner_module for r in config.participants], ['configuration'])
                    self.assertEqual([a.owner_module for a in config.required_audits], ['configuration'])
                    self.assertEqual(declared_capacity(config), 2097152)
                    self.assertIsNone(declared_capacity(roots))
                    self.assertEqual([a.owner_module for a in roots.required_audits], ['memory', 'retrieval'])
                    receipts = []
                    for definition, key in ((config, 'configuration'), (roots, stable('initialize_daily_roots', 'configuration'))):
                        receipt = await host.storage.bind_operation(definition, 'instance').read_receipt(key)
                        self.assertIs(type(receipt), Found)
                        assert type(receipt) is Found
                        receipts.append(receipt.value)
                    self.assertEqual(len(receipts[0].result['targets']), 1)
                    self.assertNotIn('root_facts', receipts[0].result)
                    self.assertEqual(len(receipts[1].result['targets']), 2)
                    for fact in receipts[1].result['root_facts'].values():
                        self.assertEqual(fact['rows_changed'], 1)
                        self.assertEqual(len(fact['targets']), 1)
                    if original:
                        self.assertEqual(receipts, original)
                    else:
                        original = receipts
                    with sqlite3.connect(root / 'database' / 'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM memory_semantic_publication').fetchone()[0], 1)
                        self.assertEqual(db.execute('SELECT count(*) FROM retrieval_semantic_control').fetchone()[0], 1)
                    self.assertFalse(credentials)
                finally:
                    self.assertTrue(await host.close())

    async def test_config_committed_roots_failed_is_not_ready_and_original_key_can_continue(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); credentials = []; host = make_host(root, 9, credentials)
            try:
                with patch.object(DailyConfigurationBinding, 'handle_roots', side_effect=ValueError('Injected root write failure')):
                    result = await host.initialize('CREATE_NEW')
                    self.assertNotEqual(host.state, 'READY')
                config = host.combination.configuration.commands[0]
                before = await host.storage.bind_operation(config, 'instance').read_receipt('configuration')
                self.assertIs(type(before), Found)
                with sqlite3.connect(root / 'database' / 'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM configuration_entries').fetchone()[0], 130)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_semantic_publication').fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT count(*) FROM retrieval_semantic_control').fetchone()[0], 0)
                result = await host.initialize('CREATE_NEW')
                self.assertIs(type(result), Found, result)
                after = await host.storage.bind_operation(config, 'instance').read_receipt('configuration')
                assert type(before) is Found and type(after) is Found
                self.assertEqual(before.value, after.value)
                self.assertFalse(credentials)
            finally:
                self.assertTrue(await host.close())

    async def test_each_committed_step_continues_in_new_interpreter_without_duplicates(self):
        script = r"""
import asyncio,os,sys
from pathlib import Path
from companion_memory.runtime.daily_initialization import DailyInitialization
from tests.daily_cognition.test_host import make_host
original=DailyInitialization.step
async def boundary(self,name,receipt):
 if name==sys.argv[2] and sys.argv[3]=='before':os._exit(23)
 result=await original(self,name,receipt)
 if name==sys.argv[2]:os._exit(23)
 return result
DailyInitialization.step=boundary
asyncio.run(make_host(Path(sys.argv[1]),9,[]).initialize('CREATE_NEW'))
"""
        for name in ('configuration','roots','media','runtime','information','schedule','persona'):
            for window in ('before','after'):
                with self.subTest(name=name,window=window),TemporaryDirectory() as directory:
                    root=Path(directory)
                    child=await asyncio.create_subprocess_exec(sys.executable,'-c',script,directory,name,window,
                        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    out,err=await asyncio.wait_for(child.communicate(),30)
                    self.assertEqual(child.returncode,23,(out,err))
                    path=root/'database'/'runtime.sqlite3'
                    with sqlite3.connect(path) as db:
                        original=dict(db.execute('SELECT operation_kind || operation_key,receipt FROM operation_receipts'))
                    credentials=[];host=make_host(root,9,credentials)
                    try:
                        result=await host.initialize('OPEN_EXISTING')
                        self.assertEqual(host.state,'READY',(result,host.phase))
                        self.assertFalse(credentials)
                        with sqlite3.connect(path) as db:
                            recovered=dict(db.execute('SELECT operation_kind || operation_key,receipt FROM operation_receipts'))
                            self.assertTrue(all(recovered[key]==value for key,value in original.items()))
                            self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                            self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_imports').fetchone()[0],1)
                            self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_publications').fetchone()[0],1)
                            self.assertEqual(db.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],1)
                            self.assertEqual(db.execute("SELECT json_extract(body,'$.state') FROM runtime_daily_initialization").fetchone()[0],'COMPLETE')
                            count=db.execute('SELECT count(*) FROM operation_receipts').fetchone()[0]
                    finally:self.assertTrue(await host.close())
                    reopened=make_host(root,9,credentials)
                    try:
                        self.assertIs(type(await reopened.initialize('OPEN_EXISTING')),Found)
                        with sqlite3.connect(path) as db:
                            self.assertEqual(db.execute('SELECT count(*) FROM operation_receipts').fetchone()[0],count)
                        self.assertFalse(credentials)
                    finally:self.assertTrue(await reopened.close())

    async def test_completed_missing_intent_or_receipt_is_not_repaired(self):
        for sql in ("DELETE FROM runtime_daily_initialization", "DELETE FROM operation_receipts WHERE operation_kind='initialize_daily_roots'",
                    "DELETE FROM operation_receipts WHERE operation_kind='begin_daily_initialization'",
                    "DELETE FROM operation_receipts WHERE operation_kind='record_daily_initialization'"):
            with self.subTest(sql=sql),TemporaryDirectory() as directory:
                root=Path(directory);host=make_host(root,9,[])
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertTrue(await host.close())
                path=root/'database'/'runtime.sqlite3'
                with sqlite3.connect(path) as db:
                    db.execute(sql);db.commit()
                    receipts=db.execute('SELECT count(*) FROM operation_receipts').fetchone()[0]
                    audits=db.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                credentials=[];host=make_host(root,9,credentials)
                try:
                    result=await host.initialize('OPEN_EXISTING')
                    self.assertNotEqual(host.state,'READY',result)
                    with sqlite3.connect(path) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM operation_receipts').fetchone()[0],receipts)
                        self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],audits)
                    self.assertFalse(credentials)
                finally:self.assertTrue(await host.close())

    async def test_physical_media_directory_without_sql_commit_continues_original_intent(self):
        script=r"""
import asyncio,os,sys
from pathlib import Path
from companion_memory.media.service import MediaService
from tests.daily_cognition.test_host import make_host
original=MediaService._execute
async def boundary(self,name,key,values):
 if name=='initialize_media_root':os._exit(23)
 return await original(self,name,key,values)
MediaService._execute=boundary
asyncio.run(make_host(Path(sys.argv[1]),9,[]).initialize('CREATE_NEW'))
"""
        with TemporaryDirectory() as directory:
            child=await asyncio.create_subprocess_exec(sys.executable,'-c',script,directory,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await asyncio.wait_for(child.communicate(),30)
            self.assertEqual(child.returncode,23,(out,err))
            credentials=[];host=make_host(Path(directory),9,credentials)
            try:
                result=await host.initialize('OPEN_EXISTING')
                self.assertEqual(host.state,'READY',(result,host.phase));self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())

    async def test_interrupted_intent_rejects_changed_trusted_initial_self(self):
        from dataclasses import replace
        from companion_memory.memory.initial_self_storage import InitialSelfBinding
        with TemporaryDirectory() as directory:
            root=Path(directory);host=make_host(root,9,[])
            with patch.object(DailyConfigurationBinding,'handle_roots',side_effect=ValueError('Controlled interruption')):
                self.assertNotEqual(type(await host.initialize('CREATE_NEW')),Found)
            self.assertTrue(await host.close())
            credentials=[];host=make_host(root,9,credentials)
            host.resources=replace(host.resources,initial_self=InitialSelfBinding('different-authority','self','Iris','SYNTHETIC_FIXTURE'))
            try:
                result=await host.initialize('OPEN_EXISTING')
                self.assertNotEqual(host.state,'READY',result)
                self.assertEqual(host.phase,'INTENT')
                self.assertFalse(credentials)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_semantic_publication').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0],0)
            finally:self.assertTrue(await host.close())
