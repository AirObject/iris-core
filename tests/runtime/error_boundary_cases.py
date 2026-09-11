"""Safe durable-data failures exercised unchanged with and without assertions."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock,patch
from typing import cast
from companion_memory.runtime import FocusGrant,FocusPort,IngressPort,WorkCapability
from companion_memory.runtime.records import digest,DomainFailure
from companion_memory.runtime.results import Committed,Found,Rejected,NotCommitted
from companion_memory.persistence import Failed,PersistenceError
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event
from tests.provider.support import record


class ErrorEnvelopeCases(unittest.IsolatedAsyncioTestCase):
    def envelope(self,result,operation,field='storage',reason='INTEGRITY_FAILURE'):
        self.assertIn(type(result),(Rejected,NotCommitted))
        error=result.error
        self.assertEqual((error.code,error.operation,error.field,error.reason,error.cleanup_pending),('STORAGE_FAILED',operation,field,reason,False))
        self.assertNotIn('secret',repr(result));self.assertNotIn('SELECT',repr(result))

    async def test_invalid_json_shape_time_and_missing_durable_state_are_safe(self):
        for damage in ('duplicate','shape','time','missing'):
            with self.subTest(damage=damage),TemporaryDirectory(prefix='iris-error-envelope-') as directory:
                fixture=Fixture(Path(directory));runtime=await fixture.initialize()
                try:
                    eid,port=await fixture.entry()
                    if type(port) is not IngressPort:self.fail(port)
                    for key in ('one','two','three'):await port.accept_event(event(key))
                    with closing(sqlite3.connect(fixture.path)) as connection:
                        if damage=='missing':connection.execute('DELETE FROM buffers_entry_state')
                        else:
                            identifier,body=connection.execute('SELECT object_id,body FROM ingress_events ORDER BY sequence LIMIT 1').fetchone()
                            if damage=='duplicate':body='{"version":1,"version":1,"data":{"secret":"private"},"digest":"secret"}'
                            elif damage=='shape':body='["secret"]'
                            else:
                                value=json.loads(body);value['data']['received_at_us']=2**63-1;value['digest']=digest(value['data']);body=json.dumps(value)
                            connection.execute('UPDATE ingress_events SET body=? WHERE object_id=?',(body,identifier))
                        connection.commit()
                    result=await runtime.request_learning({'entry_id':eid,'trigger_key':'learn','type':'THRESHOLD'})
                    self.envelope(result,'request_learning')
                    if damage=='missing':self.envelope(await port.accept_event(event('four')),'accept_event')
                    with closing(sqlite3.connect(fixture.path)) as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM buffers_batches').fetchone()[0],0)
                    self.assertFalse(fixture.adapter.calls)
                finally:await fixture.close()

    async def test_storage_causes_survive_control_and_candidate_classification(self):
        with TemporaryDirectory(prefix='iris-error-cause-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry()
                if type(port) is not IngressPort:self.fail(port)
                for key in ('one','two','three'):await port.accept_event(event(key))
                frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'learn','type':'THRESHOLD'})
                if type(frozen) is not Committed:self.fail(frozen)
                claimed=await runtime.claim_work(cast(str,record(frozen.receipt.result)['object_id']),1)
                if type(claimed) is not Found or type(claimed.value) is not WorkCapability:self.fail(claimed)
                with patch.object(runtime._models,'verify_outcome',AsyncMock(side_effect=DomainFailure('STORAGE_FAILED','storage','READ_FAILED'))):
                    self.envelope(await runtime.stage_candidate(claimed.value,{}),'stage_candidate',reason='READ_FAILED')
                malformed=await runtime.stage_candidate(claimed.value,{'malformed':object()})
                if type(malformed) is not Rejected:self.fail(malformed)
                self.assertEqual(malformed.error.reason,'RESULT_INVALID')
                focus=runtime.bind_focus_coordinator(FocusGrant('instance','dream','coordinator'))
                if type(focus) is not FocusPort:self.fail(focus)
                failure=Failed(PersistenceError('RESOURCE_BUSY','read_receipt','resources','LOCK_DEADLINE',False))
                with patch.object(type(runtime._operations['focus_ready']),'read_receipt',AsyncMock(return_value=failure)):
                    self.envelope(await focus.enter_focus('enter',runtime._epoch),'enter_focus',reason='READ_FAILED')
                self.assertFalse(fixture.adapter.calls)
            finally:await fixture.close()

    async def test_configuration_catalog_and_missing_version_return_fixed_errors(self):
        from companion_memory.configuration import RuntimeConfigurationErr
        for damage,reason in (('duplicate','CONTENT_MISMATCH'),('decimal','CONTENT_MISMATCH'),('shape','FORMAT_UNSUPPORTED'),('missing','VERSION_MISSING')):
            with self.subTest(damage=damage),TemporaryDirectory(prefix='iris-config-error-envelope-') as directory:
                fixture=Fixture(Path(directory));await fixture.initialize()
                try:
                    with closing(sqlite3.connect(fixture.path)) as connection:
                        if damage=='missing':connection.execute('DELETE FROM configuration_domains')
                        elif damage=='decimal':
                            rowid,body=connection.execute('SELECT rowid,body FROM configuration_entries LIMIT 1').fetchone()
                            value=json.loads(body);value.update(state='PRESENT',value={'type':'decimal','value':'secret'},source='EXPLICIT')
                            connection.execute('UPDATE configuration_entries SET body=? WHERE rowid=?',(json.dumps(value),rowid))
                        else:
                            body=connection.execute('SELECT body FROM configuration_snapshots').fetchone()[0]
                            body=body.replace('{','{"version":1,',1) if damage=='duplicate' else '["secret"]'
                            connection.execute('UPDATE configuration_snapshots SET body=?',(body,))
                        connection.commit()
                    binding=fixture.config_binding
                    if binding is None:self.fail('Configuration must be bound.')
                    result=await binding.load_configuration_snapshot('configuration:1',fixture.candidate.material_contracts,fixture.supplied[3],bootstrap=fixture.candidate)
                    if type(result) is not RuntimeConfigurationErr:self.fail(result)
                    self.assertEqual((result.error.code,result.error.operation,result.error.field,result.error.reason),('INTEGRITY_FAILURE','load_configuration_snapshot','storage',reason))
                    self.assertNotIn('secret',repr(result));self.assertFalse(fixture.adapter.calls)
                finally:await fixture.close()
