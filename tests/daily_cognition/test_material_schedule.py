"""Lossless physical material pages and explicit recovered scheduling admission."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.runtime.results import NotCommitted
from companion_memory.persistence.daily_records import identity
from companion_memory.persistence.content_codec import encode_content
from companion_memory.cognition.daily_material import freeze_material
from .test_persona_import import ImportFixture


def material(configuration):
    db=configuration.database_id;instance=configuration.scope_id
    cid=identity('learning-context',db,instance,'reasoning-run','provider-output')
    # Ordinary quotes, escaped backslashes and multi-byte text exercise complete
    # canonical-leaf limits rather than a padded DDL or partial source record.
    body=encode_content(MappingProxyType({'output':'这是原始材料。 "quoted" \\ complete\n'*700}),65536)
    metadata={'format_version':1,'object_id':cid,'revision':1,'database_id':db,'instance_id':instance,
        'config_snapshot_id':configuration.snapshot_id,'created_at_us':1,'updated_at_us':1,'context_version':2,
        'context_kind':'PROVIDER_RESULT','owner_ref':'reasoning-run','batch_id':'batch','run_id':'reasoning-run','source_id':'source',
        'state':'STORED','persona_publication_id':None,'persona_revision':None,'prompt_ref':None,'schema_ref':'raw-provider-response',
        'transform_ref':None,'model_binding_digest':'a'*64,'ordered_members':(),'related_objects':(),'wire_digest':None,
        'input_token_estimate':None,'reservation_input_bound':0,'original_operation':{'owner_namespace':'cognition','operation_kind':'seal_material',
        'scope_id':instance,'operation_key':'material-seal:'+cid.removeprefix('learning-context:')},'terminal_operation':None}
    return freeze_material(metadata,body)

class MaterialScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_physical_pages_full_bytes_and_reopen_reuse_original_receipts(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);f=await ImportFixture(root).open()
            try:
                original=material(f.materials.configuration)
                self.assertGreater(len(original.leaves),4)
                for leaf in original.leaves:self.assertLessEqual(len(encode_content(leaf,8192)),8192)
                first=await f.materials.persist(original,time.monotonic()+5)
                self.assertIs(type(first),Committed,first)
                if type(first) is not Committed:raise AssertionError(first)
                loaded=await f.materials.read(cast(str,original.manifest['object_id']),time.monotonic()+5)
                if type(loaded) is not Found:raise AssertionError(loaded)
                self.assertEqual(loaded.value,original)
                again=await f.materials.persist(original,time.monotonic()+5)
                if type(again) is not Committed:raise AssertionError(again)
                self.assertEqual(again.receipt,first.receipt)
            finally:await f.close()
            f=await ImportFixture(root,'OPEN_EXISTING').open()
            try:
                loaded=await f.materials.read(cast(str,original.manifest['object_id']),time.monotonic()+5)
                if type(loaded) is not Found:raise AssertionError(loaded)
                self.assertEqual(loaded.value,original)
                again=await f.materials.persist(material(f.materials.configuration),time.monotonic()+5)
                if type(again) is not Committed:raise AssertionError(again)
                self.assertEqual(again.receipt,first.receipt)
            finally:await f.close()

    async def test_entry_claim_competition_and_reopen_never_enables_dispatch(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);f=await ImportFixture(root).open()
            try:
                schedule=f.schedule
                initialized=await schedule.execute('initialize_daily_schedule','schedule',{},'bootstrap')
                if type(initialized) is not Committed:raise AssertionError(initialized)
                resumed=await schedule.execute('resume_learning','resume',{'expected_revision':1,'mode_epoch':1},'bootstrap')
                if type(resumed) is not Committed:raise AssertionError(resumed)
                self.assertTrue(schedule.enabled)
                for key,entry in (('first','entry-a'),('second','entry-a'),('third','entry-b')):
                    queued=await schedule.execute('enqueue_learning',key,{'entry_id':entry,'reason':'FOCUS','target_through_seq':2},'bootstrap')
                    if type(queued) is not Committed:raise AssertionError(queued)
                queue=await schedule.queued()
                self.assertEqual(tuple(row['entry_id'] for row in queue),('entry-a','entry-b'))
                first_id=cast(str,queue[0]['object_id'])
                claim=await schedule.execute('claim_learning','claim',{'trigger_id':first_id,'expected_revision':1,'schedule_revision':2,'batch_id':'batch-a'},'bootstrap')
                if type(claim) is not Committed:raise AssertionError(claim)
                second=next(row for row in await schedule.queued() if row['entry_id']=='entry-a')
                collision=await schedule.execute('claim_learning','collision',{'trigger_id':second['object_id'],'expected_revision':1,'schedule_revision':3,'batch_id':'batch-other'},'bootstrap')
                self.assertIs(type(collision),NotCommitted,collision)
                if type(collision) is NotCommitted:
                    self.assertIsNotNone(collision.error)
                    if collision.error is not None:self.assertEqual(collision.error.reason,'WRITE_NOT_COMMITTED')
                observed=await schedule.current()
                if observed is None:raise AssertionError()
                self.assertEqual(observed['revision'],3)
            finally:await f.close()
            f=await ImportFixture(root,'OPEN_EXISTING').open()
            try:
                current=await f.schedule.current()
                if current is None:raise AssertionError()
                self.assertEqual(current['state'],'ENABLED');self.assertFalse(f.schedule.enabled)
                confirmed=await f.schedule.execute('resume_learning','resume',{'expected_revision':1,'mode_epoch':1},'bootstrap')
                if type(confirmed) is not Committed:raise AssertionError(confirmed)
                self.assertEqual(confirmed.receipt,resumed.receipt);self.assertFalse(f.schedule.enabled)
            finally:await f.close()
