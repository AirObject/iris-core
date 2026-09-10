"""Controlled races for shared budget, deadlines, cancellation and read snapshots."""
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from typing import cast
from unittest.mock import patch
import unittest
from companion_memory.provider import Completed, Failed, Pending, Ready, Rejected, Scenario
from tests.configuration.provider_support import provider_values
from tests.provider.support import Fixture, completed, found, record, records, success, until


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_async_wait_retains_work_and_records_late_result(self):
        started,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(replace(success(),started=started,release=release),))
            try:
                await f.initialize()
                task=asyncio.create_task(f.work.generate(f.request()))
                await until(started.is_set)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertEqual(f.service.get_health().in_flight,1)
                await until(lambda:f.service.get_health().unknown_observations==1)
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                result=completed(await f.work.generate(f.request()))
                self.assertEqual(result.record["outcome"],"SUCCEEDED")
                self.assertTrue(result.record["ever_unknown"])
                self.assertEqual(record(result.record["first_error"])["code"],"CANCELLED")
                self.assertEqual(len(f.adapter.calls),1)
            finally:
                release.set();await until(lambda:f.service.get_health().in_flight==0);await f.close()

    async def test_retry_delay_cancellation_preserves_prior_cause_without_invalid_terminal(self):
        first=Scenario("RATE_LIMITED",None,{"coverage":"COMPLETE","known_cost_atoms":1})
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(first,success()),{"provider.retry_delay_ms":100})
            settled=threading.Event()
            def after(sql):
                if sql.startswith("INSERT INTO provider_cost_items"):settled.set()
            try:
                await f.initialize();f.hooks.after=after
                task=asyncio.create_task(f.work.generate(f.request()))
                await until(settled.is_set)
                f.cancellation.cancel()
                result=completed(await task)
                self.assertEqual(result.record["outcome"],"CANCELLED")
                self.assertEqual(record(result.record["first_error"])["reason"],"RATE_LIMITED")
                self.assertFalse(f.service.get_health().ledger_faulted)
                self.assertEqual(len(f.adapter.calls),1)
            finally:
                await f.close()

    async def test_two_scopes_share_account_reservation_and_cannot_split_budget(self):
        started,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            account={"account_id":"sample_account","window_id":"sample_window","currency":"TEST","max_in_flight":2,"attempt_limit":20,"cost_limit_atoms":80}
            f=Fixture(Path(directory),(replace(success(),started=started,release=release),success()),{"provider.accounts":[account]})
            try:
                await f.initialize()
                first=asyncio.create_task(f.work.generate(f.request()))
                await until(started.is_set)
                other=f.service.bind_work(replace(f.grant,caller_scope="other_scope"))
                blocked=completed(await other.generate(f.request("second")))
                self.assertEqual(blocked.record["outcome"],"PAUSED_BUDGET")
                self.assertEqual(record(blocked.record["first_error"])["reason"],"COST_LIMIT")
                self.assertEqual(blocked.record["attempt_count"],0)
                release.set()
                completed(await first)
                self.assertEqual(records(found(await f.observer.get_budget_state()))[0]["attempt_count"],1)
            finally:
                release.set();await until(lambda:f.service.get_health().in_flight==0);await f.close()

    async def test_one_aggregate_snapshot_does_not_mix_late_settlement(self):
        started,release=threading.Event(),threading.Event()
        query_entered,query_release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(replace(success(),started=started,release=release),))
            try:
                await f.initialize()
                self.assertIs(type(await f.work.generate(f.request())),Pending)
                def after(sql):
                    if sql.startswith("WITH r AS"):
                        query_entered.set();query_release.wait()
                f.hooks.after=after
                query=asyncio.create_task(f.observer.query_usage(f.query()))
                await until(query_entered.is_set)
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                f.hooks.after=lambda sql:None
                query_release.set()
                old=records(record(found(await query))["rows"])[0]
                new=records(record(found(await f.observer.query_usage(f.query())))["rows"])[0]
                self.assertEqual((old["remote_result_unknown"],old["known_cost_atoms"],old["held_atoms"]),(1,0,80))
                self.assertEqual((new["succeeded"],new["known_cost_atoms"],new["held_atoms"]),(1,35,0))
            finally:
                f.hooks.after=lambda sql:None;query_release.set();release.set()
                await until(lambda:f.service.get_health().in_flight==0);await f.close()

    async def test_completion_time_arbitrates_both_sides_despite_delayed_callback(self):
        for late in (False,True):
            with self.subTest(late=late),TemporaryDirectory() as directory:
                started,release=threading.Event(),threading.Event()
                f=Fixture(Path(directory),(replace(success(),started=started,release=release),))
                clock=[100.0]
                f.resources=replace(f.resources,monotonic=lambda:clock[0])
                loop=asyncio.get_running_loop()
                original=loop.call_soon_threadsafe
                callbacks=[]
                def dispatch(callback,*args,context=None):
                    if getattr(callback,"__name__","")=="finished":
                        callbacks.append((callback,args))
                        return asyncio.Handle(lambda:None,(),loop)
                    return original(callback,*args,context=context)
                try:
                    await f.initialize()
                    request=f.request();request["deadline"]=101.0
                    with patch.object(loop,"call_soon_threadsafe",side_effect=dispatch):
                        task=asyncio.create_task(f.work.generate(request))
                        await until(started.is_set)
                        clock[0]=100.2 if late else 100.05
                        release.set()
                        await until(lambda:bool(callbacks))
                        clock[0]=100.25
                        for callback,args in callbacks:callback(*args)
                        result=await task
                    self.assertIs(type(result),Pending if late else Completed,result)
                    await until(lambda:f.service.get_health().in_flight==0)
                    self.assertEqual(f.service.get_health().unknown_observations,1 if late else 0)
                finally:
                    release.set();await until(lambda:f.service.get_health().in_flight==0);await f.close()
