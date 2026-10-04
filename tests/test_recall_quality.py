import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from iris.db import now
from iris.retrieval import Retrieval
from iris.search_text import query_terms
from test_retrieval import Embeddings, entry, put


def people(store):
    with store.write() as conn:
        for sid, name in (("p", "江澄"), ("q", "江橙")):
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (sid, name, now()))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('p','小江')")


def test_function_words_do_not_dilute_bm25_and_numbers_survive(store):
    mid = put(store, "火山地质展在 17 号开幕")
    assert {"能不能", "怎么样", "是什么", "在哪", "呀", "来着"}.isdisjoint(query_terms("能不能说说火山地质展是什么呀，在哪来着，17号怎么样？", "jieba"))
    assert "17" in query_terms("17号在哪里呀", "jieba")
    assert mid in [m["id"] for m in Retrieval(store).search(text="请问能不能帮我看看火山地质展是哪天，在什么地方办呀？")['memories']]


def test_alias_names_anchor_the_named_subject(store):
    entry(store)
    people(store)
    right = put(store, "江澄做园林设计工作", about=["p"])
    put(store, "江橙的工作是设计游戏", about=["q"])
    r = Retrieval(store)
    assert [m["id"] for m in r.search(text="小江做什么工作呀")['memories']] == [right]
    assert r.prepare("A", text="", participants=["p"])['memories']


def test_vector_absolute_and_relative_cutoffs(store):
    good = put(store, "甲项独有事实", vector=[.9, np.sqrt(1-.9**2)])
    put(store, "乙项独有事实", vector=[.6, .8])
    r = Retrieval(store, Embeddings(store), vector_min=.5, vector_relative=.8)
    assert [m["id"] for m in r.search(text="没有共同词")['memories']] == [good]
    r = Retrieval(store, Embeddings(store), vector_min=.95, vector_relative=.8)
    assert not r.search(text="没有共同词")['memories']


def test_latin_alias_case_and_name_boundaries(store):
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('ann','person','Ann',?)", (now(),))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('ann','Maple')")
    mid = put(store, 'Ann 喜欢爬山', about=['ann'])
    assert Retrieval(store).search(text='MAPLE 喜欢爬山吗')['memories'][0]['id'] == mid
    from iris.query_analysis import analyze
    with store.read() as conn:
        assert not analyze(conn, 'annual report').people


def test_learning_context_keeps_speaker_highlights_when_another_person_is_mentioned(store):
    people(store)
    own = put(store, '江澄去年搬了家', about=['p'])
    context = Retrieval(store).learning_context('江橙刚和我讨论了植物', ['p'])
    assert own in [m['id'] for m in context]


def test_explicit_new_dimensions_require_calibration(store):
    from dataclasses import replace
    from iris.retrieval import DEFAULTS
    gateway = Embeddings(store)
    gateway.configs = {'embedding':replace(gateway.configs['embedding'], dimensions=1024)}
    store.set_setting('retrieval', {**DEFAULTS, 'embedding_model':'fake-vector', 'embedding_dimensions':2048})
    result = Retrieval(store, gateway).search(text='星空')
    assert any(h['code'] == 'embedding_uncalibrated' for h in result['hints'])


def test_legacy_settings_keep_original_dimensions_and_unprefixed_queries(store):
    saved = {'embedding_model':'doubao-embedding-vision', 'vector_min':.65, 'lexical_min':.5}
    store.set_setting('retrieval', saved)
    r = Retrieval(store)
    assert r.settings['embedding_dimensions'] == 2048
    assert r.settings['query_prefix'] == ''
    assert store.setting('retrieval') == saved


@pytest.mark.parametrize("mode", ["prepare", "search", "learning_context"])
def test_vector_scoring_does_not_block_committed_memory_write(store, monkeypatch, mode):
    entry(store)
    store.set_setting('learning_retrieval', {'embedding_model': 'fake-vector'})
    old = put(store, "天文摄影旧记录", vector=[1., 0.])
    r = Retrieval(store, Embeddings(store), vector_min=.5)
    entered, release = threading.Event(), threading.Event()
    original = np.einsum
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(np, "einsum", slow)
    def reader():
        if mode == "prepare":
            return r.prepare("A", text="天文摄影", participants=[])
        if mode == "search":
            return r.search(text="天文摄影")
        return r.learning_context("天文摄影", [])
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(reader)
        try:
            assert entered.wait(5)
            write = pool.submit(put, store, "园艺新记录", vector=[0., 1.])
            fresh = write.result(timeout=2)
            with store.read() as conn:
                assert conn.execute("SELECT content FROM memories WHERE id=?", (fresh,)).fetchone()[0] == "园艺新记录"
        finally:
            release.set()
        result = future.result(timeout=5)
    assert old in [m['id'] for m in (result if isinstance(result, list) else result['memories'])]
    assert fresh not in [m['id'] for m in (result if isinstance(result, list) else result['memories'])]


def test_vector_snapshot_is_stable_after_update_delete_and_append(store):
    first = put(store, "先前事实", vector=[1., 0.])
    index = store.vector_index("fake-vector")
    snap = index.snapshot()
    with store.write() as conn:
        conn.execute("UPDATE memories SET embedding=?, revision=revision+1 WHERE id=?", (np.array([0.,1.],dtype=np.float32).tobytes(),first))
    added = put(store, "新增事实", vector=[1., 0.])
    assert snap.scores([1.,0.]) == {first:(1.,1)}
    assert index.scores([1.,0.])[first] == (0.,2)
    assert added in index.scores([1.,0.]) and added not in snap.scores([1.,0.])
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='deleted' WHERE id=?", (first,))
    assert first not in index.scores([1.,0.]) and first in snap.scores([1.,0.])
