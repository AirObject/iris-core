"""Exact carriers, scoped observation, historical evidence and result byte limits."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.provider import (CancellationSource, Completed, Failed, Found, NotFound, ObserverGrant, Pending, ProviderResources,
    Ready, Rejected, ResultGrant, Scenario, WorkGrant)
from tests.configuration.provider_support import provider_values
from tests.provider.support import Fixture, completed, found, record, records, success, until


class Hostile:
    def __repr__(self):raise AssertionError("No repr hook.")
    def __iter__(self):raise AssertionError("No iterator hook.")
    def __str__(self):raise AssertionError("No conversion hook.")


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_fields_and_native_subclasses_are_rejected_without_hooks(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            try:
                await f.initialize()
                class DictSubclass(dict):
                    def __iter__(self):raise AssertionError("No subclass iteration.")
                values=(Hostile(),DictSubclass(f.request()),{**f.request(),"unknown":Hostile()},
                        {**f.request(),"deadline":10**1000},{**f.request(),"payload":{"unknown":Hostile()}})
                for value in values:
                    result=await f.work.generate(value)
                    self.assertIs(type(result),Rejected)
                    assert type(result) is Rejected
                    self.assertEqual(result.error.code,"INVALID_INPUT")
                self.assertEqual(f.adapter.calls,())
            finally:
                await f.close()

    async def test_history_uses_original_profile_limits_but_current_grant_permissions(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            await f.initialize()
            original=completed(await f.work.generate(f.request()))
            await f.close()
            values=provider_values("/synthetic/file")
            profiles=cast(list[dict],values["provider.profiles"])
            profiles=[item for item in profiles if item["profile_id"]!="generation"]
            changed={"provider.profiles":profiles,"provider.role_profiles":{"LEARNING":["embedding","rerank","media"]},"provider.request_max_bytes":256,"provider.result_max_bytes":256}
            other=Fixture(Path(directory),(success(),),changed)
            try:
                self.assertIs(type(await other.initialize("OPEN_EXISTING")),Ready)
                existing=completed(await other.work.generate(other.request()))
                self.assertEqual(existing.record,original.record)
                self.assertEqual(existing.result,original.result)
                denied=other.service.bind_work(replace(other.grant,profiles=("embedding",)))
                value=await denied.generate(other.request())
                assert type(value) is Rejected,value
                self.assertEqual(value.error.code,"ACCESS_DENIED")
                conflict=other.request();conflict["payload"]["messages"][0]["text"]="changed"
                value=await other.work.generate(conflict)
                assert type(value) is Rejected,value
                self.assertEqual(value.error.code,"IDEMPOTENCY_CONFLICT")
                self.assertEqual(other.adapter.calls,())
            finally:
                await other.close()

    async def test_scoped_reads_do_not_reveal_foreign_existence_or_handoff_payload(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            try:
                await f.initialize()
                result=completed(await f.work.generate(f.request()))
                stranger=f.service.bind_observer(ObserverGrant(("other_scope",)))
                self.assertIs(type(await stranger.get_request(result.record["object_id"])),NotFound)
                self.assertIs(type(await stranger.get_request("absent-request")),NotFound)
                self.assertIs(type(await stranger.get_budget_state()),Failed)
                inspected=record(found(await f.observer.get_request(result.record["object_id"])))
                self.assertNotIn("hello",repr(inspected))
                denied=await stranger.query_usage(f.query(caller_scope="sample_scope"))
                assert type(denied) is Failed
                self.assertEqual(denied.error.code,"ACCESS_DENIED")
                owner=f.service.bind_result_owner(ResultGrant("other_owner",(cast(str,result.record["object_id"]),)))
                self.assertIs(type(await owner.recover_result(result.record["object_id"])),Failed)
            finally:
                await f.close()

    async def test_complete_result_byte_boundary_with_minimum_storage_capacity(self):
        overhead=len(json.dumps({"text":"","stop_reason":"STOP"},ensure_ascii=False,separators=(",",":"),sort_keys=True).encode())
        for capacity in (57344,65536):
            with self.subTest(capacity=capacity),TemporaryDirectory() as directory:
                text="x"*(8192-overhead)
                f=Fixture(Path(directory),(success(text),),{"provider.result_max_bytes":8192,"storage.command_max_bytes":capacity,"storage.receipt_max_bytes":capacity})
                try:
                    await f.initialize()
                    value=completed(await f.work.generate(f.request()))
                    self.assertEqual(value.record["outcome"],"SUCCEEDED")
                    self.assertEqual(record(value.result)["text"],text)
                    self.assertEqual(len(json.dumps(dict(record(value.result)),ensure_ascii=False,separators=(",",":"),sort_keys=True).encode()),8192)
                finally:
                    await f.close()
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success("x"*(8193-overhead)),),{"provider.result_max_bytes":8192})
            try:
                await f.initialize()
                value=completed(await f.work.generate(f.request()))
                self.assertEqual(value.record["outcome"],"FAILED")
                self.assertEqual(record(value.record["first_error"])["reason"],"INVALID_RESPONSE")
                self.assertEqual(records(found(await f.observer.get_budget_state()))[0]["known_subtotal_atoms"],35)
            finally:
                await f.close()

    async def test_focused_mode_requires_actual_internal_dream_grant(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            try:
                await f.initialize();f.gate.mode="FOCUSED"
                denied=completed(await f.work.generate(f.request("ordinary")))
                self.assertEqual(denied.record["outcome"],"MODE_BLOCKED")
                dream=f.service.bind_work(replace(f.grant,task_role="DREAM"))
                denied=completed(await dream.generate(f.request("role-only")))
                self.assertEqual(denied.record["outcome"],"MODE_BLOCKED")
                authorized=f.service.bind_work(replace(f.grant,task_role="DREAM",internal_dream=True))
                result=completed(await authorized.generate(f.request("real-grant")))
                self.assertEqual(result.record["outcome"],"SUCCEEDED")
                self.assertEqual(len(f.adapter.calls),1)
            finally:
                await f.close()

    async def test_utc_query_variants_and_no_success_result_use_read_branches(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(Scenario("SENSITIVE_REFUSAL",None,{"coverage":"COMPLETE","known_cost_atoms":0}),))
            try:
                await f.initialize()
                result=completed(await f.work.generate(f.request()))
                owner=f.service.bind_result_owner(ResultGrant("sample_owner",(cast(str,result.record["object_id"]),)))
                restored=await owner.recover_result(result.record["object_id"])
                self.assertIs(type(restored),Found)
                self.assertEqual(record(found(restored))["outcome"],"SENSITIVE_REFUSAL")
                self.assertIsNone(record(found(restored))["result"])
                result=await f.observer.query_usage(f.query(start="2000-01-01T00:00:00Z",end="2000-01-01T00:00:00.1+00:00"))
                self.assertIs(type(result),Found,result)
                self.assertEqual(record(found(result))["sample_count"],0)
            finally:
                await f.close()

    async def test_expired_and_cancelled_calls_do_not_create_attempts(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),))
            try:
                await f.initialize()
                value=f.request();value["deadline"]=-1
                expired=await f.work.generate(value)
                assert type(expired) is Rejected
                self.assertEqual(expired.error.code,"TIMEOUT")
                f.cancellation.cancel()
                cancelled=await f.work.generate(f.request())
                assert type(cancelled) is Rejected
                self.assertEqual(cancelled.error.code,"CANCELLED")
                self.assertEqual(f.adapter.calls,())
                self.assertEqual(records(found(await f.observer.get_budget_state()))[0]["attempt_count"],0)
            finally:
                await f.close()
