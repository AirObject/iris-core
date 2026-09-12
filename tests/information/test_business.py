"""Native host wire contracts join query tickets, feedback and external state."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.information.management import HostIdentity
from companion_memory.information.errors import InformationRejected, information_result
from companion_memory.information.records import record
from tests.information.host_support import host, learn_one
from tests.information.test_queries import query


class BusinessTests(unittest.IsolatedAsyncioTestCase):
    def test_unknown_and_mutable_native_result_carriers_use_the_fixed_error_envelope(self):
        for carrier in (object(), {'status': 'COMMITTED'}, Found({'unexpected': 'mutable'}), Found('unexpected')):
            with self.subTest(carrier=type(carrier).__name__):
                result = information_result(carrier, 'search_memory')
                self.assertIs(type(result), InformationRejected)
                if type(result) is not InformationRejected: self.fail('Expected fixed boundary rejection.')
                self.assertEqual((result.error.code, result.error.operation, result.error.field, result.error.reason),
                    ('STORAGE_FAILED', 'search_memory', 'storage', 'INTEGRITY_FAILURE'))

    async def test_usage_chooses_actual_audit_branch_and_original_confirmation(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                port = await h.bind_business(HostIdentity('business', 'principal', 'host', 'entry', frozenset(('search_memory', 'record_usage')), (), time.monotonic() + 300))
                recalled = await port.search_memory(query('query'))
                self.assertIs(type(recalled), Found, recalled)
                if type(recalled) is not Found: self.fail('Expected recalled current object.')
                response = record(recalled.value); memories = record(response['sections'])['memories']
                if type(memories) is not tuple: self.fail('Expected finite current memory.')
                current = record(memories[0])
                payload = {'operation_key': 'use', 'recall_id': response['recall_id'], 'used_at': datetime.now(timezone.utc).isoformat(),
                    'used_members': ({'object_id': current['object_id'], 'returned_revision': current['revision']},)}
                used = await port.record_usage(payload)
                self.assertIs(type(used), Committed, used)
                repeated = await port.record_usage(payload)
                self.assertIs(type(repeated), Committed, repeated)
                if type(used) is Committed and type(repeated) is Committed: self.assertEqual(used.receipt, repeated.receipt)
                already = await port.record_usage(payload | {'operation_key': 'use-again'})
                self.assertIs(type(already), Found, already)
                if type(already) is Found: self.assertEqual(record(already.value)['outcome'], 'ALREADY_APPLIED')
                invalid = await port.record_usage(payload | {'used_at': 1234})
                self.assertIs(type(invalid), InformationRejected, invalid)
                self.assertEqual(h.management.tickets.protected, {})
            finally: self.assertTrue(await h.close())

    async def test_state_wire_offset_and_actual_first_report(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')
                port = await h.bind_business(HostIdentity('business', 'principal', 'host', 'entry', frozenset(('set_state',)), (), time.monotonic() + 300))
                supplied = {'operation_key': 'state', 'activity_id': None, 'expected_revision': None, 'replace_activity': False,
                    'patch': {'activity_value': '阅读', 'reported_at': datetime.now(timezone.utc).isoformat(), 'reported_offset_minutes': 0}}
                result = await port.set_state(supplied)
                self.assertIs(type(result), Committed, result)
                mismatch = await port.set_state(supplied | {'patch': dict(supplied['patch']) | {'reported_offset_minutes': 480}})
                self.assertIs(type(mismatch), InformationRejected, mismatch)
                if type(mismatch) is InformationRejected: self.assertEqual(mismatch.error.reason, 'INVALID_TIME')
            finally: self.assertTrue(await h.close())
