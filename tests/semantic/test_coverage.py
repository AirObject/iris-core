"""Actual tracker transactions, stale results, rollback and original-key reopen.

The current rows are installed by explicit fixture commands. Assertions prove
memory coverage and atomic audit behavior, not the complete public work chain.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from companion_memory.persistence import Committed,NotCommitted
from tests.persistence.support import sqlite_fault
from tests.semantic.coverage_support import CoverageFixture


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_coalesced_gaps_stale_apply_and_original_receipt_reopen(self):
        with TemporaryDirectory() as directory:
            f=await CoverageFixture(Path(directory)).open()
            try:
                await f.change('A',1);await f.change('B',1);await f.change('A',2)
                _,gap=await f.coverage.semantic_current('A');assert gap is not None
                self.assertEqual((gap['first_uncovered_seq'],gap['latest_change_seq']),(1,3))
                self.assertIs(type(await f.apply('A',1,1)),NotCommitted)
                self.assertEqual((await f.view())['material_seq'],0)
                applied=await f.apply('A',2,3);self.assertIs(type(applied),Committed,applied)
                self.assertEqual((await f.view())['material_seq'],1)
                self.assertIs(type(await f.apply('B',1,2)),Committed)
                self.assertEqual(((await f.view())['material_seq'],(await f.view())['published_seq']),(3,0))
                replay=await f.apply('A',2,3);assert type(replay) is Committed and type(applied) is Committed
                self.assertEqual(replay.receipt,applied.receipt)
            finally:await f.close()
            f=await CoverageFixture(Path(directory)).open('OPEN_EXISTING')
            try:
                replay=await f.apply('A',2,3);assert type(replay) is Committed
                self.assertEqual(replay.receipt,applied.receipt)
                self.assertEqual((await f.view())['material_seq'],3)
                self.assertEqual(len(await f.information.gaps()),2)
            finally:await f.close()

    async def test_apply_then_later_change_retains_new_gap_and_local_delete(self):
        with TemporaryDirectory() as directory:
            f=await CoverageFixture(Path(directory)).open()
            try:
                await f.change('A',1);self.assertIs(type(await f.apply('A',1,1)),Committed)
                await f.change('A',2);await f.change('A',3)
                self.assertIs(type(await f.apply('A',2,2)),NotCommitted)
                _,gap=await f.coverage.semantic_current('A');assert gap is not None
                self.assertEqual((gap['first_uncovered_seq'],gap['latest_change_seq']),(2,3))
                await f.change('A',4,True)
                applied=await f.apply('A',4,4,True);self.assertIs(type(applied),Committed,applied)
                ack=(await f.coverage.ack_page())[0]
                self.assertEqual((ack['action'],ack['resolved_from_seq'],ack['applied_seq'],ack['artifact_id'],ack['material_digest']),('DELETE',2,4,None,None))
                self.assertEqual((await f.view())['material_seq'],4)
                self.assertIs(type(await f.apply('missing',2,5,True)),NotCommitted)
            finally:await f.close()

    async def test_retrieval_failure_rolls_back_memory_ack_gap_and_audits(self):
        with TemporaryDirectory() as directory:
            f=await CoverageFixture(Path(directory)).open()
            try:
                await f.change('A',1)
                def fail(sql):
                    if sql.startswith('UPDATE retrieval_semantic_control'):raise sqlite_fault(sqlite3.SQLITE_FULL)
                f.hooks.before=fail
                result=await f.apply('A',1,1);self.assertIs(type(result),NotCommitted,result)
                f.hooks.before=lambda sql:None
                await f.close()
                f=await CoverageFixture(Path(directory)).open('OPEN_EXISTING')
                self.assertEqual(await f.coverage.ack_page(),())
                self.assertIsNotNone((await f.coverage.semantic_current('A'))[1])
                self.assertEqual((await f.view())['material_seq'],0)
                self.assertIs(type(await f.apply('A',1,1)),Committed)
            finally:f.hooks.before=lambda sql:None;await f.close()
