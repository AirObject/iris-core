"""Deterministic subject resolution; names are anchors, not attribute evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .search_text import query_terms


@dataclass(frozen=True)
class Query:
    text: str
    people: tuple[str, ...]
    tokens: tuple[str, ...]


def analyze(conn, text: str, participants=(), tokenizer="jieba") -> Query:
    labels = {}
    names = {}
    for row in conn.execute("SELECT id,name FROM subjects WHERE id NOT IN ('self','scene')"):
        labels.setdefault(row['name'].casefold(), set()).add(row['id'])
        names[row['id']] = row['name']
    for row in conn.execute("SELECT subject_id,alias FROM subject_aliases"):
        labels.setdefault(row['alias'].casefold(), set()).add(row['subject_id'])
    # Longest match protects names such as 小林妈妈 from being split into 小林.
    pattern = '|'.join(re.escape(x) if re.search('[\u4e00-\u9fff]', x) else r'(?<!\w)' + re.escape(x) + r'(?!\w)'
                       for x in sorted(labels, key=lambda x: (-len(x), x)) if x)
    mentioned = []
    def replace(match):
        ids = sorted(labels[match.group().casefold()])
        mentioned.extend(ids)
        # Ambiguous aliases stay ambiguous; never merge subjects or guess one.
        return names.get(ids[0], match.group()) if len(ids) == 1 else match.group()
    canonical = re.sub(pattern, replace, text, flags=re.I) if pattern else text
    topic = re.sub(pattern, ' ', text, flags=re.I) if pattern else text
    focused = tuple(dict.fromkeys(mentioned or participants))
    if not mentioned and participants:
        canonical = ' '.join(names.get(sid, '我' if sid == 'self' else '') for sid in participants) + '：' + text
    return Query(canonical, focused, tuple(query_terms(topic, tokenizer)))


# Narrow, explicit attribute questions need evidence about that attribute.
# This is a finite vocabulary guard, not an answer classifier: no model calls,
# no corpus IDs, no invented facts. Unrecognized paraphrases use normal retrieval.
ATTRIBUTE_PATTERNS = (
    (r'生日|生辰|出生日期', r'生日|生辰|出生|生于'),
    (r'多少钱|价格|学费|收费|票价|最低价|门票', r'\d+\s*元|[一二三四五六七八九十百千万]+元|免费|价格|收费|学费|票价|定价'),
    (r'卫生间|厕所|洗手间', r'卫生间|厕所|洗手间'),
    (r'保修|售后', r'保修|售后'),
    (r'过敏', r'过敏|红疹|荨麻疹'),
    (r'老家|籍贯|家乡', r'老家|籍贯|家乡|出生于|来自'),
    (r'住.{0,8}(哪|小区|地址)|地址.{0,5}(哪|写)', r'住|公寓|小区|宿舍|地址|路.{0,8}号|巷.{0,8}号'),
    (r'职业|做什么工作|什么工作|公司工作|在哪.{0,3}上班', r'工作|职业|任职|从事|负责|工程师|老师|护士|设计|修复|经营|教'),
)


def attribute_evidence(query: str, content: str) -> bool:
    requested = [evidence for question, evidence in ATTRIBUTE_PATTERNS if re.search(question, query)]
    # Multiple questions may be answered by separate memories.
    return not requested or any(re.search(pattern, content) for pattern in requested)
