"""Inactive proposals bind complete relocated resources without minting consent.

Old stop fixtures are synthetic files. No user evidence, credentials, native
business database or supplier endpoint is accessible through these tests.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.configuration.content_codec import entries_digest, decode_content_entry
from .authorization import AuthorizationJournal
from .deepseek_prepare import proposal


class ProposalTests(unittest.TestCase):
    def test_deterministic_complete_domains_and_inactive_explicit_approval(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);old=root/'old';old.mkdir()
            names=('continuation-report.md','continuation-requests.json','macos-learning-5-unknown-recovery/result.json',
                'macos-learning-5-reconciliation.json','continuation-authorization-snapshot/approval.json')
            for name in names:
                path=old/name;path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({'package_digest':'a'*64}))
            for ordinal in range(10):
                for offset,kind in enumerate(('RESERVED','SETTLED')):
                    value={'kind':kind,'continue_allowed':ordinal<9,'remote_known':ordinal<9,'cleanup_ended':True}
                    (old/'continuation-authorization-snapshot'/f'event-{ordinal*2+offset:03d}.json').write_text(json.dumps(value))
            roots={p:str(root/'deepseek'/p) for p in ('macos','linux')}
            value=proposal(old,roots)
            self.assertEqual(value,proposal(old,roots));self.assertIsNone(value['authorization'])
            self.assertFalse((root/'deepseek').exists())
            for platform,configuration in value['configurations'].items():
                self.assertEqual(configuration['entry_count'],118)
                for domain in configuration['native_domains']['domains']:
                    entries=tuple((e['parameter_key'],e['body']) for e in domain['entries'])
                    self.assertEqual(domain['digest'],entries_digest(entries))
                    for _,body in entries:decode_content_entry(body)
                self.assertEqual(configuration['values']['runtime.operation_timeout_ms'],60000)
            journal=AuthorizationJournal(root/'authorization')
            with self.assertRaises(ValueError):journal.create(value['activation_template'])
            self.assertFalse(journal.path.exists())
