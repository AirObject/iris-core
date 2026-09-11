"""Historical mode receipts and local terminal failures preserve live runtime gates."""
import sqlite3
import tempfile
import unittest
from typing import cast
from pathlib import Path
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService
from companion_memory.persistence import Committed, Found
from companion_memory.memory.formats import record
from tests.memory.support import Fixture
from tests.runtime.test_content_focus import ExplicitPublication
from tests.runtime.configuration_support import event
from tests.runtime.test_preparation_lifecycle import upload_window


def busy_error(path):
    """Obtain a real SQLITE_BUSY from competing writers on an owned temporary DB."""
    first = sqlite3.connect(path); second = sqlite3.connect(path, timeout=0)
    try:
        first.execute('CREATE TABLE occupied (value TEXT)'); first.execute('BEGIN IMMEDIATE')
        try: second.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as failure: return failure
        raise AssertionError('The competing writer must fail.')
    finally: first.close(); second.close()


class ContentReplayFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_enter_replay_preserves_current_normal_and_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            pub = ExplicitPublication(); fixture = await Fixture(Path(directory), candidate_input=SyntheticCandidateInput('replay:1', (), 50), publication=pub, runtime_changes={'runtime.max_active_entries': 2}, foundation_changes={'provider.max_in_flight': 3}).initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                focus = runtime.focus.bind('first')
                entered = await focus.enter_focus('enter', 1); assert type(entered) is Committed
                publication = await pub.publish('first', fixture.assembly.configuration.snapshot_id, exit_key='finish', exit_epoch=3)
                assert type(publication) is Committed
                finished = await focus.finish_focus('finish', 3, cast(str, record(publication.receipt.result)['publication_id']))
                assert type(finished) is Committed
                from unittest.mock import patch
                from companion_memory.persistence.owned_statements import OwnerFailure
                read = fixture.assembly.rows.read
                failures = []
                async def fail_current_mode(name, parameters):
                    if name == 'mode_get':
                        failures.append(name)
                        raise OwnerFailure('STORAGE_FAILED', 'storage', 'READ_FAILED')
                    return await read(name, parameters)
                with patch.object(fixture.assembly.rows, 'read', side_effect=fail_current_mode):
                    replay = await focus.enter_focus('enter', 1)
                assert type(replay) is Committed
                self.assertFalse(failures)
                self.assertEqual(replay.receipt, entered.receipt)
                self.assertEqual(runtime.gate.state, 'NORMAL'); self.assertFalse(runtime.gate._mode_cutoff)
                second = runtime.focus.bind('second')
                self.assertIs(type(await second.enter_focus('next', 5)), Committed)
                replay = await focus.enter_focus('enter', 1); assert type(replay) is Committed
                self.assertEqual(runtime.gate.state, 'DREAM_FOCUSED'); self.assertEqual(runtime.gate.epoch, 7)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()

    async def test_before_handler_sqlite_failure_restores_gate_and_same_key(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), candidate_input=SyntheticCandidateInput('gate:1', (), 50), publication=ExplicitPublication()).initialize()
            errors = []
            try:
                runtime = fixture.runtime; assert runtime is not None
                focus = runtime.focus.bind('failed')
                failure = busy_error(Path(directory) / 'busy.sqlite')
                begins = []
                def readonly(sql):
                    if sql == 'BEGIN IMMEDIATE':
                        begins.append(sql)
                        if len(begins) >= 2: errors.append(failure.sqlite_errorcode); raise failure
                fixture.hooks.before = readonly
                result = await focus.enter_focus('original', 1)
                self.assertIsNot(type(result), Committed)
                self.assertIn(sqlite3.SQLITE_BUSY, errors)
                self.assertEqual(runtime.gate.state, 'NORMAL'); self.assertFalse(runtime.gate._mode_cutoff)
                fixture.hooks.before = lambda sql: None
                self.assertIs(type(await focus.enter_focus('original', 1)), Committed)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: fixture.hooks.before = lambda sql: None; await fixture.close()

    async def test_normal_learning_advances_last_staged_page_without_finish_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            pub = ExplicitPublication()
            fixture = await Fixture(Path(directory), candidate_input=SyntheticCandidateInput('draining:1', (), 50), publication=pub,
                runtime_changes={'runtime.transfer_page_size': 1}).initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry'); focus = runtime.focus.bind('drain')
                self.assertIs(type(await focus.enter_focus('enter', 1)), Committed)
                for ordinal in range(5):
                    raw = event('staged:' + str(ordinal)); raw['event_version'] = 2
                    self.assertIs(type(await entry.accept_event('accept:' + str(ordinal), raw)), Committed)
                published = await pub.publish('drain', fixture.assembly.configuration.snapshot_id, exit_key='finish', exit_epoch=3)
                assert type(published) is Committed
                self.assertIs(type(await focus.finish_focus('finish', 3, cast(str, record(published.receipt.result)['publication_id']))), Committed)
                self.assertEqual(runtime.gate.state, 'DRAINING')
                for ordinal in range(4): await entry.run_learning('progress:' + str(ordinal))
                self.assertEqual((await fixture.assembly.buffers.rows.read('all_staged_count', {}))[0]['count'], 0)
                self.assertEqual(runtime.gate.state, 'NORMAL')
                self.assertEqual((await fixture.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'}))[0]['state'], 'NORMAL')
            finally: await fixture.close()

    async def test_terminal_prehandler_failure_releases_evidence_and_recovers_in_process(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), MediaService(), SyntheticCandidateInput('terminal:1', (), 50)).initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None and runtime.media is not None
                entry, _ = await upload_window(fixture)
                execute = runtime.execute; triggered = []
                failure = busy_error(Path(directory) / 'busy.sqlite')
                begins = []
                def readonly(sql):
                    if sql == 'BEGIN IMMEDIATE':
                        begins.append(sql)
                        if len(begins) >= 2: raise failure
                async def fail_store(kind, key, values):
                    if kind == 'store_occurrence_result' and not triggered:
                        triggered.append(key); fixture.hooks.before = readonly
                    try: return await execute(kind, key, values)
                    finally: fixture.hooks.before = lambda sql: None
                runtime.execute = fail_store
                result = await entry.run_learning('original')
                self.assertIsNot(type(result), Committed)
                self.assertEqual(len(fixture.adapter.calls), 1)
                self.assertFalse(runtime.media.media.work.evidence)
                runtime.execute = execute
                work = (await runtime.media.media.rows.read('work_active_page', {'after': '', 'limit': 16}))[0]
                original = (await runtime.media.media.rows.read('work_get', {'work_id': work['work_id']}))[0]
                result = await runtime.media.drive(original, fresh=False); assert type(result) is Found, result
                self.assertEqual(record(result.value)['state'], 'RESULT_STORED')
                self.assertEqual(len(fixture.adapter.calls), 1); self.assertFalse(runtime.media.media.work.evidence)
                finished = await entry.run_learning('original'); assert type(finished) is Committed, finished
                self.assertEqual(len(fixture.adapter.calls), 2)
                repeated = await entry.run_learning('original'); assert type(repeated) is Committed
                self.assertEqual(repeated.receipt, finished.receipt); self.assertEqual(len(fixture.adapter.calls), 2)
            finally: await fixture.close()

    async def test_late_terminal_writer_retains_same_request_evidence_until_actual_end(self):
        import asyncio
        import threading
        from companion_memory.runtime.content_assembly import stable
        from companion_memory.media.service import identity
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), MediaService(), SyntheticCandidateInput('late_terminal:1', (), 50),
                runtime_changes={'runtime.operation_timeout_ms': 1000}, foundation_changes={'storage.operation_timeout_ms': 10000}).initialize()
            entered = threading.Event(); release = threading.Event(); pending = None
            try:
                runtime = fixture.runtime; media = fixture.media; assert runtime is not None and media is not None
                entry, _ = await upload_window(fixture)
                def before(sql):
                    if sql.startswith('INSERT INTO media_interpretations'):
                        entered.set(); release.wait(5)
                fixture.hooks.before = before
                pending = asyncio.create_task(entry.run_learning('original'))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                result = await pending; self.assertIsNot(type(result), Committed)
                self.assertEqual(len(media.work.evidence), 1)
                proof = next(iter(media.work.evidence.values())); media.work.retain_terminal(proof)
                rows = await media.rows.read('work_active_page', {'after': '', 'limit': 16})
                work = (await media.rows.read('work_get', {'work_id': rows[0]['work_id']}))[0]
                busy = await runtime.execute('store_occurrence_result', identity('store_media', work['work_id'], proof.request['object_id']),
                    {'work_id': work['work_id'], 'request_id': proof.request['object_id'], 'expected_revision': work['revision']})
                self.assertIsNot(type(busy), Committed)
                self.assertEqual(len(media.work.evidence), 1)
                self.assertEqual(len(fixture.adapter.calls), 1)
                release.set(); await asyncio.wait(tuple(runtime._jobs.values()), timeout=3)
                self.assertFalse(media.work.evidence); self.assertFalse(runtime._evidence_jobs)
                fixture.hooks.before = lambda sql: None
                result = await entry.run_learning('original'); assert type(result) is Committed, result
                self.assertEqual(len(fixture.adapter.calls), 2)
            finally:
                release.set(); fixture.hooks.before = lambda sql: None
                if pending is not None: await pending
                await fixture.close()

    async def test_last_transfer_page_and_acceptance_recheck_both_orders(self):
        import asyncio
        for transfer_first in (False, True):
            with self.subTest(transfer_first=transfer_first), tempfile.TemporaryDirectory() as directory:
                pub = ExplicitPublication()
                fixture = await Fixture(Path(directory), candidate_input=SyntheticCandidateInput('last_page:1', (), 50), publication=pub,
                    runtime_changes={'runtime.transfer_page_size': 1, 'runtime.max_active_entries': 2}, foundation_changes={'provider.max_in_flight': 3}).initialize()
                entered = asyncio.Event(); release = asyncio.Event(); pending = None
                try:
                    runtime = fixture.runtime; assert runtime is not None
                    focus = runtime.focus.bind('drain'); entry = runtime.bind_entry('entry')
                    self.assertIs(type(await focus.enter_focus('enter', 1)), Committed)
                    for ordinal in range(2):
                        raw = event('before:' + str(ordinal)); raw['event_version'] = 2
                        self.assertIs(type(await entry.accept_event('before:' + str(ordinal), raw)), Committed)
                    published = await pub.publish('drain', fixture.assembly.configuration.snapshot_id, exit_key='finish', exit_epoch=3)
                    assert type(published) is Committed
                    self.assertIs(type(await focus.finish_focus('finish', 3, cast(str, record(published.receipt.result)['publication_id']))), Committed)
                    self.assertEqual(runtime.gate.state, 'DRAINING')
                    execute = runtime.execute
                    async def boundary(kind, key, values):
                        if kind == 'transfer_content_events' and not entered.is_set():
                            result = await execute(kind, key, values) if transfer_first else None
                            entered.set(); await release.wait()
                            if transfer_first: return result
                        return await execute(kind, key, values)
                    runtime.execute = boundary
                    pending = asyncio.create_task(entry.run_learning('page'))
                    await asyncio.wait_for(entered.wait(), 3)
                    raw = event('last_race'); raw['event_version'] = 2
                    accepted = await entry.accept_event('last_race', raw); assert type(accepted) is Committed, accepted
                    self.assertEqual(record(accepted.receipt.result)['terminal'], 'ACCEPTED')
                    release.set(); await pending
                    runtime.execute = execute
                    for ordinal in range(2): await entry.run_learning('remaining:' + str(ordinal))
                    self.assertEqual((await fixture.assembly.buffers.rows.read('all_staged_count', {}))[0]['count'], 0)
                    self.assertEqual(runtime.gate.state, 'NORMAL')
                    self.assertEqual(len(fixture.adapter.calls), 1)
                finally:
                    release.set()
                    if pending is not None: await pending
                    await fixture.close()
