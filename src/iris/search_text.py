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
QUERY_STOP = STOP | set("能 不能 能不能 可不可以 会不会 要不要 有没有 是不是 怎么样 怎样 为何 为什么 哪 哪儿 哪天 哪年 几 几个 几号 几点 何时 何处 在哪 在哪儿 是什么 该 也 都 呀 哦 啦 嘛 嗯 嘿 来着 告诉 请问 帮忙 帮我 看看 说说 想想 回想 一点 点 这 那 这个 那个 这些 那些 这边 那边 一些 还有 现在 平时 到底 具体 其实 一起 需要 值得 提前 注意 聊天 事情 比较 什么样 怎么办 得 想 要 去 来 给 把 对 里 上 下 前 后 着 就 过 更 最 很 太 不太 不 有关 吗是".split())
QUERY_STOP.update('会 没有 已经 再 事 台 哪家'.split())


def terms(text: str) -> list[str]:
    return list(dict.fromkeys(t.casefold() for t in _tokenizer.cut_for_search(text, HMM=False)
                             if re.search(r"\w", t) and t not in STOP))


def segmented(text: str) -> str:
    return " ".join(terms(text))


def query_terms(text: str, tokenizer: str) -> list[str]:
    if tokenizer == "jieba":
        return [t for t in terms(text) if t not in QUERY_STOP][:64]
    # Trigram queries use contiguous windows, never LIKE (which scans short terms).
    return list(dict.fromkeys(part[i:i + 3].casefold()
                for part in re.findall(r"\w+", text) for i in range(len(part) - 2)))[:64]


def match_query(tokens: list[str]) -> str:
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
