"""Keep full persona projections and recovery leaves lossless at byte boundaries."""
import unittest
from types import MappingProxyType
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge
from companion_memory.persistence.content_codec import encode_content
from companion_memory.self_model.periodic_projection import encode_projection,project_current
from companion_memory.self_model.periodic_leaves import split_candidate,join_candidate


class PersonaCapacityTests(unittest.TestCase):
    def current(self,text):
        return MappingProxyType({'publication_id':'publication:'+'a'*116,'revision':(1<<63)-1,
            'text':text,'generated_at_us':(1<<63)-1,'review':'MODEL_REVIEWED','model_origin':'REMOTE_PROVIDER',
            'publication_origin':'PERIODIC_REVIEWED','stale':False})

    def test_full_new_projection_preserves_text_and_review_provenance(self):
        text='界'*2048
        projection=project_current(self.current(text))
        self.assertEqual(projection['text'],text)
        self.assertEqual(projection['review_status'],'MODEL_REVIEWED')
        self.assertLessEqual(len(encode_projection(projection)),8192)
        invalid={**projection,'review_status':'APPROVED'}
        with self.assertRaises(InvalidValue):encode_projection(invalid)

    def test_projection_rejects_encoded_overflow_without_truncation(self):
        with self.assertRaises((InvalidValue,ValueTooLarge)):
            project_current(self.current('x'+'\x00'*6143))

    def test_utf8_boundary_and_control_escaping_round_trip(self):
        for raw in (b'x'*16384,('界'*5461+'x').encode(),b'\x00'*16384):
            root,leaves=split_candidate('candidate:'+'c'*118,raw)
            self.assertEqual(join_candidate(root,leaves),raw)
            self.assertLessEqual(len(leaves),17)
            self.assertTrue(all(len(encode_content(leaf,8192))<=8192 for leaf in leaves))
            with self.assertRaises(InvalidValue):join_candidate(root,leaves[:-1])
            with self.assertRaises(InvalidValue):join_candidate(root,tuple(reversed(leaves)))
            replaced=MappingProxyType({**leaves[0],'candidate_id':'different'})
            with self.assertRaises(InvalidValue):join_candidate(root,(replaced,*leaves[1:]))
