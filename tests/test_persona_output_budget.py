"""Output exhaustion is an incomplete judgment, never evidence of support."""
import json

import pytest

from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine, current_persona
from iris.persona_evaluation import _call_metrics
from test_persona_optimization import Model, prepared


@pytest.mark.parametrize('purpose,limit', [('persona_generate',4096),('persona_check',6144),
    ('persona_recheck',6144),('persona_sentence_repair',2048)])
def test_persona_requests_have_explicit_output_budgets(prepared,purpose,limit):
    class Gateway:
        def chat(self,messages,label,**kwargs):
            assert label==purpose and kwargs['max_tokens']==limit
            return ModelReply('{}','stop',{})
    assert PersonaEngine(prepared,Gateway())._call(purpose,{}, {})=={}


def test_length_with_parseable_json_is_not_retried_or_accepted(prepared):
    class Gateway:
        calls=0
        def chat(self,*args,**kwargs):
            self.calls+=1
            return ModelReply('{"sentences":[]}', 'length',{})
    gateway=Gateway(); outputs={}
    with pytest.raises(ModelError) as error:
        PersonaEngine(prepared,gateway)._call('persona_check',{},outputs)
    assert error.value.reason=='output_limit'
    assert gateway.calls==1
    assert outputs['persona_check']=='{"sentences":[]}'
    assert outputs['persona_check_finish_reason']=='length'


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
def test_incomplete_check_rejects_candidate_and_preserves_current(prepared,purpose):
    class Truncated(Model):
        def chat(self,messages,label,**kwargs):
            reply=super().chat(messages,label,**kwargs)
            if label==purpose:
                reply.finish_reason='length'
            return reply
    model=Truncated(prepared)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['status']=='rejected'
    assert not result['version']['checks']['passed']
    assert 'output_limit' in ' '.join(result['version']['checks']['model_errors'])
    assert current_persona(prepared)['id']==1
    assert not any(c[0]==purpose+'_repair' for c in model.calls)
    with prepared.read() as conn:
        outputs=json.loads(conn.execute('SELECT outputs_json FROM persona_attempts').fetchone()[0])
    assert outputs[purpose+'_batches'][0][purpose+'_finish_reason']=='length'


def test_truncated_repair_is_deleted_once_instead_of_accepted(prepared):
    class Truncated(Model):
        def chat(self,messages,label,**kwargs):
            reply=super().chat(messages,label,**kwargs)
            if label=='persona_sentence_repair':
                reply.finish_reason='length'
            return reply
    model=Truncated(prepared)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['version']['checks']['passed']
    assert '浅灰底' not in result['version']['content']
    assert result['version']['checks']['repair_outputs'][0]['error']=='output_limit'
    assert len(result['version']['checks']['deleted_sentences'])==1
    assert sum(c[0]=='persona_sentence_repair' for c in model.calls)==1
    assert not any(c[0]=='persona_sentence_repair_repair' for c in model.calls)


def test_output_exhaustion_has_separate_call_metric():
    calls=[dict(purpose='persona_check',duration_ms=80000,prompt_tokens=1000,
                completion_tokens=4096,reasoning_tokens=4000,finish_reason='length')]
    metrics=_call_metrics(calls)
    assert metrics['output_limited_calls']==1
    assert metrics['timed_out_calls']==0
    assert metrics['output_limits_by_purpose']=={'persona_check':1}
