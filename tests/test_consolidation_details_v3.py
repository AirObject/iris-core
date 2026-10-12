"""CO-03: detail containment and grounded report writes, with local models only."""
import json

import pytest

from iris.consolidation import snapshot
from iris.maintenance import Maintenance
from iris.models import ModelReply
from test_consolidation import pair, run, merge_answer
from test_lifecycle import Clock
from test_merge_details import bodies, DetailModel


@pytest.mark.parametrize('qualifier', [
    '一直', '总是', '经常', '偶尔', '每次', '多年', '从小', '只', '都', '非常', '稍微',
])
@pytest.mark.parametrize('side', ['a', 'b'])
def test_omitted_qualifier_selects_original_that_preserves_it(store, qualifier, side):
    a, b = pair(store)
    full = a if side == 'a' else b
    plain = b if side == 'a' else a
    action = '喜欢用纸本记录梦' if qualifier in ('非常', '稍微') else '用纸本记录梦'
    texts = {full: f'我{qualifier}{action}', plain: f'我{action}'}
    bodies(store, a, b, texts[a], texts[b])
    _, _, report = run(store, DetailModel(store, {'a': [], 'b': []}, plain))
    with store.read() as conn:
        assert snapshot(conn, full)['content'] == texts[full]
        assert snapshot(conn, plain)['merged_into'] == full
        result = json.loads(conn.execute('SELECT result_json FROM consolidation_work').fetchone()[0])
    assert report['summary'].get('merged', {}).get('count', 0) == 1
    assert result['merge_detail_check']['outcome'] == 'one_sided'
    assert qualifier in result['unique_details'][side]


def test_mutual_qualifiers_are_not_automatically_merged(store):
    a, b = pair(store)
    bodies(store, a, b, '我经常用纸本记录梦', '我偶尔用纸本记录梦')
    _, _, report = run(store, DetailModel(store, {'a': [], 'b': []}, a))
    with store.read() as conn:
        assert all(snapshot(conn, mid)['merged_into'] is None for mid in (a, b))
        assert all(snapshot(conn, mid)['annotations'] for mid in (a, b))
    assert report['summary'].get('merged', {}).get('count', 0) == 0
    assert report['summary']['conflicts']['count'] == 1


@pytest.mark.parametrize('left,right', [
    ('我星期六教口琴', '我周六教口琴'),
    ('我10点教口琴', '我十点教口琴'),
    ('我用纸本记录梦', '我用纸质本子记录梦'),
    ('我一直用纸本记录梦', '我一直用纸质本子记录梦'),
    ('我养一只猫', '我养一只小猫'),
])
def test_equivalent_spelling_and_classifier_are_not_false_detail_guards(store, left, right):
    a, b = pair(store)
    bodies(store, a, b, left, right)
    _, _, report = run(store, DetailModel(store, {'a': [], 'b': []}, b))
    assert report['summary'].get('merged', {}).get('count', 0) == 1
    with store.read() as conn:
        assert snapshot(conn, a)['merged_into'] == b


def test_each_week_already_covers_redundant_dou(store):
    a, b = pair(store)
    bodies(store, a, b, '我每周在线教口琴', '我每周都教口琴')
    _, _, report = run(store, DetailModel(store, {'a': ['在线'], 'b': []}, b))
    assert report['summary'].get('merged', {}).get('count', 0) == 1
    with store.read() as conn:
        assert snapshot(conn, b)['merged_into'] == a


def test_resumed_decision_cannot_drop_qualifier(store):
    a, b = pair(store)
    bodies(store, a, b, '我习惯用纸本记录梦', '我记梦一直用纸质本子')
    model = DetailModel(store, {'a': [], 'b': []}, a)
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    engine.run(rid, stop=lambda: bool(model.requests))
    with store.write() as conn:
        row = conn.execute('SELECT id,payload_json FROM consolidation_work').fetchone()
        result = merge_answer(json.loads(row['payload_json']))
        result['keep_id'] = a
        conn.execute('UPDATE consolidation_work SET result_json=? WHERE id=?',
                     (json.dumps(result), row['id']))
    engine.run(rid)
    with store.read() as conn:
        assert snapshot(conn, a)['merged_into'] == b
        assert snapshot(conn, b)['content'] == '我记梦一直用纸质本子'
    assert len(model.requests) == 1


def named_pair(store):
    a, b = pair(store)
    bodies(store, a, b, '尹晴说展览在10点开始', '尹晴说展览在十点开始')
    with store.write() as conn:
        for sid, name in [('yinqing', '尹晴'), ('yinqiao', '尹乔')]:
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",
                         (sid, name, Clock()().isoformat()))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('yinqing','小晴')")
        conn.execute("UPDATE memories SET speaker_subject_id='yinqing'")
        conn.execute("UPDATE memory_subjects SET subject_id='yinqing'")
        conn.execute("UPDATE messages SET sender_subject_id='yinqing'")
    return a, b


class Reports:
    health = None

    def __init__(self, store, reason, annotation=None):
        self.store, self.reason = store, reason
        self.annotation = annotation or reason
        self.requests = []

    def chat(self, messages, purpose, *args, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[1]['content'])
        self.requests.append((purpose, payload))
        evidence = sorted({s['id'] for m in payload['memories'] for s in m['sources']})
        result = {'decision': 'conflict', 'reason': '来源需要复核', 'evidence': evidence,
                  'unique_details': {'a': [], 'b': []}, 'updates': []}
        if purpose == 'consolidation_conflict':
            result = {'decision': 'conflict', 'reason': self.reason, 'evidence': evidence,
                      'updates': [{'id': payload['pair_ids'][0], 'assessment': 'disputed',
                                   'annotation': self.annotation}]}
        return ModelReply(json.dumps(result, ensure_ascii=False), 'stop', {})


@pytest.mark.parametrize('bad', [
    'yinqiao看到的通知与另一来源不同',
    '尹乔看到的通知与另一来源不同',
    '主体[yinqiao|尹乔]看到的通知不同',
    '主体[yinqing|尹乔]看到的通知不同',
    '主体[yinqing|陌生名]看到的通知不同',
    '来源写明展览77点开始',
    '来源写明展览七十七点开始',
    '依据记忆99999的内容',
])
@pytest.mark.parametrize('field', ['reason', 'annotation'])
def test_unknown_subject_name_or_number_is_invalid_output_without_any_write(store, bad, field):
    a, b = named_pair(store)
    kwargs = {'reason': '来源尚待核对', 'annotation': '来源尚待核对', field: bad}
    model = Reports(store, **kwargs)
    _, _, report = run(store, model)
    failures = [i for i in report['items'] if i['outcome'] == 'failed']
    assert len(failures) == 1 and failures[0]['reason'] == 'invalid_output'
    assert len(model.requests) == 2  # Classification + report; no spelling repair.
    with store.read() as conn:
        assert all(not snapshot(conn, mid)['annotations'] for mid in (a, b))
        assert not conn.execute("SELECT 1 FROM memory_revisions WHERE actor='consolidation'").fetchone()


@pytest.mark.parametrize('text', [
    '尹晴与yinqing是本次材料中的同一主体，来源尚待核对',
    '主体[yinqing|尹晴]的来源需要复核',
    '主体[yinqing|小晴]的来源需要复核',
    '来源消息1与消息2都记载十点开始，尚待复核',
    '记忆1与记忆2的来源记载10点开始',
    '此前摘要范围过宽，按原话修正',
])
def test_visible_subject_alias_and_numeric_paraphrase_can_be_reported(store, text):
    a, b = named_pair(store)
    model = Reports(store, text)
    _, _, report = run(store, model)
    assert report['summary'].get('conflicts', {}).get('count', 0) == 1
    with store.read() as conn:
        assert snapshot(conn, a)['annotations'][0]['text'] == text
    public = model.requests[-1][1]
    subjects = {s['id']: s for m in public['memories'] for s in m.get('subjects', [])}
    assert subjects['yinqing']['name'] == '尹晴'
    assert '小晴' in subjects['yinqing']['aliases']
    assert 'yinqiao' not in subjects  # Existing database subject, outside this material.


@pytest.mark.parametrize('quoted,accepted', [
    ('“两个月吉他维修入门课”', True),
    ('“学吉他两个月”', False),
])
def test_report_cannot_change_object_inside_a_claimed_quote(store, quoted, accepted):
    a, b = named_pair(store)
    bodies(store, a, b, '尹晴上了两个月吉他维修入门课', '尹晴参加吉他维修入门课')
    text = f'来源提到{quoted}，不能据此增加确定性'
    _, _, report = run(store, Reports(store, text))
    with store.read() as conn:
        assert bool(snapshot(conn, a)['annotations']) is accepted
    if not accepted:
        assert [i['reason'] for i in report['items'] if i['outcome'] == 'failed'] == ['invalid_output']


def test_resumed_report_is_revalidated_before_writing(store):
    a, b = named_pair(store)
    model = Reports(store, '来源尚待核对')
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    engine.run(rid, stop=lambda: len(model.requests) == 2)
    with store.write() as conn:
        row = conn.execute('SELECT id,result_json FROM consolidation_work').fetchone()
        result = json.loads(row['result_json'])
        result['reason'] = 'yinqiao看到的通知不同'
        conn.execute('UPDATE consolidation_work SET result_json=? WHERE id=?',
                     (json.dumps(result), row['id']))
    engine.run(rid)
    with store.read() as conn:
        assert not snapshot(conn, a)['annotations']
    assert [i['reason'] for i in engine.report(rid)['items'] if i['outcome'] == 'failed'] == ['invalid_output']
    assert len(model.requests) == 2
