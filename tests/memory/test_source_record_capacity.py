"""Longest legal source metadata fits independent headers and complete point leaves."""
import hashlib
import unittest
from types import MappingProxyType
from companion_memory.memory.formats import record
from companion_memory.memory.sources import MANIFEST_SCHEMA, source_digest, isolate_source
from companion_memory.memory.source_records import split_manifest, join_manifest
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import freeze_value, InvalidValue, encode_value


class SourceRecordCapacityTests(unittest.TestCase):
    def test_complete_longest_metadata_reconstructs_without_oversized_header(self):
        largest = 2**63 - 1
        source: dict[str, object] = {key: key[0] * 128 for key in ('source_id', 'batch_id', 'run_id', 'entry_id', 'host_id', 'platform_id', 'config_snapshot_id', 'material_contract_ref')}
        source.update(source_version=1, frozen_at_us=largest, digest='0' * 64,
            domain_revisions=[{'domain_id': str(i) * 128, 'revision': largest} for i in range(4)],
            ordered_members=[{'role': role, 'message_id': 'm' * 127 + str(i), 'entry_seq': largest - 3 + i,
                'payload_digest': 'a' * 64, 'received_at_us': largest, 'transferred_at_us': largest,
                'media': [{'occurrence_id': str(2 * i + j) * 128, 'interpretation_id': 'i' * 127 + str(2 * i + j),
                    'blob_id': 'b' * 127 + str(2 * i + j), 'generation': largest, 'selection_revision': largest} for j in range(2)]}
                for i, role in enumerate(('H', 'T', 'T', 'R'))])
        isolated = record(freeze_value(MANIFEST_SCHEMA, source))
        logical = isolate_source(MappingProxyType({**isolated, 'digest': source_digest(isolated)}))
        body = encode_content(logical, 8192).decode()
        self.assertGreater(len(body.encode()), 4096)
        header, members = split_manifest(body)
        self.assertLessEqual(len(header.encode()), 4096)
        self.assertEqual(len(members), 4)
        self.assertTrue(all(len(member.encode()) <= 2048 for member in members))
        self.assertEqual(join_manifest(header, members), body)
        for value in (header,) + members:
            self.assertLessEqual(len(encode_value(MappingProxyType({'body': value}), 65536)) + 8192, 57344)
        damaged = members[0].replace('a' * 64, 'b' * 64)
        with self.assertRaises(InvalidValue): join_manifest(header, (damaged,) + members[1:])
        with self.assertRaises(InvalidValue): join_manifest(header, members[:-1])
        with self.assertRaises(InvalidValue): join_manifest(header, members + members[:1])
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), hashlib.sha256(join_manifest(header, members).encode()).hexdigest())
