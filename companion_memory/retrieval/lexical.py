"""Versioned Unicode lexical material and deterministic current-object ranking.

Only an indexing copy is normalized. Letter/number runs yield unique character
and adjacent-pair terms. Exact rational coverage avoids floating-point ranking
variation. These functions own no storage and grant no object permissions.
"""
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Literal, cast
import unicodedata
from companion_memory.persistence.schema import Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record, sequence

PREPROCESS_ID = 'LOCAL_LEXICAL_V1'
UNICODE_VERSION = unicodedata.unidata_version


@dataclass(frozen=True, slots=True)
class LexicalMaterial:
    """Bounded normalized text plus sorted unique terms in one Unicode space."""
    text: str
    singles: tuple[str, ...]
    pairs: tuple[str, ...]
    preprocess_id: str = PREPROCESS_ID
    unicode_version: str = UNICODE_VERSION

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(sorted((*self.singles, *self.pairs)))


def normalize_material(text: str, *, byte_limit: int, term_limit: int) -> LexicalMaterial:
    """Build the entire material or reject; never truncate and claim coverage.

    Limits come from the validated configuration. An object that expands beyond
    them remains valid memory but cannot have complete lexical index coverage.
    The internal LIMIT_EXCEEDED cause identifies an index material limit;
    callers must distinguish an index coverage gap from a failed query.
    """
    if type(text) is not str or type(byte_limit) is not int or type(term_limit) is not int or byte_limit < 1 or term_limit < 1:
        raise OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE')
    try:
        normalized = unicodedata.normalize('NFKC', text).casefold()
        if len(normalized.encode('utf-8', errors='strict')) > byte_limit:
            raise OwnerFailure('INVALID_INPUT', 'index', 'LIMIT_EXCEEDED')
    except UnicodeError:
        raise OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE') from None
    singles: set[str] = set()
    pairs: set[str] = set()
    previous: str | None = None
    for character in normalized:
        if unicodedata.category(character)[0] not in ('L', 'N'):
            previous = None
            continue
        singles.add(character)
        if previous is not None:
            pairs.add(previous + character)
        previous = character
        if len(singles) + len(pairs) > term_limit:
            raise OwnerFailure('INVALID_INPUT', 'index', 'LIMIT_EXCEEDED')
    return LexicalMaterial(normalized, tuple(sorted(singles)), tuple(sorted(pairs)))


@dataclass(frozen=True, slots=True)
class WorldFilter:
    """Typed internal world identity; context IDs cannot impersonate REAL."""
    kind: Literal['REAL', 'FICTIONAL', 'ROLEPLAY']
    context_id: str | None


@dataclass(frozen=True, slots=True)
class StructuralFilter:
    """Independent AND predicates; subject IDs match ANY explicitly supplied ID."""
    object_ids: tuple[str, ...] = ()
    subject_ids: tuple[str, ...] = ()
    category: str | None = None
    world_scope: WorldFilter | None = None
    start_us: int | None = None
    end_us: int | None = None

    @property
    def specified(self) -> bool:
        return bool(self.object_ids or self.subject_ids or self.category is not None or self.world_scope is not None
                    or self.start_us is not None or self.end_us is not None)


def object_text(current: MappingProxyType[str, Value]) -> str:
    """Read only an already authorized current memory or relation assertion."""
    content = record(current['content'])
    return cast(str, content['body'] if current['kind'] == 'MEMORY' else content['assertion'])


def matches_structure(current: MappingProxyType[str, Value], selected: StructuralFilter) -> bool:
    """Apply exact IDs/world/category and known intersecting stored time ranges.

    World kind and context are compared separately, so a context named REAL
    cannot match the real world. No source, cache or reception time is inferred.
    """
    if selected.object_ids and current['object_id'] not in selected.object_ids:
        return False
    content = record(current['content'])
    if selected.subject_ids:
        if current['kind'] != 'MEMORY' or not set(selected.subject_ids).intersection(sequence(content['subject_ids'])):
            return False
    category = content.get('category', content.get('relation_type'))
    if selected.category is not None and category != selected.category:
        return False
    world = record(content['world_scope'])
    if selected.world_scope is not None and (selected.world_scope.kind != world['kind']
            or selected.world_scope.context_id != world['context_id']):
        return False
    if selected.start_us is not None or selected.end_us is not None:
        for name in ('occurred_range', 'applicable_range'):
            value = content.get(name)
            if value is None:
                continue
            interval = record(value)
            start, end = cast(int | None, interval['start_us']), cast(int | None, interval['end_us'])
            if start is None and end is None:
                continue
            if selected.start_us is not None and end is not None and end < selected.start_us:
                continue
            if selected.end_us is not None and start is not None and start > selected.end_us:
                continue
            return True
        return False
    return True


def rank_key(object_id: str, material: LexicalMaterial, query: LexicalMaterial,
             selected: StructuralFilter) -> tuple[int, int, Fraction, Fraction, int, str]:
    """Ascending key implements exact coverage and a stable object-ID tie break."""
    if material.preprocess_id != query.preprocess_id or material.unicode_version != query.unicode_version:
        raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'index', 'INDEX_NOT_READY')
    field_count = sum((bool(selected.object_ids), bool(selected.subject_ids), selected.category is not None,
                       selected.world_scope is not None, selected.start_us is not None or selected.end_us is not None))
    pair_coverage = Fraction(len(set(material.pairs).intersection(query.pairs)), len(query.pairs)) if query.pairs else Fraction(0)
    single_coverage = Fraction(len(set(material.singles).intersection(query.singles)), len(query.singles)) if query.singles else Fraction(0)
    phrase = bool(query.text and query.text in material.text)
    return (-int(object_id in selected.object_ids), -field_count, -pair_coverage, -single_coverage, -int(phrase), object_id)


def has_lexical_match(material: LexicalMaterial, query: LexicalMaterial) -> bool:
    """A literal match is evidence of text overlap, never semantic agreement."""
    return bool(set(material.singles).intersection(query.singles) or set(material.pairs).intersection(query.pairs))
