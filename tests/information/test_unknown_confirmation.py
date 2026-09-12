"""Real COMMIT uncertainty retains original intent without unlocking storage."""
import asyncio
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import time
import unittest
from companion_memory.information.errors import InformationRejected, InformationUnconfirmed, InformationNotCommitted
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record
from companion_memory.persistence import Found, Committed
from tests.information.host_support import host, learn_one
from tests.information.test_feedback_boundaries import payload
from tests.information.test_queries import query
from tests.persistence.support import Hooks, sqlite_fault


class UnknownConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_and_usage_unknown_commit_confirm_exact_intent_without_replay(self):
        for operation in ('ticket', 'usage'):
            for boundary in ('before', 'after'):
                with self.subTest(operation=operation, boundary=boundary), TemporaryDirectory() as directory:
                    root = Path(directory).resolve(); hooks = Hooks(); h = host(root, hooks.connect)
                    try:
                        self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                        await learn_one(h)
                        port = await h.bind_business(HostIdentity('confirmation', 'principal', 'host', 'entry',
                            frozenset(('search_memory', 'record_usage', 'resolve_recall')), (), time.monotonic() + 300))
                        supplied = payload(await port.search_memory(query('basis')), 'usage') if operation == 'usage' else query('ticket')
                        armed = False
                        failed = False
                        def fail(sql: str) -> None:
                            nonlocal armed, failed
                            if sql.startswith('INSERT INTO ' + ('retrieval_ticket ' if operation == 'ticket' else 'memory_usage_receipt ')):
                                armed = True
                            if armed and not failed and sql == 'COMMIT':
                                failed = True
                                raise sqlite_fault(sqlite3.SQLITE_IOERR)
                        if boundary == 'before': hooks.before = fail
                        else: hooks.after = fail
                        result = await (port.record_usage(supplied) if operation == 'usage' else port.search_memory(supplied))
                        self.assertIs(type(result), InformationNotCommitted if boundary == 'before' else InformationUnconfirmed, result)
                        if h.runtime is None: self.fail('Expected actual runtime.')
                        await asyncio.gather(*tuple(h.management.jobs))
                        if boundary == 'before':
                            self.assertFalse(h.management._pending)
                            self.assertFalse(h.runtime.gate._information_writers)
                            with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                                self.assertEqual(connection.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], 0)
                                self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_ticket').fetchone()[0], int(operation == 'usage'))
                            continue
                        with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                            audits = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                            writes = connection.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0]
                        changed = supplied | ({'used_at': '2026-01-01T00:00:00+00:00'} if operation == 'usage' else {'query_text': 'different'})
                        wrong = await (port.resolve_usage(changed) if operation == 'usage' else port.resolve_recall(changed))
                        self.assertIs(type(wrong), InformationRejected, wrong)
                        if type(wrong) is InformationRejected: self.assertEqual(wrong.error.reason, 'CONTENT_MISMATCH')
                        confirmed = await (port.resolve_usage(supplied) if operation == 'usage' else port.resolve_recall(supplied))
                        if boundary == 'before':
                            self.assertIs(type(confirmed), InformationNotCommitted, confirmed)
                        elif operation == 'usage':
                            self.assertIs(type(confirmed), Committed, confirmed)
                        else:
                            self.assertIs(type(confirmed), Found, confirmed)
                            if type(confirmed) is Found:
                                self.assertEqual(record(confirmed.value)['availability'], 'CONFIRMED_ONLY')
                                self.assertNotIn('sections', record(confirmed.value))
                        await asyncio.gather(*tuple(h.management.jobs))
                        self.assertFalse(h.management._pending)
                        self.assertFalse(h.runtime.gate._information_writers)
                        # Confirmation does not remove the existing storage fault.
                        self.assertEqual(h.storage.get_health().lifecycle, 'FAULTED')
                        with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                            self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], audits)
                            self.assertEqual(connection.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], writes)
                    finally: self.assertTrue(await h.close())
