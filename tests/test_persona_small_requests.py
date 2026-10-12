"""Bound proof/repair work without dropping a sentence's evidence contract."""
import json

import pytest

from iris.db import dumps
from iris.memory_ops import edit_memory, setup_role
from iris.persona import PersonaEngine, current_persona
from test_persona import add_self
from test_persona_clause_checks import BatchGateway, batch_material, batch_sentences
from test_persona_optimization import Model, prepared


def test_only_first_check_batch_compares_the_complete_draft(store):
    gateway = BatchGateway()
    result = PersonaEngine(store, gateway)._check_all('persona_check', batch_material(),
        batch_sentences(), full_content='完整稿', outputs={})
    requests = [c[1] for c in gateway.calls]
    assert [r['task'] for r in requests] == ['check', 'check_facts', 'check_facts']
    assert requests[0]['candidate_text'] == '完整稿'
    assert all('previous' not in r and 'candidate_text' not in r for r in requests[1:])
    assert result['change_degree'] == 'small'  # Later batches have no degree task.
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
        elif purpose == 'persona_sentence_repair':
            assert len(request['problems']) <= 2
            reply.content = dumps({'repairs':[
                {'index':r['index'],'text':request['replacement_text_by_ref'][r['allowed_replacement_refs'][0]]}
                for r in request['problems']]})
            self.now += self.duration
        return reply


def five_memories(store):
    setup_role(store, 'Iris', '我来自云城。')
    for i in range(5):
        add_self(store, f'我在十月一日的共读中确认了第{i+1}页。')
    store.set_setting('persona_publish_mode', 'small_medium_auto')


def test_repair_batches_keep_indices_and_only_own_options(store):
    five_memories(store)
    model = ManyRepairs(store)
    result = PersonaEngine(store, model).regenerate(expected_version=1)
    assert result['version']['checks']['passed']
    requests = [c[1] for c in model.calls if c[0] == 'persona_sentence_repair']
    assert [len(r['problems']) for r in requests] == [2,2,1]
    indices = [p['index'] for r in requests for p in r['problems']]
    assert indices == list(range(2,7))
    for request in requests:
        refs = {ref for p in request['problems'] for ref in p['allowed_replacement_refs']}
        assert set(request['replacement_text_by_ref']) == refs
        assert set(request['evidence_by_ref']) == refs
    assert len(result['version']['checks']['repair_outputs']) == 3
    assert len(result['version']['checks']['repaired_sentences']) == 5


def test_repair_batches_share_total_budget_and_timeout_never_publishes(store):
    five_memories(store)
    model = ManyRepairs(store, duration=70)
    result = PersonaEngine(store, model).regenerate(expected_version=1)
    assert result['status'] == 'failed' and result['reason'] == 'timeout'
    requests = [c for c in model.calls if c[0] == 'persona_sentence_repair']
    assert [c[2] for c in requests] == [120,110,40]
    assert current_persona(store)['id'] == 1
    with store.read() as conn:
        outputs = json.loads(conn.execute('select outputs_json from persona_attempts').fetchone()[0])
    assert len(outputs['persona_sentence_repair_batches']) == 3


@pytest.mark.parametrize('bad_field', ['reason','clauses','supported'])
def test_mapped_automatic_sentence_with_malformed_check_is_repaired(prepared,bad_field):
    class Malformed(Model):
        def chat(self,messages,purpose,**kwargs):
            reply = super().chat(messages,purpose,**kwargs)
            if purpose == 'persona_check':
                value = json.loads(reply.content)
                value['sentences'][1][bad_field] = {'reason':'','clauses':[], 'supported':1}[bad_field]
                reply.content = dumps(value)
            elif purpose == 'persona_sentence_repair':
                request=json.loads(messages[-1]['content'])
                reply.content=dumps({'repairs':[{'index':p['index'], 'text':None} for p in request['problems']]})
            return reply
    result = PersonaEngine(prepared,Malformed(prepared)).regenerate(expected_version=1)
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
