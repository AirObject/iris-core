"""Additional MG2 judging remains separate from the frozen v1 score schema."""
import copy
import json

import pytest

from test_consolidation_eval import runner, scorer
from test_merge_details import DetailModel


def sample(tmp_path, details):
    case=copy.deepcopy(runner.load_corpus(runner.ROOT/'evals/consolidation_v1.json')[0])
    a,b=case['memories'][:2]
    a['content']='我每周在线教口琴'
    b['content']='我每周教口琴，面向初学者'
    for memory in (a,b):
        for source in memory['sources']:
            source['text']=memory['content']
    row=runner.case_run(case,tmp_path/'case.db',lambda store,clock:DetailModel(store,details),'broad')
    metadata={'source_sha256':runner.source_hash(),'scoring_version':runner.SCORING_VERSION,'method':'broad',
              'resolution':'report_only_v1','corpus':{'sha256':runner._json_sha256([case])}}
    out=tmp_path/'materials'
    manifest=runner.export([row],out,metadata)
    return row,manifest,out


def test_export_details_and_refused_pair_without_altering_frozen_scoring(tmp_path):
    row,manifest,out=sample(tmp_path,{'a':['在线'],'b':['面向初学者']})
    assert not row['input']['actual_merges']
    check=row['input']['merge_checks'][0]
    assert check['result']['merge_detail_check']['outcome']=='mutual_details'
    assert check['result']['unique_details']=={'a':['在线'],'b':['面向初学者']}
    assert len(check['before_memories'])==2 and check['outcome']=='conflicts'
    assert manifest['merge_details_scoring_sha256']
    assert (out/'scoring.md').read_text()==runner.SCORING
    template=json.loads((out/'detail-judgment-template'/'0000.json').read_text())
    assert template['refusal_results'][0]['no_false_refusal'] is None
    assert scorer.load_materials(out)[1]==[row]
    (out/'merge-details-scoring.md').write_text('tampered')
    with pytest.raises(ValueError,match='fingerprint'):
        scorer.load_materials(out)


def base_judgment(row):
    return {'format_version':1,'case_id':row['input']['case']['id'],
      'merge_results':[{'action_id':m['action_id'],'key_facts_preserved':True,'source_grounded':True,
                        'attribution_correct':True,'reason':'fake'} for m in row['input']['actual_merges']],
      'contradiction_results':[],'dependency_results':[],'protection_results':[]}


def judge_dirs(tmp_path,manifest,row,*,preserved=True,justified=True,index=0):
    basic=tmp_path/f'judge-{index}';detail=tmp_path/f'detail-{index}'
    basic.mkdir();detail.mkdir()
    for directory in (basic,detail):
        runner._write_json(directory/'manifest.json',{'materials_sha256':manifest['materials_sha256']})
    runner._write_json(basic/'0000.json',base_judgment(row))
    runner._write_json(detail/'0000.json',{'format_version':1,'case_id':row['input']['case']['id'],
        'merge_detail_results':[{'action_id':m['action_id'],'all_facts_preserved':preserved,'reason':'fake'} for m in row['input']['actual_merges']],
        'refusal_results':[{'index':i,'pair':pair,'no_false_refusal':justified,'reason':'fake'}
                           for i,pair in enumerate(row['input']['case']['expected']['merges'])]})
    return basic,detail


@pytest.mark.parametrize('merging', [True,False])
def test_new_metrics_double_judge_disagreement_takes_adverse_result(tmp_path,merging):
    details={'a':[],'b':[]} if merging else {'a':['在线'],'b':['面向初学者']}
    row,manifest,materials=sample(tmp_path,details)
    a,x=judge_dirs(tmp_path,manifest,row,index=0)
    b,y=judge_dirs(tmp_path,manifest,row,index=1,preserved=False,justified=merging)
    report=scorer.score(materials,[a,b],tmp_path/'score','fake',detail_judgments=[x,y])
    assert report['detail_judge_rounds']==2
    assert report['detail_loss']['numerator']==int(merging)
    assert report['detail_loss']['denominator']==int(merging)
    assert report['false_refusal']=={'numerator':int(not merging),'denominator':1,'rate':float(not merging)}
    assert report['detail_disagreements']
    if merging:
        assert report['mismerge']['numerator']==1  # any lost fact also remains a mismerge


@pytest.mark.parametrize('change', ['boolean','length','pair','id','binding'])
def test_detail_judgment_validation(tmp_path,change):
    row,manifest,materials=sample(tmp_path,{'a':[],'b':[]})
    base,detail=judge_dirs(tmp_path,manifest,row)
    path=detail/'0000.json';j=json.loads(path.read_text())
    if change=='boolean': j['merge_detail_results'][0]['all_facts_preserved']=1
    elif change=='length': j['refusal_results']=[]
    elif change=='pair': j['refusal_results'][0]['pair'].reverse()
    elif change=='id': j['merge_detail_results'][0]['action_id']='invented'
    else: runner._write_json(detail/'manifest.json',{'materials_sha256':'other'})
    runner._write_json(path,j)
    with pytest.raises(ValueError):
        scorer.score(materials,[base],tmp_path/'score','fake',detail_judgments=[detail])


def test_missing_detail_judgments_are_unscored_not_zero(tmp_path):
    row,manifest,materials=sample(tmp_path,{'a':[],'b':[]})
    base,_=judge_dirs(tmp_path,manifest,row)
    report=scorer.score(materials,[base],tmp_path/'score','fake')
    assert report['detail_judge_rounds']==0
    assert report['detail_loss'] is None and report['false_refusal'] is None
