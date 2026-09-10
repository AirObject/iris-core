"""Real rollback, commit uncertainty, corruption and retained execution ownership."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from typing import cast
import unittest
from companion_memory.configuration import MetadataValue
from companion_memory.provider import Completed, Pending, Ready, RecoveryPending, Rejected
from tests.configuration.provider_support import provider_values
from tests.persistence.support import sqlite_fault
from tests.provider.support import Fixture, completed, found, record, records, success, until


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_registration_write_and_audit_fault_rolls_back_without_send(self):
        points=("INSERT INTO provider_requests", "INSERT INTO provider_attempts", "INSERT INTO provider_reservations", "UPDATE provider_budget_windows", "INSERT INTO audit_records", "COMMIT")
        for point in points:
            with self.subTest(point=point),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),))
                try:
                    await f.initialize()
                    fired=False
                    def before(sql):
                        nonlocal fired
                        if not fired and sql.startswith(point):
                            fired=True
                            raise sqlite_fault(sqlite3.SQLITE_FULL)
                    f.hooks.before=before
                    result=await f.work.generate(f.request())
                    self.assertNotIsInstance(result,Completed,result)
                    self.assertTrue(fired)
                    self.assertEqual(f.adapter.calls,())
                    with closing(sqlite3.connect(f.path)) as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM provider_requests").fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT count(*) FROM provider_attempts").fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT count(*) FROM audit_records").fetchone()[0],1)
                finally:
                    f.hooks.before=lambda sql:None
                    await f.close()

    async def test_completion_handoff_failure_recovers_unknown_without_freeing_money(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            await f.initialize()
            def fail(sql):
                if sql.startswith("INSERT INTO provider_handoffs"):
                    raise sqlite_fault(sqlite3.SQLITE_FULL)
            f.hooks.before=fail
            pending=await f.work.generate(f.request())
            self.assertIs(type(pending),Pending,pending)
            self.assertEqual(f.service.get_health().lifecycle,"FAULTED")
            f.hooks.before=lambda sql:None
            await f.close()
            reopened=Fixture(Path(directory),(success(),))
            try:
                self.assertIs(type(await reopened.initialize("OPEN_EXISTING")),Ready)
                observed=await reopened.work.generate(reopened.request())
                self.assertIs(type(observed),Pending,observed)
                budget=records(found(await reopened.observer.get_budget_state()))[0]
                self.assertEqual((budget["known_subtotal_atoms"],budget["held_atoms"]),(0,80))
                self.assertEqual(reopened.adapter.calls,())
            finally:
                await reopened.close()

    async def test_unknown_record_failure_keeps_blocked_worker_owned_through_close(self):
        started,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(replace(success(),started=started,release=release),),{"provider.close_timeout_ms":10})
            try:
                await f.initialize()
                def fail(sql):
                    if sql.startswith("UPDATE provider_requests"):
                        raise sqlite_fault(sqlite3.SQLITE_FULL)
                f.hooks.before=fail
                result=await f.work.generate(f.request())
                self.assertIs(type(result),Pending,result)
                self.assertEqual(f.service.get_health().in_flight,1)
                self.assertTrue(f.service.get_health().ledger_faulted)
                closed=await f.service.close()
                self.assertEqual(closed.status,"INCOMPLETE")
                self.assertTrue(f.binding is not None and f.binding.lease is not None and f.binding.lease.is_active())
            finally:
                release.set()
                f.hooks.before=lambda sql:None
                await until(lambda:f.service.get_health().in_flight==0)
                self.assertEqual(f.service.get_health().lifecycle,"CLOSED")
                await f.close()

    async def test_local_commit_outlives_caller_deadline_and_close_without_late_send(self):
        entered,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),),{"provider.close_timeout_ms":10,"storage.operation_timeout_ms":100})
            try:
                await f.initialize()
                def before(sql):
                    if sql=="COMMIT":
                        entered.set()
                        release.wait()
                f.hooks.before=before
                request=f.request();request["deadline"]=time.monotonic()+0.03
                task=asyncio.create_task(f.work.generate(request))
                await until(entered.is_set)
                result=await task
                self.assertIs(type(result),Pending,result)
                self.assertEqual(f.adapter.calls,())
                closed=await f.service.close()
                self.assertEqual(closed.status,"INCOMPLETE")
                self.assertEqual(f.service.get_health().in_flight,1)
                f.hooks.before=lambda sql:None
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                self.assertEqual(f.adapter.calls,())
                self.assertEqual(f.service.get_health().lifecycle,"CLOSED")
            finally:
                f.hooks.before=lambda sql:None
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                await f.close()

    async def test_close_during_initialization_never_restores_ready(self):
        entered,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),),{"provider.close_timeout_ms":10})
            initialized=False
            def before(sql):
                if sql.startswith("INSERT INTO provider_budget_windows"):
                    entered.set();release.wait()
            f.hooks.before=before
            task=asyncio.create_task(f.initialize())
            try:
                await until(entered.is_set)
                closed=await f.service.close()
                self.assertEqual(closed.status,"INCOMPLETE")
                f.hooks.before=lambda sql:None;release.set()
                await task
                await until(lambda:f.service.get_health().lifecycle=="CLOSED")
                value=await f.work.generate(f.request())
                self.assertIs(type(value),Rejected,value)
            finally:
                f.hooks.before=lambda sql:None;release.set()
                await task
                await f.close()

    async def test_budget_group_is_atomic_when_second_account_insert_fails(self):
        with TemporaryDirectory() as directory:
            accounts: MetadataValue=[{"account_id":name,"window_id":"sample_window","currency":"TEST","max_in_flight":1,"attempt_limit":20,"cost_limit_atoms":1000000}
                      for name in ("sample_account","other_account")]
            f=Fixture(Path(directory),(success(),),{"provider.accounts":accounts})
            count=0
            def before(sql):
                nonlocal count
                if sql.startswith("INSERT INTO provider_budget_windows"):
                    count+=1
                    if count==2:
                        raise sqlite_fault(sqlite3.SQLITE_FULL)
            f.hooks.before=before
            result=await f.initialize()
            self.assertIs(type(result),RecoveryPending,result)
            with closing(sqlite3.connect(f.path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM provider_budget_windows").fetchone()[0],0)
            f.hooks.before=lambda sql:None
            await f.close()
            reopened=Fixture(Path(directory),(success(),),{"provider.accounts":accounts})
            try:
                self.assertIs(type(await reopened.initialize("OPEN_EXISTING")),Ready)
                with closing(sqlite3.connect(reopened.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM provider_budget_windows").fetchone()[0],2)
            finally:
                await reopened.close()

    async def test_durable_type_and_relationship_corruption_refuses_recovery(self):
        cases=("amount_float","amount_bool","field_float","field_bool","estimated_missing","estimated_wrong","missing_field","extra_field","cost_item","handoff_owner","handoff_format")
        for case in cases:
            with self.subTest(case=case),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),))
                await f.initialize();completed(await f.work.generate(f.request()));await f.close()
                table="provider_attempts"
                if case=="cost_item":table="provider_cost_items"
                if case.startswith("handoff"):table="provider_handoffs"
                with closing(sqlite3.connect(f.path)) as db,db:
                    key,body=db.execute("SELECT object_id,body FROM "+table+" LIMIT 1").fetchone()
                    value=json.loads(body)
                    if case=="amount_float":value["usage"]["known_subtotal_atoms"]=35.0
                    elif case=="amount_bool":value["usage"]["known_subtotal_atoms"]=True
                    elif case=="field_float":value["usage"]["raw_usage"]["input_tokens"]=20;value["usage"]["fields"]["input_tokens"]=20.0
                    elif case=="field_bool":value["usage"]["raw_usage"]["input_tokens"]=1;value["usage"]["fields"]["input_tokens"]=True
                    elif case=="estimated_missing":value["usage"]["estimated_cost_atoms"]=None
                    elif case=="estimated_wrong":value["usage"]["estimated_cost_atoms"]=34
                    elif case=="missing_field":value.pop("profile_id")
                    elif case=="extra_field":value["foreign_field"]=0
                    elif case=="cost_item":value["cost_atoms"]=123
                    elif case=="handoff_owner":value["owner_id"]="other_owner"
                    else:value["format_version"]=2
                    db.execute("UPDATE "+table+" SET body=? WHERE object_id=?",(json.dumps(value,separators=(",",":")),key))
                reopened=Fixture(Path(directory),(success(),))
                try:
                    result=await reopened.initialize("OPEN_EXISTING")
                    self.assertIs(type(result),Rejected,result)
                    assert type(result) is Rejected
                    self.assertEqual(result.error.reason,"LEDGER_INCONSISTENT")
                    self.assertEqual(reopened.adapter.calls,())
                finally:
                    await reopened.close()

    async def test_budget_initialization_audits_every_account_in_one_commit(self):
        accounts: MetadataValue=[{'account_id':name,'window_id':'sample_window','currency':'TEST','max_in_flight':1,'attempt_limit':20,'cost_limit_atoms':1000000}
                                for name in ('sample_account','other_account')]
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),),{'provider.accounts':accounts})
            try:
                await f.initialize()
                with closing(sqlite3.connect(f.path)) as db:
                    audits=db.execute('SELECT commit_id,record FROM audit_records').fetchall()
                    self.assertEqual(len(audits),1)
                    event=json.loads(audits[0][1])
                    self.assertEqual(event['event_code'],'PROVIDER_BUDGET_INITIALIZED')
                    self.assertEqual({target['object_id'] for target in event['target_refs']},{'sample_account','other_account'})
                    self.assertEqual(db.execute('SELECT count(*) FROM operation_receipts WHERE commit_id=?',(audits[0][0],)).fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_budget_windows').fetchone()[0],2)
                    self.assertNotIn('cost_limit_atoms',repr(event))
            finally:
                await f.close()

    async def test_active_executor_lease_survives_borrowed_storage_close(self):
        started,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(replace(success(),started=started,release=release),),{'provider.close_timeout_ms':10})
            other=Fixture(Path(directory),(success(),))
            try:
                await f.initialize()
                self.assertIs(type(await f.work.generate(f.request())),Pending)
                await f.storage.close()
                rejected=await other.initialize('OPEN_EXISTING')
                assert type(rejected) is Rejected,rejected
                self.assertEqual(rejected.error.reason,'CAPABILITY_MISMATCH')
                self.assertEqual(other.adapter.calls,())
                self.assertEqual(f.service.get_health().in_flight,1)
            finally:
                release.set();await until(lambda:f.service.get_health().in_flight==0)
                await other.close();await f.close()

    async def test_two_in_flight_calls_cannot_add_commands_after_first_ledger_fault(self):
        starts=(threading.Event(),threading.Event());releases=(threading.Event(),threading.Event())
        values=provider_values('/synthetic/file')
        accounts=cast(list[dict],values['provider.accounts']);accounts[0]['max_in_flight']=2
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),tuple(replace(success(),started=start,release=release) for start,release in zip(starts,releases)),
                      {'provider.accounts':cast(MetadataValue,accounts)})
            writes=[]
            try:
                await f.initialize()
                first=asyncio.create_task(f.work.generate(f.request('first')))
                await until(starts[0].is_set)
                second=asyncio.create_task(f.work.generate(f.request('second')))
                await until(starts[1].is_set)
                def before(sql):
                    if sql.startswith('UPDATE provider_requests'):
                        writes.append(sql)
                        raise sqlite_fault(sqlite3.SQLITE_FULL)
                f.hooks.before=before;releases[0].set()
                self.assertIs(type(await first),Pending)
                self.assertIs(type(await second),Pending)
                self.assertEqual(len(writes),1)
                self.assertTrue(f.service.get_health().ledger_faulted)
                self.assertEqual(f.service.get_health().in_flight,1)
                self.assertEqual((await f.service.close()).status,'INCOMPLETE')
            finally:
                f.hooks.before=lambda sql:None
                for release in releases:release.set()
                await until(lambda:f.service.get_health().in_flight==0);await f.close()

    async def test_initialization_unconfirmed_commit_cannot_restore_faulted_service_ready(self):
        entered,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),),{'storage.operation_timeout_ms':20})
            changing=False
            def after(sql):
                nonlocal changing
                if sql.startswith('INSERT INTO provider_budget_windows'):changing=True
                if sql=='COMMIT' and changing:
                    entered.set();release.wait();changing=False
            f.hooks.after=after
            task=asyncio.create_task(f.initialize())
            try:
                await until(entered.is_set)
                await until(lambda:f.service.get_health().ledger_faulted)
                f.hooks.after=lambda sql:None;release.set()
                value=await task
                self.assertIs(type(value),RecoveryPending,value)
                self.assertEqual(f.service.get_health().lifecycle,'FAULTED')
                self.assertTrue(f.service.get_health().ledger_faulted)
                self.assertIs(type(await f.service.initialize(f.snapshot,f.binding,f.resources)),Rejected)
                self.assertEqual(f.adapter.calls,())
            finally:
                f.hooks.after=lambda sql:None;release.set();await task;await f.close()
            other=Fixture(Path(directory),(success(),))
            try:
                self.assertIs(type(await other.initialize('OPEN_EXISTING')),Ready)
                self.assertEqual(len(records(found(await other.observer.get_budget_state()))),1)
            finally:
                await other.close()

    async def test_every_completion_write_rolls_back_cost_result_and_audit_together(self):
        points=('UPDATE provider_requests','UPDATE provider_attempts','UPDATE provider_reservations','UPDATE provider_budget_windows',
                'INSERT INTO provider_cost_items','INSERT INTO provider_handoffs','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT')
        for point in points:
            with self.subTest(point=point),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),))
                armed=False;fired=False
                def before(sql):
                    nonlocal armed,fired
                    if sql.startswith('UPDATE provider_requests'):armed=True
                    if armed and not fired and sql.startswith(point):
                        fired=True
                        raise sqlite_fault(sqlite3.SQLITE_FULL)
                try:
                    await f.initialize();f.hooks.before=before
                    result=await f.work.generate(f.request())
                    self.assertIs(type(result),Pending,result)
                    self.assertTrue(fired)
                    self.assertEqual(len(f.adapter.calls),1)
                    with closing(sqlite3.connect(f.path)) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_cost_items').fetchone()[0],0)
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_handoffs').fetchone()[0],0)
                        self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],2)
                        self.assertEqual(db.execute("SELECT json_extract(body,'$.known_subtotal_atoms'),json_extract(body,'$.held_atoms') FROM provider_budget_windows").fetchone(),(0,80))
                finally:
                    f.hooks.before=lambda sql:None;await f.close()

    async def test_commit_evidence_and_blocked_connection_cleanup_keep_separate_ownership(self):
        from companion_memory.provider import ProviderService
        for point in ('registration_rollback','registration_committed','settlement_committed'):
            with self.subTest(point=point),TemporaryDirectory() as directory:
                entered,release=threading.Event(),threading.Event()
                f=Fixture(Path(directory),(success(),),{'storage.operation_timeout_ms':30,'provider.close_timeout_ms':10})
                armed=False
                def before(sql):
                    nonlocal armed
                    trigger='UPDATE provider_requests' if point=='settlement_committed' else 'INSERT INTO provider_requests'
                    if sql.startswith(trigger):
                        armed=True
                        if point=='registration_rollback':
                            raise sqlite_fault(sqlite3.SQLITE_FULL)
                def closing_connection():
                    if armed:
                        entered.set();release.wait()
                try:
                    await f.initialize();f.hooks.before=before;f.hooks.before_close=closing_connection
                    result=await f.work.generate(f.request())
                    self.assertTrue(entered.is_set())
                    self.assertEqual(f.storage.get_health().writes_in_flight,1)
                    self.assertEqual(f.service.get_health().in_flight,1)
                    self.assertTrue(f.service.get_health().cleanup_pending)
                    if point=='registration_rollback':
                        assert type(result) is Rejected,result
                        self.assertEqual(result.error.reason,'LEDGER_NOT_COMMITTED')
                        self.assertTrue(result.error.cleanup_pending)
                    elif point=='registration_committed':
                        assert type(result) is Pending,result
                        self.assertEqual(result.observation,'IN_PROGRESS')
                        self.assertIsNotNone(result.error)
                        assert result.error is not None
                        self.assertTrue(result.error.cleanup_pending)
                        self.assertEqual(result.error.reason,'RESOURCE_FAILURE')
                    else:
                        observed=completed(result)
                        self.assertEqual(observed.record['outcome'],'SUCCEEDED')
                        self.assertEqual(record(observed.result)['text'],'hello')
                    self.assertEqual(len(f.adapter.calls),1 if point=='settlement_committed' else 0)
                    with closing(sqlite3.connect(f.path)) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0 if point=='registration_rollback' else 1)
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_handoffs').fetchone()[0],1 if point=='settlement_committed' else 0)
                    report=await f.service.close()
                    self.assertEqual(report.status,'INCOMPLETE');self.assertTrue(report.cleanup_pending)
                    assert f.binding is not None and f.binding.lease is not None
                    lease=f.binding.lease
                    self.assertTrue(lease.is_active())
                    duplicate=ProviderService(f.assembly)
                    self.assertIs(type(await duplicate.initialize(f.snapshot,f.binding,f.resources)),Rejected)
                    await duplicate.close()
                    f.hooks.before=lambda sql:None;f.hooks.before_close=lambda:None;release.set()
                    await until(lambda:f.service.get_health().lifecycle=='CLOSED')
                    self.assertEqual(f.storage.get_health().writes_in_flight,0)
                    self.assertEqual(f.service.get_health().in_flight,0)
                    self.assertFalse(f.service.get_health().cleanup_pending)
                    self.assertFalse(lease.is_active())
                    self.assertIs(await f.service.close(),report)
                    self.assertEqual(len(f.adapter.calls),1 if point=='settlement_committed' else 0)
                finally:
                    f.hooks.before=lambda sql:None;f.hooks.before_close=lambda:None;release.set()
                    await until(lambda:f.service.get_health().in_flight==0);await f.close()

    async def test_initialization_keeps_committed_or_rolled_back_cleanup_owned(self):
        for rollback in (False,True):
            with self.subTest(rollback=rollback),TemporaryDirectory() as directory:
                entered,release=threading.Event(),threading.Event()
                f=Fixture(Path(directory),(success(),),{'storage.operation_timeout_ms':30,'provider.close_timeout_ms':10})
                armed=False
                def before(sql):
                    nonlocal armed
                    if sql.startswith('INSERT INTO provider_budget_windows'):
                        armed=True
                        if rollback:raise sqlite_fault(sqlite3.SQLITE_FULL)
                def closing_connection():
                    if armed:entered.set();release.wait()
                f.hooks.before=before;f.hooks.before_close=closing_connection
                try:
                    result=await f.initialize()
                    self.assertIs(type(result),RecoveryPending,result)
                    assert type(result) is RecoveryPending
                    self.assertEqual(result.error.code,'PERSISTENCE_FAILED' if rollback else 'RESOURCE_FAILED')
                    self.assertEqual(result.error.reason,'LEDGER_NOT_COMMITTED' if rollback else 'RESOURCE_FAILURE')
                    self.assertTrue(result.error.cleanup_pending)
                    self.assertTrue(entered.is_set())
                    self.assertTrue(f.service.get_health().cleanup_pending)
                    self.assertEqual(f.storage.get_health().writes_in_flight,1)
                    report=await f.service.close();self.assertEqual(report.status,'INCOMPLETE')
                    assert f.binding is not None and f.binding.lease is not None
                    lease=f.binding.lease;self.assertTrue(lease.is_active())
                    f.hooks.before=lambda sql:None;f.hooks.before_close=lambda:None;release.set()
                    await until(lambda:f.service.get_health().lifecycle=='CLOSED')
                    self.assertFalse(lease.is_active());self.assertFalse(f.service.get_health().cleanup_pending)
                    self.assertIs(await f.service.close(),report);self.assertEqual(f.adapter.calls,())
                finally:
                    f.hooks.before=lambda sql:None;f.hooks.before_close=lambda:None;release.set();await f.close()
