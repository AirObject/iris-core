"""resolution=auto_v1: every rule in both directions, with local models only."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, batch, login_admin, msg
from iris.api import create_app
from iris.consolidation import (consolidation_settings, lower_tier, merge_detail_decision, run_settings,
                                snapshot, undo_auto_action, verify_relation)
from iris.db import now
from iris.evidence_trace import independent_origins, trace_origins
from iris.learning import SUPERSEDED_NO_RESTORE, source_message_ids
from iris.maintenance import Maintenance
from iris.memory_ops import delete_memory, edit_memory, manage_memory
from iris.models import ModelReply
from iris.retrieval import Retrieval
from test_batches import memory as learned
from test_consolidation import merge_answer
from test_lifecycle import Clock
from test_retrieval import put

T1 = '2026-09-28T09:01:00+08:00'
T2 = '2026-09-29T09:01:00+08:00'
T3 = '2026-09-30T09:01:00+08:00'


class AutoModel:
    """Routes each prompt to its own canned answer and records the call."""
    health = None

    def __init__(self, store, pair=None, conflict=None, dependency=None, merge=None, hook=None):
        self.store, self.answers, self.hook = store, {'pair': pair, 'conflict': conflict, 'dependency': dependency, 'merge': merge}, hook
        self.calls = []

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        system, payload = messages[0]['content'], json.loads(messages[1]['content'])  # [1] survives a repair turn.
        step = ('conflict' if '矛盾判定器' in system else 'dependency' if '依赖复核器' in system
                else 'merge' if '互有细节的记忆写成一条正文' in system else 'pair')
        self.calls.append((step, purpose, payload))
        if self.hook:
            self.hook(step, payload)
        value = self.answers[step]
        value = value(payload) if callable(value) else value
        if value is None:
            value = {'decision': 'separate', 'reason': '不同事实', 'evidence': [], 'updates': [], 'unique_details': {'a': [], 'b': []}}
        return ModelReply(json.dumps(value, ensure_ascii=False), 'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

    def steps(self):
        return [c[0] for c in self.calls]


def auto(store, resolution='auto_v1'):
    store.set_setting('consolidation', {'resolution': resolution, 'entry_profile_enabled': False})


def run(store, model):
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    engine.run(rid)
    return rid, engine.report(rid)


def item_of(report, outcome):
    return next(i for i in report['items'] if i['phase'] == 'consolidation' and i['outcome'] == outcome)


def message(store, n, text, sender='小林', kind='message', at=None):
    return msg(store, n, text, sender=sender, kind=kind, at=at or T1)


def subject(store, message_id):
    with store.read() as conn:
        return conn.execute('SELECT sender_subject_id FROM messages WHERE id=?', (message_id,)).fetchone()[0]


def mem(store, text, ids, speaker, about=None, stance='亲历', belief=80, retention=60, event_time=None):
    mid = put(store, text, evidence=ids, speaker=speaker, about=about or (speaker,), stance=stance,
              importance=60, event_time=event_time)
    with store.write() as conn:
        conn.execute('UPDATE memories SET belief=?,retention=? WHERE id=?', (belief, retention, mid))
    return mid


def link(store, child, *parents):
    with store.write() as conn:
        conn.executemany("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",
                         [(child, p, now()) for p in parents])


def row(store, mid):
    with store.read() as conn:
        return dict(conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())


def annotations(store, mid):
    with store.read() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM consolidation_annotations WHERE memory_id=? AND cleared_at IS NULL', (mid,))]


def revisions(store, mid):
    with store.read() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY id', (mid,))]


def pin(store, mid):
    with store.write() as conn:
        conn.execute('UPDATE memories SET pinned=1 WHERE id=?', (mid,))


def conflict_of(ev):
    return {'decision': 'conflict', 'reason': '双方说法不同', 'evidence': ev, 'updates': [], 'unique_details': {'a': [], 'b': []}}


@pytest.fixture
def change(store):
    """小林 corrects a plan: the base case of supersedes."""
    auto(store)
    m1 = message(store, 1, '这次周三去游泳', at=T1)
    m2 = message(store, 2, '更正一下，改成周四去游泳', at=T2)
    who = subject(store, m1)
    a = mem(store, '小林这次周三去游泳', [m1], who)
    b = mem(store, '小林这次改成周四去游泳', [m2], who)
    return {'a': a, 'b': b, 'm1': m1, 'm2': m2, 'who': who}


def supersedes(change, **extra):
    return {'relation': 'supersedes', 'old_id': change['a'], 'new_id': change['b'], 'evidence': [change['m2']],
            'reason': '本人后来明确更正了较早的说法', **extra}


def conflict_model(store, change, answer):
    return AutoModel(store, pair=conflict_of([change['m1'], change['m2']]), conflict=answer)


def assert_undecided(store, change):
    a, b = row(store, change['a']), row(store, change['b'])
    assert a['belief'] == 80 and a['lifecycle'] == 'active' and a['retention'] == 60
    assert b['belief'] == 80 and b['lifecycle'] == 'active'
    assert [n['kind'] for n in annotations(store, change['a'])] == ['disputed']
    assert [n['kind'] for n in annotations(store, change['b'])] == ['disputed']
    assert revisions(store, change['a']) == [] and revisions(store, change['b']) == []


# ---- deterministic score rules -----------------------------------------------

@pytest.mark.parametrize('value,expected', [(100, 89), (90, 89), (89, 69), (70, 69), (69, 50), (51, 50), (50, 49),
                                            (49, 29), (30, 29), (29, 9), (10, 9), (9, 9), (0, 0)])
def test_lower_tier_follows_design_tiers(value, expected):
    assert lower_tier(value) == expected


# ---- C2 contradictions -------------------------------------------------------

def test_supersedes_forgets_old_annotates_and_keeps_new(store, change):
    rid, report = run(store, conflict_model(store, change, supersedes(change)))
    old, new = row(store, change['a']), row(store, change['b'])
    assert (old['belief'], old['lifecycle'], old['retention'], old['revision']) == (49, 'forgotten', 19, 2)
    assert (new['belief'], new['lifecycle'], new['revision']) == (80, 'active', 1)
    assert old['content'] == '小林这次周三去游泳'
    notes = annotations(store, change['a'])
    assert [(n['kind'], n['superseded_by'], n['memory_revision']) for n in notes] == [('superseded', change['b'], 2)]
    assert annotations(store, change['b']) == []
    revision = revisions(store, change['a'])[0]
    assert (revision['actor'], revision['reason']) == ('consolidation', 'auto_superseded')
    item = item_of(report, 'auto_conflict')
    assert item['details']['relation'] == 'supersedes' and item['details']['check_failure'] is None
    with store.read() as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM admin_operations WHERE action LIKE 'consolidation%'")]
        assert actions == ['consolidation_auto_conflict']
        assert json.loads(conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?', (rid,)).fetchone()[0])['resolution'] == 'auto_v1'


def test_retention_below_threshold_and_belief_below_ceiling_are_kept(store, change):
    with store.write() as conn:
        conn.execute('UPDATE memories SET belief=30,retention=10 WHERE id=?', (change['a'],))
    run(store, conflict_model(store, change, supersedes(change)))
    old = row(store, change['a'])
    assert (old['belief'], old['retention'], old['lifecycle']) == (30, 10, 'forgotten')


def test_model_may_not_rewrite_bodies_or_scores(store, change):
    answer = supersedes(change)
    answer['belief'] = 99
    model = conflict_model(store, change, answer)
    _, report = run(store, model)
    assert model.steps().count('conflict') == 2  # one repair, then a safe failure.
    assert row(store, change['a'])['belief'] == 80
    assert any(i['outcome'] == 'failed' and i['reason'] == 'invalid_output' for i in report['items'])


@pytest.mark.parametrize('label,mutate,failure', [
    ('old_is_report', lambda c, a: 'stance', 'supersedes_attribution'),
    ('evidence_not_later', lambda c, a: a.update(evidence=[c['m1']]), 'no_later_self_correction'),
    ('evidence_outside_material', lambda c, a: a.update(evidence=[c['m2'], 999]), 'evidence_outside_material'),
    ('no_evidence', lambda c, a: a.update(evidence=[]), 'no_later_self_correction'),
    ('ids_outside_pair', lambda c, a: a.update(old_id=999), 'invalid_ids'),
    ('model_undecided', lambda c, a: a.update(relation='undecided'), 'model_undecided'),
])
def test_supersedes_check_failures_are_undecided(store, change, label, mutate, failure):
    answer = supersedes(change)
    if label == 'old_is_report':
        with store.write() as conn:
            conn.execute("UPDATE memories SET stance='转述' WHERE id=?", (change['a'],))
    else:
        mutate(change, answer)
    _, report = run(store, conflict_model(store, change, answer))
    assert item_of(report, 'auto_conflict')['details']['check_failure'] == failure
    assert_undecided(store, change)


def test_supersedes_requires_the_same_speaker(store):
    auto(store)
    m1, m2 = message(store, 1, '这次周三去游泳', at=T1), message(store, 2, '改成周四', sender='小红', at=T2)
    a = mem(store, '小林这次周三去游泳', [m1], subject(store, m1), about=(subject(store, m1),))
    b = mem(store, '小林这次改成周四去游泳', [m2], subject(store, m2), about=(subject(store, m1),))
    change = {'a': a, 'b': b, 'm1': m1, 'm2': m2}
    _, report = run(store, conflict_model(store, change, supersedes(change)))
    assert item_of(report, 'auto_conflict')['details']['check_failure'] == 'supersedes_attribution'
    assert row(store, a)['lifecycle'] == 'active'


@pytest.mark.parametrize('extra,expected', [(None, 'undecided'), ('action', 'supersedes'), ('other', 'supersedes')])
def test_role_speech_alone_cannot_supersede(store, extra, expected):
    auto(store)
    m1 = message(store, 1, '我这次周三去', sender='我', kind='self_output', at=T1)
    m2 = message(store, 2, '我更正，改成周四去', sender='我', kind='self_output', at=T2)
    ev = [m2]
    if extra == 'action':
        ev.append(message(store, 3, '预约周四成功', sender='我', kind='action_result', at=T2))
    elif extra == 'other':
        ev.append(message(store, 3, '好的周四见', sender='小林', at=T2))
    a, b = mem(store, '我这次周三去游泳', [m1], 'self'), mem(store, '我这次改成周四去游泳', [m2, *ev[1:]], 'self')
    answer = {'relation': 'supersedes', 'old_id': a, 'new_id': b, 'evidence': ev, 'reason': '后来更正'}
    _, report = run(store, AutoModel(store, pair=conflict_of([m1, m2]), conflict=answer))
    details = item_of(report, 'auto_conflict')['details']
    assert details['relation'] == expected
    if expected == 'undecided':
        assert details['check_failure'] == 'role_speech_only'
        assert row(store, a)['lifecycle'] == 'active'
    else:
        assert row(store, a)['lifecycle'] == 'forgotten'


def report_case(store):
    auto(store)
    m1 = message(store, 1, '听说小林周三去游泳', sender='小红', at=T1)
    m2 = message(store, 2, '我周三没去游泳', at=T2)
    person = subject(store, m2)
    return {'a': mem(store, '小红转述小林周三去游泳', [m1], subject(store, m1), about=(person,), stance='转述', belief=93),
            'b': mem(store, '小林周三没去游泳', [m2], person), 'm1': m1, 'm2': m2, 'who': person}


def test_reported_vs_self_lowers_only_the_report(store):
    case = report_case(store)
    answer = {'relation': 'reported_vs_self', 'old_id': case['a'], 'new_id': case['b'], 'evidence': [case['m1'], case['m2']], 'reason': '本人否认了转述'}
    _, report = run(store, conflict_model(store, case, answer))
    reported, own = row(store, case['a']), row(store, case['b'])
    assert (reported['belief'], reported['lifecycle'], reported['retention'], reported['revision']) == (89, 'active', 60, 2)
    assert (own['belief'], own['revision']) == (80, 1)
    assert [n['kind'] for n in annotations(store, case['a'])] == ['disputed'] and annotations(store, case['b']) == []
    assert revisions(store, case['a'])[0]['reason'] == 'auto_reported_downgrade'
    assert item_of(report, 'auto_conflict')['details']['relation'] == 'reported_vs_self'


def test_reported_vs_self_needs_the_persons_own_message(store):
    case = report_case(store)
    answer = {'relation': 'reported_vs_self', 'old_id': case['a'], 'new_id': case['b'], 'evidence': [case['m1']], 'reason': '本人否认了转述'}
    _, report = run(store, conflict_model(store, case, answer))
    assert item_of(report, 'auto_conflict')['details']['check_failure'] == 'no_self_evidence'
    assert row(store, case['a'])['belief'] == 93


def test_coexist_touches_nothing(store, change):
    answer = {'relation': 'coexist', 'old_id': change['a'], 'new_id': change['b'], 'evidence': [], 'reason': '过去与现在可以并存'}
    _, report = run(store, conflict_model(store, change, answer))
    details = item_of(report, 'auto_conflict')['details']
    assert details['relation'] == 'coexist' and 'undo' not in details
    for mid in (change['a'], change['b']):
        assert row(store, mid)['belief'] == 80 and row(store, mid)['lifecycle'] == 'active' and annotations(store, mid) == []
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='consolidation_auto_conflict'").fetchone()[0] == 1


def test_ungrounded_reason_is_replaced_before_it_can_be_stored(store, change):
    answer = supersedes(change, reason='小林在第999次说明这是更正')
    run(store, conflict_model(store, change, answer))
    assert [n['text'] for n in annotations(store, change['a'])] == ['两条说法互相冲突，依据不足以判定。']


# ---- protection --------------------------------------------------------------

@pytest.mark.parametrize('how', ['pinned', 'admin'])
def test_protected_pair_is_recorded_without_any_model_call(store, change, how):
    if how == 'pinned':
        pin(store, change['a'])
    else:
        assert edit_memory(store, change['a'], 1, content='小林这次周三下午去游泳')
    model = conflict_model(store, change, supersedes(change))
    _, report = run(store, model)
    assert model.calls == []
    item = item_of(report, 'protected')
    assert item['reason'] == 'protected' and {m['id'] for m in item['details']['before_memories']} == {change['a'], change['b']}
    assert annotations(store, change['a']) == [] and row(store, change['a'])['lifecycle'] == 'active'


def test_different_visibility_is_not_processed(store, change):
    from iris.memory_ops import set_entry_visibility
    m3 = msg(store, 3, '私聊里说改成周四去游泳', entry='B', sender='小林', at=T3)
    c = mem(store, '小林这次改成周四去游泳', [m3], change['who'])
    set_entry_visibility(store, 'B', 'entry_only')
    model = conflict_model(store, change, supersedes(change))
    run(store, model)
    assert c not in [mid for call in model.calls for mid in call[2].get('pair_ids', [])]


# ---- C1 mutual merges --------------------------------------------------------

@pytest.fixture
def mutual(store):
    auto(store)
    m1, m2 = message(store, 1, '周三领钥匙，我带纸袋', at=T1), message(store, 2, '周三领钥匙，我在北门等', at=T2)
    who = subject(store, m1)
    a = mem(store, '小林计划周三领钥匙，带纸袋', [m1], who)
    b = mem(store, '小林计划周三领钥匙，在北门等', [m2], who)
    return {'a': a, 'b': b, 'm1': m1, 'm2': m2, 'who': who}


def pair_answer(case):
    return {'decision': 'merge', 'keep_id': case['a'], 'reason': '确认是同一安排', 'evidence': [case['m1'], case['m2']], 'updates': [],
            'unique_details': {'a': ['带纸袋'], 'b': ['在北门等']}}


def merge_model(store, case, content):
    return AutoModel(store, pair=pair_answer(case), merge={'content': content, 'reason': '把双方细节放在一起'})


GOOD = '小林计划周三领钥匙，带纸袋，在北门等'


def test_mutual_merge_writes_body_and_inherits_sources(store, mutual):
    with store.write() as conn:
        conn.execute("UPDATE memories SET embedding=X'0000',embedding_model='m' WHERE id IN (?,?)", (mutual['a'], mutual['b']))
    model = merge_model(store, mutual, GOOD)
    _, report = run(store, model)
    assert model.steps() == ['pair', 'merge']
    item = item_of(report, 'merged')
    keep, lose = item['details']['result_id'], item['details']['absorbed_ids'][0]
    survivor, absorbed = row(store, keep), row(store, lose)
    assert survivor['content'] == GOOD and survivor['revision'] == 2 and survivor['embedding'] is None
    assert absorbed['lifecycle'] == 'deleted' and absorbed['merged_into'] == keep
    assert source_message_ids(store, keep) == {mutual['m1'], mutual['m2']}
    assert annotations(store, keep) == [] and annotations(store, lose) == []
    assert revisions(store, keep)[0]['reason'] == 'auto_merge_mutual'
    assert [m['id'] for m in Retrieval(store).search(text='钥匙', include_forgotten=True)['memories']] == [keep]


@pytest.mark.parametrize('content,code', [
    ('小林计划周三领钥匙，带纸袋', 'missing_unique_detail'),
    ('小林计划周三领钥匙，带纸袋，在北门等3次', 'numbers_changed'),
    ('小林计划周三领钥匙，不带纸袋，在北门等', 'negation_changed'),
    ('小林总是计划周三领钥匙，带纸袋，在北门等', 'qualifiers_changed'),
    ('小林计划周四领钥匙，带纸袋，在北门等', 'numbers_changed'),
    ('小林计划明天周三领钥匙，带纸袋，在北门等', 'time_expressions_changed'),
    ('小林计划周三领钥匙，带纸袋，在北门等，下雨带伞', 'new_content_word'),
    ('小林计划周三领钥匙，带纸袋，在北门等张伟', 'new_content_word'),
    ('小林计划周三领钥匙，带纸袋，在北门等Zoe', 'entity_added'),
])
def test_mutual_merge_rejects_drafts_that_change_claims(store, mutual, content, code):
    _, report = run(store, merge_model(store, mutual, content))
    item = item_of(report, 'mutual_merge_rejected')
    assert item['reason'] == code
    for mid in (mutual['a'], mutual['b']):
        assert row(store, mid)['lifecycle'] == 'active' and row(store, mid)['revision'] == 1 and annotations(store, mid) == []


def test_mutual_merge_rejects_entity_loss_of_a_shared_name(store, mutual):
    with store.write() as conn:
        conn.execute("UPDATE subjects SET name='张伟' WHERE id=?", (mutual['who'],))
        conn.execute("UPDATE memories SET content=REPLACE(content,'小林','张伟')")
    answer = pair_answer(mutual)
    _, report = run(store, AutoModel(store, pair=answer, merge={'content': '计划周三领钥匙，带纸袋，在北门等', 'reason': '合并'}))
    assert item_of(report, 'mutual_merge_rejected')['reason'] == 'entity_lost'


def test_mutual_merge_rejects_different_event_time(store, mutual):
    with store.write() as conn:
        conn.execute("UPDATE memories SET event_time=? WHERE id=?", ('2026-10-01T09:00:00+08:00', mutual['a']))
    model = merge_model(store, mutual, GOOD)
    _, report = run(store, model)
    assert item_of(report, 'mutual_merge_rejected')['reason'] == 'event_time_differs'


def test_mutual_merge_not_allowed_skips_the_draft_call(store, mutual):
    with store.write() as conn:
        conn.execute("UPDATE memories SET stance='观点' WHERE id=?", (mutual['b'],))
    model = merge_model(store, mutual, GOOD)
    _, report = run(store, model)
    assert model.steps() == ['pair']
    assert item_of(report, 'mutual_merge_rejected')['reason'] == 'merge_not_allowed'


@pytest.mark.parametrize('how', ['pinned', 'admin'])
def test_mutual_merge_never_touches_protected_memories(store, mutual, how):
    if how == 'pinned':
        pin(store, mutual['b'])
    else:
        assert edit_memory(store, mutual['b'], 1, content='小林计划周三领钥匙，还在北门等')
    model = merge_model(store, mutual, GOOD)
    _, report = run(store, model)
    assert model.calls == [] and item_of(report, 'protected')
    assert row(store, mutual['a'])['lifecycle'] == 'active'


def test_report_only_still_writes_merge_suggestion(store, mutual):
    auto(store, 'report_only_v1')
    model = merge_model(store, mutual, GOOD)
    _, report = run(store, model)
    assert model.steps() == ['pair']
    assert item_of(report, 'conflicts')['details']['suggestion_type'] == 'merge_details'
    assert row(store, mutual['a'])['revision'] == 1


def test_unknown_resolution_runs_as_report_only(store, mutual):
    auto(store, 'future_mode')
    model = merge_model(store, mutual, GOOD)
    run(store, model)
    assert model.steps() == ['pair']


# ---- C3 dependencies ---------------------------------------------------------

@pytest.fixture
def dependent(store):
    auto(store)
    m1 = message(store, 1, '我会蝶泳', at=T1)
    m2 = message(store, 2, '小林可能喜欢水上运动', sender='我', kind='self_output', at=T1)
    m3 = message(store, 3, '小林上周教了我蝶泳', sender='小红', at=T1)
    who = subject(store, m1)
    parent = mem(store, '小林会蝶泳', [m1], who)
    support = mem(store, '小红见过小林教蝶泳', [m3], subject(store, m3), about=(who,))
    target = mem(store, 'Iris推测小林可能喜欢水上运动', [m2], 'self', about=(who,), stance='推断', belief=66, retention=56)
    link(store, target, parent, support)
    with store.write() as conn:
        delete_memory(store, parent, 1, _conn=conn)
    return {'parent': parent, 'support': support, 'target': target, 'who': who, 'm2': m2, 'm3': m3}


def dep_model(store, ids, reason='依据已失效'):
    return AutoModel(store, dependency={'independent_support_ids': ids, 'reason': reason})


def test_independent_support_keeps_the_memory(store, dependent):
    before = row(store, dependent['target'])
    _, report = run(store, dep_model(store, [dependent['support']]))
    details = item_of(report, 'auto_dependency')['details']
    assert details['verdict'] == 'keep' and details['independent_messages'] == [dependent['m3']]
    after = row(store, dependent['target'])
    assert (after['belief'], after['lifecycle'], after['revision']) == (66, 'active', before['revision'])
    assert annotations(store, dependent['target']) == [] and 'undo' not in details


def test_zero_independent_support_forgets_with_annotation(store, dependent):
    _, report = run(store, dep_model(store, []))
    after = row(store, dependent['target'])
    assert (after['belief'], after['lifecycle'], after['retention']) == (49, 'forgotten', 19)
    assert after['content'] == 'Iris推测小林可能喜欢水上运动' and after['merged_into'] is None
    notes = annotations(store, dependent['target'])
    assert [n['kind'] for n in notes] == ['insufficient_support']
    assert revisions(store, dependent['target'])[0]['reason'] == 'auto_insufficient_support'
    assert item_of(report, 'auto_dependency')['details']['verdict'] == 'forget'


def test_role_speech_support_is_not_independent(store):
    auto(store)
    m1, m2, m3 = (message(store, 1, '我会蝶泳', at=T1), message(store, 2, '我猜小林常去', sender='我', kind='self_output', at=T1),
                  message(store, 3, '小林可能喜欢水上运动', sender='我', kind='self_output', at=T1))
    who = subject(store, m1)
    parent = mem(store, '小林会蝶泳', [m1], who)
    guess = mem(store, 'Iris猜小林常去泳池', [m2], 'self', about=(who,), stance='推断')
    target = mem(store, 'Iris推测小林可能喜欢水上运动', [m3], 'self', about=(who,), stance='推断', belief=66)
    link(store, target, parent, guess)
    with store.write() as conn:
        delete_memory(store, parent, 1, _conn=conn)
    run(store, dep_model(store, [guess]))
    assert row(store, target)['lifecycle'] == 'forgotten'


def test_two_paths_to_the_lost_source_add_no_support(store):
    auto(store)
    m1, m2, m3, m4 = (message(store, 1, '我新岗位在江盘路', at=T1), message(store, 2, '据说岗位在江盘路', sender='我', kind='self_output', at=T1),
                      message(store, 3, '据说要全天在岗', sender='我', kind='self_output', at=T1),
                      message(store, 4, '推测不方便收快递', sender='我', kind='self_output', at=T1))
    who = subject(store, m1)
    root = mem(store, '何序的新岗位在江盘路办公室', [m1], who)
    b = mem(store, '据何序所说新岗位在江盘路', [m2], 'self', about=(who,), stance='转述')
    c = mem(store, '据何序所说新岗位全天在岗', [m3], 'self', about=(who,), stance='转述')
    d = mem(store, 'Iris推测何序不方便收快递', [m4], 'self', about=(who,), stance='推断', belief=63)
    link(store, b, root), link(store, c, root), link(store, d, b, c)
    assert manage_memory(store, root, 1, action='forget')
    def answer(payload):
        return {'independent_support_ids': [m['id'] for m in payload['memories'] if m['id'] in (b, c)] if payload['target_id'] == d else [], 'reason': '依据已失效'}
    run(store, AutoModel(store, dependency=answer))
    assert [row(store, mid)['lifecycle'] for mid in (b, c, d)] == ['forgotten', 'forgotten', 'forgotten']
    assert [row(store, mid)['belief'] for mid in (b, c, d)] == [49, 49, 49]


@pytest.mark.parametrize('label', ['outside_material', 'target_itself', 'lost_parent'])
def test_invalid_support_is_undecided_and_changes_nothing(store, dependent, label):
    ids = {'outside_material': [99999], 'target_itself': [dependent['target']], 'lost_parent': [dependent['parent']]}[label]
    _, report = run(store, dep_model(store, ids))
    details = item_of(report, 'auto_dependency')['details']
    assert details['verdict'] == 'undecided' and details['check_failure'] == 'support_outside_material'
    after = row(store, dependent['target'])
    assert after['lifecycle'] == 'active' and after['belief'] == 66 and annotations(store, dependent['target']) == []


def test_m2_penalty_is_not_applied_twice(store, dependent):
    _, first = run(store, dep_model(store, [dependent['support']]))
    retention = row(store, dependent['target'])['retention']
    assert retention == 46  # one M2 deduction for the single lost source.
    _, second = run(store, dep_model(store, [dependent['support']]))
    assert row(store, dependent['target'])['retention'] == 46


@pytest.mark.parametrize('how', ['pinned', 'admin'])
def test_protected_dependency_target_is_not_reviewed(store, dependent, how):
    if how == 'pinned':
        pin(store, dependent['target'])
    else:
        assert edit_memory(store, dependent['target'], 1, content='Iris推测小林可能喜欢水上运动，待核实')
    model = dep_model(store, [])
    _, report = run(store, model)
    assert model.calls == [] and item_of(report, 'protected')
    assert row(store, dependent['target'])['lifecycle'] == 'active'


# ---- C4 independence between pairs -------------------------------------------

def three(store):
    m1, m2, m3 = (message(store, 1, '周三去游泳馆', at=T1), message(store, 2, '周三去游泳馆哦', at=T1), message(store, 3, '周三去游泳馆呀', at=T1))
    who = subject(store, m1)
    return [mem(store, text, [m], who) for text, m in (('小林周三去游泳馆', m1), ('小林周三去游泳馆游泳', m2), ('小林这周三去游泳馆', m3))]


def pair_decider(a, b, c, first):
    def answer(payload):
        ids = set(payload['pair_ids'])
        if ids == {a, b}:
            return first(payload)
        if ids == {a, c}:
            return merge_answer(payload)
        return None
    return answer


def invalid_details(payload):
    return {'decision': 'merge', 'keep_id': payload['pair_ids'][0], 'reason': '同一件事', 'evidence': [s['id'] for m in payload['memories'] for s in m['sources']][:1],
            'updates': [], 'unique_details': {'a': ['正文里没有这句话'], 'b': []}}


def test_invalid_details_only_affects_its_own_pair(store):
    auto(store)
    a, b, c = three(store)
    _, report = run(store, AutoModel(store, pair=pair_decider(a, b, c, invalid_details)))
    merged = item_of(report, 'merged')
    assert {merged['details']['result_id'], *merged['details']['absorbed_ids']} == {a, c}
    assert not any(i.get('reason') == 'unresolved_conflict' for i in report['items'])


def test_report_only_still_blocks_on_invalid_details(store):
    auto(store, 'report_only_v1')
    a, b, c = three(store)
    _, report = run(store, AutoModel(store, pair=pair_decider(a, b, c, invalid_details)))
    assert report['summary']['skipped']['reasons'].get('unresolved_conflict')


def test_failed_pair_only_affects_itself(store):
    auto(store)
    a, b, c = three(store)
    # A malformed answer fails its own pair and nothing else.
    model = AutoModel(store, pair=pair_decider(a, b, c, lambda p: {'decision': 'nonsense'}))
    _, report = run(store, model)
    assert item_of(report, 'merged')


def test_undecided_contradiction_blocks_merges_of_shared_memories(store):
    auto(store)
    a, b, c = three(store)
    answer = pair_decider(a, b, c, lambda p: conflict_of([p['memories'][0]['sources'][0]['id']]))
    conflict = {'relation': 'undecided', 'old_id': a, 'new_id': b, 'evidence': [], 'reason': '无法判定'}
    _, report = run(store, AutoModel(store, pair=answer, conflict=conflict))
    assert report['summary']['skipped']['reasons'].get('unresolved_conflict')
    assert row(store, c)['lifecycle'] == 'active'


def test_resolved_contradiction_does_not_block_unrelated_merge(store):
    auto(store)
    a, b, c = three(store)
    answer = pair_decider(a, b, c, lambda p: conflict_of([p['memories'][0]['sources'][0]['id']]))
    conflict = {'relation': 'coexist', 'old_id': a, 'new_id': b, 'evidence': [], 'reason': '可以并存'}
    _, report = run(store, AutoModel(store, pair=answer, conflict=conflict))
    assert item_of(report, 'merged')


# ---- restart, revisions, settings ------------------------------------------

def test_interrupted_run_does_not_repeat_or_double_apply(store, mutual):
    model = merge_model(store, mutual, GOOD)
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    engine.run(rid, stop=lambda: bool(model.calls))
    assert model.steps() == ['pair']
    Maintenance(store, gateway=model, clock=Clock()).run(rid)
    assert model.steps() == ['pair', 'merge']
    assert engine.report(rid)['summary']['merged']['count'] == 1
    calls = len(model.calls)
    run(store, model)  # A later run sees the merge's receipt and does nothing.
    assert len(model.calls) == calls


def test_finished_conflict_is_not_reprocessed(store, change):
    model = conflict_model(store, change, supersedes(change))
    run(store, model)
    calls = len(model.calls)
    run(store, model)
    assert len(model.calls) == calls and row(store, change['a'])['belief'] == 49


def test_revision_conflict_skips_the_write(store, change):
    def edit(step, payload):
        if step == 'conflict':
            assert edit_memory(store, change['a'], 1, content='小林这次周三上午去游泳', actor='learning')
    model = conflict_model(store, change, supersedes(change))
    model.hook = edit
    _, report = run(store, model)
    assert report['summary']['skipped']['reasons'].get('revision_conflict') == 1
    assert row(store, change['a'])['lifecycle'] == 'active' and annotations(store, change['a']) == []


def test_model_calls_happen_outside_transactions(store, mutual):
    # AutoModel asserts this on every call; a full path through all steps exercises each.
    model = merge_model(store, mutual, GOOD)
    run(store, model)
    assert len(model.calls) == 2


def test_run_snapshot_is_not_affected_by_later_setting_change(store, change):
    model = conflict_model(store, change, supersedes(change))
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    auto(store, 'report_only_v1')
    engine.run(rid)
    assert row(store, change['a'])['lifecycle'] == 'forgotten'


@pytest.mark.parametrize('stored,expected', [(None, 'report_only_v1'), ('report_only_v1', 'report_only_v1'), ('auto_v1', 'auto_v1'),
                                             ('conservative_v2', 'report_only_v1'), ({'a': 1}, 'report_only_v1')])
def test_settings_default_and_normalization(store, stored, expected):
    value = {} if stored is None else {'resolution': stored}
    store.set_setting('consolidation', value)
    with store.write() as conn:
        assert consolidation_settings(conn)['resolution'] == expected
        rid = conn.execute("""INSERT INTO maintenance_runs(trigger,settings_json,timezone,memory_through,message_through,batch_through,created_at,goal_through)
            VALUES('manual','{}','Asia/Shanghai',0,0,0,?,0)""", (now(),)).lastrowid
        conn.execute('INSERT INTO consolidation_runs(run_id,settings_json) VALUES(?,?)', (rid, json.dumps(value)))
        assert run_settings(conn, rid)['resolution'] == expected


def test_new_database_defaults_to_report_only(store):
    with store.write() as conn:
        assert consolidation_settings(conn)['resolution'] == 'report_only_v1'


# ---- undo ---------------------------------------------------------------------

def test_undo_supersedes_restores_scores_and_clears_annotation(store, change):
    rid, _ = run(store, conflict_model(store, change, supersedes(change)))
    work = _work_id(store)
    assert undo_auto_action(store, rid, work, 2)
    old = row(store, change['a'])
    assert (old['belief'], old['lifecycle'], old['retention'], old['forgotten_at']) == (80, 'active', 60, None)
    assert annotations(store, change['a']) == []
    assert not undo_auto_action(store, rid, work, 2)  # A second undo conflicts.


def _work_id(store):
    with store.read() as conn:
        return conn.execute("SELECT work_id FROM consolidation_run_work WHERE outcome IS NOT NULL ORDER BY run_id DESC,work_id DESC LIMIT 1").fetchone()[0]


def test_undo_is_refused_after_later_modification_and_changes_nothing(store, change):
    rid, _ = run(store, conflict_model(store, change, supersedes(change)))
    work = _work_id(store)
    assert edit_memory(store, change['a'], 2, content='小林这次周三上午去游泳', actor='learning')
    assert not undo_auto_action(store, rid, work, 2)
    old = row(store, change['a'])
    assert (old['belief'], old['lifecycle']) == (49, 'forgotten')
    assert [n['kind'] for n in annotations(store, change['a'])] == ['superseded']


def test_undo_requires_matching_expected_revision(store, change):
    rid, _ = run(store, conflict_model(store, change, supersedes(change)))
    assert not undo_auto_action(store, rid, _work_id(store), 1)
    assert row(store, change['a'])['lifecycle'] == 'forgotten'


def test_undo_mutual_merge_restores_both_memories(store, mutual):
    rid, report = run(store, merge_model(store, mutual, GOOD))
    details = item_of(report, 'merged')['details']
    keep, lose = details['result_id'], details['absorbed_ids'][0]
    original = '小林计划周三领钥匙，带纸袋' if keep == mutual['a'] else '小林计划周三领钥匙，在北门等'
    assert undo_auto_action(store, rid, _work_id(store), 2)
    survivor, absorbed = row(store, keep), row(store, lose)
    assert survivor['content'] == original and survivor['belief'] == 80
    assert absorbed['lifecycle'] == 'active' and absorbed['merged_into'] is None
    assert source_message_ids(store, keep) == {mutual['m1'] if keep == mutual['a'] else mutual['m2']}
    assert source_message_ids(store, lose) == {mutual['m2'] if keep == mutual['a'] else mutual['m1']}


def test_undo_merge_redirects_dependents_back(store, mutual):
    rid_links = mem(store, 'Iris推测小林会带纸袋', [mutual['m1']], 'self', about=(mutual['who'],), stance='推断')
    link(store, rid_links, mutual['a'], mutual['b'])
    with store.read() as conn:
        before = sorted((r['source_memory_id'], r['source_revision']) for r in conn.execute("SELECT * FROM sources WHERE memory_id=? AND kind='memory'", (rid_links,)))
    rid, report = run(store, merge_model(store, mutual, GOOD))
    assert undo_auto_action(store, rid, _work_id(store), 2)
    with store.read() as conn:
        after = sorted((r['source_memory_id'], r['source_revision']) for r in conn.execute("SELECT * FROM sources WHERE memory_id=? AND kind='memory'", (rid_links,)))
    assert after == before


def test_undo_is_refused_when_survivor_changed_after_merge(store, mutual):
    rid, report = run(store, merge_model(store, mutual, GOOD))
    keep = item_of(report, 'merged')['details']['result_id']
    assert edit_memory(store, keep, 2, content='小林计划周三领钥匙，带纸袋，在北门等，下午', actor='learning')
    assert not undo_auto_action(store, rid, _work_id(store), 2)
    assert row(store, keep)['lifecycle'] == 'active'


def test_undecided_undo_clears_annotations(store, change):
    answer = supersedes(change, relation='undecided')
    rid, _ = run(store, conflict_model(store, change, answer))
    assert undo_auto_action(store, rid, _work_id(store), 1)
    assert annotations(store, change['a']) == [] and annotations(store, change['b']) == []


def test_annotations_are_not_returned_to_hosts(store, change):
    run(store, conflict_model(store, change, supersedes(change)))
    result = Retrieval(store).prepare('A', text='游泳', recent_limit=0)
    assert result['memories'] and not {key for m in result['memories'] for key in m} & {'annotations', 'consolidation_annotations'}


def test_admin_undo_requires_session_and_reports_conflict(store, change):
    rid, _ = run(store, conflict_model(store, change, supersedes(change)))
    work = _work_id(store)
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        path = f'/admin/api/maintenance/{rid}/consolidation/{work}/undo'
        assert client.post(path, json={'expected_revision': 2}).status_code in (401, 403, 409)
        login_admin(client)
        report = client.get(f'/admin/api/maintenance/{rid}').json()
        item = next(i for i in report['items'] if i['outcome'] == 'auto_conflict')
        assert item['details']['undo'] == {'expected_revision': 2, 'undone_at': None}
        assert client.post(path, json={'expected_revision': 1}).status_code == 409
        response = client.post(path, json={'expected_revision': 2})
        assert response.status_code == 200, response.text
        item = next(i for i in response.json()['items'] if i['outcome'] == 'auto_conflict')
        assert item['details']['undo']['undone_at'] is not None
        assert client.post(path, json={'expected_revision': 2}).status_code == 409
        assert client.post(f'/admin/api/maintenance/{rid}/consolidation/99999/undo', json={'expected_revision': 2}).status_code == 404
    assert row(store, change['a'])['lifecycle'] == 'active'


def test_settings_api_accepts_resolution_and_rejects_unknown(store):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        assert client.get('/admin/api/settings').json()['consolidation']['resolution'] == 'report_only_v1'
        response = client.patch('/admin/api/settings/consolidation', json={'resolution': 'auto_v1'})
        assert response.status_code == 200 and response.json()['consolidation']['resolution'] == 'auto_v1'
        assert client.patch('/admin/api/settings/consolidation', json={'resolution': 'nonsense'}).status_code == 400
        assert client.patch('/admin/api/settings/consolidation', json={'resolution': 'report_only_v1'}).json()['consolidation']['resolution'] == 'report_only_v1'


# ---- the role's own speech and traced evidence --------------------------------

def test_trace_walks_memory_edges_once_and_stops_at_blocked_or_inactive(store):
    auto(store)
    m1, m2, m3 = (message(store, 1, '原话一', at=T1), message(store, 2, '角色复述', sender='我', kind='self_output', at=T1), message(store, 3, '原话三', sender='小红', at=T1))
    who = subject(store, m1)
    root = mem(store, '根记忆', [m1], who)
    mid = mem(store, '中间记忆', [m2], 'self', about=(who,), stance='转述')
    leaf = mem(store, '叶记忆', [m3], who)
    link(store, mid, root), link(store, leaf, mid), link(store, root, leaf)
    with store.read() as conn:
        assert set(trace_origins(conn, memory_id=leaf)) == {m1, m2, m3}
        assert set(trace_origins(conn, memory_id=leaf, blocked=[root])) == {m2, m3}
        assert set(trace_origins(conn, memory_id=mid, blocked=[root])) == {m2}
        assert independent_origins(conn, [mid], blocked=[root]) == {}
        assert set(independent_origins(conn, [leaf, leaf], blocked=[root])) == {m3}
        assert set(trace_origins(conn, message_id=m3)) == {m3}
    assert manage_memory(store, root, 1, action='forget')
    with store.read() as conn:
        assert set(trace_origins(conn, memory_id=mid)) == {m2}
        assert m1 in trace_origins(conn, memory_id=mid, active_only=False)


# ---- reconfirmation after supersession ---------------------------------------

CONTENT = '小林喜欢在安静的地方画水彩'


def seeded(store):
    message(store, 1, CONTENT)
    _, result = batch(store, FakeGateway({'memories': [learned(CONTENT)]}), count=1)
    return result['created'][0]


def forget(store, memory_id, kind):
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten',retention=10,forgotten_at=? WHERE id=?", (now(), memory_id))
        work = conn.execute("INSERT INTO consolidation_work(fingerprint,kind,payload_json,importance,changed_at,created_at) VALUES(?,'pair','{}',0,?,?)",
                            (f'test-{memory_id}-{kind}', now(), now())).lastrowid
        conn.execute("""INSERT INTO consolidation_annotations(memory_id,work_id,text,evidence_json,created_at,kind,memory_revision,dedupe_key)
            VALUES(?,?,?,'[]',?,?,1,?)""", (memory_id, work, '测试', now(), kind, f'test-{kind}'))


def reconfirm(store):
    message(store, 2, CONTENT)
    return batch(store, FakeGateway({'memories': [learned(CONTENT, [2])]}), count=1)[1]


def test_superseded_memory_is_not_revived_by_confirmation(store):
    memory_id = seeded(store)
    forget(store, memory_id, 'superseded')
    before = row(store, memory_id)
    result = reconfirm(store)
    after = row(store, memory_id)
    assert result['confirmed'] == [] and [d['reason'] for d in result['dropped']] == [SUPERSEDED_NO_RESTORE]
    assert '_embedding' not in result['dropped'][0]['item']
    assert (after['retention'], after['lifecycle'], after['last_confirmed_at']) == (10, 'forgotten', before['last_confirmed_at'])
    assert source_message_ids(store, memory_id) == {1, 2}


def test_explicit_confirmation_of_superseded_memory_only_appends_source(store):
    memory_id = seeded(store)
    message(store, 2, CONTENT)
    def mark(_):
        forget(store, memory_id, 'superseded')
    result = batch(store, FakeGateway({'updates': [{'ref': 'M1', 'action': '确认', 'evidence': [2]}]}, hook=mark), count=1)[1]
    assert result['confirmed'] == [] and [d['reason'] for d in result['dropped']] == [SUPERSEDED_NO_RESTORE]
    assert row(store, memory_id)['retention'] == 10 and source_message_ids(store, memory_id) == {1, 2}


def test_dependency_forgotten_memory_can_still_be_confirmed(store):
    memory_id = seeded(store)
    forget(store, memory_id, 'insufficient_support')
    assert reconfirm(store)['confirmed'] == [memory_id]
    assert row(store, memory_id)['retention'] == 15


def test_administrator_restore_lifts_the_limit(store):
    memory_id = seeded(store)
    forget(store, memory_id, 'superseded')
    assert manage_memory(store, memory_id, 1, action='restore')
    assert row(store, memory_id)['lifecycle'] == 'active' and annotations(store, memory_id) == []
    result = reconfirm(store)
    assert result['confirmed'] == [memory_id] and row(store, memory_id)['retention'] > 35


def test_clearing_the_annotation_lifts_the_limit(store):
    memory_id = seeded(store)
    forget(store, memory_id, 'superseded')
    with store.write() as conn:
        conn.execute("UPDATE consolidation_annotations SET cleared_at=? WHERE memory_id=?", (now(), memory_id))
    assert reconfirm(store)['confirmed'] == [memory_id]


def test_verify_relation_is_pure_on_the_payload(store, change):
    with store.write() as conn:
        payload = {'pair_ids': [change['a'], change['b']], 'memories': [snapshot(conn, change['a']), snapshot(conn, change['b'])]}
    with store.read() as conn:
        assert verify_relation(conn, payload, supersedes(change)) == ('supersedes', None)
    with store.read() as conn:
        assert verify_relation(conn, payload, supersedes(change, relation='coexist')) == ('coexist', None)


def test_merge_detail_decision_keeps_auto_draft(store, mutual):
    with store.write() as conn:
        payload = {'pair_ids': [mutual['a'], mutual['b']], 'memories': [snapshot(conn, mutual['a']), snapshot(conn, mutual['b'])]}
    result = merge_detail_decision(payload, {**pair_answer(mutual), 'auto_merge': {'content': GOOD, 'reason': 'r'}})
    assert result['merge_detail_check']['outcome'] == 'mutual_details' and result['auto_merge']['content'] == GOOD


def test_consolidation_keeps_ids_of_annotated_memories_for_undo(store, change):
    rid, report = run(store, conflict_model(store, change, supersedes(change)))
    with store.read() as conn:
        details = json.loads(conn.execute("SELECT details_json FROM maintenance_items WHERE run_id=? AND outcome='auto_conflict'", (rid,)).fetchone()[0])
    assert details['undo']['changes'][0]['memory_id'] == change['a']
    assert details['undo']['annotation_ids'] and details['auto_resolution'] == 'auto_v1'
