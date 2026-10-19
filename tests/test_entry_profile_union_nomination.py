"""EF-27 nomination experiment: independent fake calls, unchanged admission."""
import copy
import json
from datetime import timedelta

import pytest

from iris import entry_profile as ep
from iris.db import dumps
from iris.models import ModelError
from test_entry_profile_incremental import (
    Gateway, NOW, TOPIC, accept, add, fixed_projection_clock, profiles,
    projection, propose, run, source,
)


def enabled(store, value=True):
    store.set_setting('entry_profile', {'union_nomination': value})


def observe_calls(gateway):
    return [c for c in gateway.calls if 'observe' in c['purpose']]


def work_row(store):
    with store.read() as conn:
        return dict(conn.execute("SELECT * FROM entry_profile_incremental_work WHERE kind='observe' ORDER BY id DESC").fetchone())


def target_change(mid, body='', action='retire'):
    return dict(op='change', memory_id=mid, category='topic', body=body,
                saying='', meaning='', definition_refs=[], source_refs=[], action=action)


def test_union_is_default_off_and_strict_boolean(store):
    with store.read() as conn:
        assert ep._runtime(conn)['union_nomination'] is False
    for value in (1, 0, 'true', None):
        enabled(store, value)
        with store.read() as conn, pytest.raises(ValueError):
            ep._runtime(conn)


def test_disabled_keeps_one_original_request_and_result_shape(store):
    add(store, '轻量背包和结实背包怎么取舍？')
    enabled(store, False)
    gateway = Gateway(store)
    run(store, gateway)
    assert len(observe_calls(gateway)) == 1
    row = work_row(store)
    assert 'union_nomination' not in json.loads(row['payload_json'])['_snapshot']
    assert list(json.loads(row['outputs_json'])) == ['observe']
    assert 'nomination_union' not in json.loads(row['result_json'])


def test_independent_identical_requests_merge_duplicates_without_admitting_one_day(store):
    add(store, '轻量背包和结实背包怎么取舍？')
    enabled(store)
    gateway = Gateway(store)
    run(store, gateway)
    calls = observe_calls(gateway)
    assert len(calls) == 2 and calls[0]['payload'] == calls[1]['payload']
    assert len(profiles(store)) == 1 and profiles(store)[0]['independent_uses'] == 1
    assert profiles(store)[0]['retention'] == 42 and projection(store) is None
    row = work_row(store)
    assert len(json.loads(row['calls_json'])) == 2
    assert set(json.loads(row['outputs_json'])) == {'observe_nomination_1', 'observe_nomination_2'}
    result = json.loads(row['result_json'])
    assert len(result['generated']) == 1 and len(result['generated'][0]['source_refs']) == 1
    assert result['nomination_union']['rounds'] == 2


def test_second_observation_can_discover_an_additional_candidate(store):
    add(store, '轻量背包怎么取舍，也讨论不同鞋子的透气性。')
    enabled(store)
    gateway = Gateway(store)
    # Both calls see the same message; the test chooses different supported hypotheses.
    def observe(payload):
        return propose(payload, body=TOPIC if len(observe_calls(gateway)) == 1 else '讨论徒步鞋子的取舍。')
    gateway.observations = observe
    run(store, gateway)
    assert len(profiles(store)) == 2
    assert all(not p['usable'] and p['independent_uses'] == 1 for p in profiles(store))


def test_union_merges_bound_sources_and_preserves_definition_refs():
    one = dict(op='propose', category='saying', body='落笔：先列要点', saying='落笔', meaning='先列要点',
               definition_refs=[1], source_refs=[{'message_id': 1, 'field': 'content', 'excerpt': '落笔就是先列要点。'}])
    two = copy.deepcopy(one)
    two['source_refs'].append({'message_id': 2, 'field': 'content', 'excerpt': '先落笔。'})
    merged, diagnostics = ep.merge_observation_nominations([{'observations': [one]}, {'observations': [two]}], [])
    assert len(merged['observations']) == 1
    assert merged['observations'][0]['source_refs'] == two['source_refs']
    assert merged['observations'][0]['definition_refs'] == [1]
    assert diagnostics['input_items'] == 2 and diagnostics['merged_items'] == 1
    assert one['source_refs'] == [two['source_refs'][0]]


@pytest.mark.parametrize('keep', ['confirm', 'propose', 'revise'])
def test_retire_conflict_keeps_existing_target(keep):
    directory = [{'memory_id': 7, 'category': 'topic', 'body': TOPIC}]
    retire = target_change(7)
    retain = (dict(op='confirm', memory_id=7, source_refs=[]) if keep == 'confirm' else
              dict(op='propose', category='topic', body=TOPIC, saying='', meaning='', definition_refs=[], source_refs=[]) if keep == 'propose' else
              target_change(7, '讨论徒步装备的适用条件。', 'revise'))
    merged, diagnostics = ep.merge_observation_nominations([{'observations': [retire]}, {'observations': [retain]}], directory)
    assert not any(x.get('action') == 'retire' for x in merged['observations'])
    assert diagnostics['conflicts']
    if keep == 'revise': assert not merged['observations']


def test_incompatible_revisions_are_not_arbitrarily_selected():
    left = target_change(7, TOPIC, 'revise')
    right = target_change(7, '讨论徒步鞋子的取舍。', 'revise')
    merged, diagnostics = ep.merge_observation_nominations([{'observations': [left]}, {'observations': [right]}], [])
    assert merged == {'observations': []} and diagnostics['conflicts']


def test_duplicate_confirmation_only_confirms_once(store):
    add(store, '轻量背包和结实背包怎么取舍？')
    run(store, Gateway(store))
    mid = profiles(store)[0]['memory_id']
    add(store, '防水鞋和透气鞋怎么取舍？', 3, sender='乙')
    enabled(store)
    gateway = Gateway(store, lambda p: {'observations': [dict(op='confirm', memory_id=mid,
        source_refs=[source(m) for m in p['messages']])]})
    run(store, gateway)
    assert len(observe_calls(gateway)) == 2
    assert profiles(store)[0]['admission_state'] == 'accepted'
    result = json.loads(work_row(store)['result_json'])
    assert len(result['applied']) == 1
    assert len(result['generated'][0]['source_refs']) == 1


def test_each_nomination_has_independent_deadline_and_repair_shares_its_deadline(store):
    add(store, '轻量背包和结实背包怎么取舍？')
    enabled(store)
    def hook(g, purpose, payload):
        if 'observe' in purpose: g.tick += 60
    gateway = Gateway(store, hook=hook)
    def observe(payload):
        return '{broken' if len(observe_calls(gateway)) == 1 else propose(payload)
    gateway.observations = observe
    run(store, gateway)
    calls = observe_calls(gateway)
    assert len(calls) == 3
    assert [c['deadline'] for c in calls] == [180, 180, 300]
    row = work_row(store)
    assert [c['nomination_round'] for c in json.loads(row['calls_json'])] == [1, 1, 2]


def test_call_budget_resume_does_not_repeat_completed_first_nomination(store):
    add(store, '轻量背包和结实背包怎么取舍？')
    enabled(store)
    gateway = Gateway(store)
    def observe(payload):
        if len(observe_calls(gateway)) == 2:
            raise ModelError('budget', 'quota', reason='call_budget')
        return propose(payload)
    gateway.observations = observe
    run(store, gateway)
    assert not profiles(store)
    row = work_row(store)
    assert row['state'] == 'deferred'
    assert len(json.loads(row['parsed_json'])['_union_rounds']) == 1
    run(store, gateway, now=NOW + timedelta(hours=1))
    assert len(observe_calls(gateway)) == 3
    assert len(profiles(store)) == 1 and profiles(store)[0]['retention'] == 42
    assert len(json.loads(work_row(store)['calls_json'])) == 3


def test_setting_change_during_second_call_prevents_writeback(store):
    add(store, '轻量背包和结实背包怎么取舍？')
    enabled(store)
    def hook(g, purpose, payload):
        if len(observe_calls(g)) == 2: enabled(store, False)
    gateway = Gateway(store, hook=hook)
    run(store, gateway)
    assert len(observe_calls(gateway)) == 2 and not profiles(store)
    assert work_row(store)['reason'] == 'settings_changed'


def test_equivalent_change_key_order_does_not_create_a_conflict():
    edit = target_change(7, TOPIC, 'revise')
    reordered = dict(reversed(list(edit.items())))
    merged, diagnostics = ep.merge_observation_nominations([{'observations': [edit]}, {'observations': [reordered]}], [])
    assert len(merged['observations']) == 1 and not diagnostics['conflicts']


def test_propose_keeps_all_identical_directory_targets_from_retirement():
    directory = [dict(memory_id=i, category='topic', body=TOPIC) for i in (7, 8)]
    keep = dict(op='propose', category='topic', body=TOPIC, saying='', meaning='', definition_refs=[], source_refs=[])
    merged, _ = ep.merge_observation_nominations([{'observations': [target_change(7)]}, {'observations': [keep]}], directory)
    assert not any(x.get('action') == 'retire' for x in merged['observations'])


def test_retire_keep_conflict_does_not_withdraw_an_accepted_memory(store):
    add(store, '轻量背包和结实背包怎么取舍？')
    run(store, Gateway(store))
    mid = profiles(store)[0]['memory_id']
    add(store, '防水鞋和透气鞋怎么取舍？', 3, sender='乙')
    confirm = lambda p: {'observations': [dict(op='confirm', memory_id=mid, source_refs=[source(m) for m in p['messages']])]}
    run(store, Gateway(store, confirm))
    before = projection(store)['text']
    add(store, '关于背包和鞋子，我还想讨论重量与透气性。', 5, sender='丙')
    enabled(store)
    gateway = Gateway(store)
    def observe(payload):
        if len(observe_calls(gateway)) == 1:
            edit = target_change(mid); edit['source_refs'] = [source(m) for m in payload['messages']]
            return {'observations': [edit]}
        return confirm(payload)
    gateway.observations = observe
    run(store, gateway)
    assert projection(store)['text'] == before and profiles(store)[0]['usable']
    assert all(c['payload'].get('memory', {}).get('action') != 'retire' for c in gateway.calls)


def test_late_second_nomination_never_applies_the_first_one_alone(store):
    add(store, '轻量背包和结实背包怎么取舍？')
    enabled(store)
    gateway = Gateway(store)
    def hook(g, purpose, payload):
        if 'observe' in purpose: g.tick += 10 if len(observe_calls(g)) == 1 else 181
    gateway.hook = hook
    run(store, gateway)
    assert len(observe_calls(gateway)) == 2 and not profiles(store)
    assert work_row(store)['reason'] == 'timeout'
