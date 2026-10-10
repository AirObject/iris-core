"""Synthetic serialization fixtures only: these are not entry-profile dev corpora."""
import copy
import json
from pathlib import Path

import pytest

from iris.db import dumps
import importlib.util
_spec=importlib.util.spec_from_file_location('entry_profile_eval_runner',Path(__file__).resolve().parents[1]/'evals/entry_profile_eval/runner.py')
_runner=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)
run_eval,score,load_corpus=_runner.run_eval,_runner.score,_runner.load_corpus
from test_entry_profile import Model, TEXTS


def corpus(tmp_path):
    value={'format_version':1,'cases':[{'id':'fixture','entry':{'id':'room','kind':'group','name':'格式测试'},
        'as_of':'2026-10-10T12:00:00+00:00','messages':[
            {'id':str(i),'at':'2026-10-10T10:00:00+00:00','sender':'self','kind':'self_output','text':'格式测试消息'+str(i)} for i in range(50)],
        'must_cover':['格式测试要点'],'forbidden':['格式测试禁义']}]}
    path=tmp_path/'fixture.json';path.write_text(dumps(value),encoding='utf-8')
    return path


def run(tmp_path):
    return run_eval(corpus(tmp_path),tmp_path/'run',configs={},gateway_factory=lambda configs,store:Model(store))


def judgments(materials,path,*,supported=True,forbidden=False):
    manifest=json.loads((materials/'manifest.json').read_text())
    path.mkdir()
    (path/'manifest.json').write_bytes((materials/'round-template.json').read_bytes())
    for case in manifest['cases']:
        data=json.loads((materials/case['file']).read_text())['input']
        sentences=data['profile']['sentences'] if data['profile'] else []
        vote={'sentence_results':[{'index':i+1,'supported':supported,'violations':[] if supported else ['无依据'],'reason':'格式测试判分'} for i,_ in enumerate(sentences)],
              'must_cover':[supported for _ in data['must_cover']], 'forbidden':[forbidden for _ in data['forbidden']]}
        (path/case['judgment_file']).write_text(dumps(vote),encoding='utf-8')
    return path


def test_runner_exports_bound_materials_and_adverse_double_scores(tmp_path):
    result=run(tmp_path)
    materials=Path(result['materials'])
    assert result['rows'][0]['result']['status']=='published'
    assert (materials/'run.json').exists() and (materials/'scoring.md').exists()
    a=judgments(materials,tmp_path/'a')
    b=judgments(materials,tmp_path/'b',supported=False,forbidden=True)
    report=score(materials,[a,b],judge_model='fake evaluator',out=tmp_path/'score')
    assert report['judge_runs']==2
    assert report['metrics']['grounded_sentence_ratio']==0
    assert report['metrics']['forbidden_occurrences']==1
    assert report['metrics']['must_cover_coverage']==0
    assert report['disagreements']
    assert report['original_judgments'][0][0]['sentence_results'][0]['supported'] is True


@pytest.mark.parametrize('target',['case','run','scoring','manifest'])
def test_tampered_materials_are_rejected(tmp_path,target):
    materials=Path(run(tmp_path)['materials']);a=judgments(materials,tmp_path/'a')
    paths={'case':materials/'cases/0000.json','run':materials/'run.json','scoring':materials/'scoring.md','manifest':materials/'manifest.json'}
    path=paths[target];path.write_text(path.read_text()+' ',encoding='utf-8')
    if target=='manifest':
        value=json.loads(path.read_text());value['run']['source_sha256']='changed';path.write_text(dumps(value))
    with pytest.raises(ValueError):
        score(materials,[a],judge_model='fake evaluator',out=tmp_path/'score')


@pytest.mark.parametrize('kind',['bool','index','count','foreign_round','extra_file','duplicate_round'])
def test_strict_vote_shape_and_fingerprint(tmp_path,kind):
    materials=Path(run(tmp_path)['materials']);a=judgments(materials,tmp_path/'a')
    path=a/'0000.json';v=json.loads(path.read_text())
    if kind=='bool': v['sentence_results'][0]['supported']=1
    elif kind=='index':v['sentence_results'][0]['index']=2
    elif kind=='count':v['must_cover']=[]
    elif kind=='foreign_round':(a/'manifest.json').write_text('{"materials_sha256":"other"}')
    elif kind=='extra_file':(a/'other.json').write_text('{}')
    path.write_text(dumps(v),encoding='utf-8')
    with pytest.raises(ValueError):
        score(materials,[a,a] if kind=='duplicate_round' else [a],judge_model='fake evaluator',out=tmp_path/'score')


def test_explicit_corpus_only_and_source_times_checked(tmp_path):
    path=corpus(tmp_path);v=json.loads(path.read_text())
    v['cases'][0]['messages'][0]['at']='2027-01-01T00:00:00Z'
    path.write_text(dumps(v),encoding='utf-8')
    with pytest.raises(ValueError):load_corpus(path)


def test_rejected_candidate_exported_and_not_scored_as_published(tmp_path):
    result=run_eval(corpus(tmp_path),tmp_path/'run',configs={},gateway_factory=lambda configs,store:Model(store,bad=(0,1)))
    materials=Path(result['materials'])
    assert result['rows'][0]['result']['status']=='rejected'
    a=judgments(materials,tmp_path/'a')
    report=score(materials,[a],judge_model='fake evaluator',out=tmp_path/'score')
    assert report['metrics']['rejected_candidates']==1
    assert report['metrics']['grounded_sentence_ratio'] is None
    assert not report['metrics']['meets_threshold']


def test_ninety_percent_basis_and_no_prohibited_content_meets_spec(tmp_path):
    texts=[f'在本次聊天的第{i+1}段讨论中，发言者描述了照片整理办法。' for i in range(10)]
    result=run_eval(corpus(tmp_path),tmp_path/'run',configs={},gateway_factory=lambda configs,store:Model(store,texts=texts))
    materials=Path(result['materials']);a=judgments(materials,tmp_path/'a')
    path=a/'0000.json';vote=json.loads(path.read_text())
    vote['sentence_results'][0].update(supported=False,violations=['无依据'])
    path.write_text(dumps(vote))
    report=score(materials,[a],judge_model='fake evaluator',out=tmp_path/'score')
    assert report['metrics']['grounded_sentence_ratio']==.9
    assert report['metrics']['meets_threshold']


def test_frozen_groups_adapter_keeps_entry_scope_quotes_and_labels_out_of_material(tmp_path):
    from iris.db import Store
    from iris.entry_profile import select_material
    value={'format_version':1,'name':'fixture','timezone':'Asia/Shanghai','role_name':'Iris','groups':[
        {'id':'A','entry_kind':'group','name':'甲','now':'2026-12-15T23:00:00+08:00',
         'messages':[{'id':'m001','sender':'共有账号','kind':'message','at':'2026-12-14T12:00:00+08:00','text':'甲原话'},
                     {'id':'m002','sender':'self','kind':'self_output','at':'2026-12-14T12:01:00+08:00','text':'接甲话',
                      'quote':{'message_id':'m001','sender':'共有账号','text':'甲原话'}}],
         'must_cover':['判分专用甲'],'forbidden':['禁止专用甲'],'notes':'不得注入材料'},
        {'id':'B','entry_kind':'live','name':'乙','now':'2026-12-15T23:00:00+08:00',
         'messages':[{'id':'m001','sender':'共有账号','kind':'message','at':'2026-12-14T12:00:00+08:00','text':'乙原话'}],
         'must_cover':['判分专用乙'],'forbidden':['禁止专用乙']}]}
    path=tmp_path/'groups.json';path.write_text(dumps(value))
    cases=load_corpus(path)
    assert len(cases)==2
    assert cases[0]['must_cover']==['判分专用甲']
    assert {m['id'] for m in cases[0]['messages']}=={'A:m001','A:m002','B:m001'}
    store=Store(tmp_path/'db')
    try:
        _runner._load_case(store,cases[0])
        material=select_material(store,'A',clock=lambda:_runner._instant(cases[0]['as_of']))
        encoded=dumps(material)
        assert '乙原话' not in encoded and '判分专用' not in encoded and '不得注入材料' not in encoded
        assert [m['source_id'] for m in material['messages']]==['A:m001','A:m002']
        assert material['messages'][1]['quote']=={'speaker':'共有账号','content':'甲原话'}
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==2
            assert conn.execute("SELECT COUNT(*) FROM subjects WHERE id='共有账号'").fetchone()[0]==1
    finally:
        store.close()
    value['groups'][0]['messages'][1]['quote']['text']='编造的引用'
    path.write_text(dumps(value))
    with pytest.raises(ValueError,match='quote'):
        load_corpus(path)
