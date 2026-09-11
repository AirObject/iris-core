"""Actual transport encoding bounds supplement public maximum-object transactions."""
import unittest
from types import MappingProxyType
from companion_memory.media.service import MediaService
from companion_memory.memory.release_plans import RELEASE_LEAF, PLAN
from companion_memory.persistence import ResultBoundCommand, OperationIdentity
from companion_memory.persistence._codec import prepare_command, command_descriptor
from companion_memory.persistence.schema import freeze_value, encode_value, InvalidValue
from companion_memory.persistence.content_codec import encode_content
from companion_memory.runtime.content_assembly import ContentAssembly


class RecordBudgetTests(unittest.TestCase):
    def test_full_candidate_transport_handles_worst_text_escape_expansion(self):
        media = MediaService(); assembly = ContentAssembly(media, media.repositories)
        definition = next(d for d in assembly.commands if d.operation_kind == 'store_content_candidate_with_media')
        values = {'operation_id': 'o' * 128, 'batch_id': 'b' * 128, 'expected_revision': 2**63 - 1,
            'generation': 2**63 - 1, 'manifest': '\x00' * 4096, 'leaves': ['\x00' * 8192 for _ in range(8)]}
        intentions = {audit.event_slot: {'actor': 'a' * 128} for audit in definition.required_audits}
        identity = OperationIdentity('d' * 128, 'runtime', definition.operation_kind, 's' * 128, 'k' * 128)
        # The transport accepts opaque bounded leaf text. Its semantic candidate
        # decoder remains responsible for JSON, original identities and owners;
        # this deliberately conservative envelope is not a business proposal.
        _, owned, intents = prepare_command(definition, identity, ResultBoundCommand(1, values, intentions), 1048576)
        encoded = encode_value(MappingProxyType({'definition': command_descriptor(definition), 'values': owned, 'intentions': intents}), 1048576)
        self.assertLessEqual(len(encoded), 483328)
        for too_large in ({**values, 'manifest': '\x00' * 4097}, {**values, 'leaves': ['x' * 8193]}, {**values, 'leaves': ['x'] * 9}):
            with self.assertRaises(InvalidValue): prepare_command(definition, identity, ResultBoundCommand(1, too_large, intentions), 1048576)
        print('CANDIDATE_TRANSPORT_BOUND', len(encoded))

    def test_longest_release_fields_and_point_envelopes_fit_fixed_leaf_budget(self):
        largest = 2**63 - 1
        leaf = freeze_value(RELEASE_LEAF, {'source_id': 's' * 128, 'references_revision': largest, 'holder_count': largest,
            'resulting_count': 0, 'has_media': True,
            'payloads': [{'message_id': str(i) * 128, 'references_revision': largest, 'holder_count': largest, 'payload_deleted': True} for i in range(4)],
            'media_effects': {'blobs': [{'blob_id': str(i) * 128, 'generation': largest, 'references_revision': largest, 'reference_count': largest} for i in range(8)],
                'blob_references': [str(i % 10) * 127 + str(i // 10) for i in range(16)],
                'interpretation_references': [str(i) * 128 for i in range(8)]}})
        encoded = encode_content(leaf, 8192)
        self.assertLessEqual(len(encoded), 8192)
        plan = freeze_value(PLAN, {'plan_version': 1, 'plan_id': 'p' * 128, 'root_id': 'r' * 128, 'ordinal': largest,
            'semantic_digest': 'a' * 64, 'mask': 'INGRESS_MEDIA', 'command_kind': 'c' * 128, 'execution_key': 'e' * 128,
            'previous_plan': 'v' * 128, 'leaf_digests': ['a' * 64 for _ in range(16)]})
        header = encode_content(plan, 4096)
        self.assertLessEqual(len(header) + 16 * len(encoded), 135168)
        point = MappingProxyType({'plan_id': 'p' * 128, 'ordinal': largest, 'body': encoded.decode()})
        self.assertLessEqual(len(encode_value(point, 65536)) + 8192, 57344)
        # Every admitted 8192-byte leaf also fits the actual ASCII point codec,
        # including its worst possible control-character expansion and metadata.
        worst = MappingProxyType({**point, 'body': '\x00' * 8192})
        self.assertLessEqual(len(encode_value(worst, 65536)), 57344)
        print('RELEASE_RECORD_BOUND', len(header), len(encoded), len(header) + 16 * len(encoded))
