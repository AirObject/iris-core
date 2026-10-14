"""Draft material/scorer contracts, never model quality scores."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.entry_profile_eval.memory_runner import (
    run_simulation, score_draft, SCORING_STATUS, validate_vote, case_snapshot, stage_items,
)
from test_entry_profile_memory_task import FakeGateway, new_topic, check


def corpus_file(tmp_path):
    case = dict(id='fixture', entry={'id':'g','kind':'group'},
        as_of='2026-10-13T12:00:00+08:00',
        messages=[{'id':'s1','text':'徒步背包的重量和承重怎么取舍？','sender':'a','at':'2025-05-01T10:00:00+08:00'},
                  {'id':'s2','text':'轻量徒步装备的耐用性也需要讨论。','sender':'b','at':'2026-10-01T11:00:00+08:00'}],
        must_cover=['轻量徒步装备取舍','某次具体活动（覆盖诊断用）'],forbidden=['非作者私事'])
    path = tmp_path/'corpus.json'; path.write_text(json.dumps({'format_version':1,'cases':[case]},ensure_ascii=False))
    return path, case


def vote(payload):
    return dict(memory_results=[dict(index=i,memory_id=m['memory_id'],revision=m['revision'],
        supported=True,violations=[],reason='假判分只测格式') for i,m in enumerate(payload['memories'],1)],
        projection_violations=[],must_cover=[True,False],forbidden=[False],
        stage_coverage={s:[True,False] for s in ('generated','deterministic','checked','projected')})


def export(tmp_path):
    corpus,case = corpus_file(tmp_path)
    factory = lambda _,repo: FakeGateway(repo,[{'operations':[new_topic()]},check([new_topic()])])
    root = tmp_path/'run'; manifest = run_simulation(corpus,root,gateway_factory=factory)
    payload = json.loads((root/'judging-materials/cases/0001.json').read_text())['input']
    return root,manifest,payload


def round_dir(tmp_path, name, manifest, judgment):
    path=tmp_path/name; path.mkdir()
    (path/'manifest.json').write_text(json.dumps(manifest))
    (path/'0001.json').write_text(json.dumps(judgment))
    return path


def test_material_is_body_sources_and_labels_without_self_judgment(tmp_path):
    root, manifest, payload=export(tmp_path)
    assert manifest['metadata']['scoring_status']==SCORING_STATUS and not manifest['metadata']['gate_eligible']
    assert manifest['metadata']['repository_mode']=='simulation'
    assert payload['must_cover']==['轻量徒步装备取舍','某次具体活动（覆盖诊断用）']
    encoded=json.dumps(payload,ensure_ascii=False)
    for excluded in ('supported','violations','privacy_safe','meaning_checks','reason','confirmed_evidence'):
        assert '"'+excluded+'"' not in encoded
    assert (root/'run.json').exists() and not (root/'judging-materials/run.json').exists()
    assert 'scoring_v3' in manifest['metadata']['scoring_version']
    assert len(manifest['metadata']['prompt_sha256'])==2


def test_double_score_combines_adversely_and_never_claims_gate(tmp_path):
    root, manifest, payload=export(tmp_path); a=vote(payload); b=copy.deepcopy(a)
    b['memory_results'][0].update(supported=False,violations=['无依据','容易过时的描述'])
    b['must_cover'][0]=False; b['forbidden'][0]=True; b['stage_coverage']['checked'][0]=False
    one=round_dir(tmp_path,'one',manifest,a);two=round_dir(tmp_path,'two',manifest,b)
    report=score_draft(root/'judging-materials',[one,two],judge_model='fake-format-test')
    assert report['metrics']['grounded_items']==0 and report['metrics']['model_items']==1
    assert report['metrics']['forbidden_occurrences']==1
    assert report['metrics']['must_cover_total']==2 and report['metrics']['must_cover_covered']==0
    assert report['metrics']['stage_losses']['deterministic_to_checked']==1
    assert report['disagreements'] and report['gate_eligible'] is False
    assert report['metrics']['meets_threshold'] is None


@pytest.mark.parametrize('kind', ['id','revision','bool','order','extra','coverage'])
def test_judgment_schema_is_strict(tmp_path,kind):
    _,_,payload=export(tmp_path); v=vote(payload)
    if kind=='id':v['memory_results'][0]['memory_id']='wrong'
    elif kind=='revision':v['memory_results'][0]['revision']=True
    elif kind=='bool':v['memory_results'][0]['supported']=1
    elif kind=='order':v['memory_results'][0]['violations']=['虚构','无依据']
    elif kind=='extra':v['extra']=True
    else:v['stage_coverage']['generated']=[]
    with pytest.raises(ValueError):validate_vote(v,payload)


def test_scoring_rejects_tampered_material_or_round_binding(tmp_path):
    root,manifest,payload=export(tmp_path);one=round_dir(tmp_path,'one',manifest,vote(payload))
    path=root/'judging-materials/cases/0001.json';doc=json.loads(path.read_text());doc['input']['memories'][0]['body']='篡改'
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='fingerprint'):score_draft(root/'judging-materials',[one],judge_model='fake')


def test_original_initial_profile_is_protected_and_not_model_evidence(tmp_path):
    _,case=corpus_file(tmp_path);case['initial_profile']='禁止发布广告。\n管理员手写说明。'
    snap=case_snapshot(case)
    assert snap.profiles[0]['body']==case['initial_profile'] and snap.profiles[0]['manual_protected']
    assert snap.profiles[0]['category'] is None and not snap.memories


def test_failed_and_private_cases_keep_denominator_without_fake_output(tmp_path):
    corpus,case=corpus_file(tmp_path);case['entry']['kind']='private'
    corpus.write_text(json.dumps({'format_version':1,'cases':[case]}))
    # Construction is lazy; no calls should occur for an ineligible entry.
    class NoCalls:
        def chat(self,*_,**__):raise AssertionError('called')
    root=tmp_path/'private';manifest=run_simulation(corpus,root,gateway_factory=lambda *_:NoCalls())
    payload=json.loads((root/'judging-materials/cases/0001.json').read_text())['input']
    assert payload['memories']==[] and len(payload['must_cover'])==2
    v=vote(payload);v['must_cover']=[False,False];v['stage_coverage']={s:[False,False] for s in v['stage_coverage']}
    one=round_dir(tmp_path,'one',manifest,v)
    metrics=score_draft(root/'judging-materials',[one],judge_model='fake')['metrics']
    assert metrics['model_items']==0 and metrics['grounded_ratio'] is None and metrics['must_cover_total']==2


def test_rejected_model_source_context_is_rebound_not_trusted(tmp_path):
    corpus,case=corpus_file(tmp_path);op=new_topic()
    op['sources'][0]['message']={'content':'forged_context','supported':True,'reason':'product_vote'}
    root=tmp_path/'forged'
    run_simulation(corpus,root,gateway_factory=lambda _,r:FakeGateway(r,[{'operations':[op]}]*2))
    payload=json.loads((root/'judging-materials/cases/0001.json').read_text())['input']
    assert not payload['memories'] and not payload['stages']['generated']
    result=json.loads((root/'run.json').read_text())['cases'][0]['result']
    assert result['reason']=='invalid_output' and len(result['calls'])==2
    # Strict shape rejection precedes generated-stage acceptance. The exporter
    # still distrusts model-provided context when rendering any raw candidate.
    stage=stage_items([op],result['material'])[0]
    assert stage['sources'][0]['message']['content']==case['messages'][0]['text']
    assert 'forged_context' not in json.dumps([payload,stage])
    assert 'product_vote' not in json.dumps([payload,stage])


def test_malformed_candidate_does_not_abort_case_export(tmp_path):
    corpus,_=corpus_file(tmp_path);root=tmp_path/'malformed'
    op={'op':'confirm','target_id':['invalid'],'body':{},'sources':[]}
    manifest=run_simulation(corpus,root,gateway_factory=lambda _,r:FakeGateway(r,[{'operations':[op]}]*2))
    assert len(manifest['cases'])==1
    payload=json.loads((root/'judging-materials/cases/0001.json').read_text())['input']
    assert not payload['memories'] and not payload['stages']['generated']
    result=json.loads((root/'run.json').read_text())['cases'][0]['result']
    assert result['reason']=='invalid_output' and len(result['calls'])==2
    assert stage_items([op],result['material'])[0]['body']==''
