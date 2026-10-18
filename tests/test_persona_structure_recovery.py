"""PE-15 format-only fake outputs; no new quality corpus or model calls."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine
from persona_fakes import add_clause_proofs
from test_persona_check_resilience import general_case
from test_persona_optimization import prepared
from test_persona_clause_checks import batch_material, batch_sentences


# (mutation, diagnostic class, earlier literal evidence failure must stay adverse)
FACT_CASES = [
    ('batch_degree_missing', 'batch_degree', False),
    ('batch_degree_type', 'batch_degree', False),
    ('batch_degree_enum', 'batch_degree', False),
    ('batch_reason_missing', 'batch_reason', False),
    ('batch_reason_type', 'batch_reason', False),
    ('batch_reason_empty', 'batch_reason', False),
    ('sentences_missing', 'batch_alignment', False),
    ('sentences_type', 'batch_alignment', False),
    ('sentences_count', 'batch_alignment', False),
    ('sentence_index', 'batch_alignment', False),
    ('sentence_object', 'batch_alignment', False),
    ('clause_object', 'clause_fields', False),
    ('scope_on_clause_type', 'scope_fields', False),
    ('supported_missing', 'sentence_bool', False),
    ('supported_type', 'sentence_bool', False),
    ('fabricated_missing', 'sentence_bool', False),
    ('fabricated_type', 'sentence_bool', False),
    ('scene_qualified_missing', 'sentence_bool', False),
    ('scene_qualified_type', 'sentence_bool', False),
    ('sentence_reason_missing', 'sentence_reason', False),
    ('sentence_reason_type', 'sentence_reason', False),
    ('sentence_reason_empty', 'sentence_reason', False),
    ('violations_missing', 'sentence_violations', False),
    ('violations_type', 'sentence_violations', False),
    ('violations_member_type', 'sentence_violations', True),
    ('violations_member_empty', 'sentence_violations', True),
    ('kind_missing', 'sentence_kind', False),
    ('kind_type', 'sentence_kind', False),
    ('kind_enum', 'sentence_kind', False),
    ('clauses_missing', 'clause_coverage', False),
    ('clauses_type', 'clause_coverage', False),
    ('clauses_empty', 'clause_coverage', False),
    ('clauses_extra', 'clause_coverage', False),
    ('clauses_order', 'clause_fields', False),
    ('clause_index_missing', 'clause_fields', False),
    ('clause_index_type', 'clause_fields', False),
    ('clause_supported_missing', 'clause_fields', False),
    ('clause_supported_type', 'clause_fields', False),
    ('clause_scene_missing', 'clause_fields', False),
    ('clause_scene_type', 'clause_fields', False),
    ('clause_reason_missing', 'clause_fields', False),
    ('clause_reason_type', 'clause_fields', False),
    ('clause_reason_empty', 'clause_fields', False),
    ('clause_evidence_missing', 'clause_fields', False),
    ('clause_evidence_type', 'clause_fields', False),
    ('short_circuit_type', 'short_circuit', False),
    ('short_circuit_invalid', 'short_circuit', False),
    ('scope_missing', 'scope_fields', False),
    ('scope_type', 'scope_fields', False),
    ('scope_keys', 'scope_fields', False),
    ('scope_preserved_type', 'scope_fields', False),
    ('scope_candidate_type', 'scope_fields', False),
    ('scope_sources_type', 'scope_fields', False),
    ('scope_source_type', 'scope_fields', False),
    ('scope_source_keys', 'scope_fields', False),
    ('scope_source_ref_type', 'scope_fields', False),
    ('scope_source_quote_type', 'scope_fields', False),
    ('scope_source_preserved_type', 'scope_fields', False),
    ('proof_object_type', 'proof_shape', True),
    ('proof_ref_missing', 'proof_shape', True),
    ('proof_ref_type', 'proof_shape', True),
    ('proof_source_missing', 'proof_shape', True),
    ('proof_source_type', 'proof_shape', True),
    ('proof_quote_missing', 'proof_shape', True),
    ('proof_quote_type', 'proof_shape', True),
    ('proof_quote_index_type', 'proof_shape', True),
]
DEGREE_CASES = [
    ('degree_missing', 'degree_value'), ('degree_type', 'degree_value'),
    ('degree_enum', 'degree_value'), ('reason_missing', 'degree_reason'),
    ('reason_type', 'degree_reason'), ('reason_empty', 'degree_reason'),
    ('fields_extra', 'degree_fields'), ('sentences_nonempty', 'degree_sentences'),
    ('sentences_type', 'degree_sentences'),
]
PARSE_CASES = [
    ('json_syntax', 'json_syntax'), ('json_absent', 'json_root'),
    ('json_array', 'json_root'), ('json_scalar', 'json_root'),
    ('json_null', 'json_root'), ('content_type', 'json_root'),
    ('gateway_output', 'gateway_output'),
    ('content_object', 'json_root'),
]


def case_material():
    material, sentence = general_case()
    sentence['text'] = '合奏排练中我倾向先听节拍，也会复述拍点。'
    memory = material['evidence']['memories'][0]
    memory['content'] = sentence['text']
    memory['excerpts'][0]['excerpt'] = '合奏排练中我先听了节拍，也复述了拍点。'
    memory['excerpts'][1]['excerpt'] = '合奏排练中我又先听了节拍，也复述了拍点。'
    return material, sentence


def damage_fact(value, name):
    row = value['sentences'][0]
    if name == 'sentence_object':
        value['sentences'][0] = None
        return
    if name == 'clause_object':
        row['clauses'][0] = None
        return
    if name == 'scope_on_clause_type':
        row['clauses'][0]['scope_check'] = None
        return
    part = row['clauses'][0]
    if name.startswith('batch_degree_'):
        field, variant = 'change_degree', name.removeprefix('batch_degree_')
        if variant == 'missing': value.pop(field)
        else: value[field] = [] if variant == 'type' else 'unknown'
    elif name.startswith('batch_reason_'):
        variant = name.removeprefix('batch_reason_')
        if variant == 'missing': value.pop('reason')
        else: value['reason'] = [] if variant == 'type' else ''
    elif name.startswith('sentences_'):
        variant = name.removeprefix('sentences_')
        if variant == 'missing': value.pop('sentences')
        else: value['sentences'] = None if variant == 'type' else []
    elif name == 'sentence_index': row['index'] = 99
    elif name.startswith(('supported_', 'fabricated_', 'scene_qualified_')):
        field, variant = name.rsplit('_', 1)
        if variant == 'missing': row.pop(field)
        else: row[field] = 1
    elif name.startswith('sentence_reason_'):
        variant = name.removeprefix('sentence_reason_')
        if variant == 'missing': row.pop('reason')
        else: row['reason'] = [] if variant == 'type' else ''
    elif name.startswith('violations_'):
        variant = name.removeprefix('violations_')
        if variant == 'missing': row.pop('violations')
        else: row['violations'] = {'type':None, 'member_type':[{}], 'member_empty':['']}[variant]
    elif name.startswith('kind_'):
        variant = name.removeprefix('kind_')
        if variant == 'missing': row.pop('persona_kind')
        else: row['persona_kind'] = [] if variant == 'type' else 'unknown'
    elif name.startswith('clauses_'):
        variant = name.removeprefix('clauses_')
        if variant == 'missing': row.pop('clauses')
        elif variant == 'type': row['clauses'] = None
        elif variant == 'empty': row['clauses'] = []
        elif variant == 'extra': row['clauses'].append({**copy.deepcopy(part), 'index':99})
        else: row['clauses'].reverse()
    elif name.startswith('clause_'):
        field, variant = name.removeprefix('clause_').rsplit('_', 1)
        field = 'scene_qualified' if field == 'scene' else field
        if variant == 'missing': part.pop(field)
        else: part[field] = '' if variant == 'empty' else None
    elif name.startswith('short_circuit_'):
        row['stop_after_failure'] = 'true' if name.endswith('type') else True
        if name.endswith('invalid'): row['clauses'] = row['clauses'][:1]
    elif name.startswith('scope_'):
        scope = row['scope_check']
        variant = name.removeprefix('scope_')
        if variant == 'missing': row.pop('scope_check')
        elif variant == 'type': row['scope_check'] = None
        elif variant == 'keys': scope['extra'] = True
        elif variant == 'preserved_type': scope['preserved'] = 'true'
        elif variant == 'candidate_type': scope['candidate_scope'] = None
        elif variant == 'sources_type': scope['source_scopes'] = {}
        elif variant == 'source_type': scope['source_scopes'] = [None]
        elif variant == 'source_keys': scope['source_scopes'][0]['extra'] = True
        elif variant == 'source_ref_type': scope['source_scopes'][0]['ref'] = []
        elif variant == 'source_quote_type': scope['source_scopes'][0]['quote'] = []
        else: scope['source_scopes'][0]['preserved'] = 1
    elif name.startswith('proof_'):
        proof = part['evidence'][0]
        variant = name.removeprefix('proof_')
        if variant == 'object_type': part['evidence'][0] = None
        elif variant.startswith('ref_'):
            if variant.endswith('missing'): proof.pop('ref')
            else: proof['ref'] = []
        elif variant.startswith('source_'):
            if variant.endswith('missing'): proof.pop('source_index')
            else: proof['source_index'] = True
        elif variant == 'quote_missing': proof.pop('quote')
        elif variant == 'quote_type': proof['quote'] = None
        else: proof['quote_index'] = '1'
    else:
        raise AssertionError(name)


def damage_degree(value, name):
    if name.startswith('degree_'):
        variant = name.removeprefix('degree_')
        if variant == 'missing': value.pop('change_degree')
        else: value['change_degree'] = [] if variant == 'type' else 'unknown'
    elif name.startswith('reason_'):
        variant = name.removeprefix('reason_')
        if variant == 'missing': value.pop('reason')
        else: value['reason'] = [] if variant == 'type' else ''
    elif name == 'fields_extra': value['extra'] = True
    else: value['sentences'] = [{'index':1}] if name.endswith('nonempty') else None


class FormatModel:
    def __init__(self, store, name, *, persistent=False, degree=False):
        self.store, self.name, self.persistent, self.degree = store, name, persistent, degree
        self.calls, self.now = [], 0.
    def monotonic(self):
        return self.now
    def chat(self, messages, purpose, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(next(m['content'] for m in messages if m['role'] == 'user'))
        self.calls.append((copy.deepcopy(messages), payload, kwargs['_deadline']))
        self.now += 1
        value = add_clause_proofs({'sentences': [
            {'index':r['index'], 'supported':True, 'fabricated':False, 'scene_qualified':False,
             'violations':[], 'reason':'完整的格式测试判定。'} for r in payload['candidate']],
            'change_degree':'small','reason':'完整的格式测试根理由。'},payload)
        if self.degree:
            value.pop('sentences')
        if self.name != 'unused' and (len(self.calls) == 1 or self.persistent):
            if self.name == 'gateway_output':
                raise ModelError('invalid_output', 'malformed provider response', reason='invalid_output')
            raw = {'json_syntax':'{"sentences":', 'json_absent':'no object',
                'json_array':'[]', 'json_scalar':'"scalar"', 'json_null':'null', 'content_type':None,
                'content_object':{}}
            if self.name in raw:
                return ModelReply(raw[self.name], 'stop', {})
            (damage_degree if self.degree else damage_fact)(value, self.name)
        return ModelReply(dumps(value),'stop',{})


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
@pytest.mark.parametrize('persistent',[False,True])
@pytest.mark.parametrize('name,code,latched', FACT_CASES)
def test_each_fact_structure_error_reasks_once_then_accepts_or_rejects(store, purpose, persistent, name, code, latched):
    material, sentence = case_material()
    model, outputs = FormatModel(store, name, persistent=persistent), {}
    result = PersonaEngine(store,model)._check_all(purpose,material,[sentence],
        full_content=sentence['text'],outputs=outputs,assess_degree=False)
    assert len(model.calls) == 2
    assert not result['validation_errors'] and not result['incomplete_checks']
    assert result['sentences'][0]['supported'] is (not persistent and not latched)
    diag = result['structure_diagnostics']
    assert diag['initial_failures'][code] >= 1
    assert diag['retry_results']['conservative' if persistent else 'corrected'] >= 1
    assert diag['conservative_sentence_count'] == int(persistent)
    assert all(deadline <= 900 for _,_,deadline in model.calls)


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
@pytest.mark.parametrize('persistent',[False,True])
@pytest.mark.parametrize('name,code',DEGREE_CASES + PARSE_CASES)
def test_each_degree_structure_error_reasks_once_then_uses_large(store, purpose, persistent, name, code):
    model, outputs = FormatModel(store,name,persistent=persistent,degree=True), {}
    result = PersonaEngine(store,model)._check_all(purpose,batch_material(),[],
        full_content='Complete final draft',outputs=outputs)
    assert len(model.calls) == 2
    assert not result['validation_errors'] and not result['incomplete_checks']
    assert result['change_degree'] == ('large' if persistent else 'small')
    assert result['degree_assessment']['status'] == ('conservative' if persistent else 'complete')
    assert result['structure_diagnostics']['initial_failures'][code] >= 1
    assert result['structure_diagnostics']['conservative_degree_count'] == int(persistent)


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
@pytest.mark.parametrize('persistent',[False,True])
@pytest.mark.parametrize('name,code',PARSE_CASES)
def test_each_fact_parse_error_has_one_batch_retry(store,purpose,persistent,name,code):
    model, outputs = FormatModel(store,name,persistent=persistent), {}
    result = PersonaEngine(store,model)._check_all(purpose,batch_material(),batch_sentences()[:2],
        full_content='Complete draft',outputs=outputs,assess_degree=False)
    assert len(model.calls)==2
    assert not result['validation_errors'] and not result['incomplete_checks']
    assert all(row['supported'] is (not persistent) for row in result['sentences'])
    assert result['structure_diagnostics']['initial_failures'][code] >= 1


def test_valid_output_does_not_spend_a_structure_retry(store):
    model=FormatModel(store,'unused')
    model.calls.append((None,None,None))  # This response is intentionally valid.
    result=PersonaEngine(store,model)._check_all('persona_check',batch_material(),batch_sentences()[:2],
        full_content='Complete draft',outputs={},assess_degree=False)
    assert len(model.calls)==2 and all(r['supported'] for r in result['sentences'])
    assert not result.get('structure_diagnostics',{}).get('initial_failures')


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
@pytest.mark.parametrize('persistent',[False, True])
@pytest.mark.parametrize('name,code',[(name,code) for name,code,_ in FACT_CASES] + PARSE_CASES)
def test_completion_output_uses_the_same_sentence_recovery_table(store,purpose,persistent,name,code):
    class Completion(FormatModel):
        def chat(self,messages,called_purpose,**kwargs):
            count = len(self.calls)
            self.name = name if count == 1 or (count > 1 and persistent) else 'unused'
            self.persistent = True
            reply = super().chat(messages,called_purpose,**kwargs)
            if count == 0:
                value = json.loads(reply.content)
                value['sentences'][0]['clauses'] = value['sentences'][0]['clauses'][:1]
                reply.content = dumps(value)
            elif count == 1 and name in ('clauses_order','short_circuit_invalid'):
                # A one-focus suffix cannot be reversed or cut to a shorter
                # nonempty prefix; make its returned explicit identity invalid.
                value = json.loads(reply.content)
                value['sentences'][0]['clauses'][0]['index'] = 99
                if name == 'short_circuit_invalid':
                    value['sentences'][0]['stop_after_failure'] = 'invalid'
                reply.content = dumps(value)
            return reply
    material,sentence = case_material()
    model = Completion(store,name)
    result = PersonaEngine(store,model)._check_all(purpose,material,[sentence],
        full_content=sentence['text'],outputs={},assess_degree=False)
    assert len(model.calls) == 3
    assert not result['validation_errors'] and not result['incomplete_checks']
    diagnostic = result['structure_diagnostics']
    assert diagnostic['initial_failures'][code] >= 1
    assert diagnostic['retry_results']['conservative' if persistent else 'corrected'] == 1
    assert diagnostic['conservative_sentence_count'] == int(persistent)
    if persistent:
        assert not result['sentences'][0]['supported']
    assert len(model.calls[1][1]['candidate']) == len(model.calls[2][1]['candidate']) == 1
    assert [p['index'] for p in model.calls[1][1]['candidate'][0]['clauses']] == [2]
    assert [p['index'] for p in model.calls[2][1]['candidate'][0]['clauses']] == [1,2]


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
def test_bad_row_in_a_batch_retry_is_rejected_without_a_third_structure_call(store,purpose):
    from test_persona_check_resilience import Checks
    def mutate(value,payload,number):
        if number == 1:
            value['reason'] = ''
        else:
            value['sentences'][1]['reason'] = ''
    model = Checks(store,mutate)
    result = PersonaEngine(store,model)._check_all(purpose,batch_material(),batch_sentences()[:3],
        full_content='Complete draft',outputs={},assess_degree=False)
    assert len(model.calls) == 2
    assert [r['supported'] for r in result['sentences']] == [True,False,True]
    diagnostic = result['structure_diagnostics']
    assert diagnostic['initial_failures'] == {'batch_reason':1}
    assert diagnostic['retry_failures'] == {'sentence_reason':1}
    assert diagnostic['retry_results'] == {'conservative':1}
    assert diagnostic['conservative_sentence_indices'] == [2]
    assert not result['validation_errors']


@pytest.mark.parametrize('mode',['all_auto','small_medium_auto','all_manual'])
def test_unassessed_degree_always_requires_manual_confirmation(store,mode):
    from test_persona import FakeGateway
    from iris.persona import current_persona
    from iris.memory_ops import setup_role
    setup_role(store,'Iris','Initial persona.')
    store.set_setting('persona_publish_mode',mode)
    class InvalidDegree(FakeGateway):
        def chat(self,messages,purpose,**kwargs):
            reply = super().chat(messages,purpose,**kwargs)
            if self.calls[-1][1].get('task') == 'degree_only':
                value = json.loads(reply.content); value.pop('reason'); reply.content = dumps(value)
            return reply
    model = InvalidDegree(store,{'sentences':[]})
    before = current_persona(store)
    result = PersonaEngine(store,model).regenerate(expected_version=before['id'])
    assert result['status'] == 'pending' and result['version']['change_degree'] == 'large'
    assert current_persona(store)['id'] == before['id']
    assert result['version']['checks']['passed']
    assert result['version']['checks']['degree_override'] == 'invalid_degree_structure_requires_confirmation'


def test_recheck_unassessed_degree_survives_final_assembly_as_manual_confirmation(prepared):
    from test_persona_optimization import Model
    prepared.set_setting('persona_publish_mode','all_auto')
    class InvalidDegree(Model):
        def chat(self,messages,purpose,**kwargs):
            reply = super().chat(messages,purpose,**kwargs)
            if purpose == 'persona_recheck' and self.calls[-1][1]['task'] == 'degree_only':
                value = json.loads(reply.content); value['change_degree'] = None; reply.content = dumps(value)
            return reply
    result = PersonaEngine(prepared,InvalidDegree(prepared)).regenerate(expected_version=1)
    assert result['status'] == 'pending' and result['version']['checks']['passed']
    assert result['version']['change_degree'] == 'large'
    assert result['version']['checks']['recheck']['degree_assessment']['status'] == 'conservative'


def test_structure_metrics_exclude_root_aliases_and_count_stage_local_decisions():
    from iris.persona_evaluation import _check_structure_metrics
    event = {'unit':'sentence','errors':['sentence 1: sentence reason required'],
        'retry_errors':['sentence 1: invalid persona kind'], 'retry_requested':True,
        'sentence_index':1,'status':'conservative_rejection'}
    raw = {'persona_check_structure_events':[event]}
    output = {**raw,'persona_check_batches':[raw]}
    result = _check_structure_metrics([{'attempts':[{'outputs_json':dumps(output)}]}])
    assert result['initial_failures'] == {'sentence_reason':1}
    assert result['retry_failures'] == {'sentence_kind':1}
    assert result['retry_results'] == {'conservative':1}
    assert result['conservative_sentence_count'] == 1


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
def test_json_failure_after_timeout_retry_keeps_its_parse_diagnostic(store,purpose):
    class TimeoutThenMalformed(FormatModel):
        def chat(self,messages,called_purpose,**kwargs):
            if not self.calls:
                self.calls.append((copy.deepcopy(messages),json.loads(messages[-1]['content']),kwargs['_deadline']))
                self.now = kwargs['_deadline']
                raise ModelError('timeout','total timeout',reason='timeout')
            return super().chat(messages,called_purpose,**kwargs)
    model,outputs = TimeoutThenMalformed(store,'json_syntax',persistent=True),{}
    result = PersonaEngine(store,model)._check_all(purpose,batch_material(),batch_sentences()[:1],
        full_content='Complete draft',outputs=outputs,assess_degree=False)
    assert len(model.calls) == 3
    assert result['structure_diagnostics']['initial_failures'] == {'json_syntax':1}
    assert result['structure_diagnostics']['retry_failures'] == {'json_syntax':1}
    assert not result['sentences'][0]['supported'] and not result['validation_errors']
    assert [c[2] for c in model.calls] == [360,720,721]


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
@pytest.mark.parametrize('unit',['sentence','batch','degree'])
@pytest.mark.parametrize('reason',['timeout','output_limit','rate_limited','call_budget','paused'])
def test_structure_reask_never_swallows_infrastructure_errors(store,purpose,unit,reason):
    class Infrastructure(FormatModel):
        def chat(self,messages,called_purpose,**kwargs):
            if self.calls:
                self.calls.append((copy.deepcopy(messages),json.loads(messages[-1]['content']),kwargs['_deadline']))
                raise ModelError('retryable',reason,reason=reason,paused=True)
            return super().chat(messages,called_purpose,**kwargs)
    name = {'sentence':'sentence_reason_empty','batch':'batch_reason_empty','degree':'reason_empty'}[unit]
    model = Infrastructure(store,name,degree=unit=='degree')
    material,sentence = case_material()
    engine = PersonaEngine(store,model)
    def run():
        return engine._check_all(purpose,material,[] if unit=='degree' else [sentence],
            full_content=sentence['text'],outputs={},assess_degree=unit=='degree')
    if reason == 'output_limit' and unit != 'sentence':
        assert 'output_limit' in ' '.join(run()['validation_errors'])
    else:
        with pytest.raises(ModelError) as error:
            run()
        assert error.value.reason == reason
    assert len(model.calls) == 2


def test_salvaged_reordered_batch_does_not_invent_conservative_rejections(store):
    from test_persona_check_resilience import Checks
    def mutate(value,payload,number):
        value['sentences'].reverse()
    result = PersonaEngine(store,Checks(store,mutate))._check_all('persona_check',batch_material(),
        batch_sentences()[:3],full_content='Complete draft',outputs={},assess_degree=False)
    diagnostic = result['structure_diagnostics']
    assert diagnostic['retry_results'] == {'salvaged':1}
    assert diagnostic['conservative_sentence_count'] == diagnostic['conservative_batch_count'] == 0
    assert all(r['supported'] for r in result['sentences'])


@pytest.mark.parametrize('damage',['root','choices','choice','message','content'])
@pytest.mark.parametrize('persistent',[False,True])
def test_provider_envelope_structure_is_recoverable_with_the_real_gateway(store,damage,persistent):
    import httpx
    from iris.models import Gateway, ModelConfig
    requests = []
    def respond(request):
        assert not store._writer.in_transaction
        payload = json.loads(json.loads(request.content)['messages'][-1]['content'])
        requests.append(payload)
        value = {'choices':[{'message':{'content':dumps(add_clause_proofs({
            'sentences':[{'index':r['index'],'supported':True,'fabricated':False,
                          'scene_qualified':False,'violations':[],'reason':'Format control'}
                         for r in payload['candidate']],
            'change_degree':'small','reason':'Format control'},payload))},'finish_reason':'stop'}]}
        if len(requests)==1 or persistent:
            if damage=='root': value=[]
            elif damage=='choices': value['choices']={1:None}
            elif damage=='choice': value['choices']=[None]
            elif damage=='message': value['choices'][0]['message']=[]
            else: value['choices'][0]['message']['content']=None
        return httpx.Response(200,json=value)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway=Gateway({'chat':ModelConfig('https://format.invalid/v1','unused','fake')},
                        store,client=client)
        result=PersonaEngine(store,gateway)._check_all('persona_check',batch_material(),
            batch_sentences()[:1],full_content='Complete draft',outputs={},assess_degree=False)
    assert len(requests)==2 and requests[0]==requests[1]
    assert not result['validation_errors'] and not result['incomplete_checks']
    assert result['sentences'][0]['supported'] is (not persistent)
    assert result['structure_diagnostics']['initial_failures']=={'gateway_output':1}
    with store.read() as conn:
        calls=[dict(c) for c in conn.execute('SELECT * FROM model_calls ORDER BY id')]
    assert len(calls)==2 and calls[0]['result_category']=='invalid_output'
    assert calls[1]['result_category']==('invalid_output' if persistent else 'success')


@pytest.mark.parametrize('purpose',['persona_check','persona_recheck'])
@pytest.mark.parametrize('persistent',[False,True])
def test_parser_resource_failure_uses_the_same_structural_recovery(store,purpose,persistent,monkeypatch):
    from iris import persona
    original=persona.parse_json_object_with_status
    parsed=0
    def bounded(raw):
        nonlocal parsed
        parsed+=1
        if parsed==1 or persistent:
            raise RecursionError('simulated JSON decoder resource limit')
        return original(raw)
    monkeypatch.setattr(persona,'parse_json_object_with_status',bounded)
    model=FormatModel(store,'unused')
    result=PersonaEngine(store,model)._check_all(purpose,batch_material(),batch_sentences()[:1],
        full_content='Complete draft',outputs={},assess_degree=False)
    assert len(model.calls)==2 and not result['validation_errors']
    assert result['structure_diagnostics']['initial_failures']=={'json_syntax':1}
    assert result['sentences'][0]['supported'] is (not persistent)
