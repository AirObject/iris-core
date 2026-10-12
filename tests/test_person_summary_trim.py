"""EF-04 capacity trimming: synthetic mechanism fixtures, no model or corpus."""
import copy
import json

import pytest

from iris import person_summary as ps
from iris.queue import estimate_tokens
from test_person_summary import Clock, Model, seed


@pytest.fixture
def clock():
    return Clock()


def sentence(kind, body, *, author='model', **extra):
    value = {'kind': kind, 'body': body, 'author': author, 'basis': [], **extra}
    value['text'] = ps._render(value)
    return value


def item(kind, text, **extra):
    return {'kind': kind, 'text': text, 'memory_refs': ['M1'], **extra}


def six_items():
    # Deliberately not sorted by category; ties must retain original order.
    return [
        item('recent', '最近在学木工', time_range='最近'),
        item('preference', '喜欢手工'),
        item('fact', '加入了木工社'),
        item('relationship', '曾与我一起练习木工'),
        item('fact', '有自己的工具箱'),
        item('fact', '学习过绘图'),
    ]


def configure(store, **limits):
    store.set_setting('person_summary', {**store.setting('person_summary'), **limits})


def run(store, clock, items, *, expected=0, **kwargs):
    model = Model(store, items=items, **kwargs)
    result = ps.PersonSummaryEngine(store, model, clock=clock).regenerate('a', expected_version=expected)
    return result, model


def checked(model):
    assert len(model.calls) == 2
    return model.calls[1][1]['candidate']


def test_category_priority_ties_and_original_order_are_stable():
    rows = [
        sentence('recent', '木工课程', time_range='最近'),
        sentence('fact', '先出现的事实'),
        sentence('relationship', '一起练习木工'),
        sentence('fact', '后出现的事实'),
        sentence('preference', '喜欢手工'),
        {'text': '人工条目。', 'author': 'admin', 'basis': []},
    ]
    before = copy.deepcopy(rows)
    config = {**ps.DEFAULTS, 'max_items': 3}
    kept, removed = ps._trim_to_capacity(rows, config)
    assert kept == [rows[1], rows[2], rows[5]]
    assert [r['index'] for r in removed] == [1, 4, 5]
    assert all(r['reason'] == 'too_many_items' and r['stage'] == 'capacity' for r in removed)
    assert [r['sentence'] for r in removed] == [rows[0], rows[3], rows[4]]
    assert rows == before
    assert ps._trim_to_capacity(rows, config) == (kept, removed)


@pytest.mark.parametrize('limit,reason', [('max_chars', 'too_long'), ('max_tokens', 'token_budget')])
def test_whole_item_fit_uses_rendered_text_and_skips_oversized_item(limit, reason):
    long = sentence('fact', '这是一段不能被切断的完整描述。' * 5)
    short = sentence('recent', '学习木工', time_range='截至本周')
    config = {**ps.DEFAULTS, limit: len(short['text']) if limit == 'max_chars' else estimate_tokens(short['text'])}
    kept, removed = ps._trim_to_capacity([long, short], config)
    assert kept == [short]
    assert removed == [{'index': 1, 'sentence': long, 'reason': reason, 'stage': 'capacity'}]
    assert not ps._length_errors(''.join(s['text'] for s in kept), kept, config)


def test_exact_capacity_keeps_every_item_and_metadata_unchanged():
    rows = [sentence('recent', '学木工', time_range='最近', expires_at='2026-11-01T00:00:00+00:00'),
            sentence('fact', '有工具箱')]
    content = ''.join(s['text'] for s in rows)
    limits = {**ps.DEFAULTS, 'max_items': 2, 'max_chars': len(content), 'max_tokens': estimate_tokens(content)}
    assert ps._trim_to_capacity(rows, limits) == (rows, [])


@pytest.mark.parametrize('limits,reason', [
    ({'max_items': 1}, 'too_many_items'),
    ({'max_chars': 3}, 'too_long'),
    ({'max_tokens': 2}, 'token_budget'),
])
def test_manual_only_overflow_is_preserved_for_normal_rejection(limits, reason):
    model = sentence('relationship', '同伴')
    manual = [{'text': '人工记录甲。', 'author': 'admin', 'basis': []},
              {'text': '人工记录乙。', 'author': 'admin', 'basis': []}]
    config = {**ps.DEFAULTS, **limits}
    kept, removed = ps._trim_to_capacity([model, *manual], config)
    assert kept == manual
    assert [r['sentence'] for r in removed] == [model]
    assert ps._length_errors(''.join(s['text'] for s in kept), kept, config) == [reason]


def test_six_items_are_trimmed_before_check_and_diagnostics_persist(store, clock):
    seed(store, clock)
    items = six_items()
    result, model = run(store, clock, items)
    assert result['status'] == 'published'
    assert [s['body'] for s in checked(model)] == [i['text'] for i in items[1:]]
    version = ps.get_version(store, 'a', result['version']['version'], clock=clock)
    removed = version['checks']['trimmed_sentences']
    assert len(removed) == 1 and removed[0]['sentence']['body'] == items[0]['text']
    assert removed[0]['reason'] == 'too_many_items' and removed[0]['stage'] == 'capacity'
    assert removed[0]['sentence']['basis'][0]['revision'] == 1
    assert version['checks']['original_item_count'] == 6
    assert version['checks']['quality_item_count'] == 5
    assert version['checks']['deterministic']['trimmed_count'] == 1
    assert not version['checks']['deleted_sentences']
    with store.read() as conn:
        outputs = json.loads(conn.execute('SELECT outputs_json FROM person_summary_attempts WHERE id=?',
                                         (result['attempt_id'],)).fetchone()[0])
    assert json.loads(outputs['consolidation_person_summary_generate'])['items'] == items


@pytest.mark.parametrize('limit,reason', [('max_chars', 'too_long'), ('max_tokens', 'token_budget')])
def test_length_overflow_keeps_complete_shorter_item_and_checks_it(store, clock, limit, reason):
    seed(store, clock)
    items = [item('fact', '完整的木工练习记录。' * 22), item('fact', '有工具箱')]
    if limit == 'max_tokens':
        items[0]['text'] = '完整的木工练习记录。' * 3
        configure(store, max_tokens=12)
    result, model = run(store, clock, items)
    assert result['status'] == 'published'
    assert [s['body'] for s in checked(model)] == [items[1]['text']]
    removed = result['version']['checks']['trimmed_sentences']
    assert removed[0]['sentence']['body'] == items[0]['text']
    assert removed[0]['reason'] == reason
    assert result['version']['content'] == '稳定事实：有工具箱。\n'


@pytest.mark.parametrize('limit', ['max_items', 'max_chars', 'max_tokens'])
def test_manual_items_reserve_capacity_before_any_model_item(store, clock, limit):
    seed(store, clock)
    manual = ps.admin_edit(store, 'a', '保留手写说明。', expected_version=0, clock=clock)
    items = [item('relationship', '一起学木工'), item('fact', '有工具箱')]
    first_text = '与我的关系：一起学木工。\n'
    limits = {'max_items': 2, 'max_chars': len(first_text + manual['content']),
              'max_tokens': estimate_tokens(first_text + manual['content'])}
    configure(store, **{limit: limits[limit]})
    result, model = run(store, clock, items, expected=1)
    assert result['status'] == 'published'
    candidate = checked(model)
    assert [s.get('body') for s in candidate if s['author'] == 'model'] == [items[0]['text']]
    saved_manual = [s for s in result['version']['sentences'] if s['author'] == 'admin']
    assert saved_manual == manual['sentences']
    assert [r['sentence']['body'] for r in result['version']['checks']['trimmed_sentences']] == [items[1]['text']]


@pytest.mark.parametrize('limits,reason', [
    ({'max_items': 1}, 'too_many_items'),
    ({'max_chars': 3}, 'too_long'),
    ({'max_tokens': 2}, 'token_budget'),
])
def test_reduced_limits_cannot_trim_manual_items_or_replace_current_version(store, clock, limits, reason):
    seed(store, clock)
    manual = ps.admin_edit(store, 'a', '手写甲。手写乙。', expected_version=0, clock=clock)
    assert len(manual['sentences']) == 2
    configure(store, **limits)
    result, model = run(store, clock, [item('fact', '有工具箱')], expected=1)
    assert result['status'] == 'rejected' and result['reason'] == reason
    assert len(model.calls) == 1
    assert result['version']['sentences'] == manual['sentences']
    assert result['version']['checks']['trimmed_sentences'][0]['reason'] == reason
    assert ps.current_summary(store, 'a', clock=clock)['version'] == 1


def test_all_oversized_model_items_still_reject_empty_summary(store, clock):
    seed(store, clock)
    first, _ = run(store, clock, [item('fact', '有工具箱')])
    assert first['status'] == 'published'
    result, model = run(store, clock, [item('fact', '长' * 201)], expected=1)
    assert result['status'] == 'rejected' and result['reason'] == 'empty'
    assert len(model.calls) == 1
    assert result['version']['sentences'] == [] and result['version']['content'] == ''
    assert len(result['version']['checks']['trimmed_sentences']) == 1
    assert ps.current_summary(store, 'a', clock=clock)['version'] == 1


def test_full_manual_capacity_checks_manual_without_dropping_it(store, clock):
    seed(store, clock)
    manual = ps.admin_edit(store, 'a', '人工记录。', expected_version=0, clock=clock)
    configure(store, max_items=1)
    result, model = run(store, clock, six_items(), expected=1)
    assert result['status'] == 'published'
    assert [s['text'] for s in checked(model)] == [manual['content']]
    assert result['version']['sentences'] == manual['sentences']
    assert len(result['version']['checks']['trimmed_sentences']) == 6
    assert result['version']['checks']['quality_item_count'] == 0


@pytest.mark.parametrize('bad,expected', [([0, 1], 'published'), ([0, 1, 2], 'rejected')])
def test_quality_deletion_ratio_excludes_capacity_items(store, clock, bad, expected):
    seed(store, clock)
    result, model = run(store, clock, six_items(), bad=bad)
    assert result['status'] == expected
    assert len(checked(model)) == 5
    checks = result['version']['checks']
    assert checks['quality_item_count'] == 5
    assert len(checks['trimmed_sentences']) == 1
    assert len(checks['deleted_sentences']) == len(bad)
    assert ('too_many_deleted' in checks['errors']) is (len(bad) > 2)


def test_reference_failures_still_count_after_capacity_trim(store, clock):
    seed(store, clock)
    configure(store, max_items=2)
    items = [*six_items(), item('fact', '无效甲', memory_refs=['M999']),
             item('fact', '无效乙', memory_refs=['M999']), item('fact', '无效丙', memory_refs=['M999'])]
    result, model = run(store, clock, items)
    assert result['status'] == 'rejected' and result['reason'] == 'too_many_deleted'
    assert len(model.calls) == 1
    assert result['version']['checks']['quality_item_count'] == 5
    assert len(result['version']['checks']['trimmed_sentences']) == 4


@pytest.mark.parametrize('manual_warning', [False, True])
def test_existing_candidate_rules_still_apply_after_trim(store, clock, manual_warning):
    seed(store, clock)
    if manual_warning:
        ps.admin_edit(store, 'a', '人工记录。', expected_version=0, clock=clock)
        expected = 1
        kwargs = {'bad': [4], 'violations': ['敏感个人信息']}
    else:
        ps.set_settings(store, 'a', expected_version=0, publish_mode='all_manual', clock=clock)
        expected = 0
        kwargs = {}
    result, model = run(store, clock, six_items(), expected=expected, **kwargs)
    assert result['status'] == 'candidate' and len(checked(model)) == 5
    assert bool(result['version']['checks']['admin_warnings']) is manual_warning
    assert (ps.current_summary(store, 'a', clock=clock) or {}).get('version', 0) == expected


def test_purging_trimmed_source_clears_diagnostics_and_original_output(store, clock):
    mids = seed(store, clock)
    items = [item('fact', '被裁掉的完整内容' * 35, memory_refs=['M8']), item('fact', '保留工具箱')]
    result, _ = run(store, clock, items)
    assert result['status'] == 'published'
    assert result['version']['checks']['trimmed_sentences'][0]['sentence']['basis'][0]['memory_id'] == mids[-1]
    with store.write() as conn:
        ps.purge_memory_references(conn, [mids[-1]], clock=clock)
    value = ps.get_version(store, 'a', result['version']['version'], clock=clock)
    assert '被裁掉' not in json.dumps(value, ensure_ascii=False)
    with store.read() as conn:
        attempt = conn.execute('SELECT outputs_json FROM person_summary_attempts WHERE id=?',
                               (result['attempt_id'],)).fetchone()
    assert json.loads(attempt[0]) == {}


def test_evidence_revision_changed_during_check_still_prevents_publish(store, clock):
    mids = seed(store, clock)

    class ConcurrentEdit(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose.endswith('check'):
                with store.write() as conn:
                    conn.execute('UPDATE memories SET revision=revision+1 WHERE id=?', (mids[0],))
            return reply

    model = ConcurrentEdit(store, items=six_items())
    with pytest.raises(ps.PersonSummaryConflict, match='evidence_changed'):
        ps.PersonSummaryEngine(store, model, clock=clock).regenerate('a', expected_version=0)
    assert len(checked(model)) == 5
    assert ps.current_summary(store, 'a', clock=clock) is None
