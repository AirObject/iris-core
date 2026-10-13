"""RS02–07/09/11/12/15/16: the real learning engine, deterministic models only."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from conftest import FakeGateway, learning_output, msg
from iris import learning
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.memory_ops import edit_memory
from iris.models import Gateway, ModelConfig, ModelError
from iris.queue import form_batch, get_batch, next_learning_batch, reset_batch
from test_batches import memory
from test_models import response
from test_refusal_split_queue import finish, split


def freeze(store, texts, *, future=0):
    for i, text in enumerate(texts, 1):
        msg(store, i, text)
    return form_batch(store, 'A', PROMPT_VERSION, target_count=len(texts)-future, future_count=future)


def target_numbers(material):
    target = material.split('—— 目标段（只从这里学习） ——')[1].split('—— 后续段')[0]
    return [int(n) for n in re.findall(r'^#(\d+) ', target, re.M)]


class MarkerGateway(FakeGateway):
    def __init__(self, store, markers=('触发拒绝',), *, both=False, content=None):
        super().__init__({})
        self.store, self.markers, self.both, self.content = store, markers, both, content

    def json_chat(self, messages, purpose, max_tokens=3500, *, batch_id=None):
        assert not self.store._writer.in_transaction
        material = messages[-1]['content']
        self.materials.append(material)
        self.requests.append({'batch_id': batch_id, 'messages': messages, 'purpose': purpose})
        hits = [marker in material for marker in self.markers]
        if (all(hits) if self.both else any(hits)):
            raise ModelError('content_rejection', 'fake safety refusal', 'refused raw')
        items = [memory(self.content or f'小林记录第{n}条事实', [n]) for n in target_numbers(material)]
        value = learning_output({'memories': items})
        return value, json.dumps(value, ensure_ascii=False), None, 'direct'


def drain(store, engine):
    visited = []
    for _ in range(80):
        with store.read() as conn:
            head = next_learning_batch(conn, 'A')
        if head is None:
            return visited
        visited.append((head['id'], head['split_path']))
        engine.run_batch(head['id'], force=True)
    pytest.fail('split tree did not terminate')


def rows(store, sql, params=()):
    with store.read() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def test_rs02_one_refusal_leaves_only_that_message_gap_and_frozen_requests(store):
    root = freeze(store, ['散步', '阅读', '触发拒绝', '喝茶', '画画', '唱歌', '跑步', '看树'])
    msg(store, 9, '冻结以后到达的内容')
    fake = MarkerGateway(store)
    engine = LearningEngine(store, fake)
    assert engine.run_batch(root.id)['state'] == 'split'
    assert not rows(store, 'SELECT * FROM memories')
    drain(store, engine)
    leaves = rows(store, "SELECT * FROM batches WHERE state!='split'")
    assert [json.loads(b['target_ids']) for b in leaves if b['state']=='refused'] == [[root.target_ids[2]]]
    assert [g['batch_id'] for g in rows(store, 'SELECT * FROM memory_gaps')] == [b['id'] for b in leaves if b['state']=='refused']
    assert all('冻结以后到达的内容' not in m for m in fake.materials)
    assert all(b['state'] in ('succeeded', 'refused') for b in leaves)
    assert all(a['prompt_version'] == PROMPT_VERSION for a in rows(store, 'SELECT * FROM batch_attempts'))
    assert not rows(store, "SELECT * FROM batch_memory_effects e JOIN batches b ON b.id=e.first_batch_id WHERE b.state!='succeeded'")


@pytest.mark.parametrize('texts,markers,both,failed', [
    (['甲拒绝', '安全甲', '安全乙', '乙拒绝'], ('甲拒绝','乙拒绝'), False, [0,3]),
    (['安全甲', '甲拒绝', '乙拒绝', '安全乙'], ('甲拒绝','乙拒绝'), False, [1,2]),
    (['拒绝甲', '拒绝乙', '拒绝丙'], ('拒绝',), False, [0,1,2]),
    (['组合甲', '组合乙'], ('组合甲','组合乙'), True, []),
])
def test_rs03_multiple_all_and_combination_refusals(store, texts, markers, both, failed):
    root = freeze(store, texts)
    drain(store, LearningEngine(store, MarkerGateway(store, markers, both=both)))
    refused = rows(store, "SELECT target_ids FROM batches WHERE state='refused' ORDER BY split_path")
    assert [json.loads(r['target_ids']) for r in refused] == [[root.target_ids[i]] for i in failed]
    assert len(rows(store, 'SELECT * FROM memory_gaps')) == len(failed)


@pytest.mark.parametrize('history', [True, False])
def test_rs04_optional_context_reduction_removes_participants_and_media_input(store, monkeypatch, history):
    if history:
        msg(store, 1, '触发拒绝', sender='上下文专属作者')
        prior = form_batch(store, 'A', PROMPT_VERSION, target_count=1)
        LearningEngine(store, FakeGateway({})).run_batch(prior.id)
        msg(store, 2, '目标事实')
        root = form_batch(store, 'A', PROMPT_VERSION, target_count=1)
    else:
        msg(store, 1, '目标事实')
        msg(store, 2, '触发拒绝', sender='上下文专属作者')
        root = form_batch(store, 'A', PROMPT_VERSION, target_count=1)
    prepared = []
    monkeypatch.setattr(learning, 'prepare_media', lambda store, gateway, ids, **kw: prepared.append(ids))
    fake = MarkerGateway(store)
    engine = LearningEngine(store, fake)
    assert engine.run_batch(root.id)['state'] == 'waiting'
    assert get_batch(store, root.id).context_mode == 'target_only'
    result = engine.run_batch(root.id)
    assert len(result['created']) == 1
    assert '上下文专属作者' not in fake.materials[-1]
    assert target_numbers(fake.materials[-1]) == [1]
    assert prepared[-1] == root.target_ids
    attempts = rows(store, 'SELECT * FROM batch_attempts WHERE batch_id=? ORDER BY number', (root.id,))
    assert [a['context_mode'] for a in attempts] == ['full','target_only']
    assert json.loads(attempts[0]['sent_context_ids']) == root.history_ids+root.future_ids
    assert json.loads(attempts[1]['sent_context_ids']) == []
    assert not rows(store, 'SELECT * FROM memory_gaps')


def test_rs04_target_quote_survives_reduction_and_is_still_refused(store):
    root = freeze(store, ['目标带引用', '安全前后文'], future=1)
    with store.write() as conn:
        sid = conn.execute('SELECT sender_subject_id FROM messages WHERE id=1').fetchone()[0]
        conn.execute("UPDATE messages SET quote_author_subject_id=?,quote_content='触发拒绝' WHERE id=1", (sid,))
    fake = MarkerGateway(store)
    drain(store, LearningEngine(store, fake))
    assert get_batch(store, root.id).state == 'refused'
    assert len(fake.materials) == 2 and all('触发拒绝' in m for m in fake.materials)
    assert not rows(store, 'SELECT * FROM memories')


def test_rs04_context_already_omitted_by_budget_does_not_get_equivalent_retry(store, monkeypatch):
    root = freeze(store, ['触发拒绝', '可选消息'], future=1)
    # Use the real material budget branch without a large synthetic database.
    original = learning.estimate_tokens
    monkeypatch.setattr(learning, 'estimate_tokens', lambda text: 10001 if '—— 目标段' in text else original(text))
    fake = MarkerGateway(store)
    assert LearningEngine(store, fake).run_batch(root.id)['state'] == 'refused'
    assert len(fake.materials) == 1
    assert json.loads(rows(store, 'SELECT sent_context_ids FROM batch_attempts')[0]['sent_context_ids']) == []


def test_rs05_context_only_evidence_dropped_but_target_plus_context_preserved(store):
    root = freeze(store, ['作为目标的回复', '被回复的前文'])
    left, right = split(store, root.id)
    fake = FakeGateway({'memories': [memory('仅有前文', [2]), memory('合法目标加前文', [1,2])]})
    result = LearningEngine(store, fake).run_batch(left)
    assert len(result['created']) == 1 and result['dropped'][0]['reason'] == 'no target-segment evidence'
    assert {r['message_id'] for r in rows(store, 'SELECT message_id FROM sources')} == set(root.target_ids)
    assert get_batch(store, right).state == 'waiting'


@pytest.mark.parametrize('kind', ['filter','refusal','code','shape_repair','syntax_repair'])
def test_rs06_explicit_gateway_refusal_splits_once_without_partial_writes_or_extra_repair(store, kind):
    root = freeze(store, ['甲', '乙'])
    calls = []
    def handler(request):
        assert not store._writer.in_transaction
        calls.append(json.loads(request.content))
        if kind.endswith('repair') and len(calls) == 1:
            return httpx.Response(200, json=response('{}' if kind=='shape_repair' else 'not json'))
        if kind == 'code':
            return httpx.Response(400, json={'error': {'code': 'content_filter', 'message': 'blocked'}})
        body = response('', finish_reason='content_filter' if kind!='refusal' else 'stop')
        if kind == 'refusal':
            body['choices'][0]['message']['refusal'] = 'cannot comply'
        return httpx.Response(200, json=body)
    gateway = Gateway({'chat': ModelConfig('https://example.invalid', '', 'fake')}, store,
                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert LearningEngine(store, gateway).run_batch(root.id)['state'] == 'split'
    finally:
        gateway.close()
    assert len(calls) == (2 if kind.endswith('repair') else 1)
    assert not rows(store, 'SELECT * FROM memories') and not rows(store, 'SELECT * FROM goals')
    assert len(rows(store, 'SELECT * FROM batch_attempts')) == 1


@pytest.mark.parametrize('category,paused', [('item_error',False), ('retryable',False), ('invalid_output',False), ('retryable',True)])
def test_rs06_ordinary_errors_do_not_split_and_keep_attempt_budget(store, category, paused):
    root = freeze(store, ['甲', '乙'])
    engine = LearningEngine(store, FakeGateway(ModelError(category, 'ordinary failure', paused=paused)))
    assert engine.run_batch(root.id)['state'] == 'waiting'
    assert get_batch(store, root.id).attempt_count == (0 if paused else 1)
    assert len(rows(store, 'SELECT * FROM batches')) == 1


@pytest.mark.parametrize('stage', ['media','retrieval','embedding'])
def test_rs06_nonlearning_content_rejection_never_splits(store, monkeypatch, stage):
    root = freeze(store, ['甲', '乙'])
    fake = FakeGateway({'memories': [memory()]})
    engine = LearningEngine(store, fake)
    def refusal(*a, **kw):
        raise ModelError('content_rejection', 'nonlearning refusal')
    if stage == 'media':
        monkeypatch.setattr(learning, 'prepare_media', refusal)
    elif stage == 'retrieval':
        monkeypatch.setattr(engine, '_related', refusal)
    else:
        fake.configs['embedding'] = ModelConfig('fake', '', 'fake')
        monkeypatch.setattr(fake, 'embedding', refusal)
    result = engine.run_batch(root.id)
    assert get_batch(store, root.id).state == ('succeeded' if stage=='embedding' else 'waiting')
    assert len(rows(store, 'SELECT * FROM batches')) == 1


@pytest.mark.parametrize('singleton', [False, True])
def test_rs07_fourth_attempt_refusal_splits_multi_but_does_not_give_single_fifth(store, singleton):
    root = freeze(store, ['甲', '乙'], future=int(singleton))
    fake = FakeGateway(ModelError('retryable', 'timeout'))
    engine = LearningEngine(store, fake)
    for _ in range(3):
        assert engine.run_batch(root.id, force=True)['state'] == 'waiting'
    fake.response = ModelError('content_rejection', 'blocked')
    assert engine.run_batch(root.id, force=True)['state'] == ('refused' if singleton else 'split')
    assert get_batch(store, root.id).attempt_count == 4
    if singleton:
        assert get_batch(store, root.id).context_mode == 'full'
    else:
        assert [b['attempt_count'] for b in rows(store, 'SELECT attempt_count FROM batches WHERE parent_batch_id=?', (root.id,))] == [0,0]


def test_rs09_real_engine_claim_cannot_skip_dfs_or_run_relearn_beside_newer_leaf(store):
    root = freeze(store, ['甲','乙','丙','丁'])
    left, right = split(store, root.id)
    first, second = split(store, left)
    entered, release = Event(), Event()
    def hook(request):
        entered.set()
        assert release.wait(5)
    fake = FakeGateway({}, hook)
    engine = LearningEngine(store, fake)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(engine.run_batch, first)
        try:
            assert entered.wait(5)
            for bid in (first, second, right):
                with pytest.raises(ValueError):
                    engine.run_batch(bid, force=True)
            assert len(fake.requests) == 1
        finally:
            release.set()
        future.result(timeout=5)
    finish(store, first, 'refused')
    # Simulate a later leaf already running when an older failed leaf is reset.
    from iris.queue import claim_batch
    claim_batch(store, second)
    reset_batch(store, first)
    with pytest.raises(ValueError):
        engine.run_batch(first, force=True)


def test_rs11_context_mode_survives_recovery_and_rs12_relearn_preserves_history(store):
    root = freeze(store, ['触发拒绝', '上下文'], future=1)
    fake = MarkerGateway(store)
    engine = LearningEngine(store, fake)
    assert engine.run_batch(root.id)['state'] == 'waiting'
    from iris.queue import claim_batch
    claim_batch(store, root.id)
    store.recover_inflight()
    assert get_batch(store, root.id).context_mode == 'target_only'
    assert engine.run_batch(root.id, force=True)['state'] == 'refused'
    reset_batch(store, root.id)
    assert rows(store, 'SELECT * FROM memory_gaps')
    LearningEngine(store, FakeGateway({})).run_batch(root.id)
    assert not rows(store, 'SELECT * FROM memory_gaps')
    attempts = rows(store, 'SELECT number,context_mode FROM batch_attempts ORDER BY number')
    assert [a['number'] for a in attempts] == [1,2,3,4]
    assert [a['context_mode'] for a in attempts] == ['full','target_only','target_only','target_only']


def test_rs15_created_once_per_root_sources_still_join_and_next_root_confirms(store):
    root = freeze(store, ['小林喜欢猫','小林喜欢猫'])
    left, right = split(store, root.id)
    fake = MarkerGateway(store, content='小林喜欢猫')
    engine = LearningEngine(store, fake)
    mid = engine.run_batch(left)['created'][0]
    initial = rows(store, 'SELECT retention FROM memories')[0]['retention']
    assert engine.run_batch(right)['confirmed'] == []
    assert rows(store, 'SELECT retention FROM memories')[0]['retention'] == initial
    assert {r['message_id'] for r in rows(store, 'SELECT message_id FROM sources')} == set(root.target_ids)
    assert rows(store, 'SELECT effect FROM batch_memory_effects') == [{'effect':'created'}]
    msg(store, 3, '小林喜欢猫')
    next_root = form_batch(store, 'A', PROMPT_VERSION)
    assert engine.run_batch(next_root.id)['confirmed'] == [mid]
    assert rows(store, 'SELECT retention FROM memories')[0]['retention'] == initial+5


def seed_then_split(store):
    msg(store, 1, '小林喜欢猫')
    seed = form_batch(store, 'A', PROMPT_VERSION)
    mid = LearningEngine(store, FakeGateway({'memories':[memory()]})).run_batch(seed.id)['created'][0]
    msg(store, 2, '小林喜欢猫')
    msg(store, 3, '小林喜欢猫')
    root = form_batch(store, 'A', PROMPT_VERSION, target_count=2)
    return mid, root, split(store, root.id)


def test_rs15_explicit_confirmation_and_dedupe_share_root_effect(store):
    mid, root, (left,right) = seed_then_split(store)
    engine = LearningEngine(store, FakeGateway({'updates':[{'ref':'M1','action':'确认','evidence':[2]}]}))
    assert engine.run_batch(left)['confirmed'] == [mid]
    before = rows(store, 'SELECT retention FROM memories')[0]['retention']
    engine.gateway.response = {'memories':[memory(evidence=[3])]}
    assert engine.run_batch(right)['confirmed'] == []
    assert rows(store, 'SELECT retention FROM memories')[0]['retention'] == before
    assert rows(store, 'SELECT effect FROM batch_memory_effects') == [{'effect':'confirmed'}]


def test_rs15_apply_failure_rolls_back_effect_then_recovery_can_confirm(store):
    mid, root, (left,right) = seed_then_split(store)
    engine = LearningEngine(store, FakeGateway({'memories':[memory(evidence=[2])]}))
    with store.write() as conn:
        conn.execute("CREATE TEMP TRIGGER fail_attempt BEFORE INSERT ON batch_attempts BEGIN SELECT RAISE(ABORT,'fail commit'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        engine.run_batch(left)
    assert not rows(store, 'SELECT * FROM batch_memory_effects')
    assert rows(store, 'SELECT retention FROM memories')[0]['retention'] == 54
    with store.write() as conn:
        conn.execute('DROP TRIGGER fail_attempt')
    store.recover_inflight()
    assert engine.run_batch(left, force=True)['confirmed'] == [mid]


@pytest.mark.parametrize('inflight', [False,True])
def test_rs16_manual_body_protection_and_revision_check_precede_effect(store, inflight):
    mid, root, (left,right) = seed_then_split(store)
    LearningEngine(store, FakeGateway({})).run_batch(left)
    def edit(request=None):
        assert edit_memory(store, mid, 1, content='手写保留的正文')
    if not inflight:
        edit()
    fake = FakeGateway({'updates':[{'ref':'M1','action':'修正','content':'覆盖手写','belief':10,'evidence':[3]}]}, edit if inflight else None)
    engine = LearningEngine(store, fake)
    # Keep the same retrieval snapshot as a real in-flight caller, independent
    # of whether the manual wording still matches the query lexically.
    original = rows(store, 'SELECT * FROM memories WHERE id=?', (mid,))[0]
    original.update(speaker_name='小林', about=[{'id':s['subject_id'],'name':'小林'} for s in rows(store,'SELECT subject_id FROM memory_subjects WHERE memory_id=?',(mid,))])
    engine._related = lambda b,s: [original]
    result = engine.run_batch(right)
    assert result['dropped'][0]['reason'] == ('memory revision changed' if inflight else 'memory manually edited')
    assert rows(store, 'SELECT content,revision FROM memories') == [{'content':'手写保留的正文','revision':2}]
    assert not rows(store, 'SELECT * FROM batch_memory_effects')


def test_rs12_relearn_failed_sibling_does_not_repeat_success_or_root_increment(store):
    mid, root, (left,right) = seed_then_split(store)
    engine = LearningEngine(store, FakeGateway({'memories':[memory(evidence=[2])]}))
    engine.run_batch(left)
    before = rows(store, 'SELECT retention FROM memories')[0]['retention']
    engine.gateway.response = ModelError('content_rejection', 'blocked')
    drain(store, engine)
    reset_batch(store, right)
    assert get_batch(store, right).context_mode == 'target_only'
    assert rows(store, 'SELECT * FROM memory_gaps')
    engine.gateway.response = {'memories':[memory(evidence=[1])]}
    assert engine.run_batch(right)['confirmed'] == []
    assert rows(store, 'SELECT retention FROM memories')[0]['retention'] == before
    assert len(rows(store, 'SELECT * FROM batch_attempts WHERE batch_id=?', (left,))) == 1
    assert not rows(store, 'SELECT * FROM memory_gaps')


def test_rs15_forgotten_confirmation_restores_once_with_revision_unchanged(store):
    mid, root, (left,right) = seed_then_split(store)
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten',retention=31 WHERE id=?", (mid,))
    engine = LearningEngine(store, MarkerGateway(store, content='小林喜欢猫'))
    engine.run_batch(left)
    engine.run_batch(right)
    assert rows(store, 'SELECT lifecycle,retention,revision FROM memories') == [
        {'lifecycle':'active','retention':36,'revision':1}]


@pytest.mark.parametrize('a,b', [('小林有两只猫','小林有三只猫'), ('小林喜欢猫','小林不喜欢猫')])
def test_rs15_distinct_numbers_and_negation_still_create_distinct_memories(store, a, b):
    root = freeze(store, [a,b])
    left, right = split(store, root.id)
    engine = LearningEngine(store, FakeGateway({'memories':[memory(a,[1])]}))
    first = engine.run_batch(left)['created']
    engine.gateway.response = {'memories':[memory(b,[2])]}
    second = engine.run_batch(right)['created']
    assert len(first) == len(second) == 1 and first != second
    assert [r['effect'] for r in rows(store, 'SELECT effect FROM batch_memory_effects')] == ['created','created']


def test_rs15_inflight_confirmation_revision_conflict_does_not_consume_root_effect(store):
    mid, root, (left,right) = seed_then_split(store)
    fake = FakeGateway({'updates':[{'ref':'M1','action':'确认','evidence':[2]}]},
                       hook=lambda request: edit_memory(store, mid, 1, content='小林喜欢长毛猫'))
    result = LearningEngine(store, fake).run_batch(left)
    assert result['dropped'][0]['reason'] == 'memory revision changed'
    assert not rows(store, 'SELECT * FROM batch_memory_effects')
    fake.hook = None
    fake.response = {'updates':[{'ref':'M1','action':'确认','evidence':[3]}]}
    assert LearningEngine(store, fake).run_batch(right)['confirmed'] == [mid]
    assert rows(store, 'SELECT revision,retention FROM memories') == [{'revision':2,'retention':59}]


def test_rs16_metadata_revision_does_not_remove_protection_and_confirmation_is_allowed(store):
    from test_learning_manual_protection import record_revision
    mid, root, (left,right) = seed_then_split(store)
    assert edit_memory(store, mid, 1, content='小林喜欢长毛猫')
    record_revision(store, mid, actor='learning', reason='belief only', belief=75)
    fake = FakeGateway({'updates':[
        {'ref':'M1','action':'修正','content':'错误覆盖','belief':10,'event_time':'2026-10-14','evidence':[2]},
        {'ref':'M1','action':'确认','evidence':[2]}]})
    result = LearningEngine(store, fake).run_batch(left)
    assert result['dropped'][0]['reason'] == 'memory manually edited'
    assert result['confirmed'] == [mid]
    fake.response = {'updates':[{'ref':'M1','action':'确认','evidence':[3]}]}
    assert LearningEngine(store, fake).run_batch(right)['confirmed'] == []
    assert rows(store, 'SELECT content,belief,revision,retention,event_time FROM memories') == [
        {'content':'小林喜欢长毛猫','belief':75,'revision':3,'retention':59,'event_time':None}]
