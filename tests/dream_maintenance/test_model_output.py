"""Reject invalid internal model suggestions without manufacturing target events."""
import json
import unittest
from types import MappingProxyType
from companion_memory.cognition.dream_output import decode_dream_output
from companion_memory.self_model.periodic_output import decode_persona_candidate,decode_persona_review
from companion_memory.persistence.schema import InvalidValue


def encoded(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()


class DreamOutputTests(unittest.TestCase):
    def test_scores_use_formal_basis_without_a_fake_target(self):
        action={'action':'SET_SCORES','local_ref':0,'basis_refs':[{'object_id':'basis','expected_revision':1,'kind':'SUPPORTS'}],
            'object_id':'memory','expected_revision':1,'belief':50,'belief_reason':'Evidence is uncertain',
            'retention_delta':-1,'retention_reason':'One support is unavailable'}
        value={'schema_version':1,'decision':'CHANGE','reason':'Review actual support','actions':[action]}
        result=decode_dream_output(encoded(value))
        self.assertEqual(result['decision'],'CHANGE')
        for key,extra in (('target_anchors',[]),('auxiliary_refs',[]),('source_id','invented')):
            with self.assertRaises(InvalidValue):decode_dream_output(encoded({**value,'actions':[{**action,key:extra}]}))
        with self.assertRaises(InvalidValue):decode_dream_output(encoded({**value,'actions':[{**action,'action':'DELETE_OBJECT'}]}))
        with self.assertRaises(InvalidValue):decode_dream_output(encoded({**value,'actions':[{**action,'retention_delta':-11}]}))

    def test_review_decision_and_actions_must_agree(self):
        valid={'schema_version':1,'decision':'KEEP','reason':'No change needed','actions':[]}
        self.assertEqual(decode_dream_output(encoded(valid))['actions'],())
        for raw in (encoded({**valid,'decision':'CHANGE'}),encoded(valid)+b'{}',b'{"schema_version":1,"schema_version":1,"decision":"KEEP","reason":"ok","actions":[]}'):
            with self.assertRaises(InvalidValue):decode_dream_output(raw)

    def test_persona_references_are_exact_and_review_is_independent(self):
        supplied=(MappingProxyType({'object_id':'self-fact','revision':2}),)
        value={'schema_version':1,'text':'A stable synthetic persona.','basis_refs':[dict(supplied[0])],'change_reason':'Current evidence'}
        self.assertEqual(decode_persona_candidate(encoded(value),supplied)['text'],value['text'])
        for refs in ([{'object_id':'self-fact','revision':1}],[{'object_id':'prior-persona','revision':2}],[dict(supplied[0])]*2):
            with self.assertRaises(InvalidValue):decode_persona_candidate(encoded({**value,'basis_refs':refs}),supplied)
        for decision in ('APPROVE','REJECT','UNCHANGED'):
            result=decode_persona_review(encoded({'schema_version':1,'decision':decision,'reason':'Independent check'}))
            self.assertEqual(result['decision'],decision)
        for raw in (b'{"schema_version":1,"decision":"APPROVE","decision":"REJECT","reason":"x"}',
                encoded({'schema_version':True,'decision':'APPROVE','reason':'x'})):
            with self.assertRaises(InvalidValue):decode_persona_review(raw)

    def test_complete_candidate_accepts_maximum_utf8_text_and_sixteen_references(self):
        supplied=tuple(MappingProxyType({'object_id':'fact-'+str(i),'revision':(1<<63)-1}) for i in range(16))
        value={'schema_version':1,'text':'界'*2048,'basis_refs':[dict(ref) for ref in supplied],'change_reason':'x'*512}
        text=decode_persona_candidate(encoded(value),supplied)['text']
        if type(text) is not str:raise AssertionError('Candidate text must be a string')
        self.assertEqual(len(text.encode()),6144)
        with self.assertRaises(InvalidValue):decode_persona_candidate(encoded({**value,'text':value['text']+'x'}),supplied)
