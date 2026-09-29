"""Identical deterministic segmentation for FTS writes and queries."""
from __future__ import annotations

import logging
import re

import jieba

jieba.setLogLevel(logging.ERROR)
_tokenizer = jieba.Tokenizer()
_tokenizer.initialize()
STOP = set("的 了 是 在 我 你 他 她 它 们 和 与 有 吗 呢 啊 吧 什么 怎么 哪个 哪些 多少 请 帮 查 找 一下 关于 最近 记得 知道 说 还 可以 是否 什么时候 哪里 谁".split())


def terms(text: str) -> list[str]:
    return list(dict.fromkeys(t.casefold() for t in _tokenizer.cut_for_search(text, HMM=False)
                             if re.search(r"\w", t) and t not in STOP))


def segmented(text: str) -> str:
    return " ".join(terms(text))


def query_terms(text: str, tokenizer: str) -> list[str]:
    if tokenizer == "jieba":
        return terms(text)[:64]
    # Trigram queries use contiguous windows, never LIKE (which scans short terms).
    return list(dict.fromkeys(part[i:i + 3].casefold()
                for part in re.findall(r"\w+", text) for i in range(len(part) - 2)))[:64]


def match_query(tokens: list[str]) -> str:
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
