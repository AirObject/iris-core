"""Deterministic name anchors and nonrestrictive references to the role."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .search_text import query_terms, query_coverage_terms, words


class SubjectNames:
    def __init__(self, conn, role_addresses=()):
        self.labels: dict[str, set[str]] = {}
        self.names = {r['id']: r['name'] for r in conn.execute("SELECT id,name FROM subjects WHERE id!='scene'")}
        for sid, name in self.names.items():
            if sid != 'self':
                self.add(name, sid)
        role = conn.execute("SELECT value_json FROM runtime_settings WHERE key='role_name'").fetchone()
        if role:
            self.add(json.loads(role[0]), 'self')
        for address in role_addresses:
            self.add(address, 'self')
        for row in conn.execute("SELECT subject_id,alias FROM subject_aliases WHERE subject_id!='scene'"):
            self.add(row['alias'], row['subject_id'])
        # Longest known label wins, including in memory prose. Do not turn
        # 江澄妈妈 into 江澄, or Ann into a mention in 'annual'.
        labels = sorted(self.labels, key=lambda x: (-len(x), x))
        pattern = '|'.join(re.escape(x) if re.search('[\u4e00-\u9fff]', x)
                           else r'(?<!\w)' + re.escape(x) + r'(?!\w)' for x in labels)
        self.pattern = re.compile(pattern or r'(?!x)x', re.I)

    def add(self, label, sid):
        if label and label not in ('我', 'self', 'scene'):
            self.labels.setdefault(label.casefold(), set()).add(sid)

    def mentioned(self, text):
        return tuple(dict.fromkeys(sid for match in self.pattern.finditer(text)
                                   for sid in sorted(self.labels[match.group().casefold()])))


@dataclass(frozen=True)
class Query:
    text: str
    people: tuple[str, ...]
    tokens: tuple[str, ...]
    names: SubjectNames
    refers_to_self: bool = False
    short_tokens: tuple[str, ...] = ()
    name_only: bool = False
    coverage_tokens: tuple[str, ...] = ()


def analyze(conn, text: str, participants=(), tokenizer='jieba', *, entry_kind=None) -> Query:
    names = SubjectNames(conn, ('主播',) if entry_kind == 'live' else ())
    mentioned = names.mentioned(text)
    def replace(match):
        ids = names.labels[match.group().casefold()]
        if len(ids) != 1:
            return match.group()  # Ambiguous names anchor every matching ID.
        sid = next(iter(ids))
        return '我' if sid == 'self' else names.names.get(sid, match.group())
    canonical = names.pattern.sub(replace, text)
    topic = names.pattern.sub(' ', text)
    self_reference = 'self' in mentioned
    if entry_kind is not None:
        # Address normalization changes the embedding hint and a small boost,
        # never the scope. It is confined to prepare's conversation context.
        addresses = {'你', '您'}
        pieces = words(canonical)
        self_reference |= any(piece in addresses for piece in pieces)
        canonical = ''.join('我' if piece in addresses else piece for piece in pieces)
        topic = ''.join(' ' if piece in addresses else piece for piece in words(topic))
    # A role-only address never narrows scope. If a label is ambiguous with
    # another subject, preserve every matching ID, including self.
    anchors = mentioned if any(sid != 'self' for sid in mentioned) else ()
    topic_tokens = query_terms(topic, 'jieba')
    return Query(canonical, anchors,
                 tuple(query_terms(topic, tokenizer)), names, self_reference,
                 tuple(t for t in topic_tokens if len(t) < 3) if tokenizer == 'trigram' else (),
                 bool(anchors and not topic_tokens), tuple(query_coverage_terms(topic)))
