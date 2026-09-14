"""Native bounded subject registration, real atomic audits and process recovery."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import subprocess
import sys
import time
import unittest
from companion_memory.persistence import Found,Committed,ResultBoundCommand
from companion_memory.persistence.text_records import stable_identity
from companion_memory.persistence.content_codec import encode_content
from companion_memory.memory.formats import record
from companion_memory.self_model.management import PersonaInitializationPort
from tests.persistence.support import Hooks,sqlite_fault
from tests.text_learning.test_text_host import make_host
from tests.text_learning.host_driver import confirm_local
from tests.text_learning.initial_subjects_support import roster,join


class InitialSubjectsTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self,root: Path,hooks: Hooks|None=None):
        host=make_host(root,1)
        if hooks is not None:host.resources=replace(host.resources,database=replace(host.resources.database,connect=hooks.connect))
        self.assertIs(type(await confirm_local(lambda:host.initialize('CREATE_NEW'))),Found)
        port=host.initialization_port()
        self.assertIs(type(await confirm_local(lambda:port.register_initial_self('input','PRESET','Synthetic operator input.','ACTUAL_INPUT',time.monotonic()+5))),Committed)
        await join(host)
        return host,port

    async def test_maximum_roster_queries_audits_and_original_confirmation_after_prepare(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();host,port=await self.ready(root)
            try:
                values=roster(maximum=True)
                saved=await confirm_local(lambda:port.register_initial_subjects('roster',values,'ACTUAL_INPUT',time.monotonic()+5))
                await join(host);self.assertIs(type(saved),Committed,saved);assert type(saved) is Committed
                self.assertEqual(record(record(record(saved.receipt.result)['facts'])['memory'])['rows_changed'],6)
                self.assertLessEqual(len(encode_content(saved.receipt.result,8192)),8192)
                assert host.runtime is not None
                read=host.runtime.memory.bind_read(tuple(str(s['subject_id']) for s in values),('read_subject',))
                for subject in values:
                    found=await read.read_subject(str(subject['subject_id']))
                    self.assertIs(type(found),Found,found)
                assert host.stored is not None
                input_id=stable_identity('self-input',host.stored.database_id,'instance')
                prepared=await confirm_local(lambda:port.prepare_initial_persona('prepare',input_id,1,host.gate.epoch,time.monotonic()+5))
                await join(host);self.assertIs(type(prepared),Committed,prepared)
                replay=await confirm_local(lambda:port.register_initial_subjects('roster',values,'ACTUAL_INPUT',time.monotonic()+5))
                await join(host);self.assertIs(type(replay),Committed,replay);assert type(replay) is Committed
                self.assertEqual(saved.receipt,replay.receipt)
                refused=await port.register_initial_subjects('new-key',values,'ACTUAL_INPUT',time.monotonic()+5)
                await join(host);self.assertIsNot(type(refused),Committed)
                with sqlite3.connect(root/'database/runtime.sqlite3') as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],7)
                    self.assertEqual(c.execute("SELECT count(*) FROM audit_records JOIN operation_receipts USING(commit_id) WHERE operation_kind='register_initial_subjects'").fetchone()[0],1)
            finally:self.assertTrue(await host.close())

    async def test_invalid_authority_shape_origin_identity_and_second_registration_write_nothing(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();host,port=await self.ready(root)
            try:
                original=roster()
                invalid=((),original+(original[0],),({**original[0],'kind':'SELF','platform_id':None,'external_subject_id':None},),
                    ({**original[0],'revision':2},),({**original[0],'label':'x'*257},),
                    ({**original[0],'subject_id':'s'*128,'external_subject_id':'e'*512,'label':'x'*256},),({**original[0],'instance_id':'elsewhere'},),
                    (original[0],original[0]),(original[0],{**original[1],'external_subject_id':original[0]['external_subject_id']}))
                for index,values in enumerate(invalid):
                    result=await port.register_initial_subjects('invalid-'+str(index),values,'ACTUAL_INPUT',time.monotonic()+5)
                    await join(host);self.assertIsNot(type(result),Committed,result)
                result=await port.register_initial_subjects('bad-origin',original,'SYNTHETIC_FIXTURE',time.monotonic()+5)
                await join(host);self.assertIsNot(type(result),Committed)
                forged=object.__new__(PersonaInitializationPort);object.__setattr__(forged,'_owner',host.persona)
                self.assertIsNot(type(await forged.register_initial_subjects('forged',original,'ACTUAL_INPUT',time.monotonic()+5)),Committed)
                definition=host.combination.persona.definitions['register_initial_subjects']
                operation=host.storage.bind_operation(definition,'instance')
                direct=await operation.execute('direct',ResultBoundCommand(1,{'operation_id':'direct','subjects':original,'input_origin':'ACTUAL_INPUT'},
                    {'memory_text_learning':{'actor':'local-operator'}}))
                self.assertIsNot(type(direct),Committed,direct)
                with sqlite3.connect(root/'database/runtime.sqlite3') as c:self.assertEqual(c.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],1)
                saved=await confirm_local(lambda:port.register_initial_subjects('first',original[:1],'ACTUAL_INPUT',time.monotonic()+5))
                await join(host);self.assertIs(type(saved),Committed,saved)
                again=await port.register_initial_subjects('second',original[1:],'ACTUAL_INPUT',time.monotonic()+5)
                await join(host);self.assertIsNot(type(again),Committed,again)
            finally:self.assertTrue(await host.close())

    async def test_subject_audit_and_receipt_failures_roll_back_whole_roster(self):
        for statement in ('INSERT INTO memory_subjects','INSERT INTO required_audit_events','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
            with self.subTest(statement=statement),TemporaryDirectory() as directory:
                root=Path(directory).resolve();hooks=Hooks();host,port=await self.ready(root,hooks);seen=[]
                try:
                    def fail(sql):
                        if sql.startswith(statement):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_FULL)
                    hooks.before=fail
                    value=await port.register_initial_subjects('fault',roster(),'ACTUAL_INPUT',time.monotonic()+5)
                    await join(host);hooks.before=lambda _:None
                    self.assertTrue(seen);self.assertIsNot(type(value),Committed,value)
                    with sqlite3.connect(root/'database/runtime.sqlite3') as c:
                        self.assertEqual(c.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],1)
                        self.assertEqual(c.execute("SELECT count(*) FROM audit_records JOIN operation_receipts USING(commit_id) WHERE operation_kind='register_initial_subjects'").fetchone()[0],0)
                finally:hooks.before=lambda _:None;self.assertTrue(await host.close())

    async def test_fresh_process_confirms_same_roster_receipt_without_model(self):
        with TemporaryDirectory() as directory:
            outputs=[]
            for mode in ('CREATE_NEW','OPEN_EXISTING','OPEN_EXISTING'):
                run=subprocess.run([sys.executable,'-m','tests.text_learning.initial_subjects_support',str(Path(directory).resolve()),mode],capture_output=True,text=True,timeout=45)
                self.assertEqual(run.returncode,0,run.stderr);outputs.append(run.stdout.strip().split())
            self.assertEqual([o[0] for o in outputs],['NEW','EXISTING','EXISTING'])
            self.assertEqual(len({o[1] for o in outputs}),1)
