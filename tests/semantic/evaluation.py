"""Score frozen reviewed queries without dropping failures or changing labels.

Inputs are complete public response projections keyed by the reviewed query ID.
Missing responses contribute zero to the fixed nonempty-gold denominator and
cannot pass a completeness or empty-result qualification. This module never
constructs a host, grants approval, or sends a model request.
"""
from dataclasses import dataclass
import math
from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class QueryGold:
    """An already reviewed query and its complete object-to-grade mapping."""
    query_id: str
    grades: Mapping[str, int]


def _record(value: object) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError('A JSON object with string keys is required.')
    return value


def _memories(response: dict[str, object]) -> tuple[str, ...]:
    memories = _record(response['sections'])['memories']
    if type(memories) is not list or len(memories) > 8:
        raise ValueError('The complete delivered ranking must contain at most eight objects.')
    ids = []
    for memory in memories:
        object_id = _record(memory)['object_id']
        if type(object_id) is not str:
            raise ValueError('A delivered object identity is required.')
        ids.append(object_id)
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate delivered objects cannot count as separate hits.')
    return tuple(ids)


def _complete(response: dict[str, object], mode: str) -> bool:
    if mode == 'LEXICAL_BASELINE':
        return response.get('response_version') == 1 and response.get('availability') == 'COMPLETE'
    if response.get('response_version') != 2:
        return False
    coverage = _record(response['coverage'])
    lexical = _record(coverage['lexical'])
    lexical_ready = (type(lexical.get('generation')) is str and bool(lexical.get('generation'))
                     and type(lexical.get('captured_seq')) is int and type(lexical.get('contiguous_seq')) is int
                     and lexical.get('pending_count') == 0
                     and lexical.get('captured_seq') == lexical.get('contiguous_seq'))
    reasons = _record(response['truncation'])['reasons']
    if type(reasons) is not list:
        raise ValueError('Complete truncation reasons are required.')
    if mode == 'LEXICAL_ONLY':
        return (lexical_ready and response.get('actual_mode') == 'LEXICAL_ONLY'
                and response.get('availability') == 'DEGRADED'
                and bool(reasons) and set(reasons) <= {'QUERY_VECTOR_MISSING', 'INDEX_NOT_READY'})
    semantic = _record(coverage['semantic'])
    vector = _record(response['query_vector'])
    return (lexical_ready and response.get('availability') == 'COMPLETE'
            and response.get('actual_mode') == 'HYBRID' and not reasons
            and semantic.get('state') == 'COMPLETE' and semantic.get('pending_count') == 0
            and all(type(semantic.get(key)) is int for key in ('captured_seq', 'published_seq', 'material_seq'))
            and semantic.get('captured_seq') == semantic.get('published_seq') == semantic.get('material_seq')
            and vector.get('state') in ('CACHE_HIT', 'REUSED_ARTIFACT', 'REMOTE_RESULT'))


def evaluate(gold: Sequence[QueryGold], responses: Mapping[str, object], mode: str) -> dict[str, object]:
    """Return per-query scores and fixed-denominator macro scores at eight.

LEXICAL_ONLY assesses controlled degradation's independent rejection rule; it
does not turn an INCOMPLETE_EMPTY response into a complete hybrid NO_MATCH.
The caller supplies original response bytes separately for provenance. This
calculation does not certify that any response came from a real supplier.
"""
    if mode not in ('LEXICAL_BASELINE', 'HYBRID', 'LEXICAL_ONLY'):
        raise ValueError('An explicit evaluation mode is required.')
    if len(gold) != 6 or len({q.query_id for q in gold}) != 6:
        raise ValueError('All six distinct reviewed queries are required.')
    if set(responses) - {q.query_id for q in gold}:
        raise ValueError('Unreviewed queries cannot enter the evaluation.')
    if any(len(q.grades) != 12 or any(type(v) is not int or not 0 <= v <= 3 for v in q.grades.values()) for q in gold):
        raise ValueError('All twelve exact graded object identities are required for every query.')
    if len({tuple(sorted(q.grades)) for q in gold}) != 1:
        raise ValueError('Every query must grade the same frozen object set.')
    relevant_count = sum(any(g >= 1 for g in q.grades.values()) for q in gold)
    if relevant_count != 4:
        raise ValueError('Four nonempty and two empty relevance sets are required.')
    rows = []; recalls = []; ndcgs = []; all_complete = True; empty_passes = []
    for query in gold:
        raw = responses.get(query.query_id)
        response = None if raw is None else _record(raw)
        ids = () if response is None else _memories(response)
        if set(ids) - set(query.grades):
            raise ValueError('Returned identities outside the reviewed fixed set must be investigated.')
        complete = response is not None and _complete(response, mode)
        all_complete = all_complete and complete
        relevant = {oid for oid, grade in query.grades.items() if grade >= 1}
        recall = len(set(ids) & relevant) / len(relevant) if relevant else None
        dcg = sum((2 ** query.grades[oid] - 1) / math.log2(rank + 1) for rank, oid in enumerate(ids, 1))
        ideal = sum((2 ** grade - 1) / math.log2(rank + 1)
                    for rank, grade in enumerate(sorted(query.grades.values(), reverse=True)[:8], 1))
        ndcg = dcg / ideal if ideal else None
        no_match = None
        if not relevant:
            expected_decision = 'NO_MATCH' if mode == 'HYBRID' else 'INCOMPLETE_EMPTY'
            no_match = (complete and not ids and response is not None and response.get('recall_id') is None
                        and (mode == 'LEXICAL_BASELINE' or response.get('decision') == expected_decision))
            empty_passes.append(no_match)
        else:
            assert recall is not None and ndcg is not None
            recalls.append(recall); ndcgs.append(ndcg)
        rows.append({'query_id': query.query_id, 'response_present': response is not None,
                     'required_path_complete': complete, 'ranked_ids': list(ids), 'relevant_count': len(relevant),
                     'recall_at_8': recall, 'ndcg_at_8': ndcg, 'no_match_passed': no_match,
                     'false_positive_objects_on_empty_gold': len(ids) if not relevant and response is not None else None})
    recall_macro = sum(recalls) / 4; ndcg_macro = sum(ndcgs) / 4
    return {'mode': mode, 'query_count': 6, 'nonempty_macro_denominator': 4, 'empty_query_count': 2,
            'macro_recall_at_8': recall_macro, 'macro_ndcg_at_8': ndcg_macro,
            'complete': all_complete, 'relevance_passed': all_complete and recall_macro >= .90 and ndcg_macro >= .85,
            'empty_rejection_passed': all(empty_passes), 'rows': rows,
            'qualification_passed': all_complete and recall_macro >= .90 and ndcg_macro >= .85 and all(empty_passes)}
