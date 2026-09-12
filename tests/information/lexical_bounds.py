"""Finite Unicode census and conservative bounds, never a maximum claim.

Count letter/number occurrences after casefold. Canonical ordering preserves
that weight; every eligible composition is checked not to increase it. A byte
knapsack on compatibility decompositions therefore bounds arbitrary input,
including composition across adjacent original scalars. Distinct terms can be
far fewer than occurrences; repeated expansions do not prove attainability.
"""
import hashlib
import json
from pathlib import Path
import unicodedata as unicode


def letter_weight(value: str) -> int:
    return sum(unicode.category(character)[0] in ('L', 'N') for character in value.casefold())


def census() -> dict[str, object]:
    maxima = [0] * 5
    witnesses = [''] * 5
    compositions = hangul = scalars = 0
    for ordinal in range(0x110000):
        if 0xD800 <= ordinal <= 0xDFFF:
            continue
        character = chr(ordinal); scalars += 1
        cost = len(character.encode('utf-8'))
        weight = letter_weight(unicode.normalize('NFKD', character))
        if weight > maxima[cost]:
            maxima[cost], witnesses[cost] = weight, 'U+' + format(ordinal, '04X')
        mapping = unicode.decomposition(character)
        if mapping and not mapping.startswith('<'):
            parts = ''.join(chr(int(item, 16)) for item in mapping.split())
            if len(parts) == 2 and unicode.normalize('NFC', parts) == character:
                compositions += 1
                if letter_weight(character) > letter_weight(parts):
                    raise RuntimeError('Canonical composition invalidates the weight proof.')
        if 0xAC00 <= ordinal <= 0xD7A3:
            offset = ordinal - 0xAC00; trailing = offset % 28
            parts = (chr(ordinal - trailing) + chr(0x11A7 + trailing) if trailing
                     else chr(0x1100 + offset // 588) + chr(0x1161 + offset % 588 // 28))
            if unicode.normalize('NFC', parts) != character or letter_weight(character) > letter_weight(parts):
                raise RuntimeError('Hangul composition invalidates the weight proof.')
            hangul += 1
    budget = [0] * 2049
    for size in range(1, len(budget)):
        budget[size] = max(budget[size - cost] + maxima[cost] for cost in range(1, min(size, 4) + 1))
    return {'unicode_version': unicode.unidata_version, 'scalars_checked': scalars,
            'canonical_compositions_checked': compositions, 'hangul_compositions_checked': hangul,
            'maximum_decomposed_letter_weights_by_utf8_bytes': maxima[1:], 'witnesses': witnesses[1:],
            'formal_body_byte_limit': 2048, 'letter_occurrence_upper_bound': budget[-1],
            'raw_distinct_term_upper_bound': 2 * budget[-1] - 1,
            'complete_index_term_hard_limit': 4096, 'complete_index_normalized_byte_limit': 8192,
            'maximum_proved': False}


def pressure_body() -> str:
    """Return the exact authored lower-bound witness under its recorded Unicode."""
    raw = json.loads(Path(__file__).with_name('lexical_pressure_material.json').read_text())
    body = raw['body']
    if (type(body) is not str or unicode.unidata_version != raw['unicode_version']
            or hashlib.sha256(body.encode()).hexdigest() != raw['body_sha256']):
        raise RuntimeError('The pressure witness or its Unicode binding changed.')
    return body


def reproduce_pressure_body() -> str:
    """Greedily append cheap stable scalar expansions with new singles/pairs.

    This finite heuristic deliberately does not search all legal strings. The
    entire concatenation is checked again, since normalization is not generally
    closed under concatenation. Tie breaks use exact integer arithmetic.
    """
    choices: dict[str, tuple[str, str, int]] = {}
    for ordinal in range(0x110000):
        if 0xD800 <= ordinal <= 0xDFFF: continue
        character = chr(ordinal); cost = len(character.encode())
        if cost > 2 and not unicode.decomposition(character).startswith('<'): continue
        normalized = unicode.normalize('NFKC', character).casefold()
        if not normalized or not all(unicode.category(item)[0] in ('L', 'N') for item in normalized): continue
        if unicode.normalize('NFKC', normalized).casefold() != normalized: continue
        previous = choices.get(normalized)
        if previous is None or (cost, ordinal) < (previous[2], ord(previous[0])):
            choices[normalized] = character, normalized, cost
    singles: set[str] = set(); pairs: set[str] = set()
    body = normalized_body = ''; used = 0
    while used < 2048:
        best: tuple[str, str, int] | None = None; best_gain = -1
        for character, normalized, cost in choices.values():
            if used + cost > 2048: continue
            additional = {normalized[i:i + 2] for i in range(len(normalized) - 1)}
            if normalized_body: additional.add(normalized_body[-1] + normalized[0])
            gain = len(set(normalized) - singles) + len(additional - pairs)
            if (best is None or gain * best[2] > best_gain * cost
                    or gain * best[2] == best_gain * cost and (gain, -ord(character)) > (best_gain, -ord(best[0]))):
                best, best_gain = (character, normalized, cost), gain
        if best is None: raise RuntimeError('No legal scalar fits the remaining byte budget.')
        character, normalized, cost = best
        if normalized_body: pairs.add(normalized_body[-1] + normalized[0])
        singles.update(normalized); pairs.update(normalized[i:i + 2] for i in range(len(normalized) - 1))
        body += character; normalized_body += normalized; used += cost
    if unicode.normalize('NFKC', body).casefold() != normalized_body:
        raise RuntimeError('Scalar concatenation changed this particular witness.')
    return body
