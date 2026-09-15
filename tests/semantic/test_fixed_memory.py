"""Native fixed establishment: two audits, rollback and original-key recovery."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from companion_memory.persistence import Committed
from companion_memory.memory.source_access import SourceAccess
from companion_memory.persistence import Found
from companion_memory.memory.formats import record,sequence
from companion_memory.persistence.semantic_records import string
from tests.persistence.support import sqlite_fault
from tests.semantic.fixed_support import FixedFixture


class FixedMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_fixed_set_creation_two_audits_and_original_key(self):
        with TemporaryDirectory() as directory:
            fixture=await FixedFixture(Path(directory)).open()
            try:
                await fixture.prepare()
                result=None
                for ordinal in range(12):
                    envelope=fixture.establishment(ordinal)
                    result=await fixture.port.establish_one(envelope,time.monotonic()+5)
                    self.assertIs(type(result),Committed,result)
                    replay=await fixture.port.resolve('fixed_establish',envelope,time.monotonic()+5)
                    self.assertIs(type(replay),Committed,replay)
                    assert type(result) is Committed and type(replay) is Committed
                    self.assertEqual(result.receipt,replay.receipt)
                    conflicting=await fixture.port.establish_one(fixture.establishment(ordinal,'new-key:'+str(ordinal)),time.monotonic()+5)
                    self.assertIsNot(type(conflicting),Committed)
                    self.assertEqual(set(record(record(result.receipt.result)['facts'])),{'cognition','memory'})
                with sqlite3.connect(fixture.root/'database/runtime.sqlite3') as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_objects').fetchone()[0],12)
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_sources WHERE batch_id IS NULL AND holder_count=1').fetchone()[0],12)
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_semantic_gap').fetchone()[0],12)
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_source_members').fetchone()[0],0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM logging_object_history').fetchone()[0],0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM audit_records JOIN operation_receipts USING(commit_id) WHERE operation_kind='fixed_establish'").fetchone()[0],24)
                    self.assertEqual(connection.execute("SELECT json_extract(body,'$.state') FROM cognition_fixed_memory_set").fetchone()[0],'ESTABLISHED')
                assert type(result) is Committed
                oid,sid=tuple(string(item) for item in sequence(record(result.receipt.result)['items']))
                access=SourceAccess(fixture.memory,None);grant=access.bind_inspection((oid,))
                source=await grant.read_source_manifest(oid,sid);self.assertIs(type(source),Found,source)
                assert type(source) is Found
                self.assertEqual(source.value['format'],'FIXED_REVIEWED_TEXT_V1')
                self.assertEqual(source.value['review_digest'],fixture.grant.claims['review_digest'])
                access.close()
                from companion_memory.memory.recovery import MemoryRecovery
                recovered=await MemoryRecovery(fixture.memory,None).advance()
                self.assertIs(type(recovered),Found,recovered)
                assert type(recovered) is Found
                self.assertEqual(recovered.value['state'],'MEMORY_VERIFIED')
            finally:await fixture.close()

    async def test_each_required_audit_failure_rolls_back_member_source_and_gap(self):
        for fragment in ('INSERT INTO required_audit_events','INSERT INTO audit_records','INSERT INTO operation_receipts'):
            for occurrence in ((1,2) if fragment=='INSERT INTO audit_records' else (1,)):
                with self.subTest(fragment=fragment,occurrence=occurrence),TemporaryDirectory() as directory:
                    fixture=await FixedFixture(Path(directory)).open()
                    try:
                        await fixture.prepare();seen=[]
                        def fail(sql):
                            if sql.startswith(fragment):
                                seen.append(sql)
                                if len(seen)==occurrence:raise sqlite_fault(sqlite3.SQLITE_FULL)
                        fixture.hooks.before=fail
                        value=await fixture.port.establish_one(fixture.establishment(0),time.monotonic()+5)
                        fixture.hooks.before=lambda _:None
                        self.assertGreaterEqual(len(seen),occurrence);self.assertIsNot(type(value),Committed,value)
                        with sqlite3.connect(fixture.root/'database/runtime.sqlite3') as connection:
                            for table in ('memory_objects','memory_sources','memory_links','memory_source_holders','memory_semantic_gap'):
                                self.assertEqual(connection.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
                            self.assertEqual(connection.execute("SELECT json_extract(body,'$.established_members') FROM cognition_fixed_memory_set").fetchone()[0],0)
                    finally:fixture.hooks.before=lambda _:None;await fixture.close()

    async def test_new_interpreter_confirms_partial_original_receipt_without_recreation(self):
        with TemporaryDirectory() as directory:
            outputs=[]
            for mode in ('CREATE_NEW','OPEN_EXISTING','OPEN_EXISTING'):
                result=subprocess.run([sys.executable,'-m','tests.semantic.fixed_recovery_worker',directory,mode],capture_output=True,text=True,timeout=45)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr);outputs.append(json.loads(result.stdout))
            self.assertEqual(len({value['fingerprint'] for value in outputs}),1)
            self.assertEqual([value['objects'] for value in outputs],[1,1,1])
            self.assertEqual([value['state'] for value in outputs],['SEALED']*3)
