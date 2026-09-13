"""Actual encoded related-object bounds and trusted roster uniqueness.

These are retained-material checks, not a claim of authority to read memory or
of a successful Provider call. Complete original object fields are preserved.
"""
import unittest
from companion_memory.cognition.text_context import freeze_context
from companion_memory.cognition.text_resources import render_learning_instructions
from companion_memory.memory.formats import isolate_object
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.text_records import digest
from companion_memory.persistence.schema import InvalidValue
from tests.cognition.test_text_context import inputs


def object_value():
    return {'object_version':2,'object_id':'related','instance_id':'instance','kind':'MEMORY','revision':1,'created_at_us':1,'modified_at_us':1,
        'lifecycle':'ACTIVE','forgotten_since_us':None,'retention_policy_ref':'retention',
        'content':{'category':'FACT','body':'x','subject_ids':['s'+str(i)+'x'*100 for i in range(4)],'speaker_subject_id':None,'stance':'ASSERTED',
            'world_scope':{'kind':'REAL','context_id':None},'occurred_range':None,'applicable_range':None},
        'scores':{'belief':50,'retention':50,'scale_id':'acceptance_100_v1','belief_reason':'b'*512,'retention_reason':'r'*512,'score_basis':[]},
        'origin':{'kind':'DIRECT_LEARNING','candidate_id':'candidate','batch_id':'batch','actor_ref':'actor','model_origin':'REMOTE_PROVIDER','candidate_origin':'MODEL_VALIDATED'}}


class ContextBoundaryTests(unittest.TestCase):
    def test_complete_related_object_exact_4096_and_plus_one(self):
        value=object_value();base=isolate_object(value,text_format=True)
        size=len(encode_content(base,8192));value['content']['body']='x'*(4096-size+1)
        exact=isolate_object(value,text_format=True);self.assertEqual(len(encode_content(exact,8192)),4096)
        args=inputs();args[0]['user']['related']=[value];args[2].append({'object_id':'related','revision':1,'grant_ref':'explicit-grant','snapshot_digest':digest(exact)})
        self.assertIsNotNone(freeze_context(*args))
        value['content']['body']+='x';large=isolate_object(value,8192,text_format=True);self.assertEqual(len(encode_content(large,8192)),4097)
        args[2][0]['snapshot_digest']=digest(large)
        with self.assertRaises(InvalidValue):freeze_context(*args)

    def test_world_context_and_duplicate_owner_identities_reject(self):
        real={'kind':'REAL','context_id':None};subject={'subject_id':'speaker','revision':1,'kind':'PLATFORM_PERSON'}
        for subjects,worlds in (([subject,subject],[real]),([subject],[real,real]),([subject],[{'kind':'FICTIONAL','context_id':'unknown'}])):
            with self.subTest(subjects=subjects,worlds=worlds),self.assertRaises(InvalidValue):render_learning_instructions(subjects,worlds)
        text=render_learning_instructions([{'subject_id':'fiction','revision':1,'kind':'CONTEXT'}],[{'kind':'FICTIONAL','context_id':'fiction'}])
        self.assertIn('fiction',text)
