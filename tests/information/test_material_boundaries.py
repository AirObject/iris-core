"""Real maximum body fields and eight-member delivery, plus tokenizer limits.

The full tokenizer limit is a separate material fixture: its expanded text is
too large for a formal body and must not be claimed as a realizable stored object.
"""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.information.index_worker import LocalIndexWorker
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, text
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.retrieval.lexical import normalize_material
from tests.information.host_support import host, learn_objects
from tests.information.test_queries import query


def maximum_body() -> str:
    """A finite Euler walk yields distinct adjacent ASCII pairs in 2048 bytes."""
    alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    edges = {character: list(alphabet) for character in alphabet}
    stack = [alphabet[0]]; path: list[str] = []
    while stack:
        remaining = edges[stack[-1]]
        if remaining: stack.append(remaining.pop())
        else: path.append(stack.pop())
    return ''.join(reversed(path))[:2048]


class MaterialBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_full_tokenizer_material_limit_is_separate_from_the_formal_body_limit(self):
        body = ''.join(chr(0x4e00 + ordinal) for ordinal in range(2048))
        full = normalize_material(body + body[-1], byte_limit=8192, term_limit=4096)
        self.assertEqual(len(full.terms), 4096)
        self.assertGreater(len(full.text.encode()), 2048)
        with self.assertRaises(OwnerFailure): normalize_material(body + chr(0x5600), byte_limit=8192, term_limit=4096)

    async def test_eight_maximum_body_objects_encode_index_deliver_and_recover(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root); body = maximum_body()
            h.candidates = SyntheticCandidateInput('maximum_body', (body,) * 8, 50)
            self.assertEqual(len(body.encode()), 2048)
            self.assertEqual(len(normalize_material(body, byte_limit=8192, term_limit=4096).terms), 1076)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                ids = tuple(sorted(await learn_objects(h)))
                port = await h.bind_query(HostIdentity('query', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300))
                found = await port.search_memory(query('maximum', query_text=body))
                # The separate query text bound rejects a full stored body.
                from companion_memory.information.errors import InformationRejected
                self.assertIs(type(found), InformationRejected)
                found = await port.search_memory(query('finite', query_text=body[0]))
                if type(found) is not Found: self.fail('Expected actual maximum-field delivery: ' + repr(found))
                memories = record(record(found.value)['sections'])['memories']
                if type(memories) is not tuple: self.fail('Expected bounded eight-member snapshot.')
                self.assertEqual(tuple(sorted(text(record(item)['object_id']) for item in memories)), ids)
                for item in memories:
                    self.assertEqual(record(record(item)['content'])['body'], body)
                    self.assertLessEqual(len(encode_content(item, 8192)), 8192)
                self.assertLessEqual(len(encode_content(found.value, 65536)), 65536)
                native = await h.bind_management(HostIdentity('index', 'principal', 'host', 'entry', frozenset(('index_begin', 'index_claim', 'index_apply_object', 'index_confirm_page', 'index_publish')), (), time.monotonic() + 300))
                begun = await native.execute('index_begin', 'begin', {'expected_generation': None})
                if type(begun) is not Committed or h.runtime is None or h.retrieval is None: self.fail('Expected actual index work.')
                generation = text(record(record(record(begun.receipt.result)['facts'])['retrieval'])['object_id'])
                worker = LocalIndexWorker(h.runtime, h.retrieval, native, 'index')
                self.assertIs(type(await worker.run(generation)), Committed)
                await asyncio.sleep(0)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_member').fetchone()[0], 8)
                    self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_posting').fetchone()[0], 8 * len(normalize_material(body, byte_limit=8192, term_limit=4096).terms))
                actual_generation, _ = await h.retrieval.work_page(generation)
                from tests.information.publication_support import publish
                await publish(native, 'publish-maximum', actual_generation)
                complete = await port.search_memory(query('active-maximum', query_text=body[0], require_complete=True))
                self.assertIs(type(complete), Found, complete)
                if type(complete) is Found: self.assertEqual(record(complete.value)['availability'], 'COMPLETE')
                self.assertTrue(await h.close()); h = host(root)
                h.candidates = SyntheticCandidateInput('maximum_body', (body,) * 8, 50)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())
