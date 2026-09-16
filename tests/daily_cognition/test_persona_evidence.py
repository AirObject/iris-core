"""Validate frozen original approval evidence without importing or generating text."""
from hashlib import sha256
from pathlib import Path
import json
import unittest
from companion_memory.self_model.approved_import_evidence import (verify_approved_persona,ApprovedPersonaEvidence,
    evidence_is_native,TEXT_DIGEST,TEXT_UTF8_DIGEST,CANDIDATE_ID)
from companion_memory.persistence.schema import InvalidValue

ROOT=Path(__file__).parent/'fixtures'/'approved_persona'


class PersonaEvidenceTests(unittest.TestCase):
    def test_exact_original_text_has_two_distinct_verified_hashes(self):
        evidence=verify_approved_persona(*[(ROOT/name).read_bytes() for name in ('review.md','publication.json','reconciliation.json')])
        self.assertTrue(evidence_is_native(evidence))
        self.assertEqual(evidence.original_candidate_id,CANDIDATE_ID)
        self.assertEqual(len(evidence.text.encode()),558)
        self.assertEqual(sha256(evidence.text.encode()).hexdigest(),TEXT_UTF8_DIGEST)
        self.assertEqual(sha256(json.dumps(evidence.text,ensure_ascii=False).encode()).hexdigest(),TEXT_DIGEST)
        self.assertEqual(evidence.original_database_id,'deepseek-text-trial-linux')
        self.assertNotEqual(TEXT_DIGEST,TEXT_UTF8_DIGEST)

    def test_bad_digest_wrong_candidate_and_edited_text_are_rejected(self):
        originals=[(ROOT/name).read_bytes() for name in ('review.md','publication.json','reconciliation.json')]
        for index in range(3):
            values=list(originals);values[index]+=b' '
            with self.subTest(index=index),self.assertRaises(InvalidValue):verify_approved_persona(*values)
        for key,value in (('text','arbitrary edited text'),('object_id','wrong-candidate')):
            published=json.loads(originals[1]);published['approved_original']['candidate'][key]=value
            with self.subTest(key=key),self.assertRaises(InvalidValue):
                verify_approved_persona(originals[0],json.dumps(published,ensure_ascii=False).encode(),originals[2])
        self.assertFalse(evidence_is_native(object()))
        self.assertFalse(evidence_is_native(object.__new__(ApprovedPersonaEvidence)))
