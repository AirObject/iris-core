"""Independent authoritative lexical/semantic gaps and path-specific strictness."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.information.errors import InformationRejected
from companion_memory.information.management import HostIdentity
from companion_memory.memory.formats import record
from companion_memory.persistence.semantic_records import identity,number,string
from companion_memory.persistence.content_codec import decode_content
from companion_memory.ingress.events import plain
from tests.semantic.test_index_deadlines import prepared
from tests.semantic.fault_support import server,lexical
from tests.information.test_queries import query


async def revise(host,oid,revision):
    assert host.semantic is not None and host.runtime is not None
    current,_=await host.semantic.owner.memory.semantic_current(oid);assert current is not None
    proposed=cast(dict,plain(current));proposed['revision']=revision;proposed['scores']['belief']=50+revision
    row=(await host.assembly.memory.rows.read('links_get',{'object_id':oid}))[0]
    links=cast(dict,decode_content(string(row['body']).encode(),2048))
    for source in links['sources']:source['object_revision']=revision
    result=await host.runtime.maintenance.bind((oid,)).replace_current('revision:'+str(revision),{'change_version':1,'action':'REPLACE_CURRENT',
        'target_id':oid,'expected_revision':revision-1,'proposed_value':proposed,'links':links})
    assert type(result) is Committed,result


class QueryCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_required_path_controls_complete_strict_and_no_match(self):
        with server(axis=lambda body:1 if b'query 0' in body else 0) as (address,calls),TemporaryDirectory() as directory:
            host,seq=await prepared(Path(directory).resolve(),address)
            try:
                management=host.semantic;assert management is not None and host.queries is not None and host.queries.semantic is not None
                oid=identity('fixed-memory','instance','fixed-set','member:0')
                first=identity('semantic-generation','complete',1)
                built=await management.publish(first,seq);assert type(built) is MappingProxyType,built
                index_port=await lexical(host,'lexical:1')
                port=await host.bind_query(HostIdentity('complete','principal','host','entry',frozenset(('search_memory',)),(),time.monotonic()+300))
                control=await management.control();earliest=number(control['last_cleanup_at'])+30000000
                while time.time_ns()//1000<earliest:await asyncio.sleep(min(.5,(earliest-time.time_ns()//1000)/1000000))
                work=await management.prepare_query('query 0','query-original',host.queries.semantic.partition(port));assert type(work) is str,work
                result=await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','QUERY','query:0'))
                assert type(result) is MappingProxyType,result
                async def search(key,**changes):
                    return await port.search_memory(query(key,query_text='query 0',retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False,**changes))
                complete=await search('complete',require_complete=True);assert type(complete) is Found,complete
                self.assertEqual(record(complete.value)['availability'],'COMPLETE')
                self.assertEqual(record(complete.value)['decision'],'NO_MATCH')
                self.assertIsNone(record(complete.value)['recall_id'])
                self.assertEqual(record(record(complete.value)['sections'])['memories'],())
                empty=await search('complete-empty',category='EVENT',require_complete=True);assert type(empty) is Found,empty
                self.assertEqual(record(empty.value)['decision'],'NO_MATCH');self.assertIsNone(record(empty.value)['recall_id'])
                await revise(host,oid,2);await lexical(host,'lexical:2',index_port)
                semantic_lag=await search('semantic-lag');assert type(semantic_lag) is Found,semantic_lag
                value=record(semantic_lag.value);self.assertEqual(value['availability'],'DEGRADED')
                self.assertEqual(record(record(value['coverage'])['lexical'])['pending_count'],0)
                self.assertGreater(number(record(record(value['coverage'])['semantic'])['pending_count']),0)
                self.assertIs(type(await search('strict-semantic',require_complete=True)),InformationRejected)
                reused=await management.prepare_document(oid,'revision:2','partition');assert type(reused) is str,reused
                applied=await management.run_work(reused);assert type(applied) is MappingProxyType,applied
                publication=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id);assert publication is not None
                second=identity('semantic-generation','complete',2)
                published=await management.publish(second,number(publication['material_seq']));assert type(published) is MappingProxyType,published
                complete_again=await search('both-complete',allow_partial=False);assert type(complete_again) is Found,complete_again
                self.assertEqual(record(complete_again.value)['availability'],'COMPLETE')
                for _ in range(4):
                    retired=await management.retire(first);assert type(retired) is bool,retired
                    if retired:break
                await revise(host,oid,3)
                reused=await management.prepare_document(oid,'revision:3','partition');assert type(reused) is str,reused
                applied=await management.run_work(reused);assert type(applied) is MappingProxyType,applied
                publication=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id);assert publication is not None
                third=await management.publish(identity('semantic-generation','complete',3),number(publication['material_seq']));assert type(third) is MappingProxyType,third
                lexical_lag=await search('lexical-lag');assert type(lexical_lag) is Found,lexical_lag
                value=record(lexical_lag.value);self.assertEqual(value['availability'],'DEGRADED')
                self.assertGreater(number(record(record(value['coverage'])['lexical'])['pending_count']),0)
                self.assertEqual(record(record(value['coverage'])['semantic'])['state'],'COMPLETE')
                reasons=record(value['truncation'])['reasons'];assert type(reasons) is tuple
                self.assertIn('INDEX_LAG_PARTIAL',reasons)
                self.assertIs(type(await search('strict-lexical',allow_partial=False)),InformationRejected)
                empty_lag=await search('lag-empty',category='EVENT');assert type(empty_lag) is Found,empty_lag
                self.assertEqual(record(empty_lag.value)['decision'],'INCOMPLETE_EMPTY')
                for key,changes in (('explicit',{'object_ids':[oid]}),('structure',{'query_text':'','world_scope':'REAL'})):
                    body=query(key,query_text='query 0',retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False,require_complete=True)
                    body.update(changes)
                    selected=await port.search_memory(body);assert type(selected) is Found,selected
                    value=record(selected.value);self.assertEqual(value['availability'],'COMPLETE');self.assertEqual(value['actual_mode'],'STRUCTURAL_ONLY')
                    self.assertEqual(record(value['query_vector'])['state'],'NOT_REQUIRED')
                self.assertEqual(len(calls),2)
            finally:self.assertTrue(await host.close())
