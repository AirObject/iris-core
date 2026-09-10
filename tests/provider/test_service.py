"""Public model calls must yield one real durable and exactly accounted result."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from companion_memory.provider import Completed, Found, Ready, ResultGrant
from tests.provider.support import Fixture, success, completed, found, record, records


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_commits_original_result_and_exact_shared_budget(self):
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), (success(),))
            try:
                self.assertIs(type(await fixture.initialize()), Ready)
                result = completed(await fixture.work.generate(fixture.request()))
                self.assertIs(type(result), Completed, result)
                self.assertEqual(result.record["outcome"], "SUCCEEDED")
                self.assertEqual(record(result.result)["text"], "hello")
                replay = completed(await fixture.work.generate(fixture.request()))
                self.assertEqual(replay.result, result.result)
                self.assertEqual(replay.source, "EXISTING")
                budget = await fixture.observer.get_budget_state()
                self.assertIs(type(budget), Found, budget)
                self.assertEqual(records(found(budget))[0]["known_subtotal_atoms"], 35)
                self.assertEqual(records(found(budget))[0]["held_atoms"], 0)
                self.assertEqual(records(found(budget))[0]["attempt_count"], 1)
                aggregate = await fixture.observer.query_usage(fixture.query())
                self.assertIs(type(aggregate), Found, aggregate)
                self.assertEqual(record(found(aggregate))["sample_count"], 1)
                self.assertEqual(records(record(found(aggregate))["rows"])[0]["known_cost_atoms"], 35)
                owner = fixture.service.bind_result_owner(ResultGrant("sample_owner", (str(result.record["object_id"]),)))
                recovered = await owner.recover_result(result.record["object_id"])
                self.assertIs(type(recovered), Found, recovered)
                self.assertEqual(record(found(recovered))["result"], result.result)
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally:
                await fixture.close()
