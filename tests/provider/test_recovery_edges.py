"""Bounded resumed recovery, physical ownership corruption and safe callback errors."""
import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.configuration import MetadataValue
from companion_memory.provider import (Failed, ObserverGrant, Pending, ProviderResources, ProviderService, Ready, RecoveryPending, Rejected, WorkPort)
from tests.configuration.provider_support import provider_values
from tests.provider.support import Fixture, completed, found, record, records, success


class RecoveryEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_short_initialization_rounds_resume_finite_scan(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),tuple(success() for _ in range(8)))
            await f.initialize()
            for index in range(8):completed(await f.work.generate(f.request("request-"+str(index))))
            await f.close()
            other=Fixture(Path(directory),(success(),))
            clock=[100.0]
            other.resources=replace(other.resources,monotonic=lambda:clock[0])
            def after(sql):
                if sql.startswith("SELECT") and "provider_" in sql:clock[0]+=0.6
            other.hooks.after=after
            try:
                outcome=await other.initialize("OPEN_EXISTING")
                rounds=1
                while type(outcome) is RecoveryPending and rounds<200:
                    self.assertEqual(other.service.get_health().lifecycle,"NEW")
                    outcome=await other.service.initialize(other.snapshot,other.binding,other.resources)
                    rounds+=1
                self.assertIs(type(outcome),Ready,outcome)
                self.assertGreater(rounds,5)
                self.assertLess(rounds,100)
                self.assertEqual(other.adapter.calls,())
            finally:
                other.hooks.after=lambda sql:None
                await other.close()

    async def test_physical_ownership_columns_are_cross_checked_against_body(self):
        for table,column,replacement in (("provider_requests","caller_scope","other_scope"),
                                         ("provider_requests","operation_key","other_operation"),
                                         ("provider_attempts","ordinal",2)):
            with self.subTest(column=column),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),))
                await f.initialize();result=completed(await f.work.generate(f.request()));await f.close()
                with closing(sqlite3.connect(f.path)) as db,db:
                    db.execute("UPDATE "+table+" SET "+column+"=?",(replacement,))
                other=Fixture(Path(directory),(success(),))
                try:
                    rejected=await other.initialize("OPEN_EXISTING")
                    self.assertIs(type(rejected),Rejected,rejected)
                    assert type(rejected) is Rejected
                    self.assertEqual(rejected.error.reason,"LEDGER_INCONSISTENT")
                    self.assertEqual(other.adapter.calls,())
                finally:
                    await other.close()

    async def test_maximum_legal_generated_identifiers_use_bounded_derived_keys(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            identifiers=iter(("o"*128,"r"*128,"a"*128))
            f.resources=replace(f.resources,new_id=lambda:next(identifiers))
            try:
                self.assertIs(type(await f.initialize()),Ready)
                result=completed(await f.work.generate(f.request()))
                self.assertEqual(result.record["outcome"],"SUCCEEDED")
                self.assertEqual(len(cast(str,result.record["object_id"])),128)
                with closing(sqlite3.connect(f.path)) as db:
                    self.assertLessEqual(db.execute("SELECT max(length(operation_key)) FROM operation_receipts").fetchone()[0],128)
                    self.assertLessEqual(db.execute("SELECT max(length(object_id)) FROM provider_cost_items").fetchone()[0],128)
            finally:
                await f.close()

    async def test_invalid_clock_id_and_forged_handles_have_safe_closed_errors(self):
        for name,value in (("new_id",lambda:(_ for _ in ()).throw(TypeError("private"))),
                           ("monotonic",lambda:10**1000)):
            with self.subTest(resource=name),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),))
                f.resources=replace(f.resources,**{name:value})
                try:
                    result=await f.initialize()
                    self.assertIs(type(result),Rejected,result)
                    assert type(result) is Rejected
                    self.assertEqual(result.error.code,"RESOURCE_FAILED")
                    self.assertNotIn("private",repr(result))
                finally:
                    await f.close()
        forged=object.__new__(WorkPort)
        denied=await forged.generate({})
        assert type(denied) is Rejected
        self.assertEqual(denied.error.code,"ACCESS_DENIED")

    async def test_account_named_unassigned_does_not_merge_missing_account(self):
        values=provider_values("/synthetic/file")
        accounts=cast(list[dict],values["provider.accounts"]);accounts[0]["account_id"]="UNASSIGNED"
        profiles=cast(list[dict],values["provider.profiles"])
        for item in profiles:item["account_id"]="UNASSIGNED"
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),),cast(dict[str, MetadataValue],{"provider.accounts":accounts,"provider.profiles":profiles}))
            try:
                await f.initialize()
                completed(await f.work.generate(f.request()))
                extended=f.service.bind_work(replace(f.grant,profiles=(*f.grant.profiles,"absent")))
                blocked=completed(await extended.generate(f.request("missing-profile","absent")))
                self.assertEqual(blocked.record["outcome"],"UNSUPPORTED_CAPABILITY")
                rows=records(record(found(await f.observer.query_usage(f.query(group_by="ACCOUNT"))))["rows"])
                self.assertEqual({cast(str | None,value["group_id"]) for value in rows},{None,"UNASSIGNED"})
                self.assertEqual([value["request_count"] for value in rows],[1,1])
            finally:
                await f.close()

    async def test_live_aggregate_rejects_corrupt_types_and_physical_attribution(self):
        damages=(('provider_attempts', "body=json_set(body,'$.usage.fields.input_tokens',20.0)",()),
                 ('provider_attempts', "body=json_set(body,'$.usage.fields.input_tokens',json('true'))",()),
                 ('provider_attempts', "body=json_remove(body,'$.usage.known_subtotal_atoms')",()),
                 ('provider_attempts', "body=json_set(body,'$.usage.held_atoms',NULL)",()),
                 ('provider_attempts', "request_id=?",('foreign-request',)),
                 ('provider_requests', "caller_scope=?",('other_scope',)))
        for table,assignment,parameters in damages:
            with self.subTest(assignment=assignment),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),))
                try:
                    await f.initialize();completed(await f.work.generate(f.request()))
                    with closing(sqlite3.connect(f.path)) as db,db:
                        db.execute('UPDATE '+table+' SET '+assignment,parameters)
                    observer=f.service.bind_observer(ObserverGrant(('sample_scope','other_scope'),()))
                    value=await observer.query_usage(f.query())
                    assert type(value) is Failed,value
                    self.assertEqual(value.error.reason,'LEDGER_INCONSISTENT')
                finally:
                    await f.close()

    async def test_settlement_cannot_overwrite_corrupt_physical_attribution(self):
        import threading
        from tests.provider.support import until
        started,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(replace(success(),started=started,release=release),))
            try:
                await f.initialize()
                task=asyncio.create_task(f.work.generate(f.request()))
                await until(started.is_set)
                with closing(sqlite3.connect(f.path)) as db,db:
                    db.execute('UPDATE provider_requests SET caller_scope=?',('other_scope',))
                release.set()
                self.assertIs(type(await task),Pending)
                self.assertTrue(f.service.get_health().ledger_faulted)
                with closing(sqlite3.connect(f.path)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_handoffs').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.state') FROM provider_attempts").fetchone()[0],'PREPARED')
            finally:
                release.set();await until(lambda:f.service.get_health().in_flight==0);await f.close()
