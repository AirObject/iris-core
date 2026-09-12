"""Real publication roots, retained audits, dual coverage and corrupt recovery."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.information.errors import InformationNotCommitted, InformationUnconfirmed
from companion_memory.information.records import record
from companion_memory.information.index_worker import LocalIndexWorker
from companion_memory.persistence import Found, Committed, Failed
from companion_memory.persistence.content_codec import encode_content
from companion_memory.memory.information_repository import SEQUENCE
from companion_memory.information.records import checked
from tests.information.host_support import host, learn_one
from tests.information.publication_support import index_port, build, publish
from tests.information.test_expiry import scores
from tests.persistence.support import Hooks, sqlite_fault


def database_root(root: Path) -> dict[str, object]:
    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
        return json.loads(c.execute('SELECT body FROM memory_change_sequence').fetchone()[0])


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_publications_keep_zero_cutoff_and_paginate_retained_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                port = await index_port(h)
                initial = database_root(root); before = initial
                self.assertIsNone(initial['published_generation_id']); self.assertEqual(initial['published_seq'], 0)
                for ordinal in range(18):
                    gid, generation = await build(h, port, 'build-' + str(ordinal))
                    result = await publish(port, 'publish-' + str(ordinal), generation)
                    before = database_root(root)
                    self.assertEqual(before['published_generation_id'], gid)
                    self.assertEqual((before['last_seq'], before['published_seq'], before['revision']), (0, 0, ordinal + 2))
                    original = await publish(port, 'publish-' + str(ordinal), generation)
                    self.assertEqual(original.receipt, result.receipt)
                    self.assertIs(type(await port.execute('index_publish', 'different-' + str(ordinal),
                        {'generation_id': gid, 'expected_revision': generation['revision']})), InformationNotCommitted)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                        audits = [json.loads(row[0]) for row in c.execute('SELECT record FROM audit_records WHERE commit_id=?', (result.receipt.commit_id,))]
                        self.assertEqual(len(audits), 2)
                        facts = record(record(result.receipt.result)['facts'])
                        for audit in audits:
                            self.assertEqual(audit['change'], dict(record(facts[audit['owner_module']])))
                            self.assertEqual(audit['target_refs'], json.loads(encode_content(record(result.receipt.result)['targets'], 4096)))
                        self.assertEqual(c.execute('SELECT count(*) FROM memory_index_gap').fetchone()[0], 0)
                        self.assertEqual(c.execute('SELECT count(*) FROM memory_generation_ack').fetchone()[0], 0)
                from companion_memory.runtime.content_assembly import stable
                await h.initializer.recover(stable('initialize_information', 'instance'))
                if h.retrieval is None: self.fail('Expected recovered owner.')
                self.assertTrue(h.retrieval.ready)
                operation = h.management.operations['index_publish']
                page = await operation.read_receipt_page()
                self.assertIs(type(page), Found)
                if type(page) is not Found: self.fail('Expected bounded receipt page.')
                self.assertEqual(len(page.value), 16)
                remainder = await operation.read_receipt_page(page.value[-1].identity.operation_key)
                if type(remainder) is not Found: self.fail('Expected remaining original receipts.')
                self.assertEqual(len(remainder.value), 2)
                self.assertIs(type(await operation.read_receipt_page('missing')), Failed)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                self.assertEqual(database_root(root), before); self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_dual_coverage_retirement_pin_and_later_object_changes_recover(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                port = await index_port(h)
                first, generation = await build(h, port, 'empty')
                await publish(port, 'empty-publish', generation)
                oid = await learn_one(h)
                self.assertEqual((database_root(root)['published_seq'], database_root(root)['last_seq']), (0, 1))
                if h.retrieval is None or h.runtime is None: self.fail('Expected real workers.')
                worker = LocalIndexWorker(h.runtime, h.retrieval, port, 'publication')
                self.assertIs(type(await worker.run(first)), Committed); await asyncio.sleep(0)
                second, generation = await build(h, port, 'second')
                self.assertIs(type(await scores(h, oid, 19)), Committed)
                self.assertIs(type(await worker.run(first)), Committed); await asyncio.sleep(0)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_index_gap').fetchone()[0], 1)
                stale = await port.execute('index_publish', 'stale', {'generation_id': second, 'expected_revision': generation['revision']})
                self.assertIs(type(stale), InformationNotCommitted)
                self.assertIs(type(await worker.run(second)), Committed); await asyncio.sleep(0)
                generation, _ = await h.retrieval.work_page(second)
                h.retrieval.retain_query('old-reader')
                await publish(port, 'second-publish', generation)
                trim = {'generation_id': first, 'page_id': None, 'expected_revision': None, 'objects': (oid,)}
                self.assertIs(type(await port.execute('index_trim_page', 'trim-held', trim)), InformationNotCommitted)
                h.retrieval.release_query('old-reader')
                self.assertIs(type(await port.execute('index_trim_page', 'trim', trim)), Committed)
                self.assertIs(type(await h.runtime.maintenance.bind((oid,)).delete_object('delete', oid, 2)), Committed)
                self.assertEqual((database_root(root)['published_seq'], database_root(root)['last_seq'], database_root(root)['revision']), (2, 3, 6))
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                port = await index_port(h)
                if h.retrieval is None or h.runtime is None: self.fail('Expected recovered owners.')
                worker = LocalIndexWorker(h.runtime, h.retrieval, port, 'publication')
                self.assertIs(type(await worker.run(second)), Committed); await asyncio.sleep(0)
                third, generation = await build(h, port, 'third')
                await publish(port, 'third-publish', generation)
                self.assertEqual(database_root(root)['published_generation_id'], third)
                self.assertEqual(database_root(root)['revision'], 7)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_delete_during_rebuild_requires_both_generation_acknowledgments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h); port = await index_port(h)
                first, generation = await build(h, port, 'first'); await publish(port, 'first-publish', generation)
                second, _ = await build(h, port, 'second')
                if h.runtime is None or h.retrieval is None: self.fail('Expected actual owners.')
                self.assertIs(type(await h.runtime.maintenance.bind((oid,)).delete_object('delete-building', oid, 1)), Committed)
                worker = LocalIndexWorker(h.runtime, h.retrieval, port, 'publication')
                self.assertIs(type(await worker.run(first)), Committed); await asyncio.sleep(0)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_index_gap').fetchone()[0], 1)
                    self.assertEqual(c.execute('SELECT generation_id FROM retrieval_index_object').fetchall(), [(second,)])
                self.assertIs(type(await worker.run(second)), Committed); await asyncio.sleep(0)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM memory_index_gap').fetchone()[0], 0)
                    self.assertEqual(c.execute('SELECT count(*) FROM retrieval_posting').fetchone()[0], 0)
                generation, _ = await h.retrieval.work_page(second)
                await publish(port, 'second-publish', generation)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                self.assertEqual(database_root(root)['published_seq'], 2)
            finally: self.assertTrue(await h.close())

    async def test_each_owner_and_each_audit_failure_roll_back_the_entire_switch(self):
        for prefix, denied in (('UPDATE memory_change_sequence', 1), ('UPDATE retrieval_index_generation', 1),
                               ('UPDATE retrieval_coordinator', 1), ('INSERT INTO audit_records', 1), ('INSERT INTO audit_records', 2), ('INSERT INTO operation_receipts', 1)):
            with self.subTest(prefix=prefix, denied=denied), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); hooks = Hooks(); h = host(root, hooks.connect)
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    port = await index_port(h); gid, generation = await build(h, port, 'build')
                    original = database_root(root); ordinal = 0
                    def fail(sql: str) -> None:
                        nonlocal ordinal
                        if sql.startswith(prefix):
                            ordinal += 1
                            if ordinal == denied: raise sqlite3.OperationalError('Isolated publication write failure.')
                    hooks.before = fail
                    result = await port.execute('index_publish', 'publish', {'generation_id': gid, 'expected_revision': generation['revision']})
                    self.assertIs(type(result), InformationNotCommitted, result)
                    hooks.before = lambda sql: None
                    self.assertEqual(database_root(root), original)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                        self.assertEqual(c.execute('SELECT status FROM retrieval_index_generation').fetchone(), ('BUILDING',))
                        self.assertEqual(c.execute("SELECT count(*) FROM operation_receipts WHERE operation_kind='index_publish'").fetchone()[0], 0)
                finally: hooks.before = lambda sql: None; self.assertTrue(await h.close())

    async def test_unknown_commit_original_confirmation_never_republishes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); hooks = Hooks(); h = host(root, hooks.connect)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                port = await index_port(h); gid, generation = await build(h, port, 'build')
                armed = False
                def fail(sql: str) -> None:
                    nonlocal armed
                    if sql.startswith('UPDATE memory_change_sequence'): armed = True
                    if armed and sql == 'COMMIT':
                        armed = False; raise sqlite_fault(sqlite3.SQLITE_IOERR)
                hooks.after = fail
                payload = {'generation_id': gid, 'expected_revision': generation['revision']}
                unknown = await port.execute('index_publish', 'publish', payload)
                self.assertIs(type(unknown), InformationUnconfirmed, unknown)
                hooks.after = lambda sql: None
                await asyncio.gather(*h.management.jobs)
                confirmed = await port.resolve('index_publish', 'publish', payload)
                self.assertIs(type(confirmed), Committed, confirmed)
                self.assertEqual(h.storage.get_health().lifecycle, 'FAULTED')
                state = database_root(root)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                port = await index_port(h)
                self.assertIs(type(await port.resolve('index_publish', 'publish', payload)), Committed)
                self.assertEqual(database_root(root), state)
            finally: hooks.after = lambda sql: None; self.assertTrue(await h.close())

    async def test_mutated_publication_identity_cutoff_revision_and_pointer_fail_recovery(self):
        for mutation in ('generation', 'cutoff', 'revision', 'pointer', 'historical_cutoff'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); h = host(root)
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                port = await index_port(h); _, generation = await build(h, port, 'empty')
                await publish(port, 'publish', generation)
                await learn_one(h)
                self.assertTrue(await h.close())
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    table = 'retrieval_coordinator' if mutation == 'pointer' else 'memory_change_sequence'
                    body = json.loads(c.execute('SELECT body FROM ' + table).fetchone()[0])
                    if mutation == 'pointer': body['active_generation'] = None
                    elif mutation == 'generation': body['published_generation_id'] = 'wrong'
                    elif mutation in ('cutoff', 'historical_cutoff'): body['published_seq'] = 1
                    else: body['revision'] += 1
                    encoded = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
                    c.execute('UPDATE ' + table + ' SET body=?' + (',revision=?' if mutation == 'revision' else ''),
                        (encoded, body['revision']) if mutation == 'revision' else (encoded,)); c.commit()
                h = host(root)
                try:
                    self.assertIsNot(type(await h.initialize('OPEN_EXISTING')), Found)
                    self.assertNotEqual(h.state, 'READY')
                    if h.retrieval is not None: self.assertFalse(h.retrieval.ready)
                finally: self.assertTrue(await h.close())

    def test_closed_sequence_maximum_is_encoded_and_rejects_missing_fields(self):
        value = {'instance_id': 'i' * 128, 'last_seq': 2**63 - 1, 'revision': 2**63 - 1,
            'published_generation_id': 'g' * 128, 'published_seq': 2**63 - 1}
        encoded = encode_content(checked(SEQUENCE, value, 512), 512)
        self.assertEqual(len(encoded), 401)
        with TemporaryDirectory() as directory, closing(sqlite3.connect(Path(directory) / 'shape.sqlite3')) as c:
            c.execute('CREATE TABLE exact_shape(body BLOB NOT NULL)'); c.execute('INSERT INTO exact_shape VALUES(?)', (encoded,)); c.commit()
            self.assertEqual(c.execute('SELECT body FROM exact_shape').fetchone()[0], encoded)
        from companion_memory.persistence.schema import InvalidValue
        with self.assertRaises(InvalidValue): checked(SEQUENCE, {k: v for k, v in value.items() if k != 'published_seq'}, 512)
