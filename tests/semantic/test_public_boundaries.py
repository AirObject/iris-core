"""Public fixed atomicity and original query deadline/permission boundaries."""
import asyncio
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.information.errors import InformationRejected
from companion_memory.persistence.semantic_records import identity
from tests.persistence.support import Hooks,sqlite_fault
from tests.semantic.public_support import establish
from tests.information.test_queries import query


class PublicBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_fixed_audit_failure_rolls_back_then_original_key_confirms(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();hooks=Hooks();host,_=await establish(root,connect=hooks.connect)
            try:
                fixed=host.fixed;assert fixed is not None and host.embedding is not None
                tables=('cognition_fixed_memory_member','memory_objects','memory_sources','memory_links','memory_semantic_gap','audit_records','operation_receipts')
                def counts():
                    with sqlite3.connect(root/'database/runtime.sqlite3') as db:
                        return tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in tables)
                before=counts();seen=[]
                def fail(sql):
                    if sql.startswith('INSERT INTO audit_records'):
                        seen.append(sql)
                        if len(seen)==2:raise sqlite_fault(sqlite3.SQLITE_FULL)
                envelope=fixed.envelope('fixed_establish','establish:1',MappingProxyType({'set_id':'fixed-set','expected_revision':15,'ordinal':1,'expected_member_revision':1}),1)
                hooks.before=fail
                rejected=await fixed.establish_one(envelope,time.monotonic()+5);self.assertIsNot(type(rejected),Committed)
                self.assertEqual(counts(),before);hooks.before=lambda sql:None
                committed=await fixed.establish_one(envelope,time.monotonic()+5);self.assertIs(type(committed),Committed,committed)
                repeated=await fixed.resolve('fixed_establish',envelope,time.monotonic()+5)
                self.assertIs(type(repeated),Committed,repeated)
                self.assertEqual(host.embedding.executions,0)
            finally:hooks.before=lambda sql:None;self.assertTrue(await host.close())

    async def test_explicit_permissions_and_absolute_query_deadline_do_not_issue_ticket(self):
        with TemporaryDirectory() as directory:
            host,oid=await establish(Path(directory).resolve())
            try:
                assert host.runtime is not None and host.queries is not None and host.embedding is not None
                port=await host.bind_query(HostIdentity('bounded','principal','host','entry',frozenset(('search_memory',)),(),time.monotonic()+300),object_ids=('different-object',))
                denied=await port.search_memory(query('denied',object_ids=[oid],retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False))
                self.assertIs(type(denied),InformationRejected,denied)
                assert type(denied) is InformationRejected
                self.assertEqual(denied.error.code,'ACCESS_DENIED')
                unrestricted=await host.bind_query(HostIdentity('timeout','principal','host','entry',frozenset(('search_memory',)),(),time.monotonic()+300))
                assert host.queries.semantic is not None;original=host.queries.semantic.select
                async def delayed(*args,**kwargs):
                    await asyncio.sleep(1.1)
                    return await original(*args,**kwargs)
                host.queries.semantic.select=delayed
                result=await unrestricted.search_memory(query('expired',object_ids=[oid],retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False))
                self.assertIs(type(result),InformationRejected,result);assert type(result) is InformationRejected
                self.assertEqual(result.error.code,'TIMEOUT')
                await asyncio.sleep(.2)
                self.assertEqual(host.embedding.executions,0)
                with sqlite3.connect(Path(directory)/'database/runtime.sqlite3') as db:self.assertEqual(db.execute('SELECT count(*) FROM retrieval_ticket').fetchone()[0],0)
            finally:self.assertTrue(await host.close())

    async def test_expired_original_intent_finishes_not_sent_without_new_request(self):
        with TemporaryDirectory() as directory:
            host,oid=await establish(Path(directory).resolve())
            try:
                from tests.semantic.public_support import activate
                from companion_memory.persistence.semantic_records import number
                activate(host);assert host.semantic is not None and host.embedding is not None
                await host.semantic.resume('resume')
                work_id=await host.semantic.prepare_document(oid,'original-expiring-intent','partition');assert type(work_id) is str,work_id
                provider=host.embedding;original=provider.send_verified_first
                async def delayed_registration(request,seal,intent,*,admission_deadline:float|None=None):
                    # Actual binding and its original deadline persist, while a
                    # controlled scheduling barrier precedes any registration.
                    provider.release_absence(seal)
                    return MappingProxyType({'state':'PENDING','cleanup_pending':False})
                provider.send_verified_first=delayed_registration
                pending=await host.semantic.run_work(work_id,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                from companion_memory.runtime.results import Failed
                self.assertIs(type(pending),Failed,pending)
                work=await host.semantic.work(work_id)
                self.assertEqual(work['state'],'PREPARED');self.assertIsNone(work['request_ref'])
                provider.send_verified_first=original
                deadline=number(work['deadline_at'])
                while time.time_ns()//1000<=deadline:await asyncio.sleep(min(.5,max(.001,(deadline-time.time_ns()//1000)/1000000)))
                ended=await host.semantic.run_work(work_id);assert type(ended) is MappingProxyType,ended
                self.assertEqual(ended['state'],'NOT_SENT');self.assertFalse(ended['cleanup_pending'])
                self.assertEqual(ended['deadline_at'],deadline);self.assertEqual(provider.executions,0)
                self.assertEqual(await provider.ledger.read('requests_page',{'after':'','limit':8}),())
                self.assertEqual(await host.semantic.run_work(work_id),ended)
            finally:self.assertTrue(await host.close())

    async def test_frozen_document_slot_rejects_later_revision_before_reserve(self):
        with TemporaryDirectory() as directory:
            host,oid=await establish(Path(directory).resolve())
            try:
                from tests.semantic.public_support import activate
                from companion_memory.ingress.events import plain
                from companion_memory.persistence.content_codec import decode_content
                from companion_memory.persistence.semantic_records import string
                from companion_memory.persistence.schema import InvalidValue
                from typing import cast
                assert host.runtime is not None and host.semantic is not None and host.embedding is not None
                current,_=await host.semantic.owner.memory.semantic_current(oid);assert current is not None
                proposed=cast(dict,plain(current));proposed['revision']=2;proposed['content']['body']='Unreviewed changed wording.'
                link=(await host.assembly.memory.rows.read('links_get',{'object_id':oid}))[0]
                links=cast(dict,decode_content(string(link['body']).encode(),2048))
                for source in links['sources']:source['object_revision']=2
                result=await host.runtime.maintenance.bind((oid,)).replace_current('replace-before-activation',{'change_version':1,'action':'REPLACE_CURRENT',
                    'target_id':oid,'expected_revision':1,'proposed_value':proposed,'links':links})
                self.assertIs(type(result),Committed,result)
                activate(host);await host.semantic.resume('resume')
                work=await host.semantic.prepare_document(oid,'unreviewed-revision','partition');assert type(work) is str,work
                with self.assertRaises(InvalidValue):await host.semantic.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                assert host.authorization is not None
                self.assertFalse(host.authorization._entries);self.assertEqual(host.embedding.executions,0)
                self.assertEqual(await host.embedding.ledger.read('requests_page',{'after':'','limit':8}),())
            finally:self.assertTrue(await host.close())
