"""V2 sentence recovery and immutable initial settings, without real models."""
import json

import pytest

from iris.db import dumps
from iris.memory_ops import setup_role, edit_memory, delete_memory, manage_memory
from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine, current_persona, admin_edit, confirm_candidate, PersonaConflict
from test_persona import add_self


class Model:
    def __init__(self, store, *, repair='在十月一日的直播中，我选择了浅灰底。', again=False, malformed=False):
        self.store, self.repair, self.again, self.malformed = store, repair, again, malformed
        self.calls = []
        self.now = 10.0
        self.hook = None

    def monotonic(self):
        return self.now

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[-1]['content'])
        self.calls.append((purpose, payload, kwargs['_deadline'] - self.now))
        if self.hook:
            self.hook(purpose)
        if purpose == 'persona_generate':
            refs = [m['ref'] for m in payload['evidence']['memories']]
            result = {'sentences':[
                {'text':'在十月一日的直播中，我解释了规则。', 'basis':[refs[0]]},
                {'text':'我一向喜欢浅灰底。', 'basis':[refs[-1]]}]}
        elif purpose == 'persona_sentence_repair':
            result = {'repairs':[{'index':s['index'], 'text':self.repair} for s in payload['problems']]}
            if self.malformed:
                result['repairs'] = []
        else:
            rows = []
            for i, sentence in enumerate(payload['candidate'], 1):
                bad = '一向' in sentence['text'] or (purpose == 'persona_recheck' and self.again)
                rows.append({'index':i, 'supported':not bad, 'fabricated':False,
                    'scene_qualified':not bad, 'violations':['单一日期泛化'] if bad else [],
                    'reason':'只支持当次直播。' if bad else '本句来源支持范围。'})
            result = {'sentences':rows, 'change_degree':'small', 'reason':'有界经历。'}
        return ModelReply(dumps(result), 'stop', {})


@pytest.fixture
def prepared(store):
    setup_role(store, 'Iris', '我是浮岛档案员。我的袖口有银线。故事中我曾整理星图。')
    add_self(store, '我在十月一日的直播中解释了规则。', importance=90)
    add_self(store, '我在十月一日的直播中选择了浅灰底。', importance=80)
    store.set_setting('persona_publish_mode', 'small_medium_auto')
    return store


def run(store, **kwargs):
    model = Model(store, **kwargs)
    result = PersonaEngine(store, model).regenerate(expected_version=current_persona(store)['id'])
    return result, model


def test_template_is_byte_exact_and_never_sent_to_generator_as_editable_evidence(prepared):
    initial = current_persona(prepared)
    result, model = run(prepared)
    version = result['version']
    assert version['content'].startswith(initial['content'])
    assert version['sentences'][:len(initial['sentences'])] == initial['sentences']
    generation = model.calls[0][1]
    assert generation['locked_sentences'] == [{'text':s['text'],'origin':s['origin']} for s in initial['sentences']]
    assert all(m['stance'] != '设定' for m in generation['evidence']['memories'])
    check = model.calls[1][1]
    assert check['candidate'][0]['initial_settings']['role_name'] == 'Iris'
    assert check['candidate'][1]['text'].startswith('初始设定：')
    assert len(check['candidate'][1]['evidence_refs']) == 3
    assert version['checks']['passed'] and result['status'] == 'current'


def test_problem_repaired_once_with_only_its_own_evidence_and_rechecked(prepared):
    result, model = run(prepared)
    assert [c[0] for c in model.calls] == ['persona_generate','persona_check','persona_sentence_repair','persona_recheck']
    request = model.calls[2][1]
    assert len(request['problems']) == 1
    assert 'previous' not in request and 'candidate' not in request and 'locked_sentences' not in request
    problem = request['problems'][0]
    assert len(problem['evidence_refs']) == 1
    assert '浅灰底' in request['evidence_by_ref'][problem['evidence_refs'][0]]['content']
    assert '解释' not in dumps(request)
    assert len(model.calls[3][1]['candidate']) == 1
    checks = result['version']['checks']
    assert len(checks['repaired_sentences']) == 1 and not checks['deleted_sentences']
    assert checks['initial_model_errors'] and not checks['model_errors']
    assert len(checks['model']['sentences']) == len(result['version']['sentences'])
    assert [c[2] for c in model.calls] == [120,180,120,180]


@pytest.mark.parametrize('options', [{'again':True}, {'repair':None}, {'repair':'第一句。第二句。'}, {'repair':'我照着M1做。'}, {'malformed':True}])
def test_failed_or_invalid_repair_is_removed_with_audit_and_good_sentences_survive(prepared, options):
    result, model = run(prepared, **options)
    version = result['version']
    assert result['status'] in ('current','pending') and version['checks']['passed']
    assert '一向' not in version['content'] and '浅灰底' not in version['content']
    assert '解释了规则' in version['content']
    deleted = version['checks']['deleted_sentences']
    assert len(deleted) == 1 and deleted[0]['sentence']['text'] == '我一向喜欢浅灰底。'
    assert deleted[0]['reasons']
    assert [c[0] for c in model.calls].count('persona_sentence_repair') == 1
    assert version['checks']['deterministic']['passed']


@pytest.mark.parametrize('change', ['edit','forget','delete'])
def test_stale_template_sentence_is_not_restored_from_setup(prepared, change):
    if change == 'edit':
        edit_memory(prepared, 1, 1, content='我改用另一项身份设定。')
    elif change == 'forget':
        manage_memory(prepared, 1, 1, action='forget')
    else:
        delete_memory(prepared, 1, 1)
    result, _ = run(prepared)
    assert '浮岛档案员' not in result['version']['content']
    assert result['version']['material']['omitted_template_sentences']


def test_template_evidence_is_reserved_inside_6000_token_budget(prepared):
    for i in range(40):
        add_self(prepared, f'另一段实际自评 {i}。'+('材料'*100), importance=99)
    result, _ = run(prepared)
    material = result['version']['material']
    assert len(material['locked_sentences']) == 2
    assert material['evidence']['estimated_tokens'] <= 6000
    assert {1,2,3} <= {m['memory_id'] for m in material['evidence']['memories']}


def test_admin_removal_after_sentence_recovery_still_forces_large(prepared):
    previous = current_persona(prepared)
    admin_edit(prepared, previous['content']+'我是无依据的银叶见证人。', expected_version=previous['id'])
    result, _ = run(prepared)
    assert result['status'] == 'pending' and result['version']['change_degree'] == 'large'
    assert result['version']['checks']['admin_content_removed_or_changed']


@pytest.mark.parametrize('point', ['persona_sentence_repair','persona_recheck'])
def test_concurrent_evidence_edit_during_recovery_cannot_publish(prepared, point):
    model=Model(prepared)
    def hook(purpose):
        if purpose == point:
            edit_memory(prepared, 5, 1, content='现在是另一个意思。')
    model.hook=hook
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['status']=='conflict' and result['version'] is None
    assert current_persona(prepared)['id']==1


@pytest.mark.parametrize('point', ['persona_check','persona_sentence_repair','persona_recheck'])
def test_model_failure_is_not_silently_treated_as_a_failed_sentence(prepared, point):
    model=Model(prepared)
    def hook(purpose):
        if purpose==point:
            raise ModelError('timeout','deadline exceeded')
    model.hook=hook
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['status']=='failed' and result['version'] is None
    assert current_persona(prepared)['id']==1


def test_pending_repaired_version_rechecks_basis_before_confirmation(prepared):
    prepared.set_setting('persona_publish_mode','all_manual')
    result,_=run(prepared)
    edit_memory(prepared,5,1,content='修改刚才的观点。')
    with pytest.raises(PersonaConflict):
        confirm_candidate(prepared,result['version']['id'],expected_version=1)


@pytest.mark.parametrize('failure', ['宣布不是完成','当前状态或待办','性别代词无依据','借用其他记忆细节','设定写成亲历'])
def test_each_semantic_failure_can_be_removed_without_rejecting_supported_sentences(prepared, failure):
    class SpecificFailure(Model):
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_check':
                value=json.loads(reply.content)
                value['sentences'][-1].update(supported=False,violations=[failure],reason=failure)
                reply.content=dumps(value)
            return reply
    model=SpecificFailure(prepared,repair=None)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert checks['passed'] and failure in checks['deleted_sentences'][0]['reasons']
    assert failure in model.calls[2][1]['problems'][0]['problems']
    assert '解释了规则' in result['version']['content']


def test_repair_cannot_replace_or_expand_evidence(prepared):
    class ForgedBasis(Model):
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_sentence_repair':
                value=json.loads(reply.content)
                value['repairs'][0]['basis']=['M1']
                reply.content=dumps(value)
            return reply
    result=PersonaEngine(prepared,ForgedBasis(prepared)).regenerate(expected_version=1)
    assert result['version']['checks']['passed']
    assert len(result['version']['checks']['deleted_sentences'])==1
    assert not result['version']['checks']['repaired_sentences']


def test_current_version_conflict_during_repair_is_not_overwritten(prepared):
    model=Model(prepared)
    def hook(purpose):
        if purpose=='persona_sentence_repair':
            admin_edit(prepared,'管理员新发布的内容。',expected_version=1)
    model.hook=hook
    with pytest.raises(PersonaConflict):
        PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert current_persona(prepared)['content']=='管理员新发布的内容。'


def test_unchanged_bad_repair_is_removed_without_rechecking_as_good(prepared):
    result,model=run(prepared,repair='我一向喜欢浅灰底。')
    assert not result['version']['checks']['repaired_sentences']
    assert '一向' not in result['version']['content']
    assert model.calls[-1][1]['candidate']==[]


@pytest.mark.parametrize('reason', ['usage_limit','temporarily_unavailable','call_budget','interrupted'])
def test_shared_pause_and_dream_budget_gate_also_apply_to_repair(prepared,reason):
    model=Model(prepared)
    def hook(purpose):
        if purpose=='persona_sentence_repair':
            raise ModelError('paused','not admitted',paused=True,reason=reason)
    model.hook=hook
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['status']=='skipped' and result['reason']==reason
    assert current_persona(prepared)['id']==1
    assert not [p for p,_,_ in model.calls if p=='persona_recheck']


@pytest.mark.parametrize('duration,success',[(121,True),(181,False)])
def test_actual_gateway_check_budget_is_180_seconds_and_records_timeout(prepared,duration,success):
    import httpx
    from iris.models import Gateway, ModelConfig
    now=[0.0]
    limits=[]
    def respond(request):
        limits.append(request.extensions['timeout']['read'])
        now[0]+=duration
        return httpx.Response(200,json={'choices':[{'message':{'content':dumps({'sentences':[],'change_degree':'small','reason':'空扩展'})},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':8,'completion_tokens':4}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway=Gateway({'chat':ModelConfig('https://model.invalid','', 'fake',reasoning_effort='high')},prepared,
                        client=client,monotonic=lambda:now[0],sleeper=lambda _:None)
        try:
            engine=PersonaEngine(prepared,gateway)
            if success:
                assert engine._call('persona_check',{'candidate':[]},{})['sentences']==[]
            else:
                with pytest.raises(ModelError) as error:
                    engine._call('persona_check',{'candidate':[]},{})
                assert error.value.reason=='timeout'
        finally:
            gateway.close()
    assert limits==[180]
    with prepared.read() as conn:
        call=dict(conn.execute('SELECT * FROM model_calls').fetchone())
    assert bool(call['timed_out']) is (not success)
    assert call['purpose']=='persona_check' and call['reasoning_effort']=='high'


def test_json_correction_does_not_reset_check_budget(prepared):
    import httpx
    from iris.models import Gateway, ModelConfig
    now=[0.0];limits=[]
    def respond(request):
        limits.append(request.extensions['timeout']['read'])
        now[0]+=100 if len(limits)==1 else 60
        content='broken' if len(limits)==1 else dumps({'sentences':[],'change_degree':'small','reason':'完整 JSON'})
        return httpx.Response(200,json={'choices':[{'message':{'content':content},'finish_reason':'stop'}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway=Gateway({'chat':ModelConfig('https://model.invalid','', 'fake')},prepared,client=client,
                        monotonic=lambda:now[0],sleeper=lambda _:None)
        try:
            outputs={}
            PersonaEngine(prepared,gateway)._call('persona_check',{'candidate':[]},outputs)
        finally:
            gateway.close()
    assert limits==[180,80]
    assert set(outputs)=={'persona_check','persona_check_repair'}


def test_repaired_sentence_must_still_pass_whole_persona_length_limit(prepared):
    result,_=run(prepared,repair='在十月一日的直播中，'+('浅灰'*370)+'。')
    assert result['status']=='rejected'
    assert 'persona exceeds 800 characters' in result['version']['checks']['deterministic']['errors']
    assert current_persona(prepared)['id']==1


def test_long_check_material_deduplicates_shared_sources(prepared):
    from iris.persona import _generation_material, _settings, _check_payload, _deterministic, _editable_material
    from iris.queue import estimate_tokens
    previous=current_persona(prepared)
    with prepared.read() as conn:
        material=_generation_material(conn,_settings(conn),previous,'2026-10-10T00:00:00+00:00')
    editable=_editable_material(material)
    ref=editable['evidence']['memories'][0]['ref']
    _,sentences,check=_deterministic({'sentences':[{'text':'在那次直播中，我解释了规则。','basis':[ref]}]},editable,previous)
    assert check['passed']
    repeated=sentences*30
    request=_check_payload(material,repeated,full_content=''.join(s['text'] for s in repeated))
    assert len(request['evidence_by_ref'])==1
    assert all(s['evidence_refs']==[ref] for s in request['candidate'])
    assert 'trace_refs' not in dumps(request) and 'trace_sha256' not in dumps(request)
    assert estimate_tokens(dumps(request))<6000
    assert material['prompt_versions']['persona_generate']=='persona_generate_v2'


def test_model_cannot_rewrite_locked_template_or_invent_its_attribution(prepared):
    class RewritesTemplate(Model):
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_generate':
                reply.content=dumps({'sentences':[{'text':'我真正在浮岛当过档案员。','basis':['M1'],
                                                   'origin':'initial_template'}]})
            return reply
    result=PersonaEngine(prepared,RewritesTemplate(prepared)).regenerate(expected_version=1)
    assert result['status']=='rejected'
    assert result['version']['checks']['model'] is None
    assert '初始设定：' in current_persona(prepared)['content']
