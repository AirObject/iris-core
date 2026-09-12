"""Information recovery keeps full source and payload checks across bounded pages."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.persistence.service import OperationPort
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.persistence import Found
from tests.information.host_support import host, learn_objects


class SourceRecoveryPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_holder_and_last_payload_in_shared_source_are_all_verified(self):
        for mutation in ('none', 'last_holder', 'last_payload', 'member_body'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); h = host(root)
                h.candidates = SyntheticCandidateInput('source_pages', tuple('完整来源对象' + str(i) for i in range(8)), 50)
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                ids = tuple(sorted(await learn_objects(h)))
                self.assertTrue(await h.close())
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_source_holders').fetchone()[0], 8)
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_source_members').fetchone()[0], 3)
                    if mutation == 'last_holder':
                        c.execute('UPDATE memory_source_holders SET owner_id=? WHERE owner_id=?', ('wrong_object', ids[-1]))
                    elif mutation == 'last_payload':
                        message = c.execute('SELECT message_id FROM memory_source_members ORDER BY ordinal DESC LIMIT 1').fetchone()[0]
                        c.execute('DELETE FROM ingress_content_payloads WHERE message_id=?', (message,))
                    elif mutation == 'member_body':
                        member = json.loads(c.execute('SELECT body FROM memory_source_members WHERE ordinal=2').fetchone()[0])
                        member['payload_digest'] = 'f' * 64
                        c.execute('UPDATE memory_source_members SET body=? WHERE ordinal=2', (json.dumps(member, sort_keys=True, separators=(',', ':')),))
                    c.commit()
                h = host(root); h.candidates = SyntheticCandidateInput('source_pages', tuple('完整来源对象' + str(i) for i in range(8)), 50)
                try:
                    # A finished memory batch is checked from its original
                    # receipt; this recovery has no command-execution need.
                    with patch.object(OperationPort, 'execute', side_effect=AssertionError('Recovery attempted another persistent command.')):
                        result = await h.initialize('OPEN_EXISTING')
                    if mutation == 'none': self.assertIs(type(result), Found, result)
                    else:
                        self.assertIsNot(type(result), Found, result); self.assertNotEqual(h.state, 'READY')
                    self.assertEqual(h.adapter.calls, ())
                finally: self.assertTrue(await h.close())
