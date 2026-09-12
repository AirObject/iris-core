"""Actual ticket delivery snapshots and atomic use keep history and beliefs intact."""
from pathlib import Path
from tempfile import TemporaryDirectory
from hashlib import sha256
from contextlib import closing
import secrets
import sqlite3
import time
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.content_codec import encode_content
from companion_memory.information.errors import RecallCommitted
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, integer, text
from companion_memory.retrieval.tickets import RecallAuthority
from tests.information.host_support import host, learn_one


class UsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_use_history_and_original_key_survive_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                if h.runtime is None or h.assembly.memory.information is None: self.fail('Expected initialized memory.')
                kinds = frozenset(('ticket_issue_normal', 'usage_change'))
                port = await h.bind_management(HostIdentity('recall-binding', 'principal', 'host', 'entry', kinds, (), time.monotonic() + 300))
                memory_port = h.runtime.memory.bind_read((oid,), ('get_current',))
                h.management.bind_recall_authority(port, RecallAuthority(port.binding_id, 'host', 'entry', h.runtime.memory, memory_port))
                current = (await h.assembly.memory.information.current_page())[0]
                now = int(time.time() * 1000000); recall_id = 'recall:' + secrets.token_hex(24)
                ticket = {'version': 1, 'recall_id': recall_id, 'database_id': 'information-database', 'instance_id': 'instance',
                    'principal_binding_id': port.binding_id, 'host_id': 'host', 'entry_id': 'entry', 'query_mode': 'NORMAL',
                    'config_snapshot_id': h.assembly.configuration.snapshot_id, 'issued_at_us': now, 'expires_at_us': now + 86400000000,
                    'clock_observation': now, 'response_digest': sha256(encode_content((current,), 8192)).hexdigest(), 'intent_digest': 'a' * 64,
                    'request_key': 'search', 'member_count': 1}
                issued = await port.execute('ticket_issue_normal', 'search', {'ticket': ticket,
                    'members': ({'recall_id': recall_id, 'object_id': oid, 'returned_revision': current['revision'], 'returned_lifecycle': current['lifecycle']},)})
                self.assertIs(type(issued), RecallCommitted, issued)
                if type(issued) is RecallCommitted:
                    self.assertEqual({k: v for k, v in issued.objects[0].items() if k != 'source_refs'}, dict(current))
                    self.assertTrue(issued.objects[0]['source_refs'])
                payload = {'recall_id': recall_id, 'used_members': ({'object_id': oid, 'returned_revision': current['revision']},), 'used_at': None}
                used = await port.execute('usage_change', 'use', payload)
                self.assertIs(type(used), Committed, used)
                if type(used) is not Committed: self.fail('Expected confirmed feedback.')
                after = (await h.assembly.memory.information.current_page())[0]
                self.assertEqual(after['revision'], integer(current['revision']) + 1)
                self.assertEqual(record(after['scores'])['belief'], record(current['scores'])['belief'])
                self.assertEqual(record(after['scores'])['retention'], integer(record(current['scores'])['retention']) + 8)
                facts = record(record(used.receipt.result)['facts']); history_ids = record(facts['logging_service'])['history_ids']
                if type(history_ids) is not tuple: self.fail('Expected bounded original history identities.')
                self.assertEqual(len(history_ids), 1)
                history_port = h.assembly.history.bind_inspection((oid,))
                historical = await history_port.read_object_history(used.receipt.identity, history_ids[0], oid)
                self.assertIs(type(historical), Found, historical)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM retrieval_consumption').fetchone()[0], 1)
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], 1)
                    audits = c.execute('SELECT count(*) FROM audit_records WHERE commit_id=?', (used.receipt.commit_id,)).fetchone()[0]
                    self.assertEqual(audits, 3)
                self.assertTrue(await h.close())
                h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                if h.runtime is None: self.fail('Expected recovered runtime.')
                port = await h.bind_management(HostIdentity('recall-binding', 'principal', 'host', 'entry', kinds, (), time.monotonic() + 300))
                memory_port = h.runtime.memory.bind_read((oid,), ('get_current',))
                h.management.bind_recall_authority(port, RecallAuthority(port.binding_id, 'host', 'entry', h.runtime.memory, memory_port))
                confirmed = await port.resolve('usage_change', 'use', payload)
                self.assertIs(type(confirmed), Committed, confirmed)
                if type(confirmed) is Committed: self.assertEqual(confirmed.receipt, used.receipt)
                self.assertEqual(h.adapter.calls, ())
                self.assertTrue(await h.close())
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    c.execute('DELETE FROM memory_usage_receipt'); c.commit()
                h = host(root)
                damaged = await h.initialize('OPEN_EXISTING')
                self.assertIsNot(type(damaged), Found, damaged)
                self.assertNotEqual(h.state, 'READY')
            finally:self.assertTrue(await h.close())
