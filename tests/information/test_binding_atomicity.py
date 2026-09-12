"""Failed native binding releases owned handles and preserves borrowed authority."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.information.management import HostIdentity
from companion_memory.information.business import InformationPort
from companion_memory.retrieval.query_service import QueryPort
from companion_memory.persistence import Found
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.information.host_support import host


class BindingAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_bindings_do_not_accumulate_and_corrected_identity_retries(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('entry', 'entry', 'host', 'sample_platform', 'entry')
                if h.queries is None or h.business is None or h.runtime is None: self.fail('Expected bound owners.')
                queries, business, memory = h.queries, h.business, h.runtime.memory
                def counts():
                    return len(h.management._ports), len(h.management._recall_authorities), len(memory._grants), len(queries._ports), len(business.ports)
                baseline = counts()
                for business_binding in (False, True):
                    bind = business.bind if business_binding else queries.bind
                    for failure in ('deep', 'scope', 'capacity'):
                        with self.subTest(business=business_binding, failure=failure):
                            grant = HostIdentity('retry', 'principal', 'host', 'entry', frozenset(('deep_recall' if failure == 'deep' else 'search_memory',)), (), time.monotonic() + 300)
                            borrowed = []
                            if failure == 'capacity':
                                while len(memory._grants) < memory._page:
                                    borrowed.append(memory.bind_query_scope(include_forgotten=False))
                            occupied = counts()
                            for _ in range(20):
                                with self.assertRaises(OwnerFailure): bind(grant, object_ids=() if failure == 'scope' else None)
                                self.assertEqual(counts(), occupied)
                            for scope in borrowed: memory.release_query_scope(scope)
                            port = bind(grant, include_forgotten=failure == 'deep')
                            if type(port) is InformationPort: business.revoke(port)
                            elif type(port) is QueryPort: queries.revoke(port)
                            else: self.fail('Expected exact opaque port.')
                            self.assertEqual(counts(), baseline)
            finally: self.assertTrue(await h.close())

    async def test_borrowed_management_and_existing_recall_link_survive_failure_and_revoke(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('entry', 'entry', 'host', 'sample_platform', 'entry')
                if h.queries is None or h.runtime is None: self.fail('Expected native owners.')
                queries, memory = h.queries, h.runtime.memory
                grant = HostIdentity('borrow', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300)
                management = h.management.issue(replace(grant, operations=frozenset(('ticket_issue_normal',))))
                for _ in range(20):
                    with self.assertRaises(OwnerFailure): queries.bind(grant, object_ids=(), management=management)
                    self.assertIs(h.management._ports[management.binding_id], management)
                    self.assertEqual(len(memory._grants), 0)
                with self.assertRaises(OwnerFailure):
                    queries.bind(replace(grant, operations=frozenset(('deep_recall',))), include_forgotten=True, management=management)
                self.assertIs(h.management._ports[management.binding_id], management)
                self.assertEqual(len(memory._grants), 0)
                original = queries.bind(grant, management=management)
                authority = original._authority
                for _ in range(20):
                    with self.assertRaises(OwnerFailure): queries.bind(grant, management=management)
                    self.assertIs(h.management._recall_authorities[management.binding_id], authority)
                    self.assertEqual(len(memory._grants), 1)
                queries.revoke(original)
                self.assertIs(h.management._ports[management.binding_id], management)
                self.assertNotIn(management.binding_id, h.management._recall_authorities)
                self.assertEqual(len(memory._grants), 0)
                retry = queries.bind(grant, management=management)
                queries.revoke(retry); management.revoke()
                self.assertFalse(h.management._ports)
            finally: self.assertTrue(await h.close())
