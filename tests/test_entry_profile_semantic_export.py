"""EF-23: export actual source intervals and structured saying bodies, offline only."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iris.db import dumps
from evals.entry_profile_eval.memory_runner import _incremental_stages, run_sqlite
from test_entry_profile_incremental import Gateway


def message():
    return dict(message_id=11, ref='msg:11', entry_id='group', kind='message',
        speaker='转述者', content='开场白。比较容器的密封性；比较容器的清洗难度。收尾。',
        date='2026-10-01', occurred_at='2026-10-01T12:00:00+08:00',
        quote=dict(ref='original:1', speaker='原作者', content='引文前言。搭桥。搭桥就是连接双方意见。引文结尾。'))


def work(raw, *, messages=None, profiles=None):
    payload=dict(messages=[message()] if messages is None else messages, profiles=profiles or [],
        as_of='2026-10-02T03:00:00+08:00')
    return dict(id=1, kind='observe', payload_json=dumps(payload),
        result_json=dumps(dict(generated=[raw], applied=[], deterministic=[])), parsed_json=None, state='applied')


def proposal(ref):
    return dict(op='propose', category='topic', body='比较容器的密封性与清洗难度。',
        saying='', meaning='', definition_refs=[], source_refs=[ref])


@pytest.mark.parametrize(('field', 'expected', 'speaker'), [
    ('content', '比较容器的密封性；比较容器的清洗难度。', '转述者'),
    ('quote', '搭桥。搭桥就是连接双方意见。', '原作者'),
])
def test_interval_export_selects_contiguous_original_and_preserves_author(field, expected, speaker):
    raw=proposal(dict(message_id=11, field=field, start_segment=1, end_segment=3))
    item=work(raw);before=copy.deepcopy(item)
    out=_incremental_stages([item], [])['generated'][0]['sources'][0]
    assert out['excerpt']==expected and out['speaker']==speaker
    assert out['message']['content']==message()['content']
    assert out['message']['quote']['content']==message()['quote']['content']
    assert out['ref']=='msg:11' and item==before
    assert 'start_segment' not in out and 'source_id' not in out


@pytest.mark.parametrize('changes', [dict(end_segment=99), dict(message_id=999), dict(start_segment=True)])
def test_invalid_interval_stays_an_unbound_claim_without_fabricated_context(changes):
    ref=dict(message_id=11, field='content', start_segment=1, end_segment=3)
    ref.update(changes)
    out=_incremental_stages([work(proposal(ref))], [])['generated'][0]['sources'][0]
    assert out==ref
    assert 'message' not in out and 'speaker' not in out and 'excerpt' not in out


@pytest.mark.parametrize(('meaning', 'expected'), [('连接双方意见', '搭桥：连接双方意见'), ('', '搭桥')])
def test_saying_export_uses_structural_fields_even_when_raw_body_differs(meaning, expected):
    raw=proposal(dict(message_id=11, field='quote', excerpt='搭桥。'))
    raw.update(category='saying', body='不应作为实际正文的旧字段', saying='搭桥', meaning=meaning,
        definition_refs=[11] if meaning else [])
    assert _incremental_stages([work(raw)], [])['generated'][0]['body']==expected


def test_confirm_preserves_directory_body_and_legacy_excerpt():
    ref=dict(message_id=11, field='quote', excerpt='搭桥。')
    raw=dict(op='confirm', memory_id=5, source_refs=[ref])
    out=_incremental_stages([work(raw, profiles=[dict(memory_id=5, category='saying', body='搭桥')])], [])
    candidate=out['generated'][0]
    assert candidate['memory_id']==5 and candidate['body']=='搭桥'
    assert candidate['sources'][0]['excerpt']=='搭桥。'
    assert candidate['sources'][0]['speaker']=='原作者'
    assert not out['checked'] and not out['projected']


def test_real_writer_export_matches_persisted_saying_and_host_projection(tmp_path):
    case=dict(id='unit-export', entry=dict(id='group', kind='group'), timezone='Asia/Shanghai',
        as_of='2026-10-03T23:00:00+08:00', initial_profile='', memories=[], must_cover=['搭桥用法及定义'], forbidden=[],
        messages=[dict(id='a', sender='甲', text='先聊别的。搭桥。搭桥就是连接双方意见。', at='2026-10-01T12:00:00+08:00'),
                  dict(id='b', sender='乙', text='今天不谈别的。搭桥。', at='2026-10-03T12:00:00+08:00')])
    path=tmp_path/'case.json';path.write_text(dumps(dict(format_version=1, cases=[case])))
    def observe(p):
        m=p['messages'][0]
        ref=dict(message_id=m['message_id'], field='content', start_segment=1, end_segment=2 if p['profiles'] else 3)
        raw=(dict(op='confirm', memory_id=p['profiles'][0]['memory_id'], source_refs=[ref]) if p['profiles'] else
             dict(op='propose', category='saying', body='', saying='搭桥', meaning='连接双方意见',
                  definition_refs=[m['message_id']], source_refs=[ref]))
        return dict(observations=[raw])
    out=tmp_path/'out';manifest=run_sqlite(path, out, gateway_factory=lambda _,store:Gateway(store,observe))
    doc=json.loads((out/'judging-materials/cases/0001.json').read_text())['input']
    assert len(doc['memories'])==1
    actual=doc['memories'][0]
    assert actual['body']=='搭桥：连接双方意见'
    assert actual['body'] in doc['projection']['text']
    assert doc['projection']['memory_ids']==[actual['memory_id']]
    assert [s['excerpt'] for s in actual['sources']]==['搭桥。搭桥就是连接双方意见。','搭桥。']
    assert {s['speaker'] for s in actual['sources']}=={'甲','乙'}
    assert all(c['body']==actual['body'] for c in doc['stages']['generated'])
    assert all(c['body']==actual['body'] for c in doc['stages']['checked'])
    assert not manifest['metadata']['gate_eligible'] and manifest['metadata']['invalidated'] is None
    trace=json.loads((out/'run.json').read_text())['cases'][0]
    raw=json.loads(next(w for w in trace['work'] if w['kind']=='observe')['result_json'])['generated'][0]
    assert raw['body']=='' and 'start_segment' in raw['source_refs'][0]
    assert not any('"'+key+'"' in dumps(doc) for key in ('decision','point_support','group_reviews','usage_reviews','reason'))
