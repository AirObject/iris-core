"""Verify actual strict resources and their bounds without a supplier request."""
import json
import unittest
from companion_memory.cognition.text_resources import (
    LEARNING_INSTRUCTIONS, PERSONA_INSTRUCTIONS, output_schema, resource_digest,
)
from companion_memory.provider.chat_protocol import ChatBinding, encode_request


class TextResourceTests(unittest.TestCase):
    def test_all_objects_are_closed_with_every_field_required(self):
        def visit(value):
            if type(value) is dict:
                if value.get('type') == 'object':
                    self.assertIs(value['additionalProperties'], False)
                    self.assertEqual(set(value['properties']), set(value['required']))
                for child in value.values(): visit(child)
            elif type(value) is list:
                for child in value: visit(child)
        for role in ('LEARNING', 'PERSONA'):
            raw = output_schema(role)
            visit(json.loads(raw))
            self.assertLessEqual(len(raw), 12288)
            self.assertEqual(raw, output_schema(role))
        self.assertNotEqual(output_schema('LEARNING'), output_schema('PERSONA'))

    def test_actual_schemas_and_maximum_messages_encode_as_chat(self):
        for role, name in (('LEARNING', 'text_learning'), ('PERSONA', 'initial_persona')):
            schema = output_schema(role)
            selected = ChatBinding('ark-code-latest', ('ark-code-latest',), None,
                name, resource_digest(schema), name, schema)
            payload = {'format_version': 2, 'messages': [{'role': 'SYSTEM', 'text': '\x00'*4096},
                {'role': 'USER', 'text': 'x'*32768}], 'schema_ref': name, 'schema_digest': resource_digest(schema),
                'output_tokens': 2048, 'reservation_input_bound': 128000, 'context_digest': 'a'*64}
            self.assertLessEqual(len(encode_request(payload, selected)), 131072)
        for text in (LEARNING_INSTRUCTIONS, PERSONA_INSTRUCTIONS):
            self.assertLessEqual(len(text.encode('utf-8')), 4096)

    def test_unsupported_role_does_not_select_a_default(self):
        with self.assertRaises(ValueError):
            output_schema('MEDIA')
