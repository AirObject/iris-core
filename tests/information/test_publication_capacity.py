"""Full real host assembly encoding, mandatory bindings and bounded durable roots."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Found, PersistenceService, RecordSchema, SequenceSchema
from companion_memory.persistence._codec import assembly_value, command_descriptor
from companion_memory.persistence.schema import encode_value, InvalidValue
from companion_memory.persistence.content_codec import encode_content
from companion_memory.information.records import checked
from companion_memory.memory.information_repository import SEQUENCE, LAYOUTS as MEMORY
from companion_memory.retrieval.repository import LAYOUTS as RETRIEVAL
from companion_memory.state.repository import LAYOUTS as STATE
from companion_memory.goals.repository import LAYOUTS as GOALS
from tests.information.host_support import host
from tests.information.publication_support import index_port, build, publish


class PublicationCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_real_commands_tables_bindings_and_publication_carriers_persist(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                commands = h.storage._commands; repositories = h.storage._repositories
                self.assertEqual(len(commands), 94)
                expected = {owner + '_' + layout.name for owner, layouts in (('memory', MEMORY), ('retrieval', RETRIEVAL), ('state', STATE), ('goals', GOALS)) for layout in layouts}
                self.assertEqual(len(expected), 26)
                carrier = assembly_value(repositories, commands, assembly_format='LOCAL_INFORMATION_V1')
                encoded = json.loads(carrier)
                descriptors = [len(encode_value(command_descriptor(command), 1048576)) for command in commands]
                repo_bytes = len(json.dumps(encoded['repositories'], ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode())
                self.assertLessEqual(sum(descriptors), 2621440); self.assertLessEqual(repo_bytes, 131072)
                self.assertLessEqual(len(carrier), 2760704); self.assertLessEqual(len(carrier), 3145728)
                publication = next(command for command in commands if command.operation_kind == 'index_publish')
                self.assertEqual(tuple(a.owner_module for a in publication.required_audits), ('retrieval', 'memory'))
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                native = await index_port(h); _, generation = await build(h, native, 'build')
                result = await publish(native, 'publish', generation)
                measurements: dict[str, object] = {'command_count': len(commands), 'new_table_count': len(expected),
                    'descriptors_bytes': sum(descriptors), 'repositories_bytes': repo_bytes, 'assembly_bytes': len(carrier)}
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT assembly FROM application_metadata').fetchone()[0], carrier)
                    self.assertTrue(expected.issubset({row[0] for row in c.execute("SELECT name FROM sqlite_schema WHERE type='table'")}))
                    measurements['ddl'] = c.execute("SELECT name,length(CAST(sql AS BLOB)) FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name").fetchall()
                    measurements['receipt_bytes'] = c.execute('SELECT length(receipt) FROM operation_receipts WHERE commit_id=?', (result.receipt.commit_id,)).fetchone()[0]
                    measurements['evidence_bytes'] = c.execute('SELECT length(manifest) FROM required_audit_events WHERE commit_id=?', (result.receipt.commit_id,)).fetchone()[0]
                    audits = c.execute('SELECT event_slot,length(record) FROM audit_records WHERE commit_id=?', (result.receipt.commit_id,)).fetchall()
                    measurements['audits'] = audits
                    self.assertTrue(all(length <= 8192 for _, length in audits))
                    self.assertLessEqual(measurements['receipt_bytes'], 16384); self.assertLessEqual(measurements['evidence_bytes'], 32768)
                    # An isolated shape fixture changes the actual owner table's
                    # canonical body/projections. It is not a valid business state.
                    maximum = checked(SEQUENCE, {'instance_id': 'instance', 'last_seq': 2**63 - 1, 'revision': 2**63 - 1,
                        'published_generation_id': 'g' * 128, 'published_seq': 2**63 - 1}, 512)
                    c.execute('UPDATE memory_change_sequence SET last_seq=?,revision=?,body=?',
                        (maximum['last_seq'], maximum['revision'], encode_content(maximum, 512).decode())); c.commit()
                if h.retrieval is None: self.fail('Expected real memory owner.')
                self.assertEqual(await h.retrieval.memory.publication_state(), maximum)
                measurements['maximum_bound_root_bytes'] = len(encode_content(maximum, 512))
                largest = max(commands, key=lambda command: len(command.required_audits))
                target = next(field for field in largest.result_schema.fields if field.name == 'targets')
                if type(target.schema) is not SequenceSchema: self.fail('Expected explicit target sequence.')
                malformed = replace(largest, result_schema=RecordSchema(tuple(replace(field, schema=replace(target.schema, minimum=0)) if field.name == 'targets' else field for field in largest.result_schema.fields)))
                with self.assertRaises(InvalidValue):
                    PersistenceService(repositories, tuple(malformed if command is largest else command for command in commands), assembly_format='LOCAL_INFORMATION_V1')
                print('ACTUAL_PUBLICATION_CAPACITY ' + json.dumps(measurements, sort_keys=True))
            finally: self.assertTrue(await h.close())
