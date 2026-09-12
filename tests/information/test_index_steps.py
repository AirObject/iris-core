"""Actual index material and checkpoints use existing memory owner transactions."""
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import time
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.content_codec import encode_content
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, text
from companion_memory.retrieval.lexical import normalize_material, object_text
from tests.information.host_support import host, learn_one


class IndexStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_object_material_and_page_acknowledgement_are_atomic(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                port = await h.bind_management(HostIdentity('index-worker', 'index-worker', 'host', 'entry',
                    frozenset(('index_begin', 'index_claim', 'index_apply_object', 'index_confirm_page')), (), time.monotonic() + 300))
                async def step(kind: str, key: str, payload: object):
                    result = await port.execute(kind, key, payload)
                    self.assertIs(type(result), Committed, result)
                    if type(result) is not Committed: self.fail('Expected confirmed index step.')
                    return record(record(record(result.receipt.result)['facts'])['retrieval'])
                generation = await step('index_begin', 'build', {'expected_generation': None})
                page = await step('index_claim', 'page', {'generation_id': generation['object_id'], 'expected_revision': 1, 'owner_id': 'index-worker'})
                if h.assembly.memory.information is None: self.fail('Expected native change tracking.')
                objects = await h.assembly.memory.information.current_page()
                self.assertEqual(len(objects), 1); self.assertEqual(objects[0]['object_id'], oid)
                current = objects[0]; material = normalize_material(object_text(current), byte_limit=8192, term_limit=4096)
                applied = await step('index_apply_object', 'object', {'generation_id': generation['object_id'], 'page_id': page['object_id'],
                    'expected_revision': 1, 'object_id': oid, 'object_revision': current['revision'], 'action': 'UPSERT',
                    'body_digest': sha256(encode_content(current, 4096)).hexdigest(), 'normalized_text': material.text})
                await step('index_confirm_page', 'confirm', {'generation_id': generation['object_id'], 'page_id': page['object_id'],
                    'expected_revision': applied['revision'], 'scan_complete': True})
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM retrieval_posting').fetchone()[0], len(material.terms))
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_index_gap').fetchone()[0], 0)
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_generation_ack').fetchone()[0], 1)
                    self.assertEqual(c.execute('SELECT status FROM retrieval_index_page').fetchone()[0], 'COMMITTED')
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                self.assertEqual(h.adapter.calls, ())
                self.assertTrue(await h.close())
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    c.execute('DELETE FROM retrieval_posting WHERE ordinal=0'); c.commit()
                h = host(root)
                damaged = await h.initialize('OPEN_EXISTING')
                self.assertIsNot(type(damaged), Found, damaged)
                self.assertNotEqual(h.state, 'READY')
            finally:self.assertTrue(await h.close())
