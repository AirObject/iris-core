"""Intrinsic proposal boundaries, distinct from source and owner authorization."""
import copy
import unittest
from companion_memory.cognition.text_output import isolate_text_output, isolate_initial_persona
from companion_memory.persistence.schema import InvalidValue
from companion_memory.memory.formats import record, sequence


def proposal() -> dict:
    return {'action': 'CREATE_MEMORY', 'category': 'FACT', 'stance': 'UNCERTAIN', 'body': 'A claim',
        'subject_ids': ['person'], 'speaker_subject_id': 'person', 'world_scope': {'kind': 'REAL', 'context_id': None},
        'occurred_range': None, 'applicable_range': None, 'belief': 50, 'belief_reason': 'Unconfirmed statement',
        'target_anchors': [{'message_id': 'message', 'part': 'BODY', 'item_index': None, 'start_utf8': 0, 'end_utf8': 7}],
        'auxiliary_refs': [], 'basis_refs': []}


class TextOutputTests(unittest.TestCase):
    def test_zero_one_eight_and_nine(self):
        for count in (0, 1, 8):
            result = isolate_text_output({'schema_version': 1, 'memories': [proposal() for _ in range(count)]})
            self.assertEqual(len(sequence(result['memories'])), count)
        with self.assertRaises(InvalidValue):
            isolate_text_output({'schema_version': 1, 'memories': [proposal() for _ in range(9)]})

    def test_invalid_action_subject_world_scores_and_missing_anchor(self):
        for key, bad in (('action', 'DELETE_OBJECT'), ('body', ''), ('body', '界'*342),
                         ('belief', True), ('belief', 101), ('target_anchors', []),
                         ('speaker_subject_id', 'outsider'), ('subject_ids', ['person', 'person']),
                         ('world_scope', {'kind': 'FICTIONAL', 'context_id': None})):
            value = proposal(); value[key] = bad
            with self.subTest(key=key), self.assertRaises(InvalidValue):
                isolate_text_output({'schema_version': 1, 'memories': [value]})

    def test_anchor_null_rules_and_media_are_closed(self):
        for patch in ({'part': 'MEDIA'}, {'part': 'QUOTATION'}, {'start_utf8': None},
                      {'end_utf8': 0}, {'end_utf8': 2049}, {'item_index': 0}):
            value = proposal(); value['target_anchors'][0].update(patch)
            with self.assertRaises(InvalidValue):
                isolate_text_output({'schema_version': 1, 'memories': [value]})

    def test_output_is_owned_and_extra_fields_reject(self):
        value = proposal(); original = copy.deepcopy(value)
        result = isolate_text_output({'schema_version': 1, 'memories': [value]})
        value['body'] = 'Changed'
        self.assertEqual(record(sequence(result['memories'])[0])['body'], original['body'])
        value['target_id'] = 'database-write'
        with self.assertRaises(InvalidValue):
            isolate_text_output({'schema_version': 1, 'memories': [value]})

    def test_initial_persona_requires_exact_input_and_nonempty_text(self):
        value = {'schema_version': 1, 'text': 'No preset identity.', 'initial_input_ids': ['initial-input']}
        self.assertEqual(isolate_initial_persona(value, 'initial-input')['text'], value['text'])
        for changed in (dict(value, text=' '), dict(value, text='x'*1025),
                        dict(value, initial_input_ids=['different']), dict(value, initial_input_ids=[])):
            with self.assertRaises(InvalidValue):
                isolate_initial_persona(changed, 'initial-input')
