"""Hand-calculated scoring and failure cases independent of model quality."""
import copy
import math
import unittest
from tests.semantic.evaluation import QueryGold, evaluate


def gold():
    return tuple(QueryGold(str(q), {str(i): (3 if i == 0 else 1 if i == 1 else 0) if q < 4 else 0
                                   for i in range(12)}) for q in range(6))


def responses():
    return {str(q): {'response_version': 2, 'availability': 'COMPLETE', 'actual_mode': 'HYBRID',
                    'recall_id': 'ticket' if q < 4 else None, 'decision': 'MATCH' if q < 4 else 'NO_MATCH',
                    'sections': {'memories': [{'object_id': str(i)} for i in (0, 1)] if q < 4 else []},
                    'truncation': {'reasons': []}, 'query_vector': {'state': 'CACHE_HIT'},
                    'coverage': {'lexical': {'generation': 'lex', 'pending_count': 0, 'captured_seq': 12, 'contiguous_seq': 12},
                                 'semantic': {'state': 'COMPLETE', 'pending_count': 0, 'captured_seq': 12,
                                              'published_seq': 12, 'material_seq': 12}}} for q in range(6)}


class EvaluationTests(unittest.TestCase):
    def test_perfect_and_reversed_order_match_independent_dcg_arithmetic(self):
        raw = responses(); perfect = evaluate(gold(), raw, 'HYBRID')
        self.assertTrue(perfect['qualification_passed']); self.assertEqual(perfect['macro_recall_at_8'], 1)
        self.assertEqual(perfect['macro_ndcg_at_8'], 1)
        for q in range(4): raw[str(q)]['sections']['memories'].reverse()
        reverse = evaluate(gold(), raw, 'HYBRID')
        expected = (1 + 7 / math.log2(3)) / (7 + 1 / math.log2(3))
        observed = reverse['macro_ndcg_at_8']; assert type(observed) is float
        self.assertAlmostEqual(observed, expected)
        self.assertFalse(reverse['relevance_passed'])

    def test_missing_query_stays_in_macro_denominator(self):
        raw = responses(); del raw['0']
        result = evaluate(gold(), raw, 'HYBRID')
        self.assertEqual(result['macro_recall_at_8'], .75); self.assertEqual(result['macro_ndcg_at_8'], .75)
        self.assertEqual(result['nonempty_macro_denominator'], 4); self.assertFalse(result['qualification_passed'])

    def test_incomplete_or_false_positive_empty_query_never_passes(self):
        for change in ('missing', 'partial', 'false_positive'):
            raw = responses()
            if change == 'missing': del raw['4']
            elif change == 'partial': raw['4']['coverage']['semantic']['state'] = 'PARTIAL'
            else: raw['4']['sections']['memories'] = [{'object_id': '2'}]
            result = evaluate(gold(), raw, 'HYBRID')
            self.assertFalse(result['empty_rejection_passed']); self.assertFalse(result['qualification_passed'])

    def test_duplicates_unknown_ids_and_oversize_ranking_are_rejected(self):
        for ids in (['0', '0'], ['not-in-fixed-set'], [str(i) for i in range(9)]):
            raw = responses(); raw['0']['sections']['memories'] = [{'object_id': i} for i in ids]
            with self.assertRaises(ValueError): evaluate(gold(), raw, 'HYBRID')
        invalid = list(gold()); invalid[0] = QueryGold('0', {str(i): True for i in range(12)})
        with self.assertRaises(ValueError): evaluate(invalid, responses(), 'HYBRID')

    def test_missing_watermarks_do_not_prove_completeness(self):
        raw = responses()
        for key in ('captured_seq', 'published_seq', 'material_seq'):
            raw['4']['coverage']['semantic'][key] = None
        self.assertFalse(evaluate(gold(), raw, 'HYBRID')['empty_rejection_passed'])

    def test_lexical_degradation_has_separate_rejection_qualification(self):
        raw = responses()
        for q, row in raw.items():
            row.update(availability='DEGRADED', actual_mode='LEXICAL_ONLY')
            row['truncation']['reasons'] = ['QUERY_VECTOR_MISSING', 'INDEX_NOT_READY']
            if q in ('4', '5'): row['decision'] = 'INCOMPLETE_EMPTY'
        self.assertTrue(evaluate(gold(), raw, 'LEXICAL_ONLY')['empty_rejection_passed'])
        self.assertFalse(evaluate(gold(), raw, 'HYBRID')['empty_rejection_passed'])
        deadline = copy.deepcopy(raw); deadline['4']['truncation']['reasons'].append('DEADLINE')
        self.assertFalse(evaluate(gold(), deadline, 'LEXICAL_ONLY')['empty_rejection_passed'])
