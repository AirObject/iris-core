"""Merge-only write guard and permanently disabled conflict/body write paths."""
import json

import pytest

from iris.consolidation import UnsafeWrite, snapshot, validate_merge_body
from iris.memory_ops import delete_memory
from test_consolidation import Model, pair, run, merge_answer
from test_lifecycle import Clock
from test_retrieval import put
from conftest import msg


@pytest.mark.parametrize('body,source,reason', [
    ('她每周教口琴','她每周教口琴','gendered_pronoun'),
    ('他每周教口琴','他每周教口琴','gendered_pronoun'),
    ('P1每周教口琴','每周教口琴','participant_reference'),
    ('我每周教口琴，见M1','我每周教口琴','numbered_reference'),
    ('我每周教口琴，见消息1','我每周教口琴','numbered_reference'),
    ('wrong_subject每周教口琴','我每周教口琴','unsupported_identifier'),
    ('我每周教2次口琴','我每周教1次口琴','claim_sequences'),
    ('我不教口琴','我教口琴','claim_sequences'),
    ('我教15小时口琴','我教1.5小时口琴','claim_sequences'),
    ('I do teach flute','I do not teach flute','claim_sequences'),
])
def test_merge_guard_checks_identity_and_forbids_numeric_or_negation_rewrites(store,body,source,reason):
    sid=msg(store,1,source)
    mid=put(store,source if reason=='claim_sequences' else body,evidence=[sid])
    with store.read() as conn:
        m=snapshot(conn,mid)
    with pytest.raises(UnsafeWrite,match=reason):
        validate_merge_body({**m,'content':body},{'memories':[m]},[sid])


@pytest.mark.parametrize('body,source,entry',[
    ('我参加吉他维修课','我参加吉他维修课','A'),
    ('我用 HTTP POST 提交','我用 HTTP POST 提交','A'),
    ('我在 poem-live 讲诗歌','我讲诗歌','poem-live'),
    ('我在 stage-16 讲诗歌','我讲诗歌','stage-16'),
    ('我在 M12-live 讲诗歌','我讲诗歌','M12-live'),
    ('我在 P1 讲诗歌','我讲诗歌','P1'),
    ('我用 keep_id 标注','我用 keep_id 标注','A'),
    ('I teach flute','I teach flute','A'),
])
def test_protocol_words_legal_entry_ids_and_guitar_are_not_false_positives(store,body,source,entry):
    sid=msg(store,1,source,entry=entry)
    mid=put(store,body,evidence=[sid])
    with store.read() as conn:
        m=snapshot(conn,mid)
    validate_merge_body(m,{'memories':[m]},[sid])


def test_merge_report_protocol_words_do_not_block_safe_body(store):
    a,b=pair(store)
    def answer(p):
        value=merge_answer(p)
        value['reason']='keep_id 保留完整正文，来源 entry_id=A，同一事实；derived_from 不变。'
        return value
    _,_,report=run(store,Model(store,answer))
    assert report['summary']['merged']['count']==1


def test_unsafe_merge_is_one_completed_failure_without_retry_or_partial_source_union(store):
    sid=msg(store,1,'我每周教口琴')
    a=put(store,'她每周教口琴',evidence=[sid],importance=80)
    b=put(store,'她固定每周教口琴课',evidence=[sid],importance=80)
    model=Model(store,merge_answer)
    _,_,report=run(store,model)
    assert len(model.requests)==1 and report['summary']['failed']['count']==1
    assert report['consolidation']['deferred']==0
    with store.read() as conn:
        assert not snapshot(conn,a)['merged_into'] and not snapshot(conn,b)['merged_into']
        assert not conn.execute("SELECT 1 FROM memory_revisions WHERE actor='consolidation'").fetchone()
    run(store,model)
    assert len(model.requests)==1


@pytest.mark.parametrize('old_policy',['original_v1','rewrite_v2','conservative_v2','report_only_v1'])
@pytest.mark.parametrize('field,value',[('content','她错误改写正文'),('belief',1)])
def test_old_settings_cannot_enable_content_or_belief_paths(store,old_policy,field,value):
    store.set_setting('consolidation',{'resolution':old_policy})
    a,b=pair(store)
    answer={'decision':'conflict','reason':'当前说法有争议','evidence':[1,2],
            'updates':[{'id':a,'annotation':'具体争议',field:value}]}
    model=Model(store,answer)
    _,_,report=run(store,model)
    assert len(model.requests)==2  # Original broad classification, then report-only check.
    assert report['summary']['failed']['count']==1 and report['consolidation']['deferred']==0
    with store.read() as conn:
        assert snapshot(conn,a)['content']=='我每周教口琴' and snapshot(conn,a)['belief']==70
        assert not snapshot(conn,a)['annotations']
        assert json.loads(conn.execute('SELECT settings_json FROM consolidation_runs').fetchone()[0])['resolution']=='report_only_v1'


@pytest.mark.parametrize('field,value',[('evidence',[99999]),('id',99999),('subject_ids',['unknown_subject'])])
def test_unknown_material_ids_are_single_failures(store,field,value):
    a,b=pair(store)
    answer={'decision':'conflict','reason':'摘要需要核对','evidence':[1,2],
            'updates':[{'id':a,'annotation':'依据不足'}]}
    if field=='evidence': answer[field]=value
    else: answer['updates'][0][field]=value
    model=Model(store,answer)
    _,_,report=run(store,model)
    assert len(model.requests)==1 and report['summary']['failed']['count']==1
    with store.read() as conn:
        assert not snapshot(conn,a)['annotations']


@pytest.mark.parametrize('field,value',[('content','完全不同的事实'),('belief',1)])
def test_dependency_rejects_all_body_or_belief_updates(store,field,value):
    a,b=pair(store)
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",(a,b,Clock()().isoformat()))
    delete_memory(store,b,1)
    model=Model(store,{'decision':'review','reason':'唯一依据已删除，不能再支持推断','evidence':[],
                       'updates':[{'id':a,'annotation':'依据不足',field:value}]})
    _,_,report=run(store,model)
    assert len(model.requests)==1 and report['summary']['failed']['count']==1
    with store.read() as conn:
        assert snapshot(conn,a)['content']=='我每周教口琴' and snapshot(conn,a)['belief']==70


def test_merge_cannot_rewrite_body_even_when_claim_sequences_match(store):
    a,b=pair(store)
    with store.read() as conn: m=snapshot(conn,a)
    with pytest.raises(UnsafeWrite,match='merge_body_rewrite'):
        validate_merge_body({**m,'content':'我每周教口琴，也教吉他'},{'memories':[m]},[1])


def test_whole_source_extra_negation_does_not_reject_unchanged_body(store):
    sid=msg(store,1,'我是2019年开始学双簧管的，到现在没换过乐器。')
    mid=put(store,'我从2019年开始学双簧管',evidence=[sid])
    with store.read() as conn: m=snapshot(conn,mid)
    validate_merge_body(m,{'memories':[m]},[sid])
