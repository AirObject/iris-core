"""Reject false run completion when unknown requests or real cleanup remain."""
import unittest
from companion_memory.dream.records import validate_run,dream_catalog
from companion_memory.persistence.schema import InvalidValue


class DreamRunRecordTests(unittest.TestCase):
    def run_record(self):
        operation={'owner_namespace':'dream','operation_kind':'start_dream','scope_id':'instance','operation_key':'start'}
        return {'format_version':1,'object_id':'run','revision':1,'database_id':'database','instance_id':'instance',
            'config_snapshot_id':'configuration','created_at_us':0,'updated_at_us':0,'run_id':'run','scope':'instance',
            'schedule_revision':1,'local_date':None,'mode':'BACKGROUND','trigger':'MANUAL','state':'RUNNING','mode_epoch':1,
            'deadline_at_us':1200000000,'step_deadline_at_us':None,'object_cursor':'','expiry_after_us':0,'expiry_after_id':'','impact_cursor':0,'self_cursor':'',
            'objects_used':0,'edges_used':0,'model_calls_used':0,'steps_completed':0,'steps_deferred':0,'active_step_id':None,
            'remaining_work':True,'coverage':'NOT_SCANNED','local_confirmation':'CONFIRMED','remote_result':'NONE',
            'cleanup_pending':False,'exit_result':None,'persona_publication_id':None,'end_reason':'NONE',
            'original_operation':operation,'last_operation':operation}

    def test_unknown_and_cleanup_cannot_be_hidden_by_completion(self):
        record=self.run_record()
        self.assertEqual(validate_run(record)['state'],'RUNNING')
        completed={**record,'state':'COMPLETED','exit_result':'KEPT_PREVIOUS'}
        self.assertEqual(validate_run(completed)['exit_result'],'KEPT_PREVIOUS')
        for change in ({'remote_result':'UNKNOWN'},{'remote_result':'PENDING'},{'cleanup_pending':True},
                {'local_confirmation':'UNCONFIRMED'},{'active_step_id':'step','step_deadline_at_us':10}):
            with self.assertRaises(InvalidValue):validate_run({**completed,**change})
            self.assertEqual(validate_run({**record,'state':'RECOVERY_REQUIRED',**change})['state'],'RECOVERY_REQUIRED')

    def test_schedule_and_publication_claims_require_real_corresponding_fields(self):
        record=self.run_record()
        for change in ({'trigger':'SCHEDULED'},{'local_date':'2026-09-16'},
                {'trigger':'SCHEDULED','local_date':'2026-02-30'},
                {'state':'COMPLETED','exit_result':'PUBLISHED_NEW'},
                {'state':'ABORTED','exit_result':'KEPT_PREVIOUS'}):
            with self.assertRaises(InvalidValue):validate_run({**record,**change})
        catalog=dream_catalog()
        names={table.name for table in catalog.definition.tables}
        self.assertIn('dream_one_active_run',names)
        self.assertIn('dream_one_work_claim',names)
