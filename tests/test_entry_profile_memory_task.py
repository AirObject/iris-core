"""EF-03 memory task contracts; hand-written fixtures, no real model calls."""
import copy
import json
import sys
from pathlib import Path
from dataclasses import replace

import pytest

from iris.entry_profile import (
    EntryProfileMemoryTask, MemoryProfileSnapshot, MemoryProfileLimits,
    MemoryProfileUnavailable, select_memory_profile_material,
)
from iris.models import ModelReply
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.entry_profile_eval.memory_runner import SimulationRepository


def message(ref, text, at, speaker='a', **extra):
    return dict(ref=ref, position=int(ref[1:]), entry_id='g', kind='message',
                speaker=speaker, content=text, occurred_at=at, **extra)


def snapshot(**changes):
    base = MemoryProfileSnapshot(
        entry_id='g', entry_subject_id='entry:g', entry_kind='group', timezone='Asia/Shanghai',
        as_of='2026-10-13T12:00:00+08:00', settings_revision=1, cursor=0,
        scan_through=2, has_more=False,
        messages=(message('s1', '徒步背包的重量和承重怎么取舍？', '2025-05-01T10:00:00+08:00'),
                  message('s2', '轻量徒步装备的耐用性也需要讨论。', '2026-10-01T11:00:00+08:00', 'b')),
    )
    return replace(base, **changes)


def source(ref, excerpt, **extra):
    return dict(ref=ref, field='content', excerpt=excerpt, via_memory=None, **extra)


def new_topic(**changes):
    return dict(op='new', category='topic', body='常讨论轻量徒步装备的取舍',
                saying='', meaning='', importance=55,
                sources=[source('s1', '徒步背包的重量和承重怎么取舍？'),
                         source('s2', '轻量徒步装备的耐用性也需要讨论。')], **changes)


def check(ops, **changes):
    return {'operations': [dict(index=i, supported=True, stable=True, independent=True,
        independent_from_previous=True, privacy_safe=True, meaning_supported=True,
        meaning_checks=[], evidence_groups=[[n] for n in range(len(op['sources']))],
        violations=[], reason='绑定原话支持稳定主题，来自独立讨论', **changes)
        for i, op in enumerate(ops, 1)]}


class FakeGateway:
    def __init__(self, repository, values, hook=None):
        self.repository, self.values, self.hook = repository, iter(values), hook
        self.requests = []

    def chat(self, messages, purpose, **kwargs):
        assert not self.repository.in_transaction
        self.requests.append((purpose, copy.deepcopy(messages), kwargs))
        if self.hook and len(self.requests) == 2:
            self.hook(self.repository)
        return ModelReply(json.dumps(next(self.values), ensure_ascii=False), 'stop', {})


def run(snap=None, operations=None, checked=None, hook=None, limits=None):
    repo = SimulationRepository(snap or snapshot())
    operations = [new_topic()] if operations is None else operations
    gateway = FakeGateway(repo, [{'operations': operations}, checked or check(operations)], hook)
    result = EntryProfileMemoryTask(repo, gateway, limits=limits).run('g')
    return repo, gateway, result


def test_new_topic_is_entry_memory_with_decay_cap_and_original_sources():
    op = new_topic(); op['importance'] = 95
    repo, gateway, result = run(operations=[op])
    row = repo.profiles[0]
    assert result['status'] == 'committed'
    assert row['kind'] == '入口画像' and row['about'] == ['entry:g']
    assert row['owner_entry_id'] == 'g' and row['event_time'] is None
    assert row['speaker'] == 'self' and row['stance'] == '推断'
    assert row['importance'] == 69 and row['pinned'] is False
    assert len(row['sources']) == 2 and repo.cursor == 2
    assert len(gateway.requests) == 2
    assert all('consolidation_entry_profile_' in x['purpose'] for x in result['calls'])
    assert len(result['prompt_sha256']['entry_profile_memory_generate_v1']) == 64


def test_missing_adapter_fails_closed():
    with pytest.raises(MemoryProfileUnavailable):
        EntryProfileMemoryTask(None, None).run('g')


def test_old_evidence_is_not_age_filtered_and_source_input_is_not_mutated():
    snap = snapshot(); before = copy.deepcopy(snap)
    material = select_memory_profile_material(snap)
    assert len(material['messages']) == 2 and material['cursor_after'] == 2
    assert snap == before


def test_no_new_material_does_not_call_or_confirm():
    repo, gateway, result = run(snapshot(cursor=2, messages=(), scan_through=2))
    assert result['status'] == 'no_new_evidence' and not gateway.requests


@pytest.mark.parametrize('replacement,reason', [
    ({'body': '她们常讨论徒步装备'}, 'gendered_pronoun'),
    ({'sources': [source('s1', '不存在的原话')]}, 'invalid_excerpt'),
    ({'sources': [source('s1', '徒步背包的重量和承重怎么取舍？')]*2}, 'insufficient_independent_evidence'),
])
def test_invalid_items_are_dropped_before_checker(replacement, reason):
    op = new_topic(); op.update(replacement)
    repo, gateway, result = run(operations=[op])
    assert not repo.profiles and len(gateway.requests) == 1
    assert result['removed'][0]['reason'] == reason


def test_word_internal_gender_characters_are_not_pronouns():
    op = new_topic(); op['body'] = '讨论吉他与其他乐器'
    _, gateway, _ = run(operations=[op])
    assert len(gateway.requests) == 2  # Semantic support remains the checker's responsibility.


@pytest.mark.parametrize('edit', ['quote', 'copy', 'registration', 'same_date'])
def test_false_recurrence_is_not_independent(edit):
    snap = snapshot(); rows = list(copy.deepcopy(snap.messages)); op = new_topic()
    if edit == 'quote':
        rows[1]['quote'] = {'ref': 's1', 'speaker': 'a', 'content': rows[0]['content']}
    elif edit == 'copy':
        rows[1]['content'] = rows[0]['content']
        op['sources'][1]['excerpt'] = rows[0]['content']
    elif edit == 'registration':
        rows[1]['content'] = '徒步活动报名接龙：算我一个'
        op['sources'][1]['excerpt'] = rows[1]['content']
    else:
        rows[1]['occurred_at'] = '2025-05-01T12:00:00+08:00'
    repo, _, result = run(replace(snap, messages=tuple(rows)), [op])
    assert not repo.profiles
    assert result['removed'][0]['reason'] == 'insufficient_independent_evidence'


@pytest.mark.parametrize('flag', ['stable', 'independent', 'privacy_safe', 'supported'])
def test_semantic_failure_drops_item_and_preserves_trace(flag):
    verdict = check([new_topic()]); verdict['operations'][0][flag] = False
    repo, _, result = run(checked=verdict)
    assert not repo.profiles and result['removed'][0]['stage'] == 'model'
    assert result['stages']['deterministic'] and not result['stages']['checked']


def test_invalid_checker_shape_does_not_commit_cursor():
    repo = SimulationRepository(snapshot())
    gateway = FakeGateway(repo, [{'operations': [new_topic()]}, {'operations': []}])
    result = EntryProfileMemoryTask(repo, gateway).run('g')
    assert result['status'] == 'failed' and repo.cursor == 0 and not repo.profiles


def test_source_changes_during_check_abort_all_writes():
    def change(repo):
        repo.messages[0]['content'] = '已被清除'
    repo, _, result = run(hook=change)
    assert result['status'] == 'conflict' and repo.cursor == 0 and not repo.profiles


def existing(repo):
    return copy.deepcopy(repo.profiles[0])


def later_snapshot(profile, text='徒步包的减重与承重取舍还可以从材质比较。', **changes):
    return snapshot(cursor=2, scan_through=3,
        messages=(message('s3', text, '2026-10-12T12:00:00+08:00', 'c'),),
        anchors=snapshot().messages, profiles=(profile,), **changes)


def test_confirm_needs_fresh_evidence_and_replay_does_not_inflate_retention():
    old = existing(run()[0]); snap = later_snapshot(old)
    op = dict(op='confirm', target_id=old['memory_id'], expected_revision=old['revision'],
              sources=[source('s3', snap.messages[0]['content'])])
    repo, gateway, result = run(snap, [op])
    assert repo.profiles[0]['retention'] == old['retention'] + 5
    assert repo.profiles[0]['revision'] == old['revision']
    assert len(repo.receipts) == 1
    second = EntryProfileMemoryTask(repo, gateway).run('g')
    assert second['status'] == 'no_new_evidence'
    assert repo.profiles[0]['retention'] == old['retention'] + 5
    assert result['receipt']['applied'][0]['op'] == 'confirm'


def test_quote_or_existing_source_cannot_confirm():
    old = existing(run()[0]); op = dict(op='confirm', target_id=old['memory_id'], expected_revision=1,
        sources=[source('s1', snapshot().messages[0]['content'])])
    repo, gateway, result = run(later_snapshot(old), [op])
    assert repo.profiles[0]['retention'] == old['retention'] and len(gateway.requests) == 1
    assert result['removed'][0]['reason'] == 'no_new_independent_evidence'


@pytest.mark.parametrize('opname', ['revise', 'retire'])
def test_protected_manual_rule_cannot_be_changed_or_suspended(opname):
    old = existing(run()[0]); old.update(author='admin', manual_protected=True, category='rule', body='群内禁发广告')
    snap = later_snapshot(old)
    op = dict(op=opname, target_id=old['memory_id'], expected_revision=1,
              sources=[source('s3', snap.messages[0]['content'])])
    if opname == 'revise':
        op.update(category='rule', body='群内允许广告', saying='', meaning='', importance=50)
    repo, _, result = run(snap, [op])
    assert repo.profiles == [old]
    assert result['removed'][0]['reason'] == 'protected_manual_conflict'


def test_manual_rule_can_receive_support_without_losing_protection():
    old = existing(run()[0]); old.update(author='admin', manual_protected=True, category='rule',
        body='群内禁发广告', sources=[], confirmed_evidence=[], importance=80)
    snap = later_snapshot(old, '再次说明本群长期禁止发布广告。')
    op = dict(op='confirm', target_id=old['memory_id'], expected_revision=1,
              sources=[source('s3', snap.messages[0]['content'])])
    repo, _, _ = run(snap, [op]); row = repo.profiles[0]
    assert row['manual_protected'] and row['body'] == old['body'] and row['importance'] == 80
    assert row['retention'] == old['retention'] + 5 and row['revision'] == 1


def test_new_candidate_checker_also_receives_protected_rules():
    old = existing(run()[0]); old.update(author='admin', manual_protected=True, category='rule', body='群内禁发广告')
    snap = later_snapshot(old, '以后可以发广告')
    op = dict(op='new', category='rule', body='群内允许广告', saying='', meaning='', importance=50,
              sources=[source('s3', snap.messages[0]['content'])])
    verdict = check([op]); verdict['operations'][0].update(supported=False, reason='protected_manual_conflict')
    repo, gateway, result = run(snap, [op], verdict)
    assert repo.profiles == [old]
    assert json.loads(gateway.requests[1][1][1]['content'])['protected_rules'][0]['body'] == old['body']
    assert result['removed'][0]['reason'] == 'protected_manual_conflict'


@pytest.mark.parametrize('field,value', [('revision', 2), ('lifecycle', 'deleted'), ('manual_protected', True)])
def test_target_race_aborts_cursor_and_operations(field, value):
    old = existing(run()[0]); snap = later_snapshot(old)
    op = dict(op='confirm', target_id=old['memory_id'], expected_revision=1,
              sources=[source('s3', snap.messages[0]['content'])])
    repo, _, result = run(snap, [op], hook=lambda r: r.profiles[0].update({field: value}))
    assert result['status'] == 'conflict' and repo.cursor == 2
    assert repo.profiles[0]['retention'] == old['retention']


def test_fact_memory_sources_expand_but_profile_self_evidence_is_excluded():
    msgs = snapshot().messages
    fact = dict(memory_id='f1', revision=2, kind='事实', body='本群多次讨论徒步装备取舍',
                lifecycle='active', visible=True, source_messages=list(msgs))
    snap = snapshot(messages=(msgs[1],), cursor=1, memories=(fact,))
    op = new_topic(); op['sources'][0]['via_memory'] = 'f1'
    repo, _, _ = run(snap, [op])
    assert repo.profiles[0]['sources'][0]['memory']['revision'] == 2
    fact['kind'] = '入口画像'
    repo, _, result = run(snap, [op])
    assert not repo.profiles and result['removed'][0]['reason'] == 'invalid_source_reference'


def test_cross_entry_fact_root_and_revision_changes_fail_closed():
    msgs = list(copy.deepcopy(snapshot().messages)); msgs[0]['entry_id'] = 'elsewhere'
    fact = dict(memory_id='f1', revision=1, kind='事实', body='讨论徒步', lifecycle='active',
                visible=True, source_messages=msgs)
    material = select_memory_profile_material(snapshot(memories=(fact,)))
    assert not material['memories']
    fact['source_messages'] = list(snapshot().messages)
    op = new_topic(); op['sources'][0]['via_memory'] = 'f1'
    repo, _, result = run(snapshot(memories=(fact,)), [op], hook=lambda r:r.memories[0].update(revision=2))
    assert result['status'] == 'conflict' and repo.cursor == 0


def saying_fixture(second_at, speaker='b', meaning=''):
    msgs = (message('s1', '这段先留一拍，再讨论。', '2026-10-01T10:00:01+08:00'),
            message('s2', '细节没齐，留一拍等资料。', second_at, speaker))
    op = dict(op='new', category='saying', body='留一拍'+('：'+meaning if meaning else ''),
              saying='留一拍', meaning=meaning, importance=50,
              sources=[source(m['ref'], m['content']) for m in msgs])
    return snapshot(messages=msgs), op


@pytest.mark.parametrize('at,speaker', [('2026-10-01T10:00:59+08:00','b'), ('2026-10-01T10:05:01+08:00','a')])
def test_saying_bursts_are_not_independent(at, speaker):
    snap, op = saying_fixture(at, speaker)
    repo, _, result = run(snap, [op])
    assert not repo.profiles and result['removed'][0]['reason'] == 'insufficient_independent_evidence'


def test_usage_without_meaning_is_allowed_and_meaning_needs_itemized_check():
    snap, op = saying_fixture('2026-10-10T11:00:00+08:00')
    assert run(snap, [op])[0].profiles
    snap, op = saying_fixture('2026-10-10T11:00:00+08:00', meaning='等资料齐了再讨论')
    repo, _, result = run(snap, [op])
    assert result['reason'] == 'missing_meaning_checks' and repo.cursor == 0
    verdict = check([op]); verdict['operations'][0]['meaning_checks'] = [dict(term='等待资料',source_indices=[1],supported=True)]
    assert run(snap, [op], verdict)[0].profiles
    verdict['operations'][0]['meaning_checks'][0]['supported'] = False
    assert not run(snap, [op], verdict)[0].profiles


def test_semantic_event_groups_cannot_claim_two_discussions_in_one_group():
    verdict = check([new_topic()]); verdict['operations'][0]['evidence_groups'] = [[0,1]]
    repo, _, result = run(checked=verdict)
    assert not repo.profiles and result['removed'][0]['stage'] == 'model'


def test_budget_prefix_resumes_with_original_anchor():
    from iris.queue import estimate_tokens
    from iris.db import dumps
    from iris.entry_profile import _memory_message
    snap = snapshot()
    cost = estimate_tokens(dumps(_memory_message(snap.messages[0], snap)))
    limits = MemoryProfileLimits(message_tokens=cost)
    repo, _, result = run(snap, [], limits=limits)
    assert repo.cursor == 1 and result['material']['has_more']
    material = select_memory_profile_material(repo.snapshot('g', MemoryProfileLimits()))
    assert material['cursor_after'] == 2 and {m['ref'] for m in material['messages']} == {'s1','s2'}


def test_cursor_cannot_jump_unseen_tail_or_reverse_order():
    with pytest.raises(MemoryProfileUnavailable, match='unexamined_cursor_tail'):
        select_memory_profile_material(snapshot(scan_through=3))
    with pytest.raises(MemoryProfileUnavailable, match='non_contiguous_snapshot'):
        select_memory_profile_material(snapshot(messages=tuple(reversed(snapshot().messages))))


def test_rules_can_have_one_explicit_source_and_are_reported_stance():
    snap = snapshot(messages=(message('s1','本群长期禁止发布广告。','2026-10-01T10:00:00+08:00'),),scan_through=1)
    op = dict(op='new', category='rule', body='群内禁止发布广告', saying='', meaning='', importance=60,
              sources=[source('s1',snap.messages[0]['content'])])
    repo,_,result = run(snap,[op])
    assert repo.profiles[0]['stance']=='转述' and result['quote_validation']=={'total':1,'matched':1}


def test_revise_replaces_current_evidence_and_keeps_revision_history():
    old=existing(run()[0]);old.update(category='rule',body='群内禁发广告')
    snap=later_snapshot(old,'本群新规：广告只限集中贴，旧的全面禁止规定撤销。')
    op=dict(op='revise',target_id=old['memory_id'],expected_revision=1,category='rule',body='广告只限集中贴',
            saying='',meaning='',importance=90,sources=[source('s3',snap.messages[0]['content'])])
    repo,_,result=run(snap,[op]);row=repo.profiles[0]
    assert result['status']=='committed' and row['revision']==2 and row['importance']==69
    assert row['history']==[{'revision':1,'body':'群内禁发广告'}]
    assert [s['ref'] for s in row['sources']]==['s3']


def test_retire_does_not_delete_or_resurrect_id():
    old=existing(run()[0]);old.update(category='rule',body='群内禁发广告')
    snap=later_snapshot(old,'本群取消禁止广告的旧规定。')
    op=dict(op='retire',target_id=old['memory_id'],expected_revision=1,sources=[source('s3',snap.messages[0]['content'])])
    repo,_,_=run(snap,[op]);assert repo.profiles[0]['lifecycle']=='forgotten'
    old['lifecycle']='deleted';repo,_,result=run(later_snapshot(old),[op])
    assert result['removed'][0]['reason']=='invalid_target_revision' and repo.profiles[0]['lifecycle']=='deleted'


def test_repeat_commit_receipt_is_idempotent_after_lost_response():
    class LostResponse(SimulationRepository):
        def commit(self,plan):
            self.plan=plan
            first=super().commit(plan)
            assert super().commit(plan)=={**first,'replayed':True}
            return first
    repo=LostResponse(snapshot());gateway=FakeGateway(repo,[{'operations':[new_topic()]},check([new_topic()])])
    result=EntryProfileMemoryTask(repo,gateway).run('g')
    assert result['status']=='committed' and len(repo.profiles)==1


@pytest.mark.parametrize('stage', ['generate','check'])
def test_model_error_keeps_cursor_and_records_sanitized_failure(stage):
    from iris.models import ModelError
    repo=SimulationRepository(snapshot())
    class Fails(FakeGateway):
        def chat(self,messages,purpose,**kwargs):
            if purpose.endswith(stage):raise ModelError('timeout','provider detail must not be recorded')
            return super().chat(messages,purpose,**kwargs)
    gateway=Fails(repo,[{'operations':[new_topic()]}])
    result=EntryProfileMemoryTask(repo,gateway).run('g')
    assert result['status']=='failed' and repo.cursor==0 and not repo.profiles
    assert result['calls'][-1]['result']=='timeout'
    assert 'provider detail' not in json.dumps(result)


def test_appending_unread_message_during_check_does_not_invalidate_snapshot():
    repo,_,result=run(hook=lambda r:r.messages.append(message('s3','新的讨论','2026-10-12T12:00:00+08:00')))
    assert result['status']=='committed' and repo.cursor==2
    assert repo.snapshot('g',MemoryProfileLimits()).scan_through==3


def test_quoted_speaker_identity_and_safe_excerpt_are_bound():
    msg=message('s1','引用管理员规定','2026-10-01T10:00:00+08:00',quote={'ref':'original','speaker':'admin','content':'本群禁止广告。邻居最近离婚。'})
    op=dict(op='new',category='rule',body='群内禁止广告',saying='',meaning='',importance=50,
            sources=[dict(ref='s1',field='quote',excerpt='本群禁止广告。',via_memory=None)])
    repo,_,_=run(snapshot(messages=(msg,),scan_through=1),[op])
    bound=repo.profiles[0]['sources'][0]
    assert bound['speaker']=='admin' and bound['evidence_identity']=='original' and '离婚' not in bound['excerpt']


def test_same_claim_new_operation_becomes_confirm_not_duplicate():
    old=existing(run()[0]); snap=later_snapshot(old)
    op=new_topic();op['sources'].append(source('s3',snap.messages[0]['content']))
    repo,_,result=run(snap,[op])
    assert len(repo.profiles)==1 and result['receipt']['applied'][0]['op']=='confirm'




def test_request_budget_and_timeout_cannot_be_exceeded():
    with pytest.raises(ValueError):
        MemoryProfileLimits(timeout_seconds=121)
    repo,gateway,result=run(limits=MemoryProfileLimits(request_tokens=1))
    assert result['reason']=='request_budget_exceeded' and not gateway.requests and repo.cursor==0
