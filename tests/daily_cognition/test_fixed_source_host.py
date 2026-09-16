"""Daily fixed-source compatibility retains a twelve-member reviewed manifest.

Only four members are established through the public native writer. The other
eight remain reviewed material and are never counted as learned output.
"""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
import unittest
from companion_memory.persistence import Found,Committed
from .test_host import make_host
from tests.semantic.test_semantic_host import material


class FixedSourceHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_reviewed_manifest_establishes_only_four_memories_and_reopens_without_sends(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);credentials=[];host=make_host(root,9,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                if host.fixed is None or host.stored is None:raise AssertionError()
                fixed=host.fixed;claims=host.resources.review.claims
                def envelope(kind,key,payload):return fixed.envelope(kind,key,MappingProxyType(payload),1)
                config={'database_id':host.stored.database_id,'instance_id':'instance','snapshot_id':host.stored.snapshot_id}
                begun=await fixed.begin(envelope('fixed_begin','begin',{'set_id':'fixed-set','config':config,
                    **{k:claims[k] for k in ('manifest_digest','review_ref','review_digest')}}),time.monotonic()+5)
                self.assertIs(type(begun),Committed,begun)
                for ordinal,member in enumerate(material()):
                    added=await fixed.add_member(envelope('fixed_add_member','add-'+str(ordinal),{'set_id':'fixed-set','expected_revision':ordinal+1,**member}),time.monotonic()+5)
                    self.assertIs(type(added),Committed,added)
                sealed=await fixed.seal(envelope('fixed_seal','seal',{'set_id':'fixed-set','expected_revision':13}),time.monotonic()+5)
                self.assertIs(type(sealed),Committed,sealed)
                for ordinal in range(4):
                    original=envelope('fixed_establish','establish-'+str(ordinal),{'set_id':'fixed-set','expected_revision':14+ordinal,
                        'ordinal':ordinal,'expected_member_revision':1})
                    established=await fixed.establish_one(original,time.monotonic()+5)
                    self.assertIs(type(established),Committed,established)
                    confirmed=await fixed.resolve('fixed_establish',original,time.monotonic()+5)
                    self.assertIs(type(confirmed),Committed,confirmed)
                    if type(confirmed) is Committed and type(established) is Committed:self.assertEqual(confirmed.receipt,established.receipt)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],4)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0],4)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())
            host=make_host(root,9,credentials)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened);self.assertFalse(credentials)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],4)
            finally:self.assertTrue(await host.close())
