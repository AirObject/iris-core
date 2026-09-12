"""Real fixed deletion plans race with ticket use on isolated SQLite objects."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import time
import unittest
from typing import cast
from companion_memory.persistence import Found, Committed, NotFound
from companion_memory.persistence.content_codec import decode_content
from companion_memory.ingress.events import plain
from companion_memory.information.expiry import ForgottenExpiry
from companion_memory.information.errors import InformationRejected, InformationNotCommitted
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, integer
from companion_memory.runtime.information_host import InformationHost
from tests.information.host_support import host, learn_one
from tests.information.test_queries import query


async def scores(h: InformationHost, oid: str, retention: int):
    if h.runtime is None or h.assembly.memory.information is None: raise RuntimeError('Expected real owners.')
    current = await h.assembly.memory.information.index_current(oid)
    if current is None: raise RuntimeError('Expected the selected real current object.')
    value = cast(dict, plain(current)); revision = integer(current['revision']) + 1
    value['revision'] = revision; value['scores']['retention'] = retention
    value['lifecycle'] = 'ACTIVE' if retention >= 35 else 'FORGOTTEN'
    value['forgotten_since_us'] = None if retention >= 35 else current['forgotten_since_us'] or current['modified_at_us']
    row = (await h.assembly.memory.rows.read('links_get', {'object_id': oid}))[0]
    links = cast(dict, decode_content(cast(str, row['body']).encode(), 2048))
    for link in links['sources']: link['object_revision'] = revision
    return await h.runtime.maintenance.bind((oid,)).set_scores('scores:' + str(revision), {'change_version': 1,
        'action': 'SET_SCORES', 'target_id': oid, 'expected_revision': current['revision'], 'proposed_value': value, 'links': links})


class ExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_boundary_and_feedback_delete_both_orders(self):
        for feedback_first in (False, True):
            with self.subTest(feedback_first=feedback_first), TemporaryDirectory() as directory:
                root = Path(directory); h = host(root)
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    oid = await learn_one(h)
                    self.assertIs(type(await scores(h, oid, 19)), Committed)
                    self.assertIs(type(await scores(h, oid, 30)), Committed)
                    if h.runtime is None or h.assembly.memory.information is None: self.fail('Expected initialized owners.')
                    memory = h.assembly.memory.information
                    current = (await memory.current_page())[0]; since = integer(current['forgotten_since_us'])
                    cutoff = since + 2592000000000
                    worker = ForgottenExpiry(h.runtime, memory, utc_now_us=lambda: cutoff - 1)
                    boundary = await worker.run()
                    self.assertIs(type(boundary), Found, boundary)
                    if type(boundary) is Found: self.assertEqual(record(boundary.value)['deleted'], 0)
                    port = await h.bind_business(HostIdentity('expiry-feedback', 'principal', 'host', 'entry',
                        frozenset(('deep_recall', 'record_usage')), (), time.monotonic() + 300), include_forgotten=True)
                    recalled = await port.deep_recall(query('deep'))
                    self.assertIs(type(recalled), Found, recalled)
                    if type(recalled) is not Found: self.fail('Expected actual deep ticket.')
                    payload = {'operation_key': 'feedback', 'recall_id': record(recalled.value)['recall_id'], 'used_at': None,
                        'used_members': ({'object_id': oid, 'returned_revision': current['revision']},)}
                    worker.utc_now_us = lambda: cutoff
                    if feedback_first:
                        original = memory.expired_page
                        async def raced(*args, **kwargs):
                            page = await original(*args, **kwargs)
                            self.assertIs(type(await port.record_usage(payload)), Committed)
                            return page
                        memory.expired_page = raced
                    removed = await worker.run()
                    self.assertIs(type(removed), Found, removed)
                    if type(removed) is Found: self.assertEqual(record(removed.value)['deleted'], 0 if feedback_first else 1)
                    reader = h.runtime.memory.bind_read((oid,), ('get_current', 'get_for_deep_read'))
                    visible = await reader.get_current(oid)
                    self.assertIs(type(visible), Found if feedback_first else NotFound, visible)
                    if feedback_first:
                        if type(visible) is Found: self.assertIsNone(record(visible.value)['forgotten_since_us'])
                    else:
                        late = await port.record_usage(payload)
                        self.assertIn(type(late), (InformationRejected, InformationNotCommitted), late)
                        if type(late) is InformationRejected or type(late) is InformationNotCommitted: self.assertEqual(late.error.reason, 'OBJECT_DELETED')
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                        self.assertEqual(c.execute('SELECT count(*) FROM retrieval_consumption').fetchone()[0], int(feedback_first))
                        self.assertEqual(c.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], int(feedback_first))
                finally: self.assertTrue(await h.close())
