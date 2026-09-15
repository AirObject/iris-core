"""Native SQLite publication and old-revision rejection with actual vector files."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
import subprocess
import sys
from companion_memory.persistence import Committed,NotCommitted
from tests.semantic.generation_support import GenerationFixture

FIRST='semantic-generation:'+'1'*64
SECOND='semantic-generation:'+'2'*64
THIRD='semantic-generation:'+'3'*64


class SemanticGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_generations_real_reader_retirement_and_paid_artifacts_survive(self):
        with TemporaryDirectory() as directory:
            f=await GenerationFixture(Path(directory)).open()
            try:
                await f.change('A',1);self.assertIs(type(await f.apply('A',1,1)),Committed)
                first=await f.build(FIRST,1);self.assertIs(type(await f.publish(FIRST)),Committed)
                await f.change('B',1);self.assertIs(type(await f.apply('B',1,2)),Committed)
                await f.build(SECOND,2)
                with f.files.reader(first):
                    self.assertIs(type(await f.publish(SECOND)),Committed)
                    self.assertFalse(await f.generations.prepare_retirement(FIRST))
                    denied=await f.command('begin_generation','third',{'generation_id':THIRD,'space_id':f.coverage.space_id,
                        'captured_seq':2,'expected_control_revision':(await f.control())['revision']})
                    self.assertIs(type(denied),NotCommitted)
                pages=0
                while (await f.row(FIRST))['state']!='RETIRED':
                    self.assertTrue(await f.generations.prepare_retirement(FIRST));row=await f.row(FIRST)
                    result=await f.command('retire_page','retire:'+str(pages),{'generation_id':FIRST,'expected_revision':row['revision'],
                        'after_row_id':row['retired_cursor'],'expected_control_revision':(await f.control())['revision']})
                    self.assertIs(type(result),Committed,result);pages+=1;self.assertLessEqual(pages,3)
                self.assertEqual(pages,2);self.assertFalse((f.root/'vectors'/FIRST).exists())
                self.assertEqual((await f.control())['current_generation'],SECOND)
                self.assertEqual(len(await f.retrieval.page('embedding_artifact')),2)
                self.assertEqual((await f.view())['published_seq'],2)
                self.assertIs(type(await f.command('begin_generation','third-after-retire',{'generation_id':THIRD,'space_id':f.coverage.space_id,
                    'captured_seq':2,'expected_control_revision':(await f.control())['revision']})),Committed)
            finally:await f.close()

    async def test_current_revision_changes_before_publication_preserve_original_pointer(self):
        with TemporaryDirectory() as directory:
            f=await GenerationFixture(Path(directory)).open()
            try:
                await f.change('A',1);self.assertIs(type(await f.apply('A',1,1)),Committed)
                await f.build(FIRST,1);self.assertIs(type(await f.publish(FIRST)),Committed)
                await f.build(SECOND,1)
                await f.change('A',2)
                result=await f.publish(SECOND);self.assertIs(type(result),NotCommitted,result)
                self.assertEqual((await f.control())['current_generation'],FIRST)
                self.assertEqual((await f.row(SECOND))['state'],'READY')
                self.assertEqual((await f.view())['published_seq'],1)
                self.assertIsNotNone((await f.coverage.semantic_current('A'))[1])
                self.assertIs(type(await f.command('generation_fail','failed-second',{'generation_id':SECOND,
                    'expected_revision':(await f.row(SECOND))['revision'],'error':'STORAGE'})),Committed)
                step=0
                while (await f.row(SECOND))['state']!='RETIRED':
                    self.assertTrue(await f.generations.prepare_retirement(SECOND));row=await f.row(SECOND)
                    self.assertIs(type(await f.command('retire_page','failed-retire:'+str(step),{'generation_id':SECOND,
                        'expected_revision':row['revision'],'after_row_id':row['retired_cursor'],
                        'expected_control_revision':(await f.control())['revision']})),Committed)
                    step+=1;self.assertLessEqual(step,3)
                self.assertEqual(len(await f.retrieval.page('embedding_artifact')),1)
                self.assertIs(type(await f.apply('A',2,2)),Committed)
                await f.build(THIRD,2);self.assertIs(type(await f.publish(THIRD)),Committed)
                self.assertEqual((await f.view())['published_seq'],2)
            finally:await f.close()

    async def test_nine_members_two_pages_and_bounded_retirement(self):
        with TemporaryDirectory() as directory:
            f=await GenerationFixture(Path(directory)).open()
            try:
                for ordinal in range(9):
                    name=f'M:{ordinal:02}'
                    await f.change(name,1);self.assertIs(type(await f.apply(name,1,ordinal+1)),Committed)
                self.assertIs(type(await f.command('begin_generation','begin-nine',{'generation_id':FIRST,'space_id':f.coverage.space_id,
                    'captured_seq':9,'expected_control_revision':(await f.control())['revision']})),Committed)
                after=None
                for page,count in ((0,8),(1,1)):
                    result=await f.command('append_page',f'append-nine:{page}',{'generation_id':FIRST,'expected_revision':(await f.row(FIRST))['revision'],
                        'page_no':page,'after_object_id':after});self.assertIs(type(result),Committed,result)
                    proof=await f.generations.write_page(FIRST,page);self.assertEqual(proof.member_count,count)
                    self.assertIs(type(await f.command('confirm_page',f'confirm-nine:{page}',{'generation_id':FIRST,
                        'expected_revision':(await f.row(FIRST))['revision'],'page_no':page,'page_digest':proof.page_digest})),Committed)
                    after='M:07'
                sealed=await f.generations.seal_file(FIRST)
                self.assertIs(type(await f.command('seal_generation','seal-nine',{'generation_id':FIRST,
                    'expected_revision':(await f.row(FIRST))['revision'],'file_digest':sealed.file_digest,'file_bytes':sealed.file_bytes})),Committed)
                self.assertIs(type(await f.publish(FIRST)),Committed)
                self.assertEqual((await f.row(FIRST))['page_count'],2)
            finally:await f.close()

    async def test_empty_generation_seals_publishes_and_reopens_without_provider(self):
        with TemporaryDirectory() as directory:
            path=Path(directory);f=await GenerationFixture(path).open()
            try:
                await f.build(FIRST,0,False)
                result=await f.publish(FIRST);self.assertIs(type(result),Committed,result)
                assert type(result) is Committed
                receipt=result.receipt
            finally:await f.close()
            f=await GenerationFixture(path).open('OPEN_EXISTING')
            try:
                self.assertEqual((await f.control())['current_generation'],FIRST)
                self.assertEqual(f.files.verify(f.coverage.space_id,FIRST,lambda:None).file_bytes,4096)
                definition=next(d for d in f.semantic_commands.commands if d.operation_kind=='publish_generation')
                confirmed=await f.storage.bind_operation(definition,'instance').read_receipt('publish:'+FIRST)
                from companion_memory.persistence import Found
                self.assertIs(type(confirmed),Found,confirmed)
                assert type(confirmed) is Found
                self.assertEqual(confirmed.value,receipt)
            finally:await f.close()
            result=subprocess.run([sys.executable,'-m','tests.semantic.generation_recovery_worker',str(path),FIRST],
                capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            recovered=json.loads(result.stdout)
            self.assertEqual(recovered['generation'],FIRST)
            self.assertEqual(recovered['receipt_commit_id'],receipt.commit_id)
            self.assertEqual(recovered['real_provider_calls'],0)

