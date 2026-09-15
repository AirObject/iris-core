"""Fixed sources survive public maintenance; deletion remains purely local."""
from types import MappingProxyType
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from typing import cast
from companion_memory.persistence import Committed,Found,NotFound
from companion_memory.persistence.content_codec import decode_content
from companion_memory.memory.formats import record
from companion_memory.ingress.events import plain
from companion_memory.persistence.semantic_records import number,string
from tests.semantic.public_support import establish


class PublicChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_source_replacement_delete_gap_and_zero_provider_work(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();host,oid=await establish(root)
            try:
                assert host.runtime is not None and host.semantic is not None and host.embedding is not None
                read=host.runtime.memory.bind_read((oid,),('get_current','get_for_deep_read'))
                current=await read.get_current(oid);self.assertIs(type(current),Found);assert type(current) is Found
                proposed=cast(dict,plain(current.value));proposed['revision']=2;proposed['content']['body']='Updated fixed-source fact.'
                linkrow=(await host.assembly.memory.rows.read('links_get',{'object_id':oid}))[0]
                links=cast(dict,decode_content(string(linkrow['body']).encode(),2048))
                for source in links['sources']:source['object_revision']=2
                port=host.runtime.maintenance.bind((oid,))
                changed=await port.replace_current('replace',{'change_version':1,'action':'REPLACE_CURRENT','target_id':oid,
                    'expected_revision':1,'proposed_value':proposed,'links':links})
                self.assertIs(type(changed),Committed,changed)
                current,gap=await host.semantic.owner.memory.semantic_current(oid);assert current is not None and gap is not None
                self.assertEqual(current['revision'],2);self.assertEqual(gap['object_revision'],2)
                deleted=await port.delete_object('delete',oid,2);self.assertIs(type(deleted),Committed,deleted);assert type(deleted) is Committed
                self.assertIs(type(await read.get_current(oid)),NotFound)
                current,gap=await host.semantic.owner.memory.semantic_current(oid);assert gap is not None
                self.assertIsNone(current);self.assertEqual(gap['action'],'DELETE');self.assertEqual(gap['object_revision'],3)
                work_id=await host.semantic.prepare_delete(oid,3,number(gap['latest_change_seq']),host.embedding.reference(deleted.receipt));assert type(work_id) is str,work_id
                result=await host.semantic.run_work(work_id);assert type(result) is MappingProxyType,result;self.assertEqual(result['state'],'LOCAL_APPLIED');self.assertFalse(result['cleanup_pending'])
                _,gap=await host.semantic.owner.memory.semantic_current(oid);self.assertIsNone(gap)
                self.assertEqual(host.embedding.executions,0)
                with sqlite3.connect(root/'database/runtime.sqlite3') as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_embedding_input_leaf').fetchone()[0],0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_embedding_artifact').fetchone()[0],0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM logging_object_history').fetchone()[0],2)
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_sources').fetchone()[0],1)
                replay=await host.semantic.run_work(work_id);assert type(replay) is MappingProxyType,replay;self.assertEqual(replay,result)
            finally:self.assertTrue(await host.close())
