"""Actual audited generation ledger and original-key recovery with loopback I/O.

Protocol evidence is synthetic; SQLite, sockets, ownership and commit receipts
are real. Reopening is local and never consumes another credential or attempt.
"""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.provider import Ready, Completed, ResultGrant, Found, Pending, Rejected, ObserverGrant
from companion_memory.provider.values import as_record
from tests.provider.test_chat_transport import server
from tests.text_learning.provider_support import Fixture


def response(content='{"fixture":"complete"}', usage=None):
    body = json.dumps({'id': 'response', 'object': 'chat.completion', 'created': 1, 'model': 'fixture_backend',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
        'usage': usage if usage is not None else {'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12,
            'prompt_tokens_details': {'cached_tokens': 0}, 'completion_tokens_details': {'reasoning_tokens': 1}}}).encode()
    return f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_persisted_generation_and_original_reopen_never_send_twice(self):
        with TemporaryDirectory() as directory, server(response()) as (port, requests, failures):
            root = Path(directory); fixture = Fixture(root, port)
            try:
                self.assertIs(type(await fixture.initialize()), Ready)
                self.assertEqual(fixture.leases, [])
                result = await fixture.work.generate(fixture.request())
                self.assertIs(type(result), Completed, result)
                assert type(result) is Completed
                self.assertEqual(result.record['outcome'], 'SUCCEEDED')
                self.assertEqual(result.record['source'], 'REMOTE_PROVIDER')
                self.assertEqual(result.record['format_version'], 2)
                self.assertIsNotNone(result.record['config_snapshot_id'])
                self.assertIsNotNone(result.result)
                original = result.record
                again = await fixture.work.generate(fixture.request())
                self.assertIs(type(again), Completed, again)
                assert type(again) is Completed
                self.assertEqual(again.record, original)
                self.assertEqual(len(requests), 1); self.assertEqual(len(fixture.leases), 1)
                self.assertTrue(fixture.leases[0].released)
                owner = fixture.service.bind_result_owner(ResultGrant('cognition', (str(result.record['object_id']),)))
                recovered = await owner.recover_result(result.record['object_id'])
                self.assertIs(type(recovered), Found, recovered)
                observed = fixture.service.bind_observer(ObserverGrant(('instance',), ('fixture_account',)))
                from tests.provider.support import Fixture as OriginalFixture
                query = await observed.query_usage(OriginalFixture.query())
                self.assertIs(type(query), Found, query)
                assert type(query) is Found
                self.assertEqual(as_record(query.value)['source'], 'REMOTE_PROVIDER')
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    counts = {table: db.execute('SELECT count(*) FROM provider_'+table).fetchone()[0]
                        for table in ('requests', 'attempts', 'budget_windows', 'reservations', 'cost_items', 'handoffs')}
                    self.assertEqual(counts, {'requests':1, 'attempts':1, 'budget_windows':1, 'reservations':1, 'cost_items':4, 'handoffs':1})
            finally:
                await fixture.close()
            reopened = Fixture(root, port)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')), Ready)
                result = await reopened.work.generate(reopened.request())
                self.assertIs(type(result), Completed, result)
                assert type(result) is Completed
                self.assertEqual(result.record, original)
                self.assertEqual(reopened.leases, []); self.assertEqual(reopened.ended, [])
                self.assertEqual(len(requests), 1)
            finally:
                await reopened.close()

    async def test_unknown_response_retains_money_and_blocks_new_keys_without_another_send(self):
        with TemporaryDirectory() as directory, server(b'HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n') as (port, requests, failures):
            root = Path(directory); fixture = Fixture(root, port)
            try:
                self.assertIs(type(await fixture.initialize()), Ready)
                result = await fixture.work.generate(fixture.request())
                self.assertIs(type(result), Pending, result)
                self.assertIs(type(await fixture.work.generate(fixture.request('another'))), Rejected)
                self.assertEqual(len(requests),1);self.assertEqual(len(fixture.leases),1)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    budget = json.loads(db.execute('SELECT body FROM provider_budget_windows').fetchone()[0])
                    self.assertGreater(budget['held_atoms'],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],1)
                    before = db.execute('SELECT body FROM provider_reservations').fetchone()[0]
            finally:
                await fixture.close()
            reopened = Fixture(root,port)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')),Ready)
                result = await reopened.work.lookup_request('generate',reopened.request())
                self.assertIs(type(result),Found,result)
                assert type(result) is Found
                self.assertEqual(as_record(as_record(result.value)['request'])['phase'],'REMOTE_RESULT_UNKNOWN')
                self.assertIs(type(await reopened.work.generate(reopened.request('another'))),Rejected)
                self.assertEqual(reopened.leases,[])
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT body FROM provider_reservations').fetchone()[0],before)
            finally:
                await reopened.close()

    async def test_new_interpreter_recovers_each_actual_generation_cut_without_resending(self):
        import selectors, subprocess, sys, threading
        for action in ('prepared_commit','in_flight','result_received','completed_commit'):
            with self.subTest(action=action),TemporaryDirectory() as directory:
                root=Path(directory);fixture=Fixture(root,1)
                try:self.assertIs(type(await fixture.initialize()),Ready)
                finally:await fixture.close()
                entered,release=threading.Event(),threading.Event()
                if action!='in_flight':release.set()
                with server(response(),entered=entered,release=release) as (port,requests,failures):
                    args=[sys.executable,'-m','tests.text_learning.provider_process_worker',str(root),str(port)]
                    process=subprocess.Popen(args+[action],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    try:
                        assert process.stdout is not None
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout,selectors.EVENT_READ)
                            self.assertTrue(selector.select(10),'Provider process did not reach its barrier.')
                        message=process.stdout.readline().decode().strip()
                        self.assertEqual(message,'PROVIDER_ACTIVE' if action=='in_flight' else 'PROVIDER_BARRIER')
                        if action=='in_flight':self.assertTrue(entered.wait(2))
                        process.kill();process.wait(timeout=10)
                    finally:
                        if process.poll() is None:process.kill();process.wait(timeout=10)
                        for stream in (process.stdin,process.stdout,process.stderr):
                            if stream is not None:stream.close()
                        release.set()
                    recovered=subprocess.run(args+['inspect'],capture_output=True,text=True,timeout=10)
                    self.assertEqual(recovered.returncode,0,recovered.stderr)
                    value=json.loads(recovered.stdout)
                    self.assertEqual(value['phase'],'TERMINAL' if action=='completed_commit' else 'REMOTE_RESULT_UNKNOWN')
                    self.assertEqual(value['has_result'],action=='completed_commit')
                    self.assertEqual((value['attempts'],value['credentials'],value['ended']),(1,0,0))
                    again=subprocess.run(args+['inspect'],capture_output=True,text=True,timeout=10)
                    self.assertEqual(again.returncode,0,again.stderr)
                    self.assertEqual(json.loads(again.stdout),value)
                    self.assertEqual(len(requests),0 if action=='prepared_commit' else 1)

    async def test_each_settlement_failure_preserves_preparation_without_partial_handoff_cost_or_audit(self):
        from tests.persistence.support import sqlite_fault
        for point in ('UPDATE provider_requests','UPDATE provider_attempts','UPDATE provider_reservations',
                      'UPDATE provider_budget_windows','INSERT INTO provider_cost_items','INSERT INTO provider_handoffs',
                      'INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
            with self.subTest(point=point),TemporaryDirectory() as directory,server(response()) as (port,requests,failures):
                root=Path(directory);fixture=Fixture(root,port);settling=False
                try:
                    self.assertIs(type(await fixture.initialize()),Ready)
                    def before(sql):
                        nonlocal settling
                        if sql.startswith('UPDATE provider_requests'):settling=True
                        if settling and sql.startswith(point):raise sqlite_fault(sqlite3.SQLITE_FULL)
                    fixture.hooks.before=before
                    result=await fixture.work.generate(fixture.request())
                    self.assertIs(type(result),Pending,result)
                    self.assertEqual(len(requests),1);self.assertEqual(len(fixture.leases),1)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_handoffs').fetchone()[0],0)
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_cost_items').fetchone()[0],0)
                        attempt=json.loads(db.execute('SELECT body FROM provider_attempts').fetchone()[0])
                        self.assertEqual(attempt['state'],'PREPARED')
                        self.assertEqual(db.execute("SELECT count(*) FROM operation_receipts WHERE owner_namespace='provider'").fetchone()[0],2)
                finally:
                    fixture.hooks.before=lambda sql:None
                    await fixture.close()

    async def test_reliable_local_rollback_replays_only_original_settlement_once(self):
        from tests.persistence.support import sqlite_fault
        with TemporaryDirectory() as directory,server(response()) as (port,requests,failures):
            fixture=Fixture(Path(directory),port);failed=False
            try:
                self.assertIs(type(await fixture.initialize()),Ready)
                def before(sql):
                    nonlocal failed
                    if sql.startswith('INSERT INTO provider_handoffs') and not failed:
                        failed=True;raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                fixture.hooks.before=before
                result=await fixture.work.generate(fixture.request())
                self.assertIs(type(result),Completed,result)
                assert type(result) is Completed
                self.assertEqual(result.record['outcome'],'SUCCEEDED');self.assertTrue(failed)
                self.assertEqual(len(requests),1);self.assertEqual(len(fixture.leases),1)
            finally:await fixture.close()
