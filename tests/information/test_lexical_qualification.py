"""Reachable formal witnesses and Unicode bounds retain distinct qualifications."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from tests.information.lexical_bounds import census, pressure_body, reproduce_pressure_body
from tests.information.lexical_pressure import pressure
from tests.information.lexical_quality import capture_quality
from tests.information.lexical_review import packet, export_review


class LexicalQualificationTests(unittest.IsolatedAsyncioTestCase):
    def test_unicode_census_and_reproducible_lower_bound_do_not_claim_a_maximum(self):
        result = census()
        self.assertEqual(result['unicode_version'], '15.0.0')
        self.assertEqual(result['scalars_checked'], 1112064)
        self.assertEqual(result['canonical_compositions_checked'], 941)
        self.assertEqual(result['hangul_compositions_checked'], 11172)
        self.assertEqual(result['maximum_decomposed_letter_weights_by_utf8_bytes'], [1, 2, 15, 3])
        self.assertEqual(result['raw_distinct_term_upper_bound'], 20463)
        self.assertIs(result['maximum_proved'], False)
        self.assertEqual(reproduce_pressure_body(), pressure_body())

    async def test_reachable_pressure_is_formal_indexed_delivered_and_cold_reopened(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            created = await pressure(root, 'create')
            reopened = await pressure(root, 'reopen')
            self.assertEqual(created['term_count'], 2237)
            self.assertEqual(reopened['term_count'], 2237)
            self.assertEqual(created['formal_object_sha256'], reopened['formal_object_sha256'])

    async def test_legal_expanding_body_remains_readable_but_never_claims_complete_index(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            created = await pressure(root, 'create', expanded=True)
            reopened = await pressure(root, 'reopen', expanded=True)
            self.assertEqual(created['body_bytes'], 2048)
            self.assertIs(created['index_complete'], False)
            self.assertEqual(created['formal_object_sha256'], reopened['formal_object_sha256'])

    async def test_authored_review_packet_retains_actual_rankings_and_pending_human_labels(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            created = await capture_quality(root, 'create')
            reopened = await capture_quality(root, 'reopen')
            self.assertEqual((created['documents'], created['queries']), (68, 73))
            self.assertEqual((created['observations'], reopened['observations']), (146, 73))
            self.assertEqual(reopened['human_review_status'], 'PENDING')
            self.assertIsNone(created['relevance_metrics'])
            self.assertIsNone(reopened['relevance_metrics'])
            review = packet(root)
            self.assertIsNone(review['relevance_metrics'])
            self.assertEqual(review['review_status'], 'PENDING_USER_REVIEW')
            export_review(root, root / 'review')
            self.assertTrue((root / 'review/review.html').exists())
            self.assertNotIn('REVIEW_PACKET_DATA', (root / 'review/review.html').read_text())
