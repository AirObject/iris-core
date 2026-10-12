"""Small proof requests preserve full sentence context and fail closed as a whole."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine, _check_verdicts
from test_persona_clause_checks import batch_material, batch_sentences


def material_and_sentence(count=4):
    text='在那次共读中，'+ '，'.join(f'我核对了第{i}项' for i in range(1,count))+'。'
    memory={'ref':'M1','memory_id':7,'revision':1,'content':text,'excerpts':[
        {'excerpt':text,'date':'2026-10-01'}], 'initial_sources':[],
        'stance':'亲历','speaker_subject_id':'self','about':['self'], 'date':'2026-10-01',
        'event_time':None,'entry_kind':'group','dates':['2026-10-01'],'date_count':1,'belief':90}
    material=batch_material();material['evidence']['memories']=[memory]
    sentence={'text':text,'origin':'memory','basis':[{'memory_id':7,'revision':1}],
              'initial_setting':False,'dates':['2026-10-01'],'date_count':1}
    return material,sentence


class FocusModel:
    def __init__(self, *, bad=None, duration=1):
        self.calls=[];self.now=0.;self.bad=bad;self.duration=duration
    def monotonic(self):return self.now
    def chat(self,messages,purpose,**kwargs):
        request=json.loads(messages[-1]['content']);self.calls.append((request,kwargs['_deadline']))
        self.now+=self.duration
        rows=[]
        for sentence in request['candidate']:
            parts=[]
            if sentence['origin']=='memory':
                for focus in sentence['clauses']:
                    proof={'ref':'M1','source_index':1,'quote':request['evidence_by_ref']['M1']['excerpts'][0]['excerpt']}
                    part={'index':focus['index'],'evidence':[proof], 'supported':True,
                          'scene_qualified':True,'reason':'本次只核对所列焦点。'}
                    if focus['index']==3:
                        if self.bad=='unsupported':part['supported']=False
                        elif self.bad=='wrong_quote':proof['quote']='原文没有的内容'
                        elif self.bad=='wrong_index':part['index']=99
                    parts.append(part)
            rows.append({'index':sentence['index'],'clauses':parts,'supported':True,
                'fabricated':False,'persona_kind':'episode','scene_qualified':True,'violations':[],'reason':'完整句作为语境。'})
        return ModelReply(dumps({'sentences':rows,'change_degree':'small','reason':'有界变化。'}),'stop',{})


def run(store, model, material, sentences, outputs=None):
    return PersonaEngine(store,model)._check_all('persona_check',material,sentences,
        full_content=''.join(s['text'] for s in sentences),outputs=outputs if outputs is not None else {})


def test_large_sentence_is_checked_in_small_windows_without_losing_context(store):
    material,sentence=material_and_sentence();before=copy.deepcopy(sentence);model=FocusModel()
    result=run(store,model,material,[sentence])
    assert len(model.calls)==2
    assert [r['task'] for r,_ in model.calls]==['check','check_facts_and_rules']
    assert [[p['index'] for p in r['candidate'][0]['clauses']] for r,_ in model.calls]==[[1,2],[3,4]]
    assert all(r['candidate'][0]['text']==sentence['text'] for r,_ in model.calls)
    assert all(r['candidate'][0]['evidence_refs']==['M1'] for r,_ in model.calls)
    assert all(not any(k.startswith('_check_') for k in r['candidate'][0]) for r,_ in model.calls)
    assert sentence==before
    assert len(result['sentences'])==1 and result['sentences'][0]['supported']
    assert [p['index'] for p in result['sentences'][0]['clauses']]==[1,2,3,4]
    assert result['window_map']==[[{'sentence_index':1,'clause_indices':[1,2]}],
                                  [{'sentence_index':1,'clause_indices':[3,4]}]]


@pytest.mark.parametrize('bad',['unsupported','wrong_quote','wrong_index'])
def test_one_bad_focus_in_later_window_keeps_entire_sentence_unsupported(store,bad):
    material,sentence=material_and_sentence();model=FocusModel(bad=bad)
    result=run(store,model,material,[sentence])
    assert len(model.calls)==2
    degree,structural,problems=_check_verdicts(result,[sentence],material=material)
    assert not structural and 1 in problems
    assert not result['sentences'][0]['supported']
    assert result['batches'][-1]['sentences'][0]['supported'] is True  # Raw claim is kept.


def test_protected_sentences_are_checked_once_and_original_sentence_order_survives(store):
    material,sentence=material_and_sentence(5);protected=batch_sentences()[:1]
    model=FocusModel();result=run(store,model,material,protected+[sentence]+protected)
    assert [r['index'] for r in result['sentences']]==[1,2,3]
    assert sum(s['origin']=='admin' for r,_ in model.calls for s in r['candidate'])==2
    assert all(sum(len(s['clauses']) for s in r['candidate'] if s['origin']=='memory')<=2
               for r,_ in model.calls)
    assert len(result['sentences'][1]['clauses'])==5


def test_late_focus_timeout_keeps_one_stage_deadline_and_partial_raw_outputs(store):
    material,sentence=material_and_sentence(19);model=FocusModel(duration=130);outputs={}
    with pytest.raises(ModelError) as error:run(store,model,material,[sentence],outputs)
    assert error.value.reason=='timeout'
    assert len(model.calls)==7
    assert [d for _,d in model.calls]==[180,310,440,570,700,830,900]
    assert len(outputs['persona_check_batches'])==7
