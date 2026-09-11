"""Unified encoder output has scoped event, cursor and independent metadata rights."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.logging_service import ObservationGrant,LoggingOk,LogPage,LogReadFailed
from tests.runtime.support import Fixture
from tests.provider.support import record,records


def query(**changes):
    return {'cursor':None,'limit':1,'start':None,'end':None,'minimum_level':None,'modules':[],'event_codes':[],
        'entry_id':None,'run_id':None,'request_id':None,'attempt_id':None,**changes}


class RuntimeWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_scope_cannot_infer_global_positions_or_has_more(self):
        with TemporaryDirectory(prefix='iris-log-window-') as directory:
            fixture=Fixture(Path(directory));await fixture.initialize()
            try:
                logger=fixture.logging.get_logger('ingress');assert type(logger) is LoggingOk
                for entry in ('first','hidden','hidden','hidden'):
                    logger.value.emit({'level':'INFO','event_code':'OPERATION_COMPLETED','context':{'entry_id':entry},'attributes':{'count':1,'outcome':'SUCCESS'}})
                partial=fixture.log_window.bind_runtime_log_reader(ObservationGrant('instance',('first',),False,False))
                page=await partial.query_runtime_logs(query());self.assertIs(type(page),LogPage,page);assert type(page) is LogPage
                self.assertEqual(len(records(page.value['events'])),1);self.assertFalse(page.value['has_more'])
                self.assertEqual(set(page.value),{'events','next_cursor','has_more','observed_at','coverage'})
                self.assertNotIn('window_metadata',record(partial.read_window_health()))
                token=page.value['next_cursor'];self.assertIs(type(token),str)
                global_meta=fixture.log_window.bind_runtime_log_reader(ObservationGrant('instance',('first',),False,True))
                other=await global_meta.query_runtime_logs(query());assert type(other) is LogPage
                self.assertEqual(len(records(other.value['events'])),1)
                self.assertIn('window_metadata',other.value)
                self.assertFalse(other.value['has_more'])
                crossed=await global_meta.query_runtime_logs(query(cursor=token));self.assertIs(type(crossed),LogReadFailed)
                denied=await partial.query_runtime_logs(query(entry_id='hidden'));assert type(denied) is LogReadFailed
                self.assertEqual(denied.code,'ACCESS_DENIED')
                malformed=await partial.query_runtime_logs(query(minimum_level={}));assert type(malformed) is LogReadFailed
                self.assertEqual(malformed.code,'INVALID_QUERY')
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:await fixture.close()

    async def test_eviction_gaps_project_only_authorized_information(self):
        from companion_memory.logging_service import LogGap
        with TemporaryDirectory(prefix='iris-log-gaps-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'logging.web_window_events':16});await fixture.initialize()
            try:
                logger=fixture.logging.get_logger('ingress');assert type(logger) is LoggingOk
                def emit(entry):logger.value.emit({'level':'INFO','event_code':'OPERATION_COMPLETED','context':{'entry_id':entry},'attributes':{'count':1,'outcome':'SUCCESS'}})
                partial=fixture.log_window.bind_runtime_log_reader(ObservationGrant('instance',('first',),False,False))
                privileged=fixture.log_window.bind_runtime_log_reader(ObservationGrant('instance',('first',),False,True))
                emit('first');emit('first')
                start=await partial.query_runtime_logs(query());assert type(start) is LogPage
                global_start=await privileged.query_runtime_logs(query());assert type(global_start) is LogPage
                self.assertTrue(start.value['has_more'])
                for _ in range(17):emit('hidden')
                gap=await partial.query_runtime_logs(query(cursor=start.value['next_cursor']));assert type(gap) is LogGap,gap
                self.assertEqual(set(gap.value),{'reason','observed_at','coverage','restart_cursor','lost_authorized_events'})
                self.assertEqual(gap.value['lost_authorized_events'],'UNKNOWN')
                global_gap=await privileged.query_runtime_logs(query(cursor=global_start.value['next_cursor']));assert type(global_gap) is LogGap
                self.assertEqual(record(global_gap.value['global_gap'])['reason'],'WINDOW_EVICTED')
                restart=await partial.query_runtime_logs(query(cursor=gap.value['restart_cursor']));assert type(restart) is LogPage
                self.assertFalse(restart.value['has_more']);self.assertEqual(restart.value['events'],())
                token=restart.value['next_cursor']
                for _ in range(17):emit('hidden')
                still=await partial.query_runtime_logs(query(cursor=token));assert type(still) is LogPage,still
                self.assertFalse(still.value['has_more'])
                fixture.log_window.revoke(partial)
                denied=await partial.query_runtime_logs(query(cursor=token));assert type(denied) is LogReadFailed
                self.assertEqual(denied.code,'ACCESS_DENIED')
            finally:await fixture.close()

    async def test_diagnostic_window_failure_does_not_retract_business_or_audit(self):
        from companion_memory.runtime import IngressPort
        from companion_memory.runtime.results import Committed
        from tests.runtime.configuration_support import event
        import sqlite3
        from contextlib import closing
        with TemporaryDirectory(prefix='iris-log-fault-') as directory:
            fixture=Fixture(Path(directory));await fixture.initialize()
            try:
                _,port=await fixture.entry();assert type(port) is IngressPort
                fixture.log_window.mark_unavailable();fixture.logging.close()
                accepted=await port.accept_event(event('audit-survives'));assert type(accepted) is Committed,accepted
                with closing(sqlite3.connect(fixture.path)) as connection:
                    count=connection.execute('SELECT count(*) FROM audit_records WHERE commit_id=?',(accepted.receipt.commit_id,)).fetchone()[0]
                    self.assertEqual(count,2)
                repeated=await port.accept_event(event('audit-survives'));assert type(repeated) is Committed
                self.assertEqual(repeated.receipt,accepted.receipt)
            finally:await fixture.close()
