"""SQLite generates real readonly/full failures; no fabricated business rows."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import cast

from companion_memory.persistence import Committed, NotCommitted, Unconfirmed
from .storage_support import ControlStorage


class DreamStorageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_connection_rolls_back_run_root_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await ControlStorage(Path(directory)).open()
            try:
                before = await fixture.control.schedule()
                fixture.fault = 'READONLY'
                values = {'run_id': 'readonly-run', 'expected_revision': 1, 'mode_epoch': 1,
                          'trigger': 'MANUAL', 'local_date': None}
                failed = await fixture.control.execute('start_background_dream', 'readonly-start', values, actor='admin')
                # Read-only storage cannot acquire the writer isolation needed
                # for authoritative original-key resolution. Preserve that
                # uncertainty instead of rewriting it to a confirmed failure.
                self.assertIsInstance(failed, Unconfirmed)
                self.assertIn(sqlite3.SQLITE_READONLY, [error & 255 for error in fixture.errors])
                errors = list(fixture.errors)
            finally:
                await fixture.close()
            # A read-only error closes normal service admission. Reopen the
            # actual original database under restored resources before making
            # any authoritative absence or atomicity assertion.
            recovered = await ControlStorage(Path(directory)).open('OPEN_EXISTING')
            try:
                self.assertEqual(await recovered.control.schedule(), before)
                self.assertIsNone(await recovered.control.inspect('readonly-run'))
                self.assertIsNone(await recovered.control.confirm('start_background_dream', 'readonly-start'))
                self.assertFalse(recovered.control.dispatch_enabled)
                print({'scope': 'DREAM_CONTROL_OWNER_ONLY', 'fault': 'ACTUAL_SQLITE_QUERY_ONLY',
                       'sqlite_errors': errors, 'partial_run_after_reopen': False,
                       'partial_receipt_after_reopen': False, 'original_confirmation': 'UNCONFIRMED'})
            finally:
                await recovered.close()

    async def test_page_limit_failure_preserves_last_committed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await ControlStorage(Path(directory)).open()
            try:
                fixture.fault = 'FULL'
                failed = None
                for number in range(128):
                    before = await fixture.control.schedule()
                    if before is None:
                        raise AssertionError('Missing root')
                    run_id = 'full-run-' + str(number)
                    key = 'full-start-' + str(number)
                    started = await fixture.control.execute('start_background_dream', key, {
                        'run_id': run_id, 'expected_revision': cast(int, before['revision']),
                        'mode_epoch': 1, 'trigger': 'MANUAL', 'local_date': None}, actor='admin')
                    if type(started) is not Committed:
                        failed = started
                        self.assertEqual(await fixture.control.schedule(), before)
                        self.assertIsNone(await fixture.control.inspect(run_id))
                        self.assertIsNone(await fixture.control.confirm('start_background_dream', key))
                        break
                    aborted = await fixture.control.execute('abort_background_dream', 'full-abort-' + str(number), {
                        'run_id': run_id, 'expected_revision': 1, 'mode_epoch': 1}, actor='admin')
                    if type(aborted) is not Committed:
                        failed = aborted
                        current = await fixture.control.inspect(run_id)
                        if current is None:
                            raise AssertionError('Committed run was lost')
                        self.assertEqual(current['state'], 'RUNNING')
                        self.assertEqual(current['revision'], 1)
                        self.assertIsNone(current['exit_result'])
                        self.assertIsNone(await fixture.control.confirm('abort_background_dream', 'full-abort-' + str(number)))
                        break
                self.assertIsInstance(failed, NotCommitted)
                self.assertIn(sqlite3.SQLITE_FULL, [error & 255 for error in fixture.errors])
                print({'scope': 'DREAM_CONTROL_OWNER_ONLY', 'fault': 'ACTUAL_SQLITE_MAX_PAGE_COUNT',
                       'sqlite_errors': fixture.errors, 'business_seed_sql': False, 'partial_commit': False})
            finally:
                await fixture.close()

    async def test_two_competing_controls_only_one_creates_run(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await ControlStorage(Path(directory)).open()
            try:
                from companion_memory.persistence.owned_statements import OwnerFailure
                values: dict[str, object] = {'expected_revision': 1, 'mode_epoch': 1, 'trigger': 'MANUAL', 'local_date': None}
                outcomes = await asyncio.gather(*(fixture.control.execute('start_background_dream', name,
                    dict(values) | {'run_id': name}, actor='admin') for name in ('left', 'right')), return_exceptions=True)
                self.assertEqual(sum(type(outcome) is Committed for outcome in outcomes), 1)
                self.assertEqual(sum(type(outcome) is OwnerFailure for outcome in outcomes), 1)
                present = [await fixture.control.inspect(name) for name in ('left', 'right')]
                self.assertEqual(sum(run is not None for run in present), 1)
                self.assertFalse(fixture.control.dispatch_enabled)
            finally:
                await fixture.close()
