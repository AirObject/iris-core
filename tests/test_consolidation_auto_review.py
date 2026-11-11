"""#139 review: annotations per action, draft wording, undo side effects, restore guard, C3, C4."""
import pytest

from conftest import msg
from iris.consolidation import dependency_material, mutual_merge_rejection, snapshot, undo_auto_action
from iris.db import now
from iris.maintenance import Maintenance
from iris.memory_ops import (adjust_retention, delete_memory, edit_memory, entry_profile_source, manage_memory,
                             superseded_forgotten, write_entry_profile_memory)
from iris.models import ModelError
from iris.retrieval import Retrieval
from test_consolidation_auto import (GOOD, T1, T2, AutoModel, _work_id, annotations, auto, change, conflict_model,
                                     conflict_of, dep_model, forget, link, mem, merge_answer, merge_model, message,
                                     mutual, pair_decider, report_case, row, run, seeded, subject, supersedes, three)
from test_entry_profile_incremental import NOW
from test_entry_profile_incremental_quality import seed as profile_seed
from test_lifecycle import Clock

__all__ = ['change', 'mutual']  # pytest fixtures shared with test_consolidation_auto


# ---- 1. every action keeps its own annotation -----------------------------------

def test_second_supersession_after_restore_keeps_an_active_annotation(store, change):
    model = conflict_model(store, change, supersedes(change))
    run(store, model)
    assert manage_memory(store, change['a'], row(store, change['a'])['revision'], action='restore')
    assert annotations(store, change['a']) == []
    rid, _ = run(store, model)
    notes = annotations(store, change['a'])
    assert [(n['kind'], n['run_id']) for n in notes] == [('superseded', rid)]
    assert row(store, change['a'])['lifecycle'] == 'forgotten'
    with store.read() as conn:
        assert superseded_forgotten(conn, change['a'])


def test_undo_clears_only_the_annotations_of_its_own_action(store):
    case = report_case(store)
    undecided = {'relation': 'undecided', 'old_id': case['a'], 'new_id': case['b'], 'evidence': [], 'reason': '无法判定'}
    model = conflict_model(store, case, undecided)
    first, _ = run(store, model)
    assert edit_memory(store, case['b'], 1, content='小林周三确实没去游泳', actor='learning')
    model.answers['conflict'] = {'relation': 'reported_vs_self', 'old_id': case['a'], 'new_id': case['b'],
                                 'evidence': [case['m1'], case['m2']], 'reason': '本人否认了转述'}
    second, _ = run(store, model)
    assert sorted(n['run_id'] for n in annotations(store, case['a'])) == [first, second]
    assert undo_auto_action(store, second, _work_id(store), row(store, case['a'])['revision'])
    assert [n['run_id'] for n in annotations(store, case['a'])] == [first]


def test_interrupted_run_writes_each_annotation_once(store, change):
    model = conflict_model(store, change, supersedes(change))
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    engine.run(rid, stop=lambda: 'conflict' in model.steps())
    assert annotations(store, change['a']) == []
    Maintenance(store, gateway=model, clock=Clock()).run(rid)
    assert len(annotations(store, change['a'])) == 1 and model.steps().count('conflict') == 1


# ---- 2. the merged body uses exactly the words of both bodies --------------------

def body(i, text):
    return {'id': i, 'content': text, 'speaker': 'p1', 'stance': '亲历', 'about': ['p1'], 'event_time': None,
            'pinned': False, 'last_edit': 'learning', 'type': '计划', 'lifecycle': 'active', 'merged_into': None,
            'derived_from': [], 'world': 'real', 'subjects': [{'id': 'p1', 'name': '小林', 'aliases': []}], 'sources': []}


KEYS = ('小林计划周三领钥匙，带纸袋', '小林计划周三领钥匙，在北门等', ['带纸袋'], ['在北门等'])
CAT = ('小林养了一只橘猫', '小林喜欢猫', ['养了一只橘猫'], ['喜欢猫'])


@pytest.mark.parametrize('case,draft,code', [
    (KEYS, GOOD, None),
    (KEYS, '小林已经周三领钥匙，带纸袋，在北门等', 'new_content_word'),
    (KEYS, '小林想周三领钥匙，带纸袋，在北门等过', 'new_content_word'),
    (KEYS, '小林周三领钥匙，带纸袋，在北门等', 'content_word_lost'),
    (KEYS, '小林计划周三领钥匙，带纸袋，并在北门等', None),
    (CAT, '小林喜欢猫，养了一只橘猫', None),
    (CAT, '小林喜欢猫，想养了一只橘猫', 'new_content_word'),
    (CAT, '小林最喜欢猫，养了一只橘猫', 'new_content_word'),
])
def test_draft_words_must_equal_the_union(case, draft, code):
    left, right, a, b = case
    payload = {'pair_ids': [1, 2], 'memories': [body(1, left), body(2, right)], 'merge_allowed': True, 'preferred_keep_id': 1}
    assert mutual_merge_rejection(payload, {'unique_details': {'a': a, 'b': b}, 'evidence': []}, draft) == code


# ---- 3 / 4 / 9. undo side effects ------------------------------------------------

def test_undo_of_mutual_merge_does_not_look_like_a_corrected_source(store, mutual):
    n3 = message(store, 3, '我也去', sender='小红', at=T2)
    dependent = mem(store, '小红知道小林周三领钥匙', [n3], subject(store, n3), about=(mutual['who'],))
    link(store, dependent, mutual['a'], mutual['b'])
    rid, _ = run(store, merge_model(store, mutual, GOOD))
    with store.read() as conn:
        assert dependency_material(conn, snapshot(conn, dependent)) is None
    assert undo_auto_action(store, rid, _work_id(store), 2)
    with store.read() as conn:
        assert dependency_material(conn, snapshot(conn, dependent)) is None


@pytest.fixture
def leaning(store, change):
    """A dependent of the memory that gets superseded."""
    m3 = message(store, 3, '那我周三也去', sender='小红', at=T1)
    change['d'] = mem(store, '小红打算和小林一起游泳', [m3], subject(store, m3), about=(change['who'],))
    with store.write() as conn:  # Importance 80 does not decay, so only the loss moves retention.
        conn.execute('UPDATE memories SET importance=80 WHERE id=?', (change['d'],))
    link(store, change['d'], change['a'])
    return change


def loss_rows(store):
    with store.read() as conn:
        return [tuple(r) for r in conn.execute('SELECT memory_id,source_memory_id,applied_at IS NOT NULL FROM memory_dependency_losses')]


def test_undo_cancels_a_dependency_loss_not_yet_applied(store, leaning):
    rid, _ = run(store, conflict_model(store, leaning, supersedes(leaning)))
    assert loss_rows(store) == [(leaning['d'], leaning['a'], 0)]
    assert undo_auto_action(store, rid, _work_id(store), 2)
    assert loss_rows(store) == []
    run(store, AutoModel(store))
    assert row(store, leaning['d'])['retention'] == 60


def test_undo_refunds_a_dependency_loss_already_applied(store, leaning):
    rid, _ = run(store, conflict_model(store, leaning, supersedes(leaning)))
    work = _work_id(store)
    run(store, AutoModel(store))
    assert row(store, leaning['d'])['retention'] == 50 and loss_rows(store) == [(leaning['d'], leaning['a'], 1)]
    assert undo_auto_action(store, rid, work, 2)
    assert row(store, leaning['d'])['retention'] == 60 and loss_rows(store) == []
    run(store, AutoModel(store))
    assert row(store, leaning['d'])['retention'] == 60


def test_manual_forget_and_restore_keep_the_existing_loss_behaviour(store, leaning):
    assert manage_memory(store, leaning['a'], 1, action='forget')
    assert manage_memory(store, leaning['a'], 1, action='restore')
    assert loss_rows(store) == [(leaning['d'], leaning['a'], 0)]


def test_undo_tolerates_retention_changes_of_a_forgotten_memory(store, change):
    rid, _ = run(store, conflict_model(store, change, supersedes(change)))
    with store.write() as conn:  # an M2 deduction: retention moves, revision does not.
        conn.execute('UPDATE memories SET retention=retention-5 WHERE id=?', (change['a'],))
    assert undo_auto_action(store, rid, _work_id(store), 2)
    restored = row(store, change['a'])
    assert (restored['lifecycle'], restored['retention'], restored['belief']) == ('active', 55, 80)


# ---- 5. the target's own messages count ------------------------------------------

@pytest.mark.parametrize('kind,expected', [('message', 'active'), ('self_output', 'forgotten')])
def test_target_own_non_role_message_is_independent_support(store, kind, expected):
    auto(store)
    n1 = message(store, 1, '我会蝶泳', at=T1)
    n2 = message(store, 2, '我每周末都去游泳馆', sender='小林' if kind == 'message' else '我', kind=kind, at=T1)
    who = subject(store, n1)
    parent = mem(store, '小林会蝶泳', [n1], who)
    target = mem(store, '小林喜欢水上运动', [n2], who if kind == 'message' else 'self', about=(who,), stance='推断', belief=66)
    link(store, target, parent)
    with store.write() as conn:
        delete_memory(store, parent, 1, _conn=conn)
    _, report = run(store, dep_model(store, []))
    assert row(store, target)['lifecycle'] == expected


# ---- 6. no automatic restore while superseded ------------------------------------

def superseded_at(store, memory_id, retention):
    forget(store, memory_id, 'superseded')
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=? WHERE id=?', (retention, memory_id))


def test_feedback_raises_retention_but_does_not_restore(store):
    memory_id = seeded(store)
    retrieval = Retrieval(store)
    with store.write() as conn:
        conn.execute("INSERT INTO recalls(id,entry_id,request_json,created_at) VALUES('r1','A','{}',?)", (now(),))
        conn.execute("INSERT INTO recall_items(recall_id,memory_id,revision) VALUES('r1',?,1)", (memory_id,))
    superseded_at(store, memory_id, 30)
    assert retrieval.feedback('r1', [memory_id])['strengthened'] == [memory_id]
    assert (row(store, memory_id)['retention'], row(store, memory_id)['lifecycle']) == (38, 'forgotten')


def test_lowered_restore_threshold_does_not_restore_in_daily_decay(store):
    memory_id = seeded(store)
    superseded_at(store, memory_id, 10)
    store.set_setting('lifecycle', {**(store.setting('lifecycle') or {}), 'forget_threshold': 3, 'restore_threshold': 5})
    engine = Maintenance(store, clock=Clock())
    engine.run(engine.request())
    assert row(store, memory_id)['lifecycle'] == 'forgotten'


def test_entry_profile_admission_does_not_restore(store):
    memory_id, ids = profile_seed(store, [(1, '甲', '木材和钢材哪种更适合望远镜支架？'), (4, '乙', '望远镜支架选材还要比较携带重量。')])
    superseded_at(store, memory_id, 10)
    with store.write() as conn:
        revision = conn.execute('SELECT revision FROM memories WHERE id=?', (memory_id,)).fetchone()[0]
        refs = [entry_profile_source(conn, mid) for mid in ids]
        write_entry_profile_memory(store, 'group', op='admit', memory_id=memory_id, expected_revision=revision,
                                   sources=refs, current=NOW, _conn=conn)
    after = row(store, memory_id)
    assert after['retention'] >= 35 and after['lifecycle'] == 'forgotten'


def test_retention_edit_by_admin_does_not_lift_the_limit_but_restore_does(store):
    memory_id = seeded(store)
    superseded_at(store, memory_id, 10)
    assert manage_memory(store, memory_id, 1, retention=80)
    assert (row(store, memory_id)['retention'], row(store, memory_id)['lifecycle']) == (80, 'forgotten')
    assert manage_memory(store, memory_id, 1, action='restore')
    assert row(store, memory_id)['lifecycle'] == 'active' and annotations(store, memory_id) == []


def test_other_forgotten_memories_still_restore(store):
    memory_id = seeded(store)
    forget(store, memory_id, 'insufficient_support')
    assert adjust_retention(store, memory_id, 30)['lifecycle'] == 'active'


# ---- 10. an unfinished contradiction blocks merges of its memories ---------------

def conflicting(a, b, c):
    return pair_decider(a, b, c, lambda p: conflict_of([p['memories'][0]['sources'][0]['id']]))


def test_failed_conflict_step_blocks_merges_of_shared_memories(store):
    auto(store)
    a, b, c = three(store)
    model = AutoModel(store, pair=conflicting(a, b, c), conflict=ModelError('item_error', 'bad request'))
    _, report = run(store, model)
    assert report['summary']['skipped']['reasons'].get('unresolved_conflict')
    assert all(row(store, mid)['merged_into'] is None for mid in (a, b, c))


def test_budget_stop_after_conflict_classification_blocks_merges(store):
    auto(store)
    a, b, c = three(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET importance=90 WHERE id=?', (c,))
    model = AutoModel(store, pair=conflicting(a, b, c), conflict=ModelError('budget', 'call budget', reason='call_budget'))
    _, report = run(store, model)
    assert model.steps().count('conflict') == 1
    assert all(row(store, mid)['merged_into'] is None for mid in (a, b, c))
    assert report['summary']['skipped']['reasons'].get('unresolved_conflict')


# ---- follow-up: decay report and merges never revive a superseded memory -----------

def lowered_thresholds(store):
    store.set_setting('lifecycle', {**(store.setting('lifecycle') or {}), 'forget_threshold': 3, 'restore_threshold': 5})


def maintenance_run(store):
    engine = Maintenance(store, clock=Clock())
    rid = engine.request()
    engine.run(rid)
    return engine.report(rid)


def test_blocked_restore_is_a_skip_not_a_daily_restored_item(store):
    superseded, plain = seeded(store), seeded_other(store)
    superseded_at(store, superseded, 10)
    forget(store, plain, 'insufficient_support')
    lowered_thresholds(store)
    for _ in range(2):
        report = maintenance_run(store)
        assert superseded not in report['summary']['restored']['memory_ids']
        assert not [i for i in report['items'] if i['phase'] == 'decay' and i['memory_id'] == superseded]
        assert report['summary']['skipped']['reasons'].get('superseded') == 1
        assert row(store, superseded)['lifecycle'] == 'forgotten'
    assert row(store, plain)['lifecycle'] == 'active'


def seeded_other(store):
    source = message(store, 5, '小红喜欢在河边跑步', sender='小红')
    return mem(store, '小红喜欢在河边跑步', [source], subject(store, source))


def calls_with(model, memory_id):
    return [c for c in model.calls if memory_id in c[2].get('pair_ids', [])]


def skip_item(report):
    return next((i for i in report['items'] if i['item_key'] == 'skip:superseded_member'), None)


@pytest.mark.parametrize('resolution', ['auto_v1', 'report_only_v1'])
def test_pairs_with_a_superseded_forgotten_member_are_not_planned(store, resolution):
    auto(store, resolution)
    a, b, c = three(store)
    forget(store, a, 'superseded')
    model = AutoModel(store, pair=merge_answer)
    _, report = run(store, model)
    assert calls_with(model, a) == []
    item = skip_item(report)
    assert item['outcome'] == 'skipped' and item['reason'] == 'superseded_member' and item['details']['count'] == 2
    assert row(store, a)['lifecycle'] == 'forgotten' and row(store, a)['merged_into'] is None
    # The ordinary pair of two active memories is unaffected.
    assert {row(store, b)['merged_into'], row(store, c)['merged_into']} - {None} <= {b, c}
    assert any(row(store, mid)['merged_into'] for mid in (b, c))


def test_restore_puts_the_pair_back_into_consolidation(store):
    auto(store)
    a, b, c = three(store)
    forget(store, a, 'superseded')
    model = AutoModel(store)
    run(store, model)
    assert calls_with(model, a) == []
    assert manage_memory(store, a, 1, action='restore')
    _, report = run(store, model)
    assert calls_with(model, a) and skip_item(report) is None


def test_superseded_but_active_memory_is_still_planned(store):
    auto(store, 'report_only_v1')
    a, b, c = three(store)
    forget(store, a, 'superseded')
    with store.write() as conn:  # a report-only suggestion on an active memory is not the limit.
        conn.execute("UPDATE memories SET lifecycle='active',forgotten_at=NULL,retention=50 WHERE id=?", (a,))
    model = AutoModel(store)
    run(store, model)
    assert calls_with(model, a)


# ---- only annotations written by auto_v1 rules carry the limit ---------------------

def report_only_superseded(store, change):
    """A report_only run suggests superseded; the memory then decays to forgotten on its own."""
    auto(store, 'report_only_v1')
    suggestion = {'decision': 'conflict', 'reason': '本人后来改了安排', 'evidence': [change['m1'], change['m2']],
                  'updates': [{'id': change['a'], 'assessment': 'superseded', 'superseded_by': change['b'], 'annotation': '旧安排已被替代'}]}
    from test_consolidation import Model  # fills unique_details for the pair step, as report_only expects
    run(store, Model(store, suggestion))
    notes = annotations(store, change['a'])
    assert [(n['kind'], n['automatic']) for n in notes] == [('superseded', 0)]
    assert adjust_retention(store, change['a'], value=10)['lifecycle'] == 'forgotten'


def test_report_only_superseded_suggestion_does_not_block_restore(store, change):
    report_only_superseded(store, change)
    assert adjust_retention(store, change['a'], 30)['lifecycle'] == 'active'
    with store.read() as conn:
        assert not superseded_forgotten(conn, change['a'])


def test_report_only_superseded_suggestion_does_not_block_planning(store, change):
    report_only_superseded(store, change)
    model = AutoModel(store)
    _, report = run(store, model)
    assert calls_with(model, change['a']) and skip_item(report) is None


def test_auto_superseded_annotation_still_blocks_restore_and_planning(store, change):
    run(store, conflict_model(store, change, supersedes(change)))
    assert [(n['kind'], n['automatic']) for n in annotations(store, change['a'])] == [('superseded', 1)]
    assert adjust_retention(store, change['a'], 30)['lifecycle'] == 'forgotten'
    model = AutoModel(store)
    _, report = run(store, model)
    assert calls_with(model, change['a']) == [] and skip_item(report)['details']['count'] == 1


def test_restore_keeps_report_only_suggestions(store, change):
    report_only_superseded(store, change)
    assert manage_memory(store, change['a'], 1, action='restore')
    assert [n['kind'] for n in annotations(store, change['a'])] == ['superseded']
