"""Profile storage must not contaminate existing readers or leak hard scope."""
from datetime import datetime, timezone

import pytest

from iris.api import Search
from iris import admin_data, person_summary, persona
from iris.consolidation import snapshot, merge_exclusion
from iris.memory_ops import write_entry_profile_memory, Visibility, purge_memory, entry_profile_source
from iris.people import list_people, person_detail, add_alias
from iris.query_analysis import SubjectNames
from iris.retrieval import Retrieval
from conftest import msg


def profile(store, entry='A', content='小林常讨论背包', category='topic'):
    msg(store,1,'介绍入口',entry=entry)
    return write_entry_profile_memory(store,entry,op='new',category=category,content=content,manual=True)['memory_id']


def test_people_names_and_summaries_do_not_treat_entry_as_person(store):
    mid=profile(store)
    with store.read() as conn:
        sid=conn.execute("SELECT subject_id FROM entry_subjects WHERE entry_id='A'").fetchone()[0]
        assert sid not in SubjectNames(conn).names
        assert sid not in person_summary._SummaryNames(conn).names
    assert sid not in {p['id'] for p in admin_data.catalog(store)['people']}
    assert sid not in {p['id'] for p in list_people(store)['items']}
    with pytest.raises(KeyError):
        person_detail(store,sid)
    with pytest.raises((KeyError,ValueError)):
        add_alias(store,sid,'新称呼',expected_revision=1)
    with pytest.raises(KeyError):
        person_summary.summary_settings(store,sid)


def test_ranked_search_prepare_learning_persona_and_person_summary_exclude_profiles(store):
    mid=profile(store)
    retrieval=Retrieval(store)
    assert mid not in {m['id'] for m in retrieval.search(text='',entry_id='A')['memories']}
    assert mid not in {m['id'] for m in retrieval.prepare('A',text='',judge=False)['memories']}
    assert mid not in {m['id'] for m in retrieval.learning_context('小林背包',[],entry_id='A')}
    with store.read() as conn:
        person=conn.execute("SELECT id FROM subjects WHERE kind='person' AND name='小林'").fetchone()[0]
        assert not person_summary._eligible(conn,person,datetime(2030,1,1,tzinfo=timezone.utc))
        assert not persona._select(conn)['memories']
    assert [m['id'] for m in retrieval.search(entry_id='A',kinds=['入口画像'])['memories']]==[mid]
    with pytest.raises(ValueError,match='entry'):
        retrieval.search(kinds=['入口画像'])
    with pytest.raises(ValueError):
        Search(kinds=['入口画像'])
    assert Search(kinds=['入口画像'],entry_id='A').entry_id=='A'
    msg(store,1,'别处',entry='B')
    assert not retrieval.search(entry_id='B',kinds=['入口画像'])['memories']


def test_consolidation_does_not_cross_kind_entry_or_category(store):
    a=profile(store)
    b=profile(store,'B')
    c=write_entry_profile_memory(store,'A',op='new',category='rule',content='群规',manual=True)['memory_id']
    with store.read() as conn:
        sa,sb,sc=[snapshot(conn,i) for i in (a,b,c)]
    assert merge_exclusion(sa,sb)
    assert merge_exclusion(sa,sc)
    ordinary={**sa,'type':'事实'}
    assert merge_exclusion(sa,ordinary)=='profile_kind'


def test_purge_clears_derived_profile_body_and_revisions(store):
    original=msg(store,1,'唯一的原始材料')
    stamp='2026-10-13T00:00:00+00:00'
    with store.write() as conn:
        base=conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
            created_at,updated_at,first_confirmed_at,last_confirmed_at) VALUES('原始事实','事实','self','亲历',80,50,50,?,?,?,?)''',(stamp,)*4).lastrowid
        conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",(base,original,stamp))
        source=entry_profile_source(conn,original,source_memory_id=base,source_revision=1)
    child=write_entry_profile_memory(store,'A',op='new',category='rule',content='派生的画像',sources=[source])['memory_id']
    from iris.memory_ops import edit_memory
    assert edit_memory(store,child,1,content='人工改过的派生画像')
    assert purge_memory(store,base,1,confirm=True)
    with store.read() as conn:
        value=conn.execute('SELECT content,purged_at FROM memories WHERE id=?',(child,)).fetchone()
        assert value['content']=='' and value['purged_at']
        assert not conn.execute('SELECT 1 FROM memory_revisions WHERE memory_id=?',(child,)).fetchone()
        assert not conn.execute('SELECT 1 FROM entry_profile_evidence_receipts WHERE memory_id=?',(child,)).fetchone()


def test_vector_profiles_cannot_set_relative_cutoff_or_steal_learning_slots(store):
    import numpy as np
    from test_retrieval import put, Embeddings
    from iris.learning_retrieval import LearningRetrieval
    mid=profile(store,content='完全不同的入口主题')
    ordinary=put(store,'另一条普通事实',vector=[.7,(1-.49)**.5])
    with store.write() as conn:
        conn.execute("UPDATE memories SET embedding=?,embedding_model='fake-vector' WHERE id=?",(np.asarray([1.,0.],dtype=np.float32).tobytes(),mid))
    retrieval=Retrieval(store,Embeddings(store),vector_min=.35,vector_relative=.75)
    assert [m['id'] for m in retrieval.search(text='无关检索词',entry_id='A')['memories']]==[ordinary]
    store.set_setting('learning_retrieval',{'embedding_model':'fake-vector','embedding_dimensions':2})
    learning=LearningRetrieval(store,Embeddings(store))
    assert [m['id'] for m in learning.context('无关检索词',[],15,entry_id='A')]==[ordinary]


def test_profile_purge_redacts_projected_versions_raw_outputs_and_late_writes(store):
    from test_purge_raw_outputs import seed_derivatives, PROSE
    from iris.db import dumps
    source=msg(store,1,PROSE)
    with store.read() as conn:
        evidence=entry_profile_source(conn,source)
    mid=write_entry_profile_memory(store,'A',op='new',category='rule',content=PROSE,sources=[evidence])['memory_id']
    seed_derivatives(store,mid,source)
    purge_memory(store,mid,1,confirm=True)
    with store.write() as conn:
        conn.execute('UPDATE entry_profile_attempts SET outputs_json=?',(dumps({'generate':PROSE}),))
    with store.read() as conn:
        for table in ('entry_profile_versions','entry_profile_attempts','consolidation_work','consolidation_calls','memory_revisions'):
            assert PROSE not in dumps([dict(r) for r in conn.execute(f'SELECT * FROM {table}')]),table
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_compatible_profile_merge_preserves_receipts_and_purge_cascade(store):
    from test_consolidation import Model, merge_answer, run
    from iris.memory_ops import entry_profile_source
    from iris.db import now
    a=msg(store,1,'本群不允许广告')
    b=msg(store,2,'不要在本群发布广告')
    with store.read() as conn:
        sources=[entry_profile_source(conn,i) for i in (a,b)]
    ids=[write_entry_profile_memory(store,'A',op='new',category='rule',content=text,sources=[evidence])['memory_id']
         for text,evidence in zip(('本群禁止广告','本群禁止发布广告'),sources)]
    _,_,report=run(store,Model(store,merge_answer))
    with store.read() as conn:
        rows=[dict(conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()) for mid in ids]
        lost=next(r for r in rows if r['merged_into'])
        kept=next(r for r in rows if not r['merged_into'])
        assert Visibility(conn).memory(kept['id'])==frozenset({'A'})
        assert conn.execute('SELECT COUNT(*) FROM entry_profile_revision_sources WHERE memory_id=? AND revision=?',(kept['id'],kept['revision'])).fetchone()[0]==2
    replay=write_entry_profile_memory(store,'A',op='confirm',memory_id=kept['id'],expected_revision=kept['revision'],sources=sources)
    assert not replay['confirmed']
    purge_memory(store,lost['id'],lost['revision'],confirm=True)
    with store.read() as conn:
        assert conn.execute('SELECT content FROM memories WHERE id=?',(kept['id'],)).fetchone()[0]==''


def test_bulk_purge_cascade_does_not_conflict_with_a_later_chunk(store,monkeypatch):
    from iris import memory_ops
    from test_retrieval import put
    a=msg(store,1,'批量清除的原始依据')
    base=put(store,'原始事实',evidence=[a],entry='A')
    independent=put(store,'无依赖的普通事实',entry='A',evidence=[a])
    with store.read() as conn:
        evidence=entry_profile_source(conn,a,source_memory_id=base,source_revision=1)
    child=write_entry_profile_memory(store,'A',op='new',category='rule',content='依赖该事实的画像',sources=[evidence])['memory_id']
    monkeypatch.setattr(memory_ops,'PURGE_CHUNK_SIZE',1)
    preview=admin_data.bulk_memory_preview(store,entry_id='A',action='purge')
    result=memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)
    assert result['status']=='completed'
    assert result['memory_ids']==[base,independent,child]
    assert result['derived_profile_ids']==[child]
    with store.read() as conn:
        assert all(r[0] for r in conn.execute('SELECT purged_at FROM memories'))
