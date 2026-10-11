"""Fake-only mechanism fixtures; these are not the frozen quality corpus."""
import importlib.util
import json
from pathlib import Path

import pytest

from iris.db import dumps
spec=importlib.util.spec_from_file_location('person_summary_runner',Path(__file__).parents[1]/'evals/person_summary_eval/runner.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
load_corpus,run_eval,score,_Responses=runner.load_corpus,runner.run_eval,runner.score,runner._Responses


def fixture_case():
    return {'id':'tiny','subject_id':'a','as_of':'2026-10-11T12:00:00+00:00',
        'subjects':[{'id':'a','name':'测试人'}],
        'entries':[{'id':'local','kind':'group','visibility':'entry_only'}],
        'memories':[{'id':f'm{i}','content':'公开机制测试内容','speaker':'a',
                     'created_at':f'2026-10-{i+1:02}T00:00:00+00:00'} for i in range(8)]+[
                     {'id':'private','content':'紫色密钥盒测试标记','speaker':'a','source_entries':['local'],
                      'importance':100,'created_at':'2026-10-01T00:00:00+00:00'}],
        'must_cover':['公开机制测试要点'],'forbidden':['入口限定事项'],
        'leak_probes':[{'memory_id':'private','text':'紫色密钥盒测试标记'}]}


def bundle(tmp_path, *, leaking=False):
    case=fixture_case(); path=tmp_path/'tiny.json'
    path.write_text(dumps({'format_version':1,'cases':[case]}),encoding='utf-8')
    model=_Responses([{'items':[{'kind':'fact','text':'紫色密钥盒测试标记' if leaking else '公开机制测试内容','memory_refs':['M1']}]},
        {'items':[{'index':1,'supported':True,'scene_qualified':True,'violations':[],'reason':'流程测试'}],
         'change_degree':'small','reason':'流程测试'}])
    report=run_eval(path,tmp_path/'out',gateway_factory=lambda configs,store:model)
    return report,case


def make_round(tmp_path,report,*,supported=True,covered=True,forbidden=False):
    materials=Path(report['materials']); path=tmp_path; path.mkdir()
    (path/'manifest.json').write_bytes((materials/'round-template.json').read_bytes())
    (path/'0000.json').write_text(dumps({'item_results':[{'index':1,'supported':supported,
        'violations':[] if supported else ['无依据'],'reason':'假判分结构测试'}],
        'must_cover':[covered],'forbidden':[forbidden]}),encoding='utf-8')
    return path


def test_isolated_material_export_adverse_two_rounds_and_no_annotations_in_prompt(tmp_path):
    report,case=bundle(tmp_path)
    assert report['metrics']['scoped_leaks']==0
    assert report['metrics']['scoped_memories_checked']==1
    assert report['metrics']['meets_threshold'] is False
    material=json.loads(report['rows'][0]['attempts'][0]['material_json'])
    assert '紫色密钥盒测试标记' not in dumps(material)
    assert case['must_cover'][0] not in dumps(material)
    a=make_round(tmp_path/'round-a',report)
    b=make_round(tmp_path/'round-b',report,supported=False,covered=False,forbidden=True)
    result=score(report['materials'],[a,b],judge_model='fake-format-test',out=tmp_path/'score')
    assert result['metrics']['grounded_item_ratio']==0
    assert result['metrics']['forbidden_occurrences']==1
    assert result['metrics']['must_cover_coverage']==0
    assert result['disagreements'] and not result['metrics']['meets_threshold']


def test_leak_probe_is_a_deterministic_gate_independent_of_external_judge(tmp_path):
    report,_=bundle(tmp_path,leaking=True)
    assert report['metrics']['scoped_leaks']>0
    a=make_round(tmp_path/'round',report)
    result=score(report['materials'],[a],judge_model='fake',out=tmp_path/'score')
    assert result['metrics']['grounded_item_ratio']==1
    assert not result['metrics']['meets_threshold']


@pytest.mark.parametrize('change',['bool','count','index','path','fingerprint'])
def test_reject_malformed_or_mismatched_judgments(tmp_path,change):
    report,_=bundle(tmp_path); path=make_round(tmp_path/'round',report)
    vote=json.loads((path/'0000.json').read_text())
    if change=='bool': vote['must_cover']=[1]
    elif change=='count': vote['item_results']=[]
    elif change=='index': vote['item_results'][0]['index']=True
    elif change=='fingerprint':
        (path/'manifest.json').write_text(dumps({'materials_sha256':'wrong'}))
    elif change=='path':
        copy_path=tmp_path/'external.json'; copy_path.write_text(dumps(vote))
        (path/'0000.json').unlink(); (path/'0000.json').symlink_to(copy_path)
    if change!='path': (path/'0000.json').write_text(dumps(vote))
    with pytest.raises(ValueError):
        score(report['materials'],[path],judge_model='fake',out=tmp_path/'score')


def test_loader_rejects_ambiguous_leak_marker_and_missing_parent(tmp_path):
    case=fixture_case(); case['leak_probes'][0]['text']='公开'
    path=tmp_path/'bad.json'
    path.write_text(dumps({'format_version':1,'cases':[case]}))
    with pytest.raises(ValueError): load_corpus(path)
    case=fixture_case(); case['memories'][0]['derived_from']=['missing']
    path.write_text(dumps({'format_version':1,'cases':[case]}))
    with pytest.raises(ValueError): load_corpus(path)


def test_unicode_escaped_model_output_cannot_bypass_leak_check(tmp_path,monkeypatch):
    from iris.models import ModelReply
    original=_Responses.chat
    def escaped(self,*args,**kwargs):
        reply=original(self,*args,**kwargs)
        return ModelReply(json.dumps(json.loads(reply.content),ensure_ascii=True),reply.finish_reason,reply.usage)
    monkeypatch.setattr(_Responses,'chat',escaped)
    report,_=bundle(tmp_path,leaking=True)
    assert report['metrics']['scoped_leaks']>0


def test_quoted_multiline_probe_uses_text_not_json_serialization(tmp_path):
    case=fixture_case(); phrase='紫色"盒子\n测试标记'
    case['memories'][-1]['content']=phrase
    case['leak_probes'][0]['text']=phrase
    path=tmp_path/'quoted.json';path.write_text(dumps({'format_version':1,'cases':[case]}),encoding='utf-8')
    model=_Responses([{'items':[{'kind':'fact','text':phrase,'memory_refs':['M1']}]},
        {'items':[{'index':1,'supported':True,'scene_qualified':True,'violations':[],'reason':'格式测试'}],
         'change_degree':'small','reason':'格式测试'}])
    report=run_eval(path,tmp_path/'out',gateway_factory=lambda configs,store:model)
    assert report['metrics']['scoped_leaks']>0
