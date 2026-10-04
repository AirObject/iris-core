"""Identical deterministic segmentation for FTS writes and queries."""
from __future__ import annotations

import logging
import re

import jieba

jieba.setLogLevel(logging.ERROR)
_tokenizer = jieba.Tokenizer()
_tokenizer.initialize()
STOP = set("的 了 是 在 我 你 他 她 它 们 和 与 有 吗 呢 啊 吧 什么 怎么 哪个 哪些 多少 请 帮 查 找 一下 关于 最近 记得 知道 说 还 可以 是否 什么时候 哪里 谁".split())
# Query-only stop words: keep the indexed vocabulary stable across upgrades.
QUERY_STOP = set("的 了 是 在 我 你 您 他 她 它 们 和 与 有 吗 呢 啊 吧 什么 怎么 哪个 哪些 多少 请 一下 关于 还 可以 是否 什么时候 哪里 谁 能 不能 能不能 可不可以 会不会 要不要 有没有 是不是 怎么样 怎样 为何 为什么 哪 哪儿 哪天 哪年 几 几个 几号 几点 何时 何处 在哪 在哪儿 是什么 该 也 都 呀 哦 啦 嘛 嗯 嘿 来着 请问 这 那 这个 那个 这些 那些 这边 那边 一些 还有 到底 其实 需要 什么样 怎么办 得 想 要 把 对 着 就 过 更 最 很 太 不太 不 会 没有 已经 再".split())


def words(text: str) -> list[str]:
    return list(_tokenizer.cut(text, HMM=False))


def terms(text: str) -> list[str]:
    return list(dict.fromkeys(t.casefold() for t in _tokenizer.cut_for_search(text, HMM=False)
                             if re.search(r"\w", t) and t not in STOP))


def segmented(text: str) -> str:
    return " ".join(terms(text))


def query_terms(text: str, tokenizer: str) -> list[str]:
    if tokenizer == "jieba":
        return list(dict.fromkeys(t.casefold() for t in _tokenizer.cut_for_search(text, HMM=False)
                                  if re.search(r"\w", t) and t not in QUERY_STOP))[:64]
    cleaned = ''.join(' ' if word in QUERY_STOP else word for word in words(text))
    return learning_query_terms(cleaned, tokenizer)


def learning_query_terms(text: str, tokenizer: str) -> list[str]:
    """PR #4 vocabulary; reply-query stopword changes must not affect learning."""
    if tokenizer == 'jieba':
        return terms(text)[:64]
    # Trigram queries use contiguous windows, never LIKE (which scans short terms).
    return list(dict.fromkeys(part[i:i + 3].casefold()
                for part in re.findall(r"\w+", text) for i in range(len(part) - 2)))[:64]


def match_query(tokens: list[str]) -> str:
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
