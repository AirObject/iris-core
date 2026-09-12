"""Labeled literal ranking, Unicode boundaries and external-time qualifications.

The fixture is an authored lexical relevance set, not human quality acceptance
or a full SQLite retrieval benchmark. Semantic limitations are asserted apart.
"""
import json
from pathlib import Path
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence.schema import Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.retrieval.lexical import (
    normalize_material, rank_key, has_lexical_match, StructuralFilter, WorldFilter, matches_structure,
)
from companion_memory.state.time_values import parse_reported_time, duration_view


def lexical(text: str):
    return normalize_material(text, byte_limit=8192, term_limit=4096)


class LexicalTests(unittest.TestCase):
    def test_authored_chinese_and_mixed_literal_relevance_set(self):
        cases = cast(list[list[str]], json.loads(Path(__file__).with_name('lexical_cases.json').read_text()))
        self.assertEqual(len(cases), 60)
        material = [(str(i).zfill(3), lexical(text)) for i, (_, text) in enumerate(cases)]
        reciprocal = []
        recalled = 0
        precision = []
        for index, (query, _) in enumerate(cases):
            with self.subTest(query=query):
                terms = lexical(query)
                matches = [(oid, value) for oid, value in material if has_lexical_match(value, terms)]
                ranked = [oid for oid, _ in sorted(matches, key=lambda item: rank_key(item[0], item[1], terms, StructuralFilter()))]
                expected = str(index).zfill(3)
                rank = ranked.index(expected) + 1 if expected in ranked else 0
                reciprocal.append(1 / rank if rank else 0)
                recalled += expected in ranked[:8]
                precision.append(int(expected in ranked[:5]) / 5)
                self.assertIn(expected, ranked[:8])
        self.assertGreaterEqual(recalled / len(cases), 0.9)
        print({'literal_fixture_cases': len(cases), 'recall_at_8': recalled / len(cases),
               'precision_at_5': sum(precision) / len(cases), 'mrr': sum(reciprocal) / len(cases),
               'candidate_truncation': 0, 'scope': 'PURE_LEXICAL_FIXTURE'})

    def test_normalization_boundaries_and_ties_preserve_literal_semantics(self):
        self.assertEqual(lexical('ＡＩ').terms, lexical('ai').terms)
        self.assertEqual(lexical('雨').terms, ('雨',))
        self.assertNotIn('北京', lexical('北 京').terms)
        self.assertNotIn('北京', lexical('北😀京').terms)
        self.assertFalse(has_lexical_match(lexical('难过'), lexical('伤心')))
        self.assertTrue(has_lexical_match(lexical('没有去北京'), lexical('北京')))
        query = lexical('雨')
        self.assertLess(rank_key('a', query, query, StructuralFilter()), rank_key('b', query, query, StructuralFilter()))
        with self.assertRaises(OwnerFailure) as expanded:
            normalize_material('㍿' * 100, byte_limit=100, term_limit=4096)
        self.assertEqual((expanded.exception.code, expanded.exception.field, expanded.exception.reason),
                         ('INVALID_INPUT', 'index', 'LIMIT_EXCEEDED'))
        with self.assertRaises(OwnerFailure) as excessive:
            normalize_material('abcdef', byte_limit=100, term_limit=5)
        self.assertEqual((excessive.exception.code, excessive.exception.field, excessive.exception.reason),
                         ('INVALID_INPUT', 'index', 'LIMIT_EXCEEDED'))
        with self.assertRaises(OwnerFailure):
            lexical('\ud800')

    def test_structure_keeps_subject_world_and_unknown_time_boundaries(self):
        def object_value(subject: str, world: str | None, start: int | None, end: int | None):
            return cast(MappingProxyType[str, Value], MappingProxyType({'object_id': 'memory', 'kind': 'MEMORY',
                'content': MappingProxyType({'body': '同名人物曾说去北京', 'subject_ids': (subject,), 'category': 'EVENT',
                    'world_scope': MappingProxyType({'kind': 'REAL' if world is None else 'ROLEPLAY', 'context_id': world}),
                    'occurred_range': MappingProxyType({'start_us': start, 'end_us': end}), 'applicable_range': None})}))
        selected = StructuralFilter(subject_ids=('platform_person',), world_scope=WorldFilter('REAL', None), start_us=10, end_us=20)
        self.assertTrue(matches_structure(object_value('platform_person', None, 12, 14), selected))
        self.assertFalse(matches_structure(object_value('other_platform_person', None, 12, 14), selected))
        self.assertFalse(matches_structure(object_value('platform_person', 'fiction', 12, 14), selected))
        self.assertFalse(matches_structure(object_value('platform_person', 'REAL', 12, 14), selected))
        self.assertFalse(matches_structure(object_value('platform_person', None, None, None), selected))
        self.assertFalse(matches_structure(object_value('platform_person', None, 1, 9), selected))
        self.assertFalse(matches_structure(object_value('platform_person', None, 12, 14), StructuralFilter(object_ids=('other',))))


class ExternalTimeTests(unittest.TestCase):
    def test_offsets_precision_and_invalid_time_are_distinct(self):
        utc = parse_reported_time('2026-09-12T00:00:00.000001Z')
        shifted = parse_reported_time('2026-09-12T08:00:00.000001+08:00')
        self.assertEqual(utc.utc_us, shifted.utc_us)
        self.assertEqual((utc.offset_minutes, shifted.offset_minutes), (0, 480))
        self.assertEqual(parse_reported_time('1969-12-31T23:59:59.999999Z').utc_us, -1)
        for invalid in ('2026-09-12T00:00:00', '2026-09-12T00:00:60Z', '2026-09-12T00:00:00.0000001Z',
                        '2026-09-12T00:00:00+24:00', '2026-09-12T00:00:00+00:60', '2026-02-30T00:00:00Z', True, 123):
            with self.subTest(invalid=invalid), self.assertRaises(OwnerFailure):
                parse_reported_time(invalid)

    def test_elapsed_basis_future_and_end_do_not_rewrite_stored_start(self):
        self.assertEqual(duration_view(100, 20, 30).elapsed_us, 80)
        self.assertEqual(duration_view(100, None, 30).basis, 'FIRST_REPORT')
        self.assertEqual(duration_view(100, None, None).basis, 'UNKNOWN')
        self.assertIsNone(duration_view(100, None, None).elapsed_us)
        self.assertEqual(duration_view(100, 120, 30).elapsed_us, 0)
        self.assertEqual(duration_view(100, 120, 30).clock, 'CLOCK_AHEAD')
        self.assertEqual(duration_view(100, 20, 30, 40).elapsed_us, 20)
        self.assertEqual(duration_view(200, 20, 30, 40).elapsed_us, 20)
