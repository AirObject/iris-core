"""Two-page deadline component test with explicitly fabricated paid artifacts.

The public host tests separately prove Provider and fixed-source establishment.
This fixture proves only actual index SQLite/file ownership over nine members.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.semantic.generation_support import GenerationFixture
from tests.semantic.test_generations import FIRST


class TwoPageDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_two_pages_cannot_refresh_budget_after_first_page(self):
        with TemporaryDirectory() as directory:
            fixture=await GenerationFixture(Path(directory)).open()
            try:
                for ordinal in range(9):
                    name=f'M:{ordinal:02}'
                    await fixture.change(name,1)
                    self.assertIs(type(await fixture.apply(name,1,ordinal+1)),Committed)
                self.assertIs(type(await fixture.command('begin_generation','begin-two',{'generation_id':FIRST,
                    'space_id':fixture.coverage.space_id,'captured_seq':9,'expected_control_revision':(await fixture.control())['revision']})),Committed)
                for page in (0,1):
                    self.assertIs(type(await fixture.command('append_page',f'append:{page}',{'generation_id':FIRST,
                        'expected_revision':(await fixture.row(FIRST))['revision'],'page_no':page,'after_object_id':None if page==0 else 'M:07'})),Committed)
                    proof=await fixture.generations.write_page(FIRST,page)
                    self.assertIs(type(await fixture.command('confirm_page',f'confirm:{page}',{'generation_id':FIRST,
                        'expected_revision':(await fixture.row(FIRST))['revision'],'page_no':page,'page_digest':proof.page_digest})),Committed)
                sealed=await fixture.generations.seal_file(FIRST)
                self.assertIs(type(await fixture.command('seal_generation','seal-two',{'generation_id':FIRST,
                    'expected_revision':(await fixture.row(FIRST))['revision'],'file_digest':sealed.file_digest,'file_bytes':sealed.file_bytes})),Committed)
                self.assertIs(type(await fixture.publish(FIRST)),Committed)
                target=fixture.files._root/fixture.files._name(FIRST);target.unlink()
                original=fixture.files.stage_page;started=[];completed=[]
                def slow(space,generation,captured,page,members,checkpoint):
                    started.append(page);time.sleep(2.7)
                    result=original(space,generation,captured,page,members,checkpoint)
                    completed.append(page);return result
                with patch.object(fixture.files,'stage_page',slow):
                    start=time.monotonic()
                    with self.assertRaises(OwnerFailure) as failure:await fixture.generations.rebuild(FIRST)
                    self.assertEqual(failure.exception.code,'TIMEOUT');self.assertLess(time.monotonic()-start,5.5)
                    self.assertTrue(fixture.generations.io_pending)
                    self.assertEqual(started,[0,1]);self.assertEqual(completed,[0])
                    actual=fixture.generations._io;assert actual is not None
                    await asyncio.gather(actual,return_exceptions=True)
                self.assertEqual(completed,[0]);self.assertFalse(target.exists())
                recovered=await fixture.generations.rebuild(FIRST)
                self.assertEqual(recovered.file_digest,sealed.file_digest)
                self.assertEqual(recovered.file_bytes,sealed.file_bytes)
                self.assertEqual((await fixture.row(FIRST))['page_count'],2)
            finally:await fixture.close()
