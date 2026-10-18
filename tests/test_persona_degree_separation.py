"""Factual proof and whole-draft degree use separate tasks and one deadline."""
import json

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine
from test_persona import store
from test_persona_clause_checks import batch_material, batch_sentences


class SeparateModel:
    def __init__(self, *, negative=False, degree=None, degree_finish='stop', durations=()):
        self.negative=negative
        self.degree=degree if degree is not None else {'change_degree':'medium','reason':'调整了一个稳定侧重。'}
        self.degree_finish=degree_finish
        self.durations=durations
        self.now=0.0
        self.calls=[]

    def monotonic(self):
        return self.now

    def chat(self,messages,purpose,**kwargs):
        payload=json.loads(messages[-1]['content'])
        self.calls.append({'purpose':purpose,'request':payload,'prompt':messages[0]['content'],'deadline':kwargs['_deadline']})
        self.now+=self.durations[len(self.calls)-1] if len(self.calls)<=len(self.durations) else 1
        if payload['task']=='degree_only':
            return ModelReply(dumps(self.degree),self.degree_finish,{})
        rows=[{'index':row['index'],'persona_kind':'admin','clauses':[],
               'supported':not self.negative,'fabricated':False,'scene_qualified':True,
               'violations':[],'reason':'保留原文。' if not self.negative else '明确违反监管。'}
              for row in payload['candidate']]
        # This unrelated field must never become the final comparison verdict.
        return ModelReply(dumps({'sentences':rows,'change_degree':'large','reason':'只核查事实。'}),'stop',{})


def check(store,model,*,sentences=None,outputs=None,**kwargs):
    return PersonaEngine(store,model)._check_all('persona_check',
        {**batch_material(),'previous':'上一当前版。'},
        batch_sentences() if sentences is None else sentences,
        full_content='已经组装好的全文。',outputs=outputs if outputs is not None else {},**kwargs)


def test_all_fact_windows_exclude_draft_comparison_and_only_final_degree_counts(store):
    model=SeparateModel();outputs={};result=check(store,model,outputs=outputs)
    facts=[c for c in model.calls if c['request']['candidate']]
    degrees=[c for c in model.calls if c['request']['task']=='degree_only']
    assert len(facts)==3 and len(degrees)==1
    assert all(c['request']['task']=='check_facts_and_rules' for c in facts)
    assert all('previous' not in c['request'] and 'candidate_text' not in c['request'] for c in facts)
    assert model.calls[-1]==degrees[0]
    request=degrees[0]['request']
    assert request['previous']=='上一当前版。' and request['candidate_text']=='已经组装好的全文。'
    assert request['candidate']==[] and request.get('evidence_by_ref',{})=={}
    assert 'persona_kind' not in degrees[0]['prompt']
    assert result['change_degree']=='medium'
    assert result['degree_assessment']['status']=='complete'
    assert result['degree_assessment']['output']==model.degree
    assert len(outputs['persona_check_batches'])==4 and result['window_map'][-1]==[]
    assert outputs['persona_check_batches'][-1]['persona_check_task']=='degree_only'


def test_empty_facts_require_only_short_degree_prompt(store):
    model=SeparateModel();result=check(store,model,sentences=[])
    assert len(model.calls)==1 and model.calls[0]['request']['task']=='degree_only'
    assert 'persona_kind' not in model.calls[0]['prompt']
    assert result['sentences']==[] and result['change_degree']=='medium'
    assert result['degree_assessment']['status']=='complete'


def test_known_factual_rejection_does_not_request_or_fabricate_degree(store):
    model=SeparateModel(negative=True);result=check(store,model,sentences=batch_sentences()[:1])
    assert len(model.calls)==1 and model.calls[0]['request']['task']=='check_facts_and_rules'
    assert not result['sentences'][0]['supported']
    assert result['degree_assessment']=={'status':'deferred_after_rejection'}
    assert result['change_degree']=='large' and not result['incomplete_checks']


def test_prechecked_recovery_can_defer_degree_without_fake_model_comparison(store):
    model=SeparateModel();result=check(store,model,sentences=batch_sentences()[:1],assess_degree=False)
    assert len(model.calls)==1 and model.calls[0]['request']['task']=='check_facts_and_rules'
    assert result['sentences'][0]['supported']
    assert result['degree_assessment']=={'status':'deferred_to_recovery'}
    assert result['change_degree']=='large'


def test_degree_shares_fixed_stage_budget_after_a_slow_single_window(store):
    model=SeparateModel(durations=[350,11]);outputs={}
    result = check(store,model,sentences=batch_sentences()[:1],outputs=outputs)
    assert result['degree_assessment']['status']=='complete'
    assert [c['deadline'] for c in model.calls]==[360,710]
    assert len(outputs['persona_check_batches'])==2
    assert outputs['persona_check_batches'][0]['persona_check']
    assert outputs['persona_check_batches'][1]['persona_check']


@pytest.mark.parametrize('degree', [
    {'change_degree':'medium'},
    {'reason':'缺程度。'},
    {'change_degree':'unknown','reason':'未知程度。'},
    {'change_degree':'medium','reason':''},
    {'change_degree':'medium','reason':'非空事实。','sentences':[{'index':1}]},
    {'change_degree':'medium','reason':'未知字段。','extra':True},
])
def test_invalid_degree_requires_confirmation_after_one_reask(store,degree):
    model=SeparateModel(degree=degree);result=check(store,model,sentences=batch_sentences()[:1])
    assert len(model.calls)==3
    assert not result['validation_errors']
    assert result['change_degree']=='large'
    assert result['degree_assessment']['status']=='conservative'


def test_degree_output_exhaustion_remains_incomplete_with_raw_record(store):
    model=SeparateModel(degree_finish='length');outputs={}
    result=check(store,model,sentences=batch_sentences()[:1],outputs=outputs)
    assert 'output_limit' in ' '.join(result['validation_errors'])
    assert len(model.calls)==2
    assert outputs['persona_check_batches'][-1]['persona_check_finish_reason']=='length'
    assert result['degree_assessment']['status']=='invalid'


def test_legacy_explicit_empty_sentences_is_valid_degree_only_output(store):
    model=SeparateModel(degree={'sentences':[],'change_degree':'small','reason':'局部变化。'})
    result=check(store,model,sentences=[])
    assert result['change_degree']=='small' and result['degree_assessment']['status']=='complete'
    assert 'persona_kind' not in model.calls[0]['prompt']


@pytest.mark.parametrize('failure', ['malformed', 'length', 'timeout'])
def test_failed_degree_after_valid_facts_keeps_current_persona(store, failure):
    from iris.persona import current_persona
    from test_persona import FakeGateway
    class Gateway(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if self.calls[-1][1].get('task') == 'degree_only':
                if failure == 'timeout':
                    raise ModelError('timeout', 'total timeout', reason='timeout')
                reply.content = dumps({'change_degree':'small'} if failure == 'malformed' else
                                      {'change_degree':'small','reason':'局部变化。'})
                reply.finish_reason = 'length' if failure == 'length' else 'stop'
            return reply
    old = current_persona(store)
    model = Gateway(store, {'sentences':[]})
    result = PersonaEngine(store, model).regenerate(expected_version=old['id'])
    assert current_persona(store)['id'] == old['id']
    assert result['status'] == ('failed' if failure == 'timeout' else 'pending' if failure == 'malformed' else 'rejected')
    assert [p.get('task') for _,p in model.calls] == (
        ['generate', 'check_facts_and_rules', 'degree_only'] + (['degree_only'] if failure in ('timeout', 'malformed') else []))
    with store.read() as conn:
        saved = json.loads(conn.execute('SELECT outputs_json FROM persona_attempts WHERE id=?',
            (result['attempt_id'],)).fetchone()[0])
    assert saved['persona_check_batches'][0]['persona_check']
    assert saved['persona_check_batches'][-1]['persona_check_task'] == 'degree_only'
    if failure != 'timeout':
        assert bool(result['version']['checks']['model_errors']) is (failure == 'length')
        assert saved['persona_check_batches'][-1]['persona_check']


@pytest.mark.parametrize('mutation', ['memory', 'current'])
def test_degree_call_is_outside_transactions_and_publication_rechecks_revisions(store, mutation):
    from iris.memory_ops import edit_memory
    from iris.persona import PersonaConflict, admin_edit, current_persona, select_evidence
    from test_persona import FakeGateway, add_self
    text = '在那次共读中，我核对了纸上的页码。'
    mid = add_self(store, text)
    ref = next(m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] == mid)
    old = current_persona(store)
    class Gateway(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)  # Also asserts no writer transaction.
            if self.calls[-1][1].get('task') == 'degree_only':
                if mutation == 'memory':
                    edit_memory(store, mid, 1, content='这次没有核对页码。')
                else:
                    admin_edit(store, '新的手写正文。', expected_version=old['id'])
            return reply
    model = Gateway(store, {'sentences':[{'text':text,'basis':[ref]}]})
    engine = PersonaEngine(store, model)
    if mutation == 'current':
        with pytest.raises(PersonaConflict):
            engine.regenerate(expected_version=old['id'])
        assert current_persona(store)['content'] == '新的手写正文。'
    else:
        result = engine.regenerate(expected_version=old['id'])
        assert result['status'] == 'conflict' and result['reason'] == 'evidence_changed'
        assert current_persona(store)['id'] == old['id']
    assert model.calls[-1][1]['task'] == 'degree_only'
