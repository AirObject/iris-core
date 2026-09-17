"""Pure protocol fixtures are simulated responses, with no Provider registration."""
from hashlib import sha256
import json
import unittest
from typing import cast

from companion_memory.cognition.dream_resources import output_schema, prompt_resource
from companion_memory.provider.dream_protocol import DreamChatBinding, encode_dream_request, decode_dream_response, wire_capacity
from companion_memory.provider.values import InvalidData
from tests.daily_cognition.test_protocol import binding as daily_binding, response


def binding(role: str) -> DreamChatBinding:
    schema = output_schema(role)
    prompt = prompt_resource(role)
    return DreamChatBinding(role, 'deepseek-flash', role.lower() + '_schema', sha256(schema).hexdigest(),
        schema, sha256(prompt).hexdigest(), prompt)


class DreamProtocolResourcesTests(unittest.TestCase):
    def test_resources_exclude_batch_authority_and_bind_three_independent_roles(self):
        resources = []
        for role in ('DREAM_REVIEW', 'PERSONA_DREAM', 'PERSONA_REVIEW'):
            value = binding(role)
            resources.append((value.prompt_digest, value.schema_digest))
            encoded = encode_dream_request(value, '{"evidence":"合成数据"}')
            wire = json.loads(encoded)
            self.assertEqual(wire['max_tokens'], 4096)
            self.assertEqual(wire['messages'][1]['content'], '{"evidence":"合成数据"}')
            self.assertFalse(wire['stream'])
            maximum = wire_capacity(role)
            self.assertLessEqual(maximum['normal_wire_bytes'], 1048576)
            self.assertLessEqual(maximum['input_byte_bound'], 319488)
            print({'scope': 'PURE_WIRE_CODEC', 'role': role, **maximum})
        self.assertEqual(len(set(resources)), 3)
        schema = output_schema('DREAM_REVIEW').decode()
        self.assertNotIn('REGISTER_SUBJECT', schema)
        self.assertNotIn('target_anchors', schema)
        self.assertNotIn('auxiliary_refs', schema)
        self.assertNotIn('DELETE_OBJECT', schema)
        self.assertIn('basis_refs', schema)

    def test_native_formats_do_not_accept_each_others_binding(self):
        with self.assertRaises(InvalidData):
            encode_dream_request(cast(DreamChatBinding, cast(object, daily_binding('PERSONA'))), '{}')
        # Supply invalid bytes through the actual native dream binding. The
        # role-specific resource check must reject even a matching wrong digest.
        original = binding('PERSONA_REVIEW')
        with self.assertRaises(InvalidData):
            DreamChatBinding(original.role, original.requested_model, original.schema_ref,
                original.schema_digest, original.schema_bytes, sha256(b'approve').hexdigest(), b'approve')

    def test_supplier_errors_preserve_usage_and_do_not_repair_framing(self):
        resource = binding('PERSONA_REVIEW')
        valid = response({'schema_version': 1, 'decision': 'APPROVE', 'reason': '模拟完整检查'})
        observed = decode_dream_response(valid, resource)
        self.assertEqual(observed.outcome, 'SUCCEEDED')
        self.assertIsNotNone(observed.result)
        if observed.result is None:
            raise AssertionError(observed)
        self.assertEqual(observed.result['format_version'], 6)
        for finish in ('length', 'tool_calls', 'aborted'):
            failed = decode_dream_response(response({'schema_version': 1}, finish=finish), resource)
            self.assertNotEqual(failed.outcome, 'SUCCEEDED')
            self.assertIsNone(failed.result)
        self.assertEqual(decode_dream_response(response({}, model='other'), resource).outcome, 'MODEL_BINDING_MISMATCH')
        self.assertFalse(decode_dream_response(response({}, usage=False), resource).usage.billing_covered)
        extra = json.loads(valid)
        extra['choices'][0]['message']['content'] += '\n{}'
        self.assertEqual(decode_dream_response(json.dumps(extra).encode(), resource).outcome, 'INVALID_RESPONSE')
        with self.assertRaises(InvalidData):
            encode_dream_request(resource, '\0' * 262144)
