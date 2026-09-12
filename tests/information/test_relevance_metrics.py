"""Synthetic annotation fixtures exercise strict bindings and exact denominators.

These labels are test inputs, never additional user judgments. Negative cases
must fail before producing scores; return order must not redefine relevance.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from tests.information.relevance_metrics import (ConfirmedCase, ReviewValidationError, canonical,
    aggregate, evaluate, number, read_json, record, score, sequence, text, validate_annotations,
    validate_corpus_mapping, validate_observations)


def fixture() -> tuple[dict[str, object], dict[str, object], dict[str, object], list[object]]:
    """Create three fully labeled toy queries including semantic and empty sets."""
    identities = {'first': 'person:first', 'second': 'person:second', 'scene': 'world:scene'}
    documents = []
    for alias, body, world in (('rain', '雨', 'REAL'), ('umbrella', '要带雨伞', 'REAL'), ('fiction', '雨', 'FICTIONAL'), ('sad', '他很难过', 'REAL')):
        context = None if world == 'REAL' else identities['scene']
        subjects = [identities['first']] if alias in ('rain', 'fiction') else []
        content = {'body': body, 'subject_ids': subjects, 'world_scope': {'kind': world, 'context_id': context}, 'category': 'FACT', 'occurred_range': None, 'applicable_range': None}
        formal = {'object_id': 'object:' + alias, 'revision': 1, 'lifecycle': 'ACTIVE', 'content': content}
        documents.append({'document_id': alias, 'object_id': 'object:' + alias, 'revision': 1, 'body': body, 'subject_ids': subjects, 'world_kind': world, 'world_context': context, 'formal_object': formal})
    cases: list[dict[str, object]] = []; labels: list[dict[str, object]] = []; observations: list[object] = []
    aliases = ['rain', 'umbrella', 'fiction', 'sad']; docs = {d['document_id']: d for d in documents}
    for key, query, relevant, returned in (('literal-rain', '雨', ['rain', 'umbrella'], ['fiction', 'rain']), ('synonym', '伤心', ['sad'], []), ('empty', '空查询', [], ['rain'])):
        cases.append({'case_id': key, 'query_text': query, 'subject': None, 'world': None, 'include_goals': False, 'eligible_documents': aliases, 'structurally_excluded_documents': []})
        labels.append({'case_id': key, 'relevant_documents': relevant, 'nonrelevant_documents': [a for a in aliases if a not in relevant], 'reviewed': True, 'unlabeled_documents': [], 'notes': 'SYNTHETIC fixture source, not human review.'})
        for phase in ('dirty', 'active', 'reopened'):
            request = {'request_key': phase + ':' + key, 'entry_id': 'entry', 'query_text': query, 'world_scope': None, 'subject_ids': [], 'object_ids': [], 'category': None, 'time_range': None, 'retrieval_mode': 'LOCAL_LEXICAL_V1', 'rerank': False, 'include_state': False, 'include_goals': False, 'require_complete': phase != 'dirty', 'allow_partial': True}
            response = {'request_id': phase + ':' + key, 'availability': 'DEGRADED' if phase == 'dirty' else 'COMPLETE', 'coverage': {'preprocess_id': 'LOCAL_LEXICAL_V1', 'unicode_version': '15.0.0'}, 'sections': {'memories': [docs[a]['formal_object'] for a in returned]}, 'truncation': {'reasons': ['INDEX_BUILDING'] if phase == 'dirty' else [], 'omitted_memories': 0}}
            observations.append({'phase': phase, 'case_id': key, 'request': request, 'response': response, 'returned_documents': returned})
    packet: dict[str, object] = {'cases': cases, 'documents': documents, 'identities': identities, 'original_pairs_sha256': 'synthetic-source'}
    packet['packet_sha256'] = hashlib.sha256(canonical(packet)).hexdigest()
    annotations: dict[str, object] = {'packet_sha256': packet['packet_sha256'], 'review_status': 'LABELS_READY_FOR_USER_SUBMISSION', 'reviewer': 'SYNTHETIC TEST ONLY', 'relevance_metrics': None, 'cases': labels}
    corpus: dict[str, object] = {'documents': deepcopy(documents), 'identities': deepcopy(identities), 'source_pairs_sha256': 'synthetic-source'}
    return packet, annotations, corpus, observations


class RelevanceMetricsTests(unittest.TestCase):
    def test_full_partition_uses_fixed_precision_and_undefined_empty_recall(self):
        label = ConfirmedCase('query', frozenset(('found', 'missing')), frozenset(('wrong',)), 'source')
        actual = score(('wrong', 'found'), label)
        self.assertEqual(actual['precision_at_5'], number(Fraction(1, 5)))
        self.assertEqual(actual['recall_at_8'], number(Fraction(1, 2)))
        self.assertEqual(actual['reciprocal_rank_at_8'], number(Fraction(1, 2)))
        self.assertEqual(actual['missed_relevant_documents'], ['missing'])
        empty = score(('wrong',), ConfirmedCase('empty', frozenset(), frozenset(('wrong',)), 'source'))
        self.assertIsNone(empty['recall_at_8']); self.assertEqual(empty['reciprocal_rank_at_8'], number(Fraction(0)))

    def test_exact_macros_include_empty_cases_without_changing_recall_denominator(self):
        packet, annotations, corpus, observations = fixture()
        confirmed = validate_annotations(packet, annotations, text(packet['packet_sha256']))
        output = evaluate(packet, confirmed, corpus, observations)
        rows = [record(row) for row in sequence(output['aggregates'])]
        complete = next(row for row in rows if row['phase'] == 'active' and row['cohort'] == 'all' and row['subset'] == 'complete_human_partition')
        self.assertEqual((complete['queries'], complete['recall_defined_queries']), (3, 2))
        self.assertEqual(complete['precision_at_5'], number(Fraction(1, 15)))
        self.assertEqual(complete['recall_at_8'], number(Fraction(1, 4)))
        self.assertEqual(complete['mrr_at_8'], number(Fraction(1, 6)))
        self.assertEqual(complete['empty_relevant_zero_return_rate'], number(Fraction(0)))
        self.assertEqual(complete['empty_relevant_false_positive_query_rate'], number(Fraction(1)))
        literal = next(row for row in rows if row['phase'] == 'active' and row['cohort'] == 'all' and row['subset'] == 'literal_matchable')
        self.assertEqual(literal['case_ids'], ['literal-rain'])
        self.assertEqual(literal['recall_at_8'], number(Fraction(1, 2)))

    def test_confirmation_and_material_binding_fail_before_scoring(self):
        for mutation in ('signature', 'status', 'reviewed', 'unlabeled', 'digest', 'material'):
            with self.subTest(mutation=mutation):
                packet, annotations, _, _ = fixture()
                first = record(sequence(annotations['cases'])[0])
                if mutation == 'signature': annotations['reviewer'] = ' '
                elif mutation == 'status': annotations['review_status'] = 'DRAFT'
                elif mutation == 'reviewed': first['reviewed'] = 1
                elif mutation == 'unlabeled': first['unlabeled_documents'] = ['rain']
                elif mutation == 'digest': annotations['packet_sha256'] = 'wrong'
                else: record(sequence(packet['cases'])[0])['query_text'] = 'changed'
                with self.assertRaises(ReviewValidationError): validate_annotations(packet, annotations, text(packet['packet_sha256']))

    def test_duplicate_missing_overlapping_and_out_of_scope_labels_are_rejected(self):
        for mutation in ('duplicate', 'missing-case', 'overlap', 'out-of-scope', 'missing-label'):
            with self.subTest(mutation=mutation):
                packet, annotations, _, _ = fixture(); cases = sequence(annotations['cases']); first = record(cases[0])
                if mutation == 'duplicate': cases.append(deepcopy(cases[0]))
                elif mutation == 'missing-case': cases.pop()
                elif mutation == 'overlap': first['nonrelevant_documents'] = ['rain', 'fiction', 'sad']
                elif mutation == 'out-of-scope': first['relevant_documents'] = ['rain', 'umbrella', 'secret']
                else: first['relevant_documents'] = ['rain']
                with self.assertRaises(ReviewValidationError): validate_annotations(packet, annotations, text(packet['packet_sha256']))

    def test_matching_aliases_do_not_override_body_person_or_world_changes(self):
        for mutation in ('body', 'person', 'world', 'identity', 'missing-alias'):
            with self.subTest(mutation=mutation):
                packet, _, corpus, _ = fixture(); docs = sequence(corpus['documents']); first = record(docs[0]); content = record(record(first['formal_object'])['content'])
                if mutation == 'body': first['body'] = content['body'] = '雪'
                elif mutation == 'person': first['subject_ids'] = content['subject_ids'] = ['person:second']
                elif mutation == 'world': first['world_kind'] = record(content['world_scope'])['kind'] = 'ROLEPLAY'
                elif mutation == 'identity': record(corpus['identities'])['second'] = 'person:first'
                else: docs.pop()
                with self.assertRaises(ReviewValidationError): validate_corpus_mapping(packet, corpus)

    def test_foreign_object_ids_map_by_verified_meaning_but_are_not_accepted_in_responses(self):
        packet, _, corpus, observations = fixture()
        for raw in sequence(corpus['documents']):
            doc = record(raw); doc['object_id'] = 'other:' + text(doc['object_id']); record(doc['formal_object'])['object_id'] = doc['object_id']
        mapping = validate_corpus_mapping(packet, corpus)
        self.assertTrue(all(row['reference_object_id'] != row['platform_object_id'] for row in mapping))
        with self.assertRaises(ReviewValidationError): validate_observations(packet, corpus, observations)

    def test_return_scope_body_and_complete_phase_coverage_are_checked(self):
        for mutation in ('missing', 'duplicate', 'request-world', 'body', 'ranking'):
            with self.subTest(mutation=mutation):
                packet, _, corpus, observations = fixture(); row = record(observations[0])
                if mutation == 'missing': observations.pop()
                elif mutation == 'duplicate': observations.append(deepcopy(row))
                elif mutation == 'request-world': record(row['request'])['world_scope'] = 'REAL'
                elif mutation == 'body':
                    memory = record(sequence(record(record(row['response'])['sections'])['memories'])[0]); record(memory['content'])['body'] = 'tampered'
                else: row['returned_documents'] = ['rain', 'fiction']
                with self.assertRaises(ReviewValidationError): validate_observations(packet, corpus, observations)

    def test_normal_top_k_is_distinct_from_candidate_and_memory_byte_limits(self):
        labels = ConfirmedCase('query', frozenset(('hit',)), frozenset(('miss',)), 'synthetic')
        base = score(('hit', 'miss'), labels)
        rows = [base | {'case_id': 'query', 'candidate_truncated': False,
                        'index_lag_partial': False, 'memory_byte_omitted': 0}]
        complete = aggregate(rows)
        self.assertEqual(complete['candidate_truncation_ratio'], number(Fraction(0)))
        self.assertEqual(complete['memory_byte_omitted_total'], 0)
        limited = aggregate([rows[0] | {'candidate_truncated': True, 'index_lag_partial': True}])
        self.assertEqual(limited['candidate_truncation_ratio'], number(Fraction(1)))
        self.assertEqual(limited['index_lag_partial_queries'], 1)
        clipped = aggregate([rows[0] | {'memory_byte_omitted': 2}])
        self.assertEqual(clipped['candidate_truncation_ratio'], number(Fraction(0)))
        self.assertEqual(clipped['memory_byte_omitted_total'], 2)

    def test_notes_remain_source_records_and_never_override_confirmed_flags(self):
        packet, annotations, _, _ = fixture(); note = '模型预判，未经人工确认；原备注必须保留。'
        record(sequence(annotations['cases'])[0])['notes'] = note
        confirmed = validate_annotations(packet, annotations, text(packet['packet_sha256']))
        self.assertEqual(confirmed['literal-rain'].notes, note)
        self.assertEqual(confirmed['literal-rain'].relevant, frozenset(('rain', 'umbrella')))

    def test_json_duplicates_and_nonfinite_numbers_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'labels.json'
            for value in ('{"reviewed":true,"reviewed":false}', '{"score":NaN}'):
                path.write_text(value)
                with self.assertRaises(ReviewValidationError): read_json(path)
