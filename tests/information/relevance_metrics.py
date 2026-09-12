"""Read confirmed annotations and retained observations without changing either.

Corpus aliases map only after body, explicit person roles and world identity
match. Complete human partitions determine denominators; lexical reachability
is a separate, result-independent diagnostic. All outputs are offline evidence.
"""
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
from typing import cast
import unicodedata
from companion_memory.retrieval.lexical import normalize_material, has_lexical_match


class ReviewValidationError(ValueError):
    """An input binding or partition failed; no score may be published for it."""


def require(condition: bool, message: str) -> None:
    if not condition: raise ReviewValidationError(message)


def record(value: object) -> dict[str, object]:
    require(type(value) is dict, 'Expected a plain JSON record.')
    result = cast(dict[object, object], value)
    require(all(type(key) is str for key in result), 'Expected string record keys.')
    return cast(dict[str, object], result)


def sequence(value: object) -> list[object]:
    require(type(value) is list, 'Expected a JSON list.')
    return cast(list[object], value)


def text(value: object) -> str:
    require(type(value) is str, 'Expected text.')
    return cast(str, value)


def strings(value: object) -> tuple[str, ...]:
    items = tuple(text(item) for item in sequence(value))
    require(len(items) == len(set(items)), 'Repeated list identity.')
    return items


def integer(value: object) -> int:
    require(type(value) is int and cast(int, value) >= 0, 'Expected a nonnegative integer.')
    return cast(int, value)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def read_json(path: Path) -> dict[str, object]:
    """Read one immutable evidence file, rejecting ambiguous duplicate keys."""
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        require(len(items) == len({key for key, _ in items}), 'Duplicate JSON key.')
        return dict(items)
    def invalid_constant(value: str) -> object:
        raise ReviewValidationError('Nonfinite JSON number: ' + value)
    return record(json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=invalid_constant))


def indexed(items: object, key: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in sequence(items):
        row = record(item); identity = text(row[key])
        require(identity not in result, 'Duplicate identity: ' + identity)
        result[identity] = row
    return result


@dataclass(frozen=True)
class ConfirmedCase:
    """User-confirmed full in-scope partition; notes keep their original source."""
    case_id: str
    relevant: frozenset[str]
    nonrelevant: frozenset[str]
    notes: str


def validate_annotations(packet: dict[str, object], annotations: dict[str, object], expected_packet: str) -> dict[str, ConfirmedCase]:
    """Reject wrong material, unfinished review, missing labels and scope leaks."""
    unsigned = {key: value for key, value in packet.items() if key != 'packet_sha256'}
    require(hashlib.sha256(canonical(unsigned)).hexdigest() == expected_packet == packet['packet_sha256'] == annotations['packet_sha256'], 'Material fingerprint mismatch.')
    require(annotations['review_status'] == 'LABELS_READY_FOR_USER_SUBMISSION' and bool(text(annotations['reviewer']).strip()), 'User review is not signed and complete.')
    require(annotations['relevance_metrics'] is None, 'Expected labels, not precomputed scores.')
    definitions = indexed(packet['cases'], 'case_id'); rows = indexed(annotations['cases'], 'case_id')
    require(rows.keys() == definitions.keys(), 'Confirmed case set differs from the bound material.')
    documents = indexed(packet['documents'], 'document_id'); results: dict[str, ConfirmedCase] = {}
    for key, definition in definitions.items():
        row = rows[key]; scope = set(strings(definition['eligible_documents'])); excluded = set(strings(definition['structurally_excluded_documents']))
        require(not scope & excluded and scope | excluded == documents.keys(), 'Material scope is not a full corpus partition.')
        relevant = frozenset(strings(row['relevant_documents'])); nonrelevant = frozenset(strings(row['nonrelevant_documents']))
        require(row['reviewed'] is True and strings(row['unlabeled_documents']) == (), 'Case is not fully reviewed: ' + key)
        require(not relevant & nonrelevant and relevant | nonrelevant == scope, 'Labels overlap, omit or exceed authorized scope: ' + key)
        results[key] = ConfirmedCase(key, relevant, nonrelevant, text(row['notes']))
    return results


def semantic_document(document: dict[str, object], identities: dict[str, object]) -> dict[str, object]:
    """Normalize explicit person/context IDs to the corpus's stable role aliases."""
    require(set(identities) == {'first', 'second', 'scene'}, 'Unexpected corpus identity roles.')
    reverse = {text(value): key for key, value in identities.items()}
    require(len(reverse) == 3, 'Ambiguous explicit identity binding.')
    subjects = strings(document['subject_ids']); context = document['world_context']
    require(set(subjects) <= reverse.keys() and (context is None or text(context) in reverse), 'Unbound subject or world context.')
    formal = record(document['formal_object']); content = record(formal['content']); world = record(content['world_scope'])
    require(formal['object_id'] == document['object_id'] and formal['revision'] == document['revision'] and formal['lifecycle'] == 'ACTIVE', 'Formal identity/revision/lifecycle differs from corpus.')
    require(content['body'] == document['body'] and strings(content['subject_ids']) == subjects and world['kind'] == document['world_kind'] and world['context_id'] == context, 'Formal content differs from corpus projection.')
    return {'body': text(document['body']), 'subjects': sorted(reverse[s] for s in subjects),
        'world_kind': text(document['world_kind']), 'world_context': reverse[text(context)] if context is not None else None,
        'category': content['category'], 'revision': integer(document['revision']), 'lifecycle': formal['lifecycle'],
        'occurred_range': content['occurred_range'], 'applicable_range': content['applicable_range']}


def validate_corpus_mapping(packet: dict[str, object], corpus: dict[str, object]) -> list[dict[str, object]]:
    """Map aliases by complete body, explicit roles and worlds, never by foreign IDs."""
    reference = indexed(packet['documents'], 'document_id'); target = indexed(corpus['documents'], 'document_id')
    require(reference.keys() == target.keys(), 'Corpus aliases differ.')
    require(corpus['source_pairs_sha256'] == packet['original_pairs_sha256'], 'Original authored material differs.')
    require(len({text(d['object_id']) for d in target.values()}) == len(target), 'Repeated actual object ID.')
    result = []
    for alias, expected in reference.items():
        meaning = semantic_document(expected, record(packet['identities']))
        require(meaning == semantic_document(target[alias], record(corpus['identities'])), 'Body/person/world mapping conflict: ' + alias)
        result.append({'document_id': alias, 'reference_object_id': expected['object_id'], 'platform_object_id': target[alias]['object_id'], 'verified_meaning': meaning})
    return result


def eligible_documents(definition: dict[str, object], corpus: dict[str, object]) -> frozenset[str]:
    identities = record(corpus['identities']); selected_subject = definition['subject']; selected_world = definition['world']
    result = set()
    for alias, doc in indexed(corpus['documents'], 'document_id').items():
        if selected_subject is not None and identities[text(selected_subject)] not in strings(doc['subject_ids']): continue
        if selected_world is not None and (doc['world_kind'] != selected_world or selected_world != 'REAL' and doc['world_context'] != identities['scene']): continue
        result.add(alias)
    return frozenset(result)


def validate_observations(packet: dict[str, object], corpus: dict[str, object], observations: list[object]) -> dict[tuple[str, str], dict[str, object]]:
    """Verify complete phase/case coverage, request scopes and actual returned bodies."""
    definitions = indexed(packet['cases'], 'case_id'); documents = indexed(corpus['documents'], 'document_id'); identities = record(corpus['identities'])
    result: dict[tuple[str, str], dict[str, object]] = {}
    for raw in observations:
        row = record(raw); phase, key = text(row['phase']), text(row['case_id'])
        require(phase in ('dirty', 'active', 'reopened') and key in definitions and (phase, key) not in result, 'Unknown or repeated observation.')
        definition = definitions[key]; request = record(row['request']); response = record(row['response'])
        eligible = eligible_documents(definition, corpus)
        require(eligible == frozenset(strings(definition['eligible_documents'])), 'Actual structural scope differs from the reviewed scope.')
        expected_world = definition['world']
        if expected_world not in (None, 'REAL'): expected_world = text(expected_world) + ':' + text(identities['scene'])
        expected_subject = () if definition['subject'] is None else (text(identities[text(definition['subject'])]),)
        require(request['query_text'] == definition['query_text'] and request['world_scope'] == expected_world and strings(request['subject_ids']) == expected_subject, 'Query text/person/world differs from reviewed intent.')
        require(request['entry_id'] == 'entry' and strings(request['object_ids']) == () and request['category'] is None and request['time_range'] is None, 'Unexpected object/entry/category/time scope.')
        require(request['retrieval_mode'] == 'LOCAL_LEXICAL_V1' and request['rerank'] is False and request['include_goals'] == definition['include_goals'] and request['include_state'] is False, 'Unexpected capability or sidecar intent.')
        require(request['require_complete'] is (phase != 'dirty') and request['allow_partial'] is True, 'Completeness intent differs.')
        require(request['request_key'] == phase + ':' + key == response['request_id'], 'Response belongs to another request.')
        require(response['availability'] == ('DEGRADED' if phase == 'dirty' else 'COMPLETE'), 'Unexpected observation availability.')
        coverage = record(response['coverage'])
        require(coverage['preprocess_id'] == 'LOCAL_LEXICAL_V1' and coverage['unicode_version'] == unicodedata.unidata_version == '15.0.0', 'Unicode or preprocessing version differs.')
        returned = strings(row['returned_documents']); memories = sequence(record(response['sections'])['memories'])
        require(len(returned) <= 8 and len(returned) == len(memories) and set(returned) <= eligible, 'Actual result exceeds scope or public bound.')
        for alias, raw_memory in zip(returned, memories, strict=True):
            memory = record(raw_memory); formal = record(documents[alias]['formal_object'])
            require({name: value for name, value in memory.items() if name != 'source_refs'} == formal, 'Returned body/ID/revision differs from its platform corpus: ' + alias)
        strings(record(response['truncation'])['reasons']); integer(record(response['truncation'])['omitted_memories'])
        result[(phase, key)] = row
    require(set(result) == {(phase, key) for phase in ('dirty', 'active', 'reopened') for key in definitions}, 'Missing retained phase/case observation.')
    for key in definitions:
        require(result[('dirty', key)]['returned_documents'] == result[('active', key)]['returned_documents'] == result[('reopened', key)]['returned_documents'], 'Ranking changed within a persisted corpus.')
    return result


def unreachable_relevant(definition: dict[str, object], labels: ConfirmedCase, documents: dict[str, dict[str, object]]) -> tuple[str, ...]:
    """Find human-relevant bodies with zero lexical intersection, ignoring returns."""
    query = normalize_material(text(definition['query_text']), byte_limit=8192, term_limit=4096)
    return tuple(sorted(alias for alias in labels.relevant if not has_lexical_match(
        normalize_material(text(documents[alias]['body']), byte_limit=8192, term_limit=4096), query)))


def number(value: Fraction | None) -> dict[str, object] | None:
    return None if value is None else {'numerator': value.numerator, 'denominator': value.denominator, 'value': float(value)}


def score(returned: tuple[str, ...], labels: ConfirmedCase) -> dict[str, object]:
    """P@5 uses five slots; empty R@8 is undefined; MRR@8 includes zero cases."""
    require(len(returned) <= 8 and len(returned) == len(set(returned)) and set(returned) <= labels.relevant | labels.nonrelevant, 'Ranking exceeds the labeled scope.')
    hit5 = sum(alias in labels.relevant for alias in returned[:5]); hit8 = sum(alias in labels.relevant for alias in returned)
    first = next((index for index, alias in enumerate(returned, 1) if alias in labels.relevant), None)
    return {'precision_at_5': number(Fraction(hit5, 5)), 'recall_at_8': number(Fraction(hit8, len(labels.relevant))) if labels.relevant else None,
        'reciprocal_rank_at_8': number(Fraction(1, first) if first else Fraction(0)),
        'relevant_count': len(labels.relevant), 'relevant_returned_at_5': hit5, 'relevant_returned_at_8': hit8,
        'first_relevant_rank': first, 'returned_count': len(returned), 'returned_documents': returned,
        'false_positive_documents': [alias for alias in returned if alias not in labels.relevant],
        'missed_relevant_documents': sorted(labels.relevant - set(returned))}


def mean(rows: list[dict[str, object]], key: str) -> dict[str, object] | None:
    values = [record(row[key]) for row in rows if row[key] is not None]
    return number(sum((Fraction(integer(v['numerator']), integer(v['denominator'])) for v in values), Fraction(0)) / len(values)) if values else None


def aggregate(rows: list[dict[str, object]]) -> dict[str, object]:
    """Macro denominators remain explicit, including zero-relevance diagnostics."""
    empty = [row for row in rows if row['relevant_count'] == 0]; total = len(rows)
    rate = lambda count, denominator: number(Fraction(count, denominator)) if denominator else None
    return {'queries': total, 'case_ids': [row['case_id'] for row in rows], 'recall_defined_queries': total - len(empty),
        'precision_at_5': mean(rows, 'precision_at_5'), 'recall_at_8': mean(rows, 'recall_at_8'), 'mrr_at_8': mean(rows, 'reciprocal_rank_at_8'),
        'candidate_truncated_queries': sum(bool(row['candidate_truncated']) for row in rows),
        'candidate_truncation_ratio': rate(sum(bool(row['candidate_truncated']) for row in rows), total),
        'index_lag_partial_queries': sum(bool(row['index_lag_partial']) for row in rows),
        'memory_byte_omitted_total': sum(integer(row['memory_byte_omitted']) for row in rows),
        'empty_relevant_queries': len(empty), 'empty_relevant_case_ids': [row['case_id'] for row in empty],
        'empty_relevant_zero_return_rate': rate(sum(row['returned_count'] == 0 for row in empty), len(empty)),
        'empty_relevant_false_positive_query_rate': rate(sum(row['returned_count'] != 0 for row in empty), len(empty)),
        'empty_relevant_returned_objects': sum(integer(row['returned_count']) for row in empty)}


def evaluate(packet: dict[str, object], labels: dict[str, ConfirmedCase], corpus: dict[str, object], observations: list[object]) -> dict[str, object]:
    """Compute all fixed strata from validated observations; never rewrite inputs."""
    mapping = validate_corpus_mapping(packet, corpus); validated = validate_observations(packet, corpus, observations)
    definitions = indexed(packet['cases'], 'case_id'); documents = indexed(corpus['documents'], 'document_id'); rows = []
    require(labels.keys() == definitions.keys(), 'Confirmed partition is incomplete.')
    for (phase, key), observation in validated.items():
        definition = definitions[key]; confirmed = labels[key]
        require(confirmed.relevant | confirmed.nonrelevant == eligible_documents(definition, corpus), 'Confirmed labels exceed platform scope.')
        missing = unreachable_relevant(definition, confirmed, documents)
        response = record(observation['response']); truncation = record(response['truncation']); reasons = strings(truncation['reasons'])
        row = score(strings(observation['returned_documents']), confirmed)
        row.update(phase=phase, case_id=key, cohort='original' if key.startswith('literal-') else 'additional',
            query_text=definition['query_text'], relevant_documents=sorted(confirmed.relevant), nonrelevant_documents=sorted(confirmed.nonrelevant),
            original_notes=confirmed.notes, lexically_unreachable_relevant=missing, literal_matchable=bool(confirmed.relevant) and not missing,
            candidate_truncated='CANDIDATE_LIMIT' in reasons, index_lag_partial='INDEX_LAG_PARTIAL' in reasons,
            memory_byte_omitted=integer(truncation['omitted_memories']), availability=response['availability'], truncation_reasons=reasons)
        rows.append(row)
    aggregates = []
    for phase in ('dirty', 'active', 'reopened'):
        for cohort in ('original', 'additional', 'all'):
            selected = [row for row in rows if row['phase'] == phase and (cohort == 'all' or row['cohort'] == cohort)]
            for subset in ('complete_human_partition', 'literal_matchable'):
                chosen = selected if subset == 'complete_human_partition' else [row for row in selected if row['literal_matchable']]
                aggregates.append({'phase': phase, 'cohort': cohort, 'subset': subset, **aggregate(chosen)})
    return {'mapping': mapping, 'per_query': rows, 'aggregates': aggregates}
