"""Controlled Chat payloads verify strict shape, identity and usage separation.

All responses are local fixtures. No credential, endpoint or network is used;
wire correctness does not imply supplier strict support or a durable receipt.
"""
import hashlib
import json
from types import MappingProxyType
import unittest
from companion_memory.provider.chat_json import decode_wire, encode_wire
from companion_memory.provider.chat_protocol import ChatBinding, decode_response, encode_request
from companion_memory.provider.values import InvalidData, as_record


def binding() -> ChatBinding:
    schema = b'{"type":"object","properties":{},"required":[],"additionalProperties":false}'
    return ChatBinding('ark-code-latest', ('ark-code-latest', 'known_backend'), None,
                       'schema', hashlib.sha256(schema).hexdigest(), 'text_learning', schema)


def response() -> dict:
    return {'id': 'response-id', 'object': 'chat.completion', 'created': 123, 'model': 'ark-code-latest',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"schema_version":1,"memories":[]}'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15,
                  'prompt_tokens_details': {'cached_tokens': 3}, 'completion_tokens_details': {'reasoning_tokens': 2}}}


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


class ChatProtocolTests(unittest.TestCase):
    def test_request_has_only_fixed_chat_fields(self):
        selected = binding()
        payload = {'format_version': 2, 'messages': [{'role': 'SYSTEM', 'text': 'Instructions'}, {'role': 'USER', 'text': '正文'}],
            'schema_ref': selected.schema_ref, 'schema_digest': selected.schema_digest, 'output_tokens': 2048,
            'reservation_input_bound': 128000, 'context_digest': 'a'*64}
        wire = json.loads(encode_request(payload, selected))
        self.assertEqual(set(wire), {'model', 'messages', 'max_tokens', 'n', 'stream', 'response_format'})
        self.assertEqual([m['role'] for m in wire['messages']], ['system', 'user'])
        self.assertTrue(wire['response_format']['json_schema']['strict'])
        self.assertFalse(wire['stream'])
        for name in ('tools', 'store', 'max_output_tokens', 'temperature', 'extra_body'):
            with self.subTest(name=name), self.assertRaises(InvalidData):
                encode_request(dict(payload, **{name: None}), selected)
        for name, bad in (('format_version', True), ('output_tokens', 2049), ('schema_digest', 'b'*64),
                          ('context_digest', 'wrong'), ('reservation_input_bound', True)):
            with self.subTest(name=name), self.assertRaises(InvalidData):
                encode_request(dict(payload, **{name: bad}), selected)

    def test_maximum_message_bytes_and_utf8_reject(self):
        selected = binding()
        payload = {'format_version': 2, 'messages': [{'role': 'SYSTEM', 'text': '\x00'*4096}, {'role': 'USER', 'text': 'x'*32768}],
            'schema_ref': selected.schema_ref, 'schema_digest': selected.schema_digest, 'output_tokens': 2048,
            'reservation_input_bound': 128000, 'context_digest': 'a'*64}
        self.assertLessEqual(len(encode_request(payload, selected)), 131072)
        payload['messages'][1]['text'] += 'x'
        with self.assertRaises(InvalidData):
            encode_request(payload, selected)

    def test_success_is_deeply_immutable_and_model_is_not_guessed(self):
        result = decode_response(encoded(response()), binding())
        self.assertEqual(result.outcome, 'SUCCEEDED')
        self.assertIsNotNone(result.result)
        saved = as_record(result.result)
        self.assertEqual(saved['reported_model_id'], 'ark-code-latest')
        self.assertIsNone(saved['resolved_model_id'])
        self.assertIs(type(as_record(saved['output'])['memories']), tuple)
        self.assertIs(type(saved), MappingProxyType)
        self.assertEqual(result.usage.fields['input_tokens'], 10)
        self.assertEqual(result.usage.fields['cache_read_tokens'], 3)
        self.assertEqual(result.usage.fields['output_tokens'], 5)
        self.assertEqual(result.usage.fields['reasoning_tokens'], 2)

    def test_model_allowlist_does_not_resolve_alias(self):
        sample = response(); sample['model'] = 'known_backend'
        result = decode_response(encoded(sample), binding())
        self.assertEqual(result.outcome, 'SUCCEEDED')
        self.assertIsNone(as_record(result.result)['resolved_model_id'])
        sample['model'] = 'unapproved_backend'
        result = decode_response(encoded(sample), binding())
        self.assertEqual(result.outcome, 'MODEL_BINDING_MISMATCH')
        self.assertEqual(result.usage.fields['input_tokens'], 10)

    def test_refusal_length_tools_and_unknown_finish(self):
        for reason, refusal, expected in (('content_filter', None, 'OTHER_REFUSAL'), ('stop', 'refused', 'OTHER_REFUSAL'),
            ('length', None, 'OUTPUT_LIMIT'), ('tool_calls', None, 'INVALID_RESPONSE'), ('future', None, 'INVALID_RESPONSE')):
            sample = response(); sample['choices'][0]['finish_reason'] = reason
            sample['choices'][0]['message']['refusal'] = refusal
            result = decode_response(encoded(sample), binding())
            self.assertEqual(result.outcome, expected)
            self.assertIsNone(result.result)
            self.assertEqual(result.usage.fields['input_tokens'], 10)

    def test_invalid_shapes_do_not_erase_usage(self):
        for location in ('root', 'choice', 'message', 'multiple', 'index', 'content', 'tool'):
            sample = response()
            if location == 'root': sample['unknown'] = True
            elif location == 'choice': sample['choices'][0]['unknown'] = True
            elif location == 'message': sample['choices'][0]['message']['unknown'] = True
            elif location == 'multiple': sample['choices'].append(sample['choices'][0])
            elif location == 'index': sample['choices'][0]['index'] = True
            elif location == 'content': sample['choices'][0]['message']['content'] = 'not json'
            else: sample['choices'][0]['message']['tool_calls'] = []
            result = decode_response(encoded(sample), binding())
            self.assertEqual(result.outcome, 'INVALID_RESPONSE', location)
            self.assertEqual(result.usage.fields['input_tokens'], 10)

    def test_missing_partial_and_contradictory_usage(self):
        cases = ((None, True, None), ({'prompt_tokens': 10}, True, 10),
            ({'prompt_tokens': True}, False, None), ({'prompt_tokens': -1}, False, None),
            ({'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 99}, False, 10),
            ({'prompt_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 11}}, False, 10),
            ({'completion_tokens': 2, 'completion_tokens_details': {'reasoning_tokens': 3}}, False, None),
            ({'prompt_tokens': 2**63-1, 'completion_tokens': 1}, False, 2**63-1))
        for usage, valid, incoming in cases:
            sample = response(); sample['usage'] = usage
            observation = decode_response(encoded(sample), binding())
            self.assertEqual(observation.outcome, 'SUCCEEDED')
            self.assertEqual(observation.usage.valid, valid)
            self.assertEqual(observation.usage.fields['input_tokens'], incoming)

    def test_unpriced_fields_are_not_assumed_free(self):
        for details in ({'cached_tokens': 3, 'audio_tokens': 1}, {'cached_tokens': 3, 'provisioned_tokens': 1},
                        {'cached_tokens': 3, 'future_usage': 0}):
            sample = response(); sample['usage']['prompt_tokens_details'] = details
            observation = decode_response(encoded(sample), binding())
            self.assertFalse(observation.usage.billing_covered)
            self.assertEqual(observation.usage.fields['input_tokens'], 10)

    def test_duplicate_unicode_nonfinite_and_depth(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":"\\ud800"}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":'+b'['*13+b'0'+b']'*13+b'}', b'\xff', b'{} trailing'):
            with self.subTest(raw=raw), self.assertRaises(InvalidData):
                decode_wire(raw, 262144)
        # A plain wire object is not the internal tagged float representation.
        value = decode_wire(b'{"$float":"0x1.0p+0"}', 100)
        self.assertEqual(value['$float'], '0x1.0p+0')
        self.assertEqual(encode_wire(decode_wire(b'{"x":1.25}', 100), 100), b'{"x":1.25}')

    def test_empty_response_is_not_zero_memory_success(self):
        for raw in (b'', b'{}', b'[]', b'x'*262145):
            self.assertEqual(decode_response(raw, binding()).outcome, 'INVALID_RESPONSE')

    def test_invalid_inner_json_is_not_extracted_or_repaired(self):
        for text in ('prefix {"memories":[]} suffix', '{"x":1,"x":2}', '{"x":NaN}', 'x'*6145):
            sample = response(); sample['choices'][0]['message']['content'] = text
            self.assertEqual(decode_response(encoded(sample), binding()).outcome, 'INVALID_RESPONSE')

    def test_nontext_message_is_invalid_even_with_a_failure_finish(self):
        for reason in ('length', 'content_filter'):
            sample = response(); sample['choices'][0]['finish_reason'] = reason
            sample['choices'][0]['message']['content'] = {'unexpected': 'content'}
            self.assertEqual(decode_response(encoded(sample), binding()).outcome, 'INVALID_RESPONSE')
