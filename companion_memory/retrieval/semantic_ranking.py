"""Absolute candidate admission followed by equal-weight reciprocal rank fusion.

Scores only order candidates already admitted by semantic or lexical evidence.
They confer no permission, object freshness, lifecycle or delivery guarantee.
Explicit IDs are prioritized only after the query owner authorizes them.
"""
from collections.abc import Iterable
from fractions import Fraction
import math
import unicodedata
from .lexical import LexicalMaterial
from companion_memory.persistence.schema import valid_identifier,InvalidValue


def _segments(text: str) -> frozenset[str]:
    segments=[];current=[]
    for character in text:
        if unicodedata.category(character)[0] in ('L','N'): current.append(character)
        elif current: segments.append(''.join(current));current=[]
    if current: segments.append(''.join(current))
    return frozenset(segments)


def lexical_admitted(query: LexicalMaterial, material: LexicalMaterial, minimum_millionths: int) -> bool:
    """Compare distinct query bigrams, or exact complete normalized segments."""
    if minimum_millionths != 600000 or query.preprocess_id != material.preprocess_id or query.unicode_version != material.unicode_version:
        raise InvalidValue()
    if query.pairs:
        return len(set(query.pairs).intersection(material.pairs))*1_000_000 >= len(query.pairs)*minimum_millionths
    return bool(_segments(query.text).intersection(_segments(material.text)))


def semantic_admitted(score: float, minimum_millionths: int) -> bool:
    if type(score) is not float or not math.isfinite(score) or not -1 <= score <= 1 or minimum_millionths != 700000:
        raise InvalidValue()
    return score >= minimum_millionths/1_000_000


def fuse(lexical: tuple[str,...],semantic: tuple[str,...],explicit: tuple[str,...], *, rrf_k: int,
         combined_limit: int) -> tuple[str,...]:
    """Fuse pre-admitted lists with exact rational scores and stable ID ties.

    Each route has at most 64 distinct candidates. At most eight explicitly
    selected IDs precede the fused remainder; duplicates consume no extra slot.
    """
    if rrf_k != 60 or combined_limit != 128:
        raise InvalidValue()
    for values,limit in ((lexical,64),(semantic,64),(explicit,8)):
        if type(values) is not tuple or len(values)>limit or any(not valid_identifier(v) for v in values) or len(set(values))!=len(values): raise InvalidValue()
    scores: dict[str,Fraction]={}
    for route in (lexical,semantic):
        for rank,object_id in enumerate(route,1): scores[object_id]=scores.get(object_id,Fraction(0))+Fraction(1,rrf_k+rank)
    order=sorted(scores,key=lambda object_id:(-scores[object_id],object_id))
    return (explicit+tuple(object_id for object_id in order if object_id not in explicit))[:combined_limit]
