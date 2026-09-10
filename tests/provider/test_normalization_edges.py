"""Capability formats, immutable ownership, conservative late evidence and limits."""
import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from typing import cast
import unittest
from companion_memory.configuration import MetadataValue
from companion_memory.provider import Completed, Failed, Pending, Rejected, Scenario
from companion_memory.provider.normalization import normalize_usage
from companion_memory.provider.values import as_record
from tests.configuration.provider_support import provider_values
from tests.provider.support import Fixture, completed, found, record, records, success, until


class NormalizationEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_schema_failures_preserve_exact_cost_without_retry(self):
        embedding={'vectors':[[0.5,1.0]],'dimensions':2,'space_id':'sample_space','model_id':'sample_model','input_items':1}
        examples=(('embedding',{'texts':['first'],'purpose':'QUERY','dimensions':2},{**embedding,'dimensions':True},None),
                  ('embedding',{'texts':['first'],'purpose':'QUERY','dimensions':2},{**embedding,'vectors':[[1,2]]},None),
                  ('embedding',{'texts':['first'],'purpose':'QUERY','dimensions':2},{**embedding,'space_id':'foreign'},None),
                  ('embedding',{'texts':['first'],'purpose':'QUERY','dimensions':2},embedding,'NON_FINITE'),
                  ('rerank',{'query':'q','candidates':[{'candidate_id':'one','text':'a'}],'top_n':1},{'ranked':[{'candidate_id':'foreign','score':0.5}]},None),
                  ('rerank',{'query':'q','candidates':[{'candidate_id':'one','text':'a'},{'candidate_id':'two','text':'b'}],'top_n':2},{'ranked':[{'candidate_id':'one','score':0.5},{'candidate_id':'one','score':0.6}]},None))
        for profile,payload,response,malformed in examples:
            with self.subTest(profile=profile,response=response),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(Scenario('SUCCEEDED',response,{'coverage':'COMPLETE','known_cost_atoms':3},malformed=malformed),success()))
                try:
                    await f.initialize()
                    call=f.work.embed if profile=='embedding' else f.work.rerank
                    result=completed(await call(f.request(profile=profile,payload=payload)))
                    self.assertEqual(result.record['outcome'],'FAILED')
                    self.assertEqual(record(result.record['first_error'])['reason'],'INVALID_RESPONSE')
                    self.assertIsNone(result.result)
                    self.assertEqual(len(f.adapter.calls),1)
                    self.assertEqual(records(found(await f.observer.get_budget_state()))[0]['known_subtotal_atoms'],3)
                finally:
                    await f.close()

    async def test_partial_thirty_five_to_fifty_uses_difference_and_repeated_evidence_is_inert(self):
        values=provider_values('/synthetic/file')
        profiles=cast(list[dict],values['provider.profiles']);profiles[0]['input_price_atoms']=7
        started,release=threading.Event(),threading.Event()
        scenario=replace(success(),started=started,release=release,interim_usage={'coverage':'PARTIAL','billing_input_units':5},
                         usage={'coverage':'COMPLETE','billing_input_units':5,'billing_output_units':5})
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(scenario,),{'provider.profiles':cast(MetadataValue,profiles)})
            request=f.request(payload={'messages':[{'role':'USER','text':'12345'}],'input_units_limit':5,'output_units_limit':15})
            try:
                await f.initialize()
                self.assertIs(type(await f.work.generate(request)),Pending)
                job=next(iter(f.service._jobs))
                old=records(found(await f.observer.get_budget_state()))[0]
                self.assertEqual((old['known_subtotal_atoms'],old['held_atoms']),(35,45))
                release.set();await until(lambda:f.service.get_health().in_flight==0)
                final=completed(await f.work.generate(request))
                new=records(found(await f.observer.get_budget_state()))[0]
                self.assertEqual((new['known_subtotal_atoms'],new['held_atoms']),(50,0))
                assert job.attempt is not None and job.stored is not None
                profile=record(record(job.stored['execution_evidence'])['profile'])
                with closing(sqlite3.connect(f.path)) as db:
                    before=db.execute('SELECT count(*) FROM operation_receipts').fetchone()[0]
                await f.service._settle(job,profile,'SUCCEEDED',None,record(job.attempt['usage']),final.result,False)
                with closing(sqlite3.connect(f.path)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM operation_receipts').fetchone()[0],before)
                    costs=db.execute("SELECT json_extract(body,'$.cost_atoms') FROM provider_cost_items WHERE item!='reported'").fetchall()
                    self.assertEqual(sum(value[0] for value in costs),50)
                self.assertEqual(len(f.adapter.calls),1)
            finally:
                release.set();await until(lambda:f.service.get_health().in_flight==0);await f.close()

    async def test_conflicting_late_known_fact_preserves_old_partial_responsibility(self):
        started,release=threading.Event(),threading.Event()
        scenario=replace(success(),started=started,release=release,interim_usage={'coverage':'PARTIAL','billing_input_units':10},
                         usage={'coverage':'COMPLETE','billing_input_units':11,'billing_output_units':5})
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(scenario,))
            try:
                await f.initialize();self.assertIs(type(await f.work.generate(f.request())),Pending)
                release.set();await until(lambda:f.service.get_health().in_flight==0)
                self.assertTrue(f.service.get_health().ledger_faulted)
                self.assertEqual(f.service.get_health().reason,'CONTENT_MISMATCH')
                with closing(sqlite3.connect(f.path)) as db:
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.known_subtotal_atoms'),json_extract(body,'$.held_atoms') FROM provider_budget_windows").fetchone(),(20,60))
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_handoffs').fetchone()[0],0)
            finally:
                release.set();await f.close()

    async def test_owned_request_and_result_cannot_change_after_admission(self):
        started,release=threading.Event(),threading.Event()
        body={'text':'original','stop_reason':'STOP'}
        scenario=Scenario('SUCCEEDED',body,{'coverage':'COMPLETE','known_cost_atoms':0},started,release)
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(scenario,))
            request=f.request();original=f.request()
            try:
                await f.initialize()
                task=asyncio.create_task(f.work.generate(request));await until(started.is_set)
                cast(dict,request['payload'])['messages'][0]['text']='changed'
                body['text']='changed'
                release.set();result=completed(await task)
                self.assertEqual(record(result.result)['text'],'original')
                same=completed(await f.work.generate(original))
                self.assertEqual(same.result,result.result)
                with self.assertRaises(TypeError):cast(dict,same.result)['text']='replacement'
                self.assertIs(type(await f.work.generate(request)),Rejected)
                self.assertEqual(len(f.adapter.calls),1)
            finally:
                release.set();await until(lambda:f.service.get_health().in_flight==0);await f.close()

    async def test_finite_retry_exhaustion_and_deadline_during_backoff(self):
        first=Scenario('TRANSIENT_FAILURE',None,{'coverage':'COMPLETE','known_cost_atoms':1})
        for expire in (False,True):
            with self.subTest(expire=expire),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(first,first,success()),{'provider.retry_delay_ms':200 if expire else 0})
                try:
                    await f.initialize()
                    request=f.request()
                    if expire:request['deadline']=time.monotonic()+0.07
                    result=await f.work.generate(request)
                    if expire:
                        self.assertIs(type(result),Pending)
                        await until(lambda:f.service.get_health().in_flight==0)
                        result=await f.work.generate(f.request())
                    terminal=completed(result)
                    self.assertEqual(terminal.record['outcome'],'TIMED_OUT' if expire else 'FAILED')
                    self.assertEqual(len(f.adapter.calls),1 if expire else 2)
                    self.assertFalse(f.service.get_health().ledger_faulted)
                finally:
                    await f.close()

    async def test_cross_profile_budget_limits_and_exact_admission_boundaries(self):
        for cost,attempts in ((79,20),(80,20),(1000000,1)):
            values=provider_values('/synthetic/file')
            accounts=cast(list[dict],values['provider.accounts']);accounts[0]['cost_limit_atoms']=cost;accounts[0]['attempt_limit']=attempts
            profiles=cast(list[dict],values['provider.profiles']);profiles.append({**profiles[0],'profile_id':'alias'})
            roles=cast(dict,values['provider.role_profiles']);roles['LEARNING'].append('alias')
            with self.subTest(cost=cost,attempts=attempts),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),success()),cast(dict[str,MetadataValue],{'provider.accounts':accounts,'provider.profiles':profiles,'provider.role_profiles':roles}))
                try:
                    await f.initialize()
                    first=completed(await f.work.generate(f.request()))
                    port=f.service.bind_work(replace(f.grant,caller_scope='other_scope',profiles=(*f.grant.profiles,'alias')))
                    second=completed(await port.generate(f.request('second','alias')))
                    self.assertEqual(first.record['outcome'],'PAUSED_BUDGET' if cost==79 else 'SUCCEEDED')
                    self.assertEqual(second.record['outcome'],'PAUSED_BUDGET')
                    self.assertEqual(record(second.record['first_error'])['reason'],'ATTEMPT_LIMIT' if attempts==1 else 'COST_LIMIT')
                finally:
                    await f.close()

    async def test_usage_subsets_do_not_duplicate_price_and_overflow_never_becomes_float(self):
        examples=({'coverage':'COMPLETE','input_tokens':10,'output_tokens':5,'cache_read_tokens':3,'cache_write_tokens':2,'reasoning_tokens':2,'billing_input_units':10,'billing_output_units':5},
                  {'coverage':'COMPLETE','input_tokens':10,'cache_read_tokens':11,'billing_input_units':10,'billing_output_units':5},
                  {'coverage':'COMPLETE','billing_input_units':2**63-1,'billing_output_units':0})
        for index,usage in enumerate(examples):
            with self.subTest(index=index),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(Scenario('SUCCEEDED',success().payload,usage),))
                try:
                    await f.initialize()
                    result=completed(await f.work.generate(f.request()))
                    self.assertEqual(result.record['outcome'],'SUCCEEDED' if index==0 else 'FAILED')
                    budget=records(found(await f.observer.get_budget_state()))[0]
                    self.assertEqual(budget['known_subtotal_atoms'],35 if index<2 else 0)
                    self.assertIs(type(budget['known_subtotal_atoms']),int)
                    summary=records(record(found(await f.observer.query_usage(f.query())))['rows'])[0]
                    if index==0:
                        self.assertEqual((summary['input_tokens_sum'],summary['cache_read_tokens_sum'],summary['reasoning_tokens_sum'],summary['known_cost_atoms']),(10,3,2,35))
                    else:
                        self.assertGreater(cast(int,summary['held_atoms']),0)
                    self.assertEqual(len(f.adapter.calls),1)
                finally:
                    await f.close()

    async def test_native_cancellation_rejects_forged_and_replaced_event_without_hooks(self):
        from companion_memory.provider import CancellationSource, CancellationToken
        hits=[]
        class Hooks:
            def is_set(self):hits.append('is_set');raise AssertionError('Must not run.')
        forged=object.__new__(CancellationToken)
        object.__setattr__(forged,'_event',Hooks())
        original=CancellationSource().token
        object.__setattr__(original,'_event',Hooks())
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            try:
                await f.initialize()
                for token in (forged,original):
                    request=f.request();request['cancellation']=token
                    value=await f.work.generate(request)
                    assert type(value) is Rejected,value
                    self.assertEqual(value.error.code,'INVALID_INPUT')
                self.assertEqual(hits,[]);self.assertEqual(f.adapter.calls,())
            finally:
                await f.close()

    async def test_bad_observation_clocks_fail_query_without_faulting_healthy_ledger(self):
        from datetime import datetime
        callbacks=(lambda:None,lambda:datetime(2026,1,1),lambda:(_ for _ in ()).throw(TypeError('private-clock')))
        for callback in callbacks:
            with self.subTest(callback=callback),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),))
                try:
                    await f.initialize();completed(await f.work.generate(f.request()))
                    object.__setattr__(f.resources,'utc_now',callback)
                    value=await f.observer.query_usage(f.query())
                    assert type(value) is Failed,value
                    self.assertEqual(value.error.reason,'LEDGER_READ_FAILED')
                    self.assertFalse(f.service.get_health().ledger_faulted)
                    self.assertNotIn('private-clock',repr(value))
                finally:
                    await f.close()

    async def test_input_encoding_checks_deadline_without_creating_a_request(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            try:
                await f.initialize()
                ticks=iter((100.0,100.1,101.1))
                object.__setattr__(f.resources,'monotonic',lambda:next(ticks))
                request=f.request();request['deadline']=100.5
                value=await f.work.generate(request)
                assert type(value) is Rejected,value
                self.assertEqual(value.error.code,'TIMEOUT')
                self.assertEqual(f.adapter.calls,())
                with closing(sqlite3.connect(f.path)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
            finally:
                await f.close()
