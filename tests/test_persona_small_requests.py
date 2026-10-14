"""Bound proof/repair work without dropping a sentence's evidence contract."""
import json

import pytest

from iris.db import dumps
from iris.memory_ops import edit_memory, setup_role
from iris.persona import PersonaEngine, current_persona
from test_persona import add_self
from test_persona_clause_checks import BatchGateway, batch_material, batch_sentences
from test_persona_optimization import Model, prepared


def test_only_final_separate_request_compares_the_complete_draft(store):
    gateway = BatchGateway()
    result = PersonaEngine(store, gateway)._check_all('persona_check', batch_material(),
        batch_sentences(), full_content='完整稿', outputs={})
    requests = [c[1] for c in gateway.calls]
    assert [r['task'] for r in requests] == ['check_facts_and_rules']*3 + ['degree_only']
    assert requests[-1]['candidate_text'] == '完整稿' and requests[-1]['candidate'] == []
    assert all('previous' not in r and 'candidate_text' not in r for r in requests[:-1])
    assert result['change_degree'] == 'small'  # Only the separate comparison supplies degree.
    assert len(result['sentences']) == 9


def test_no_eligible_repair_option_skips_repair_model_and_preserves_reason(prepared):
    edit_memory(prepared, 5, 1, content='我今天在直播中选择了浅灰底。')
    model = Model(prepared)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = result['version']['checks']
    assert checks['passed']
    assert not any(c[0] == 'persona_sentence_repair' for c in model.calls)
    assert checks['deleted_sentences'][0]['stage'] == 'repair_options'
    assert 'relative time' in ' '.join(checks['repair_options_rejected'][0]['reasons'])
    assert '解释了规则' in result['version']['content']


class ManyRepairs(Model):
    def __init__(self, store, duration=0):
        super().__init__(store)
        self.duration = duration

    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        request = json.loads(messages[-1]['content'])
        if purpose == 'persona_generate':
            reply.content = dumps({'sentences': [
                {'text':f'我一向会确认第{i}页。', 'basis':[m['ref']]}
                for i,m in enumerate(request['evidence']['memories'],1)]})
        elif purpose == 'persona_recheck':
            self.now += self.duration
        return reply


def five_memories(store):
    setup_role(store, 'Iris', '我来自云城。')
    for i in range(5):
        add_self(store, f'我在十月一日的共读中确认了第{i+1}页。')
    store.set_setting('persona_publish_mode', 'small_medium_auto')


def test_deterministic_selections_keep_indices_and_only_own_options(store):
    five_memories(store)
    model=ManyRepairs(store)
    result=PersonaEngine(store,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert checks['passed'] and not any(p=='persona_sentence_repair' for p,*_ in model.calls)
    selections=checks['repair_selections']
    assert [s['index'] for s in selections]==list(range(2,7))
    assert all(len(s['eligible_options'])==1 and s['selected']==s['eligible_options'][0] for s in selections)
    assert len({s['selected']['basis'][0] for s in selections})==5
    assert len(checks['repaired_sentences'])==5 and checks['repair_outputs']==[]
    assert all(change['after']['basis']==change['before']['basis'] for change in checks['repaired_sentences'])


def test_selected_repairs_still_share_recheck_budget_and_timeout_never_publishes(store):
    five_memories(store)
    model=ManyRepairs(store,duration=350)
    result=PersonaEngine(store,model).regenerate(expected_version=1)
    assert result['status']=='failed' and result['reason']=='timeout'
    requests=[c for c in model.calls if c[0]=='persona_recheck']
    assert [c[2] for c in requests]==[360,360,200]
    assert current_persona(store)['id']==1
    with store.read() as conn:
        outputs=json.loads(conn.execute('SELECT outputs_json FROM persona_attempts').fetchone()[0])
    assert len(outputs['persona_recheck_batches'])==3
    assert len(outputs['persona_repair_selections'])==5
    assert 'persona_sentence_repair_batches' not in outputs


@pytest.mark.parametrize('bad_field', ['reason','clauses','supported'])
def test_mapped_automatic_sentence_with_malformed_check_is_repaired(prepared,bad_field):
    class Malformed(Model):
        def chat(self,messages,purpose,**kwargs):
            reply = super().chat(messages,purpose,**kwargs)
            if purpose == 'persona_check':
                value = json.loads(reply.content)
                request=json.loads(messages[-1]['content'])
                for i,sentence in enumerate(request['candidate']):
                    if '解释了规则' in sentence['text']:
                        value['sentences'][i][bad_field] = {'reason':'','clauses':[], 'supported':1}[bad_field]
                reply.content = dumps(value)
            elif purpose == 'persona_sentence_repair':
                request=json.loads(messages[-1]['content'])
                reply.content=dumps({'repairs':[{'index':p['index'], 'text':None} for p in request['problems']]})
            return reply
    result = PersonaEngine(prepared,Malformed(prepared,again=True)).regenerate(expected_version=1)
    assert result['version']['checks']['passed']
    assert '解释了规则' not in result['version']['content']
    assert result['version']['checks']['deleted_sentences']


def test_malformed_protected_template_check_still_rejects_whole_candidate(prepared):
    class Malformed(Model):
        def chat(self,messages,purpose,**kwargs):
            reply = super().chat(messages,purpose,**kwargs)
            if purpose == 'persona_check':
                value = json.loads(reply.content)
                value['sentences'][0]['supported'] = 1
                reply.content = dumps(value)
            return reply
    result=PersonaEngine(prepared,Malformed(prepared)).regenerate(expected_version=1)
    assert result['status']=='rejected'
    assert current_persona(prepared)['id']==1


def test_unknown_sentence_mapping_cannot_inject_diagnostic_fields(store):
    class Forged(BatchGateway):
        def chat(self,*args,**kwargs):
            reply = super().chat(*args,**kwargs)
            result = json.loads(reply.content)
            result['sentences'][0]['clause_errors'] = 'untrusted diagnostics'
            result['sentences'][-1]['index'] = 99
            reply.content = dumps(result)
            return reply
    result = PersonaEngine(store,Forged())._check_all('persona_check',batch_material(),
        batch_sentences(),full_content='完整稿',outputs={})
    assert result['validation_errors'] and result['sentences']==[]


def test_three_valid_check_batches_keep_their_request_budget(store):
    gateway = BatchGateway(duration=130)
    result=PersonaEngine(store,gateway)._check_all('persona_check',batch_material(),
        batch_sentences(),full_content='完整稿',outputs={})
    assert len(result['sentences'])==9
    assert [c[2] for c in gateway.calls]==[360,490,620,750]


def test_empty_fact_check_cannot_be_treated_as_degree_only(prepared):
    class EmptyFactCheck(Model):
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_check':
                request=json.loads(messages[-1]['content'])
                assert request['task']=='check_facts_and_rules' and request['candidate']
                reply.content=dumps({'sentences':[], 'change_degree':'small',
                                     'reason':'只比较了程度，漏掉事实检查'})
            return reply
    model=EmptyFactCheck(prepared)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['status']=='rejected'
    assert current_persona(prepared)['id']==1
    assert 'cover every sentence' in ' '.join(result['version']['checks']['model_errors'])
    assert not any(p in ('persona_sentence_repair','persona_recheck') for p,*_ in model.calls)
    assert result['version']['checks']['initial_model']['batches'][0]['sentences']==[]


def test_single_date_repair_without_explicit_scene_never_reaches_fact_recheck(prepared):
    edit_memory(prepared,5,1,content='我在回声读书会交流时说明了故事背景。')
    model=Model(prepared)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['version']['checks']['passed']
    assert not any(p=='persona_sentence_repair' for p,*_ in model.calls)
    requests=[r for p,r,*_ in model.calls if p=='persona_recheck']
    assert requests and all(r['task']=='degree_only' and r['candidate']==[] for r in requests)
    assert 'single-date' in str(result['version']['checks']['repair_options_rejected'])
