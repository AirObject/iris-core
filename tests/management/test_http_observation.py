"""Actual socket requests exercise capabilities, safe rendering and fault views."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.configuration.observation_settings import observation_settings
from companion_memory.management import ReadOnlyHTTP
from companion_memory.runtime import RuntimeObservationGrant
from tests.runtime.support import Fixture


async def request(address,path,token=None,method='GET'):
    reader,writer=await asyncio.open_connection(*address)
    writer.write((method+' '+path+' HTTP/1.1\r\nHost: 127.0.0.1\r\n'+('Authorization: Bearer '+token+'\r\n' if token else '')+'\r\n').encode())
    await writer.drain();response=await reader.read();writer.close();await writer.wait_closed()
    header,body=response.split(b'\r\n\r\n',1)
    return int(header.split(b' ')[1]),body


class HTTPObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_log_default_comes_from_its_own_configuration_projection(self):
        from companion_memory.logging_service import ObservationGrant
        with TemporaryDirectory(prefix='iris-http-log-default-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'management.observation_row_limit':64,'logging.web_query_row_limit':8,'management.refresh_min_interval_ms':100})
            await fixture.initialize()
            server=ReadOnlyHTTP(observation_settings(fixture.candidate));address=await server.start()
            try:
                eid,_=await fixture.entry()
                logs=fixture.log_window.bind_runtime_log_reader(ObservationGrant('instance',(eid,),False,False))
                token=server.issue_test_session(None,logs,time.monotonic()+60)
                for suffix,expected in (('',200),('?limit=8',200),('?limit=9',400)):
                    status,body=await request(address,'/api/observe/logs'+suffix,token)
                    self.assertEqual(status,expected,body)
                    if status==400:self.assertEqual(json.loads(body),{'error':'INVALID_QUERY','reason':'LIMIT_EXCEEDED'})
                    else:self.assertEqual(json.loads(body)['coverage'],'CURRENT_PROCESS_WINDOW')
                    await asyncio.sleep(0.11)
            finally:await server.close();await fixture.close()

    async def test_complete_oversized_view_is_limit_error_and_smaller_page_is_usable(self):
        from companion_memory.runtime.results import Failed,Found
        with TemporaryDirectory(prefix='iris-http-view-limit-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'management.observation_max_bytes':2048,'logging.web_query_max_bytes':2048,'management.refresh_min_interval_ms':100})
            runtime=await fixture.initialize()
            server=ReadOnlyHTTP(observation_settings(fixture.candidate));address=await server.start()
            try:
                from companion_memory.runtime.records import stable_id
                entries=tuple(stable_id('entry','instance','host','sample_platform','entry:'+str(i)) for i in range(5))
                await fixture.entry('entry:0')
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',entries))
                query={'entry_id':None,'cursor':None,'limit':5}
                cached=await observer.read_entry_status(query);assert type(cached) is Found,cached
                cursor_count=len(runtime._observations.cursors)
                for index in range(1,5):await fixture.entry('entry:'+str(index))
                oversized=await observer.read_entry_status(query);assert type(oversized) is Failed,oversized
                self.assertEqual((oversized.error.code,oversized.error.reason),('INVALID_INPUT','LIMIT_EXCEEDED'))
                self.assertEqual(len(runtime._observations.cursors),cursor_count)
                smaller=await observer.read_entry_status({**query,'limit':1});assert type(smaller) is Found,smaller
                from tests.provider.support import record,records
                self.assertEqual(record(smaller.value)['availability'],'AVAILABLE')
                self.assertEqual(len(records(record(smaller.value)['rows'])),1)
                token=server.issue_test_session(observer,None,time.monotonic()+60)
                status,body=await request(address,'/api/observe/entries?limit=5',token)
                self.assertEqual((status,json.loads(body)),(400,{'error':'INVALID_INPUT','reason':'LIMIT_EXCEEDED'}))
                await asyncio.sleep(0.11)
                status,body=await request(address,'/api/observe/entries?limit=1',token)
                self.assertEqual(status,200,body);self.assertEqual(json.loads(body)['availability'],'AVAILABLE')
                self.assertEqual(len(json.loads(body)['rows']),1)
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:await server.close();await fixture.close()

    async def test_real_http_permissions_escape_and_no_write_routes(self):
        with TemporaryDirectory(prefix='iris-http-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            server=ReadOnlyHTTP(observation_settings(fixture.candidate));address=await server.start()
            try:
                eid,port=await fixture.entry()
                scoped=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                instance=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,),True))
                token=server.issue_test_session(scoped,None,time.monotonic()+60)
                global_token=server.issue_test_session(instance,None,time.monotonic()+60)
                self.assertEqual((await request(address,'/status'))[0],401)
                self.assertEqual((await request(address,'/api/observe/runtime',token))[0],403)
                status,body=await request(address,'/api/observe/entries?entry_id='+eid,token)
                self.assertEqual(status,200,body)
                self.assertEqual(json.loads(body)['rows'][0]['pending_total'],0)
                self.assertEqual((await request(address,'/api/observe/logs',global_token))[0],403)
                self.assertEqual((await request(address,'/api/observe/runtime',global_token,'POST'))[0],405)
                self.assertEqual((await request(address,'/api/admin/activate',global_token))[0],404)
                status,body=await request(address,'/api/observe/runtime',global_token)
                self.assertEqual(status,200,body);self.assertEqual(json.loads(body)['rows'][0]['mode'],'NORMAL')
                status,page=await request(address,'/status',global_token)
                self.assertEqual(status,200);self.assertNotIn(global_token.encode(),page)
                status,script=await request(address,'/status.js',global_token)
                self.assertIn(b'textContent',script);self.assertNotIn(b'innerHTML',script)
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:await server.close();await fixture.close()

    async def test_slow_header_does_not_block_another_bounded_client(self):
        with TemporaryDirectory(prefix='iris-http-slow-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            server=ReadOnlyHTTP(observation_settings(fixture.candidate));address=await server.start()
            slow=None
            try:
                token=server.issue_test_session(runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(),True)),None,time.monotonic()+60)
                _,slow=await asyncio.open_connection(*address)
                slow.write(b'GET /status HTTP/1.1\r\n');await slow.drain()
                status,body=await request(address,'/api/observe/runtime',token)
                self.assertEqual(status,200,body)
            finally:
                if slow:slow.close();await slow.wait_closed()
                self.assertTrue(await server.close());await fixture.close()

    async def test_focused_reads_scoped_logs_and_storage_fault_views_are_real_http(self):
        from companion_memory.runtime import FocusGrant,FocusPort,IngressPort
        from companion_memory.runtime.results import Committed
        from companion_memory.logging_service import ObservationGrant,LoggingOk
        from tests.runtime.configuration_support import event
        from tests.persistence.support import sqlite_fault
        import sqlite3
        with TemporaryDirectory(prefix='iris-http-fault-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'management.refresh_min_interval_ms':100});runtime=await fixture.initialize()
            server=ReadOnlyHTTP(observation_settings(fixture.candidate));address=await server.start()
            try:
                eid,ingress=await fixture.entry();assert type(ingress) is IngressPort
                accepted=await ingress.accept_event(event('body','<script>secret-event-body</script>'));assert type(accepted) is Committed
                scoped=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                logs=fixture.log_window.bind_runtime_log_reader(ObservationGrant('instance',(eid,),False,False))
                token=server.issue_test_session(scoped,logs,time.monotonic()+60)
                logger=fixture.logging.get_logger('ingress');assert type(logger) is LoggingOk
                for target in (eid,'unseen_entry'):
                    logger.value.emit({'level':'INFO','event_code':'OPERATION_COMPLETED','context':{'entry_id':target},'attributes':{'count':1,'outcome':'SUCCESS'}})
                status,body=await request(address,'/api/observe/logs',token)
                self.assertEqual(status,200,body)
                page=json.loads(body);self.assertEqual(len(page['events']),1);self.assertFalse(page['has_more'])
                self.assertEqual(set(page),{'events','next_cursor','has_more','observed_at','coverage'})
                self.assertNotIn(b'unseen_entry',body);self.assertNotIn(b'secret-event-body',body)
                focus=runtime.bind_focus_coordinator(FocusGrant('instance','dream','operator'));assert type(focus) is FocusPort
                result=await focus.enter_focus('focus',1);assert type(result) is Committed,result
                status,body=await request(address,'/api/observe/entries?entry_id='+eid,token)
                self.assertEqual(status,200,body);self.assertEqual(json.loads(body)['rows'][0]['mode'],'DREAM_FOCUSED')
                self.assertNotIn(b'secret-event-body',body)
                def fail(sql):
                    if 'FROM buffers_entry_state' in sql:raise sqlite_fault(sqlite3.SQLITE_IOERR)
                fixture.hooks.before=fail
                await asyncio.sleep(0.11)
                status,body=await request(address,'/api/observe/entries?entry_id='+eid,token)
                self.assertEqual(status,200,body);self.assertEqual(json.loads(body)['availability'],'STALE')
                other_observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                other_token=server.issue_test_session(other_observer,None,time.monotonic()+60)
                status,body=await request(address,'/api/observe/entries?entry_id='+eid,other_token)
                self.assertEqual(status,503,body);self.assertEqual(json.loads(body)['availability'],'UNAVAILABLE')
                self.assertNotIn('rows',json.loads(body));self.assertEqual(len(fixture.adapter.calls),0)
            finally:fixture.hooks.before=lambda sql:None;await server.close();await fixture.close()

    async def test_batch_filter_cursor_is_scoped_and_filter_changes_are_rejected(self):
        from companion_memory.runtime import IngressPort
        from companion_memory.runtime.results import Committed
        from tests.runtime.configuration_support import event
        from tests.provider.support import record
        with TemporaryDirectory(prefix='iris-http-cursor-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'management.refresh_min_interval_ms':100});runtime=await fixture.initialize()
            server=ReadOnlyHTTP(observation_settings(fixture.candidate));address=await server.start()
            try:
                batches=[];entries=[]
                for name in ('visible','hidden'):
                    eid,port=await fixture.entry(name);assert type(port) is IngressPort;entries.append(eid)
                    for key in ('one','two','three'):await port.accept_event(event(key))
                    frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'threshold','type':'THRESHOLD'})
                    assert type(frozen) is Committed,frozen
                    batches.append(record(frozen.receipt.result)['object_id'])
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(entries[0],)))
                token=server.issue_test_session(observer,None,time.monotonic()+60)
                path='/api/observe/batches?state=FROZEN&batch_id='+str(batches[0])
                status,body=await request(address,path,token)
                self.assertEqual(status,200,body)
                result=json.loads(body);self.assertEqual(len(result['rows']),1);self.assertFalse(result['has_more'])
                self.assertEqual(result['rows'][0]['batch_id'],batches[0]);self.assertNotIn(str(batches[1]).encode(),body)
                cursor=result['next_cursor'];await asyncio.sleep(0.11)
                status,body=await request(address,path.replace('FROZEN','TERMINAL')+'&cursor='+cursor,token)
                self.assertEqual(status,409,body)
                await asyncio.sleep(0.11)
                status,body=await request(address,'/api/observe/batches?batch_id='+str(batches[1]),token)
                self.assertEqual(status,200,body);self.assertEqual(json.loads(body)['rows'],[])
                self.assertFalse(json.loads(body)['has_more'])
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:await server.close();await fixture.close()
