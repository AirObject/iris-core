"""Public host shutdown drains real retired SQLite connections without lost leases."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.configuration.content_resolution import ContentConfigurationOk, resolve_content_configuration
from companion_memory.media.service import MediaResources
from companion_memory.persistence import Committed, Found, DatabaseResources
from companion_memory.provider import SimulationAdapter
from companion_memory.runtime.content_host import ContentHost, ContentHostResources
from companion_memory.runtime.content_media import MediaPolicy
from tests.configuration.content_support import inputs
from tests.persistence.support import Hooks
from tests.provider.support import success


def host_at(root: Path, hooks: Hooks) -> ContentHost:
    """Create one explicit native assembly with short caller-only shutdown waits."""
    supplied = inputs(root)
    supplied[0]['explicit_values'].update({'storage.operation_timeout_ms': 200, 'storage.close_timeout_ms': 200})
    supplied[1]['explicit_values']['runtime.close_timeout_ms'] = 200
    resolved = resolve_content_configuration(*supplied)
    assert type(resolved) is ContentConfigurationOk, resolved
    resources = ContentHostResources(DatabaseResources('shutdown-database',
        lambda db, path: (db, path) == ('shutdown-database', str(root / 'database' / 'runtime.sqlite3')), connect=hooks.connect),
        MediaResources('shutdown-media', lambda rid, db, path: (rid, db, path) == ('shutdown-media', 'shutdown-database', str(root / 'media'))),
        'instance', 'configuration', supplied[4])
    return ContentHost(resolved.value, resources, SimulationAdapter((success(),)),
        SyntheticCandidateInput('shutdown_input:1', (), 50), 'sample_learning', MediaPolicy('domain', 'sample_media', 'describe:1'))


class ContentShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_connection_failure_is_cleaned_by_public_host_close(self):
        with tempfile.TemporaryDirectory() as directory:
            hooks = Hooks(); root = Path(directory).resolve(); host = host_at(root, hooks)
            closes = 0; armed = False
            def after(sql: str) -> None:
                nonlocal armed
                if sql.startswith('INSERT INTO ingress_content_entries'): armed = True
            def closing() -> None:
                nonlocal closes
                if not armed: return
                closes += 1
                if closes == 1: raise OSError('Controlled first connection release failure.')
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')), Found)
                hooks.before_close = closing; hooks.after = after
                result = await host.register_entry('original', 'entry', 'host', 'sample_platform', 'conversation')
                assert type(result) is Committed, result
                assert host.runtime is not None
                self.assertTrue(host.runtime._commands); self.assertTrue(host.storage.get_health().cleanup_pending)
                self.assertIsNotNone(host.media._root_fd)
                self.assertTrue(await asyncio.wait_for(host.close(), 3))
                self.assertFalse(host.runtime._commands); self.assertFalse(host.storage._connections)
                self.assertFalse(host.storage._connection_notifications); self.assertIsNone(host.storage._cleanup_job)
                self.assertIsNone(host.storage._owner_fd); self.assertIsNone(host.media._root_fd)
                self.assertIsNone(host.media._owner_fd); self.assertEqual(closes, 2)
                self.assertEqual(host.adapter.calls, ())
                self.assertTrue(await host.close()); self.assertEqual(closes, 2)
                hooks.before_close = lambda: None; hooks.after = lambda sql: None
                reopened = host_at(root, hooks)
                try:
                    self.assertIs(type(await reopened.initialize('OPEN_EXISTING')), Found)
                    repeated = await reopened.register_entry('original', 'entry', 'host', 'sample_platform', 'conversation')
                    assert type(repeated) is Committed, repeated
                    self.assertEqual(repeated.receipt, result.receipt)
                    self.assertEqual(reopened.adapter.calls, ())
                finally: self.assertTrue(await reopened.close())
            finally:
                hooks.before_close = lambda: None; hooks.after = lambda sql: None
                self.assertTrue(await host.close())

    async def test_unfinished_original_or_cleanup_close_keeps_one_owner_and_both_roots(self):
        for block_attempt in (1, 2):
            with self.subTest(block_attempt=block_attempt), tempfile.TemporaryDirectory() as directory:
                hooks = Hooks(); host = host_at(Path(directory).resolve(), hooks)
                entered = threading.Event(); release = threading.Event(); attempts: list[int] = []
                armed = False
                def after(sql: str) -> None:
                    nonlocal armed
                    if sql.startswith('INSERT INTO ingress_content_entries'): armed = True
                def closing() -> None:
                    if not armed: return
                    attempts.append(threading.get_ident())
                    if len(attempts) == block_attempt:
                        entered.set(); release.wait()
                    if len(attempts) == 1: raise OSError('Controlled first connection release failure.')
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')), Found)
                    hooks.before_close = closing; hooks.after = after
                    result = await host.register_entry('original', 'entry', 'host', 'sample_platform', 'conversation')
                    assert type(result) is Committed, result
                    assert host.runtime is not None
                    writer = host.storage._writer
                    command_tasks = tuple(host.runtime._commands)
                    self.assertFalse(await asyncio.wait_for(host.close(), 1))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    retained = host._close_task
                    cleanup = host.storage._cleanup_job
                    self.assertTrue(host.runtime._commands)
                    self.assertIsNotNone(host.media._root_fd); self.assertIsNotNone(host.storage._owner_fd)
                    self.assertEqual(len(host.storage._connections), 1)
                    self.assertFalse(await asyncio.wait_for(host.close(), 1))
                    self.assertEqual(len(attempts), block_attempt)
                    if block_attempt == 2:
                        self.assertIs(host._close_task, retained); self.assertIs(host.storage._cleanup_job, cleanup)
                        self.assertIsNotNone(cleanup)
                    else:
                        self.assertIs(host.storage._writer, writer); self.assertIsNotNone(writer)
                    self.assertEqual(tuple(host.runtime._commands), command_tasks)
                    self.assertEqual(host.adapter.calls, ())
                    release.set()
                    assert retained is not None
                    await asyncio.wait_for(asyncio.shield(retained), 3)
                    self.assertTrue(await asyncio.wait_for(host.close(), 3))
                    self.assertFalse(host.runtime._commands); self.assertFalse(host.storage._connections)
                    self.assertFalse(host.storage._connection_notifications)
                    self.assertIsNone(host.media._root_fd); self.assertIsNone(host.storage._owner_fd)
                    self.assertEqual(len(attempts), 2)
                    self.assertIs(type(result), Committed)
                finally:
                    release.set(); hooks.before_close = lambda: None
                    if host._close_task is not None: await asyncio.wait_for(asyncio.shield(host._close_task), 3)
                    self.assertTrue(await host.close())

    async def test_shutdown_preserves_admitted_commit_and_file_publication(self):
        from typing import cast
        from companion_memory.memory.formats import record
        for operation in ('registration', 'publication'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                hooks = Hooks(); host = host_at(Path(directory).resolve(), hooks)
                entered = threading.Event(); release = threading.Event(); closing_started = asyncio.Event()
                armed = False; request = None; closing_task = None
                def after(sql: str) -> None:
                    nonlocal armed
                    prefix = 'INSERT INTO ingress_content_entries' if operation == 'registration' else 'UPDATE media_blobs SET'
                    if sql.startswith(prefix): armed = True
                def before(sql: str) -> None:
                    if armed and sql == 'COMMIT': entered.set(); release.wait()
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')), Found)
                    await host.register_entry('setup', 'entry', 'host', 'sample_platform', 'conversation')
                    upload = host.media.bind_upload('entry')
                    begun = await upload.begin_upload('image', 'IMAGE'); assert type(begun) is Committed
                    uid = cast(str, record(begun.receipt.result)['upload_id'])
                    await upload.append_upload(uid, 0, b'actual owned publication bytes')
                    assert host.runtime is not None
                    original_close = host.runtime.close
                    async def close_runtime() -> bool:
                        closing_started.set()
                        return await original_close()
                    host.runtime.close = close_runtime
                    hooks.before = before; hooks.after = after
                    request = asyncio.create_task(host.register_entry('admitted', 'second', 'host', 'sample_platform', 'second')
                        if operation == 'registration' else upload.finish_upload(uid))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    closing_task = asyncio.create_task(host.close())
                    await asyncio.wait_for(closing_started.wait(), 3)
                    self.assertEqual(host.storage.get_health().lifecycle, 'READY')
                    self.assertIsNotNone(host.storage._writer); self.assertIsNotNone(host.media._root_fd)
                    self.assertFalse(host.media.get_health().ready)
                    self.assertFalse(request.done())
                    release.set()
                    result = await request; assert type(result) is Committed, result
                    await closing_task
                    if host._close_task is not None: await asyncio.wait_for(asyncio.shield(host._close_task), 3)
                    self.assertTrue(await host.close())
                    self.assertFalse(host.runtime._commands); self.assertFalse(host.media._upload_jobs)
                    self.assertFalse(host.storage._connections); self.assertIsNone(host.media._root_fd)
                    self.assertEqual(host.adapter.calls, ())
                finally:
                    release.set(); hooks.before = lambda sql: None; hooks.after = lambda sql: None
                    if request is not None: await request
                    if closing_task is not None: await closing_task
                    self.assertTrue(await host.close())
