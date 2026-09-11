"""Real disposable SQLite lock/full/read-only errors, never fabricated success codes."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import sys
import unittest
from companion_memory.persistence import DatabaseResources
from companion_memory.runtime import IngressPort
from companion_memory.runtime.results import Committed,NotCommitted,Rejected,Unconfirmed
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event
from tests.provider.support import record


class RealStorageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_process_write_lock_never_confirms_acceptance(self):
        with TemporaryDirectory(prefix='iris-runtime-lock-') as directory:
            fixture=Fixture(Path(directory));await fixture.initialize()
            locker=None
            try:
                _,port=await fixture.entry();assert type(port) is IngressPort
                code='import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("BEGIN IMMEDIATE"); print("LOCKED",flush=True); sys.stdin.readline(); c.rollback(); c.close()'
                locker=await asyncio.create_subprocess_exec(sys.executable,'-c',code,str(fixture.path),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                assert locker.stdout is not None and locker.stdin is not None
                async with asyncio.timeout(5):self.assertEqual(await locker.stdout.readline(),b'LOCKED\n')
                result=await port.accept_event(event('locked'))
                self.assertIs(type(result),Rejected,result);assert type(result) is Rejected
                self.assertEqual(result.error.reason,'LOCK_BUSY')
                locker.stdin.write(b'RELEASE\n');await locker.stdin.drain()
                async with asyncio.timeout(5):self.assertEqual(await locker.wait(),0)
                result=await port.accept_event(event('locked'));assert type(result) is Committed,result
                self.assertEqual(record(result.receipt.result)['sequence'],1)
            finally:
                if locker is not None and locker.returncode is None:locker.kill();await locker.wait()
                await fixture.close()

    async def engine_failure(self,kind:str):
        with TemporaryDirectory(prefix='iris-runtime-engine-') as directory:
            fixture=Fixture(Path(directory));armed=False;actual_codes=[]
            class Connection(sqlite3.Connection):
                def execute(self,sql,parameters=(),/):
                    if armed and sql=='BEGIN IMMEDIATE':
                        if kind=='full':
                            pages=super().execute('PRAGMA page_count').fetchone()[0]
                            super().execute('PRAGMA max_page_count='+str(pages))
                        else:super().execute('PRAGMA query_only=ON')
                    try:return super().execute(sql,parameters)
                    except sqlite3.Error as failure:
                        actual_codes.append(failure.sqlite_errorcode)
                        raise
            def connect(database,**kwargs):return sqlite3.connect(database,factory=Connection,**kwargs)
            fixture.resources=DatabaseResources('runtime-database',lambda identity,path:json.loads(fixture.identity_path.read_text())=={'identity':identity,'path':path},connect=connect)
            await fixture.initialize()
            try:
                _,port=await fixture.entry();assert type(port) is IngressPort
                armed=True
                confirmed=0
                for index in range(64):
                    result=await port.accept_event(event('fill:'+str(index),'x'*750))
                    if type(result) is Committed:confirmed+=1
                    else:
                        self.assertIn(type(result),(NotCommitted,Rejected,Unconfirmed));break
                else:self.fail('The real SQLite bound did not fail within the finite sample.')
                expected=sqlite3.SQLITE_FULL if kind=='full' else sqlite3.SQLITE_READONLY
                self.assertIn(expected,[code&255 for code in actual_codes])
                armed=False
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM ingress_events').fetchone()[0],confirmed)
                    self.assertEqual(connection.execute('SELECT count(*) FROM buffers_positions').fetchone()[0],confirmed)
                    self.assertEqual(connection.execute("SELECT count(*) FROM operation_receipts WHERE operation_kind='accept'").fetchone()[0],confirmed)
            finally:armed=False;await fixture.close()
    async def test_actual_sqlite_full_via_disposable_database_page_limit(self):await self.engine_failure('full')
    async def test_actual_engine_read_only_connection(self):await self.engine_failure('readonly')

    async def test_actual_missing_directory_rejects_new_io_without_recreating_it(self):
        with TemporaryDirectory(prefix='iris-runtime-path-') as directory:
            fixture=Fixture(Path(directory));await fixture.initialize()
            old=fixture.path.parent;moved=old.with_name('moved-database')
            try:
                _,port=await fixture.entry();assert type(port) is IngressPort
                old.rename(moved)
                result=await port.accept_event(event('unavailable'))
                self.assertIsNot(type(result),Committed,result)
                self.assertFalse(old.exists())
            finally:
                if moved.exists():moved.rename(old)
                await fixture.close()

    async def test_required_audit_failure_rolls_back_every_acceptance_participant(self):
        from tests.persistence.support import sqlite_fault
        with TemporaryDirectory(prefix='iris-runtime-audit-failure-') as directory:
            fixture=Fixture(Path(directory));await fixture.initialize()
            try:
                _,port=await fixture.entry();assert type(port) is IngressPort
                def fail(sql):
                    if sql.startswith('INSERT INTO audit_records'):raise sqlite_fault(sqlite3.SQLITE_FULL)
                fixture.hooks.before=fail
                result=await port.accept_event(event('required-audit'))
                self.assertIn(type(result),(NotCommitted,Unconfirmed),result)
                fixture.hooks.before=lambda sql:None
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM ingress_events').fetchone()[0],0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM buffers_positions').fetchone()[0],0)
                if type(result) is NotCommitted:
                    accepted=await port.accept_event(event('required-audit'));assert type(accepted) is Committed,accepted
                    self.assertEqual(record(accepted.receipt.result)['entry_seq'],1)
            finally:fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_audit_and_rollback_failure_stays_unknown_then_confirms_no_acceptance(self):
        from tests.persistence.support import sqlite_fault
        from companion_memory.ingress.events import event_identity,isolate_event
        with TemporaryDirectory(prefix='iris-runtime-rollback-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                original=event('rollback')
                key=event_identity(('instance','host',eid),isolate_event(original,1024))[0]
                def fail(sql):
                    if sql.startswith('INSERT INTO audit_records') or sql=='ROLLBACK':raise sqlite_fault(sqlite3.SQLITE_IOERR)
                fixture.hooks.before=fail
                result=await port.accept_event(original);assert type(result) is Unconfirmed,result
                self.assertEqual(result.error.reason,'COMMIT_UNCONFIRMED')
                fixture.hooks.before=lambda sql:None
                resolved=await port.resolve_acceptance(key,original);self.assertIs(type(resolved),NotCommitted,resolved)
                pending=await runtime.recover_runtime()
                from companion_memory.runtime.results import RecoveryPending
                assert type(pending) is RecoveryPending,pending
                self.assertEqual(pending.stage,'STORAGE')
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM ingress_events').fetchone()[0],0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM buffers_positions').fetchone()[0],0)
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_commit_return_delay_keeps_original_write_owner_until_release(self):
        import threading
        from companion_memory.ingress.events import event_identity,isolate_event
        with TemporaryDirectory(prefix='iris-runtime-late-write-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'runtime.operation_timeout_ms':100})
            runtime=await fixture.initialize();entered=threading.Event();release=threading.Event();armed=True;writing=False
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                original=event('late');key=event_identity(('instance','host',eid),isolate_event(original,1024))[0]
                def before(sql):
                    nonlocal writing
                    if sql.startswith('INSERT INTO ingress_events'):writing=True
                def after(sql):
                    nonlocal armed
                    if sql=='COMMIT' and armed and writing:armed=False;entered.set();release.wait(5)
                fixture.hooks.before=before;fixture.hooks.after=after
                pending=asyncio.create_task(port.accept_event(original))
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                result=await pending;assert type(result) is Unconfirmed,result
                self.assertTrue(result.error.cleanup_pending)
                self.assertTrue(fixture.storage.get_health().writes_in_flight)
                unresolved=await port.resolve_acceptance(key,original)
                self.assertIsNot(type(unresolved),NotCommitted)
                release.set()
                async with asyncio.timeout(3):
                    while runtime._command_jobs or fixture.storage.get_health().writes_in_flight:await asyncio.sleep(0.01)
                found=await port.resolve_acceptance(key,original);assert type(found) is Committed,found
                repeated=await port.accept_event(original);assert type(repeated) is Committed,repeated
                self.assertEqual(found.receipt,repeated.receipt)
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM ingress_events').fetchone()[0],1)
                    self.assertEqual(connection.execute('SELECT count(*) FROM buffers_positions').fetchone()[0],1)
            finally:release.set();fixture.hooks.before=lambda sql:None;fixture.hooks.after=lambda sql:None;await fixture.close()
