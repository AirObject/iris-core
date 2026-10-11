"""GD2 threshold affects weak review hints, never the automatic-merge guards."""
import pytest

from iris import goal_dedup_judge as dedup
from iris.goals import Goals
from test_goal_dedup_apply import Judge
from test_goal_dedup_judge import goal


@pytest.mark.parametrize('threshold,expected', [(0, 'possible_duplicate'), (.59, 'possible_duplicate'),
    (.6, 'possible_duplicate'), (.61, 'created'), (1, 'created')])
def test_weak_hint_uses_inclusive_similarity_threshold(threshold, expected):
    result = dedup.decision_from_verdicts({1: 'uncertain'}, origin='host', scores={1: .6},
                                        min_similarity=threshold)
    assert result['status'] == expected


@pytest.mark.parametrize('source', ['model_uncertain', 'sequence_conflict', 'overflow'])
def test_suppressed_hint_never_unblocks_a_merge(source):
    result = dedup.decision_from_verdicts({1: 'same', **({2: 'uncertain'} if source == 'model_uncertain' else {})},
        origin='host', scores={1: 1, 2: .1}, min_similarity=.8,
        possible_ids=[2] if source == 'sequence_conflict' else [], overflow=source == 'overflow')
    assert result['status'] == 'possible_duplicate' and result['target_ids'] == [1]


def test_strong_same_for_admin_stays_visible_even_with_low_text_overlap():
    result = dedup.decision_from_verdicts({1: 'same'}, origin='admin', scores={1: .1}, min_similarity=.9)
    assert result['status'] == 'possible_duplicate' and result['target_ids'] == [1]
    assert dedup.decision_from_verdicts({1: 'same'}, origin='host', scores={1: .1},
                                      min_similarity=.9)['status'] == 'merged'


@pytest.mark.parametrize('bad', [-.1, 1.1, True, '0.6', float('nan'), float('inf')])
def test_threshold_validation(bad):
    with pytest.raises(ValueError):
        dedup.validate_similarity(bad)


def test_default_and_legacy_rollback_are_explicit():
    assert dedup.settings()['possible_min_similarity'] == 0
    assert dedup.settings()['pending_fallback_minutes'] == 30
    assert dedup.validate_similarity(0) == 0


@pytest.mark.parametrize('saved_threshold,marked', [(None, True), (.6, False)])
def test_calibrated_default_preserves_weak_recognition_and_saved_override(store, saved_threshold, marked):
    options = {'enabled': True}
    if saved_threshold is not None:
        options['possible_min_similarity'] = saved_threshold
    store.set_setting('goal_dedup_judge', options)
    judge = Judge(store)
    goals = Goals(store, gateway=judge)
    old = goals.create(content='检查三台服务器')['goal']
    new = goals.create(content='检查服务器并记录设备的运行状况')['goal']
    assert .25 <= dedup.similarity(old, new) < .6
    assert new['possible_duplicate_ids'] == ([old['id']] if marked else [])
    assert new['merged_into'] is None and not judge.calls
    expected = 0 if saved_threshold is None else saved_threshold
    assert dedup.settings(store)['possible_min_similarity'] == expected
    with store.read() as conn:
        assert dedup.connection_settings(conn)['possible_min_similarity'] == expected
    # A new default must not overwrite the persisted configuration.
    assert store.setting('goal_dedup_judge', {}) == options


def test_runtime_threshold_filters_conflicts_without_model_and_keeps_existing_marks(store):
    store.set_setting('goal_dedup_judge', {'enabled': True, 'possible_min_similarity': .99})
    goals = Goals(store)
    a = goals.create(content='检查三台服务器')['goal']
    b = goals.create(content='检查服务器')['goal']
    assert b['possible_duplicate_ids'] == []
    assert dedup.select_sequence_conflicts([goal(a['id'], a['content'])], goal(b['id'], b['content']))
    store.set_setting('goal_dedup_judge', {'enabled': True, 'possible_min_similarity': 0})
    c = goals.create(content='检查四台服务器')['goal']
    assert a['id'] in c['possible_duplicate_ids'] and b['id'] in c['possible_duplicate_ids']
    store.set_setting('goal_dedup_judge', {'enabled': True, 'possible_min_similarity': 1})
    assert goals.get(c['id'])['possible_duplicate_ids'] == c['possible_duplicate_ids']


def test_runtime_threshold_change_during_call_requires_new_review(store):
    store.set_setting('goal_dedup_judge', {'enabled': True, 'possible_min_similarity': .6})
    judge = Judge(store, verdict='same')
    goals = Goals(store, gateway=judge)
    goals.create(content='整理操作手册')
    judge.hook = lambda: store.set_setting('goal_dedup_judge', {'enabled': True, 'possible_min_similarity': .8})
    result = goals.create(content='整理操作说明手册')
    assert result['dedup']['status'] == 'pending'
    assert result['dedup']['reason'] == 'configuration_changed'
    assert result['goal']['merged_into'] is None


def test_module_settings_validate_persist_and_audit_without_new_http_surface(store):
    goals = Goals(store)
    result = goals.configure_dedup(pending_fallback_minutes=12, possible_min_similarity=.7, actor='admin')
    assert result['pending_fallback_minutes'] == 12 and result['possible_min_similarity'] == .7
    assert dedup.settings(store)['possible_min_similarity'] == .7
    with store.read() as conn:
        operation = conn.execute("SELECT actor,details_json FROM admin_operations WHERE action='goal_dedup_judge_saved'").fetchone()
    assert operation['actor'] == 'admin' and 'pending_fallback_minutes' in operation['details_json']
    for fields in ({'pending_fallback_minutes':-1}, {'pending_fallback_minutes':1441},
                   {'pending_fallback_minutes':True}, {'possible_min_similarity':float('nan')},
                   {'possible_min_similarity':True}, {'possible_min_similarity':1.1}):
        with pytest.raises(ValueError):
            goals.configure_dedup(**fields)
    assert dedup.settings(store)['pending_fallback_minutes'] == 12
