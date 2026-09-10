"""Four capabilities, finite attempts and persistent uncertainty over real commits."""
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from typing import cast
from companion_memory.provider import (CancellationSource, Completed, Found, ObserverGrant, Pending, ProviderService, Ready, Rejected, ResultGrant, Scenario)
from tests.provider.support import Fixture, completed, found, record, records, success, until
from tests.configuration.provider_support import provider_values


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_capabilities_share_one_account_without_extra_generation(self):
        scenarios = (success(),
            Scenario("SUCCEEDED", {"vectors": [[0.5, -1.0], [0.0, 2.0]], "dimensions": 2, "space_id": "sample_space", "model_id": "sample_model", "input_items": 2}, {"coverage": "COMPLETE", "billing_input_units": 2, "billing_output_units": 0}),
            Scenario("SUCCEEDED", {"ranked": [{"candidate_id": "second", "score": 0.8}]}, {"coverage": "COMPLETE", "billing_input_units": 2, "billing_output_units": 0}),
            Scenario("SUCCEEDED", {"text": "a synthetic square", "modality": "IMAGE", "task": "DESCRIBE", "source": "SIMULATED", "profile_id": "media", "model_id": "sample_model"}, {"coverage": "COMPLETE", "billing_input_units": 3, "billing_output_units": 0}))
        with TemporaryDirectory() as directory:
            f = Fixture(Path(directory), scenarios)
            try:
                self.assertIs(type(await f.initialize()), Ready)
                a = completed(await f.work.generate(f.request("a")))
                b = completed(await f.work.embed(f.request("b", "embedding", {"texts": ["first", "second"], "purpose": "DOCUMENT", "dimensions": 2})))
                c = completed(await f.work.rerank(f.request("c", "rerank", {"query": "sample", "candidates": [{"candidate_id": "first", "text": "one"}, {"candidate_id": "second", "text": "two"}], "top_n": 1})))
                media = f.service.authorize_media("sample_scope", "sample_owner", "sample_artifact", b"abc", "IMAGE")
                d = completed(await f.work.understand_media(f.request("d", "media", {"media": media, "modality": "IMAGE", "task": "DESCRIBE"})))
                self.assertEqual([value.record["outcome"] for value in (a,b,c,d)], ["SUCCEEDED"]*4)
                self.assertEqual(record(b.result)["vectors"], ((0.5,-1.0),(0.0,2.0)))
                self.assertEqual(len(f.adapter.calls), 4)
                budget = records(found(await f.observer.get_budget_state()))[0]
                self.assertEqual((budget["attempt_count"],budget["known_subtotal_atoms"],budget["held_atoms"]), (4,49,0))
                groups = records(record(found(await f.observer.query_usage(f.query(group_by="CAPABILITY"))))["rows"])
                self.assertEqual(len(groups),4)
                self.assertEqual(sum(cast(int,item["confirmed_calls"]) for item in groups),4)
            finally:
                await f.close()

    async def test_refusals_authentication_and_invalid_payload_never_retry(self):
        examples = (("SENSITIVE_REFUSAL", "SENSITIVE_REFUSAL"), ("OTHER_REFUSAL", "OTHER_REFUSAL"), ("AUTHENTICATION_FAILED", "FAILED"), ("INVALID_RESPONSE", "FAILED"))
        for outcome, logical in examples:
            with self.subTest(outcome=outcome), TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(Scenario(outcome,None,{"coverage":"COMPLETE","known_cost_atoms":7}),success()))
                try:
                    await f.initialize()
                    value=completed(await f.work.generate(f.request()))
                    self.assertEqual(value.record["outcome"],logical)
                    self.assertEqual(len(f.adapter.calls),1)
                    self.assertEqual(records(found(await f.observer.get_budget_state()))[0]["known_subtotal_atoms"],7)
                finally:
                    await f.close()

    async def test_retry_second_attempt_blocked_cannot_reuse_first_completion(self):
        started,release=threading.Event(),threading.Event()
        f=None
        with TemporaryDirectory() as directory:
            first=Scenario("TRANSIENT_FAILURE",None,{"coverage":"COMPLETE","billing_input_units":10,"billing_output_units":0})
            second=replace(success("second result"),started=started,release=release)
            f=Fixture(Path(directory),(first,second))
            try:
                await f.initialize()
                task=asyncio.create_task(f.work.generate(f.request()))
                await until(started.is_set)
                value=await task
                self.assertIs(type(value),Pending,value)
                assert type(value) is Pending
                self.assertEqual(f.service.get_health().in_flight,1)
                self.assertEqual(len(f.adapter.calls),2)
                observed=record(found(await f.observer.get_request(value.reference["request_id"])))
                attempts=records(observed["attempts"])
                self.assertEqual([item["state"] for item in attempts],["COMPLETED","REMOTE_RESULT_UNKNOWN"])
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                original=completed(await f.work.generate(f.request()))
                self.assertEqual(record(original.result)["text"],"second result")
                self.assertTrue(original.record["ever_unknown"])
                budget=records(found(await f.observer.get_budget_state()))[0]
                self.assertEqual((budget["known_subtotal_atoms"],budget["held_atoms"]),(55,0))
            finally:
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                await f.close()

    async def test_late_partial_cost_adjusts_delta_and_preserves_unknown_observation(self):
        started,release=threading.Event(),threading.Event()
        scenario=replace(success(),started=started,release=release,
            interim_usage={"coverage":"PARTIAL","billing_input_units":10},
            usage={"coverage":"COMPLETE","billing_input_units":10,"billing_output_units":5})
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(scenario,))
            try:
                await f.initialize()
                value=await f.work.generate(f.request())
                self.assertIs(type(value),Pending,value)
                assert type(value) is Pending
                old=records(found(await f.observer.get_budget_state()))[0]
                self.assertEqual((old["known_subtotal_atoms"],old["held_atoms"]),(20,60))
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                final=completed(await f.work.generate(f.request()))
                new=records(found(await f.observer.get_budget_state()))[0]
                self.assertEqual((new["known_subtotal_atoms"],new["held_atoms"]),(35,0))
                self.assertEqual(value.observation,"REMOTE_RESULT_UNKNOWN")
                self.assertTrue(final.record["ever_unknown"])
                self.assertEqual(len(f.adapter.calls),1)
            finally:
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                await f.close()

    async def test_overrun_blocks_shared_account_and_survives_reopen(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(Scenario("SUCCEEDED",success().payload,{"coverage":"COMPLETE","known_cost_atoms":100}),success()))
            await f.initialize()
            result=completed(await f.work.generate(f.request()))
            self.assertEqual(result.record["outcome"],"SUCCEEDED")
            blocked=completed(await f.work.generate(f.request("another")))
            self.assertEqual(blocked.record["outcome"],"PAUSED_BUDGET")
            self.assertEqual(record(blocked.record["first_error"])["reason"],"RESERVATION_OVERRUN")
            await f.close()
            other=Fixture(Path(directory),(success(),))
            try:
                self.assertIs(type(await other.initialize("OPEN_EXISTING")),Ready)
                blocked=completed(await other.work.generate(other.request("new-process")))
                self.assertEqual(blocked.record["outcome"],"PAUSED_BUDGET")
                self.assertEqual(len(other.adapter.calls),0)
                self.assertEqual(records(found(await other.observer.get_budget_state()))[0]["risk_state"],"RESERVATION_OVERRUN")
            finally:
                await other.close()

    async def test_blocked_worker_retains_account_and_both_binding_forms(self):
        started,release=threading.Event(),threading.Event()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(replace(success(),started=started,release=release),success()),{"provider.close_timeout_ms":10})
            try:
                await f.initialize()
                pending=await f.work.generate(f.request())
                self.assertIs(type(pending),Pending,pending)
                busy=await f.work.generate(f.request("other"))
                self.assertIs(type(busy),Rejected,busy)
                assert type(busy) is Rejected
                self.assertEqual(busy.error.code,"RESOURCE_BUSY")
                second=ProviderService(f.assembly)
                for binding in (f.binding,f.assembly.bind(f.storage,f.snapshot)):
                    denied=await second.initialize(f.snapshot,binding,f.resources)
                    self.assertIs(type(denied),Rejected,denied)
                    self.assertEqual(denied.error.code,"ACCESS_DENIED")
                closed=await f.service.close()
                self.assertEqual(closed.status,"INCOMPLETE")
                self.assertTrue(closed.cleanup_pending)
                self.assertIs(await f.service.close(),closed)
                release.set()
                await until(lambda:f.service.get_health().lifecycle=="CLOSED")
                self.assertEqual(closed.status,"INCOMPLETE")
            finally:
                release.set()
                await until(lambda:f.service.get_health().in_flight==0)
                await f.close()

    async def test_mode_revocation_after_registration_proves_not_sent(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            f.gate.before_dispatch=lambda:setattr(f.gate,"mode","RECOVERY")
            try:
                await f.initialize()
                value=completed(await f.work.generate(f.request()))
                self.assertEqual(value.record["outcome"],"MODE_BLOCKED")
                details=record(found(await f.work.get_request(value.record["object_id"])))
                self.assertEqual(records(details["attempts"])[0]["state"],"NOT_SENT")
                self.assertEqual(len(f.adapter.calls),0)
                budget=records(found(await f.observer.get_budget_state()))[0]
                self.assertEqual((budget["attempt_count"],budget["held_atoms"],budget["known_subtotal_atoms"]),(1,0,0))
            finally:
                await f.close()
