"""Fake-only mechanism fixtures; these are not the frozen quality corpus."""
import importlib.util
import json
from pathlib import Path

import pytest

from iris.db import dumps
spec=importlib.util.spec_from_file_location('person_summary_runner',Path(__file__).parents[1]/'evals/person_summary_eval/runner.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
load_corpus,run_eval,score,_Responses=runner.load_corpus,runner.run_eval,runner.score,runner._Responses


def fixture_case():
    return {'id':'tiny','subject_id':'a','as_of':'2026-10-11T12:00:00+00:00',
        'subjects':[{'id':'a','name':'测试人'}],
        'entries':[{'id':'local','kind':'group','visibility':'entry_only'}],
        'memories':[{'id':f'm{i}','content':'公开机制测试内容','speaker':'a',
                     'created_at':f'2026-10-{i+1:02}T00:00:00+00:00'} for i in range(8)]+[
                     {'id':'private','content':'紫色密钥盒测试标记','speaker':'a','source_entries':['local'],
                      'importance':100,'created_at':'2026-10-01T00:00:00+00:00'}],
        'must_cover':['公开机制测试要点'],'forbidden':['入口限定事项'],
        'leak_probes':[{'memory_id':'private','text':'紫色密钥盒测试标记'}]}


def bundle(tmp_path, *, leaking=False):
    case=fixture_case(); path=tmp_path/'tiny.json'
    path.write_text(dumps({'format_version':1,'cases':[case]}),encoding='utf-8')
    model=_Responses([{'items':[{'kind':'fact','text':'紫色密钥盒测试标记' if leaking else '公开机制测试内容','memory_refs':['M1']}]},
        {'items':[{'index':1,'supported':True,'scene_qualified':True,'violations':[],'reason':'流程测试'}],
         'change_degree':'small','reason':'流程测试'}])
    report=run_eval(path,tmp_path/'out',gateway_factory=lambda configs,store:model)
    return report,case


def make_round(tmp_path,report,*,supported=True,covered=True,forbidden=False):
    materials=Path(report['materials']); path=tmp_path; path.mkdir()
    (path/'manifest.json').write_bytes((materials/'round-template.json').read_bytes())
    (path/'0000.json').write_text(dumps({'item_results':[{'index':1,'supported':supported,
        'violations':[] if supported else ['无依据'],'reason':'假判分结构测试'}],
        'must_cover':[covered],'forbidden':[forbidden]}),encoding='utf-8')
    return path


def test_isolated_material_export_adverse_two_rounds_and_no_annotations_in_prompt(tmp_path):
    report,case=bundle(tmp_path)
    assert report['metrics']['scoped_leaks']==0
    assert report['metrics']['scoped_memories_checked']==1
    assert report['metrics']['meets_threshold'] is False
    material=json.loads(report['rows'][0]['attempts'][0]['material_json'])
    assert '紫色密钥盒测试标记' not in dumps(material)
    assert case['must_cover'][0] not in dumps(material)
    a=make_round(tmp_path/'round-a',report)
    b=make_round(tmp_path/'round-b',report,supported=False,covered=False,forbidden=True)
    result=score(report['materials'],[a,b],judge_model='fake-format-test',out=tmp_path/'score')
    assert result['metrics']['grounded_item_ratio']==0
    assert result['metrics']['forbidden_occurrences']==1
    assert result['metrics']['must_cover_coverage']==0
    assert result['disagreements'] and not result['metrics']['meets_threshold']


def test_leak_probe_is_a_deterministic_gate_independent_of_external_judge(tmp_path):
    report,_=bundle(tmp_path,leaking=True)
    assert report['metrics']['scoped_leaks']>0
    a=make_round(tmp_path/'round',report)
    result=score(report['materials'],[a],judge_model='fake',out=tmp_path/'score')
    assert result['metrics']['grounded_item_ratio']==1
    assert not result['metrics']['meets_threshold']


@pytest.mark.parametrize('change',['bool','count','index','path','fingerprint'])
def test_reject_malformed_or_mismatched_judgments(tmp_path,change):
    report,_=bundle(tmp_path); path=make_round(tmp_path/'round',report)
    vote=json.loads((path/'0000.json').read_text())
    if change=='bool': vote['must_cover']=[1]
    elif change=='count': vote['item_results']=[]
    elif change=='index': vote['item_results'][0]['index']=True
    elif change=='fingerprint':
        (path/'manifest.json').write_text(dumps({'materials_sha256':'wrong'}))
    elif change=='path':
        copy_path=tmp_path/'external.json'; copy_path.write_text(dumps(vote))
        (path/'0000.json').unlink(); (path/'0000.json').symlink_to(copy_path)
    if change!='path': (path/'0000.json').write_text(dumps(vote))
    with pytest.raises(ValueError):
        score(report['materials'],[path],judge_model='fake',out=tmp_path/'score')


def test_loader_rejects_ambiguous_leak_marker_and_missing_parent(tmp_path):
    case=fixture_case(); case['leak_probes'][0]['text']='公开'
    path=tmp_path/'bad.json'
    path.write_text(dumps({'format_version':1,'cases':[case]}))
    with pytest.raises(ValueError): load_corpus(path)
    case=fixture_case(); case['memories'][0]['derived_from']=['missing']
    path.write_text(dumps({'format_version':1,'cases':[case]}))
    with pytest.raises(ValueError): load_corpus(path)


def test_unicode_escaped_model_output_cannot_bypass_leak_check(tmp_path,monkeypatch):
    from iris.models import ModelReply
    original=_Responses.chat
    def escaped(self,*args,**kwargs):
        reply=original(self,*args,**kwargs)
        return ModelReply(json.dumps(json.loads(reply.content),ensure_ascii=True),reply.finish_reason,reply.usage)
    monkeypatch.setattr(_Responses,'chat',escaped)
    report,_=bundle(tmp_path,leaking=True)
    assert report['metrics']['scoped_leaks']>0


def test_quoted_multiline_probe_uses_text_not_json_serialization(tmp_path):
    case=fixture_case(); phrase='紫色"盒子\n测试标记'
    case['memories'][-1]['content']=phrase
    case['leak_probes'][0]['text']=phrase
    path=tmp_path/'quoted.json';path.write_text(dumps({'format_version':1,'cases':[case]}),encoding='utf-8')
    model=_Responses([{'items':[{'kind':'fact','text':phrase,'memory_refs':['M1']}]},
        {'items':[{'index':1,'supported':True,'scene_qualified':True,'violations':[],'reason':'格式测试'}],
         'change_degree':'small','reason':'格式测试'}])
    report=run_eval(path,tmp_path/'out',gateway_factory=lambda configs,store:model)
    assert report['metrics']['scoped_leaks']>0


@pytest.mark.parametrize('reason', ['server_error','rate_limit','paused','usage_limit','temporary_unavailable'])
def test_check_model_error_is_incomplete_even_with_rejected_version(tmp_path, reason):
    from iris.models import ModelError
    class Unavailable(_Responses):
        def chat(self, messages, purpose, **kwargs):
            if purpose.endswith('check'):
                raise ModelError('paused' if reason in ('paused','usage_limit') else 'server_error',
                                 'synthetic unavailable response', reason=reason)
            return super().chat(messages, purpose, **kwargs)
    path=tmp_path/'tiny.json'; path.write_text(dumps({'format_version':1,'cases':[fixture_case()]}))
    model=Unavailable([{'items':[{'kind':'fact','text':'公开机制测试内容','memory_refs':['M1']}]}])
    report=run_eval(path,tmp_path/'out',gateway_factory=lambda configs,store:model)
    assert report['metrics']['incomplete_cases']==1
    assert report['rows'][0]['complete'] is False


@pytest.mark.parametrize('reason', ['unconfigured','paused','usage_limit','temporary_unavailable'])
def test_unavailable_before_generation_is_incomplete(tmp_path, reason):
    from iris.models import ModelError
    class Health:
        def check(self, *args, **kwargs):
            raise ModelError('paused','synthetic unavailable response',reason=reason)
    model=_Responses([]); model.health=Health()
    path=tmp_path/'tiny.json'; path.write_text(dumps({'format_version':1,'cases':[fixture_case()]}))
    report=run_eval(path,tmp_path/'out',gateway_factory=lambda configs,store:model)
    assert report['metrics']['incomplete_cases']==1


def test_invalid_actual_output_is_a_completed_judgment(tmp_path):
    from iris.models import ModelReply
    class Invalid:
        def chat(self, *args, **kwargs):
            return ModelReply('invalid JSON', 'stop', {})
    path=tmp_path/'tiny.json'; path.write_text(dumps({'format_version':1,'cases':[fixture_case()]}))
    report=run_eval(path,tmp_path/'out',gateway_factory=lambda configs,store:Invalid())
    assert report['metrics']['incomplete_cases']==0
    assert report['rows'][0]['result']['reason']=='invalid_output'


def test_alias_source_extension_is_backward_compatible_and_audited_independently(tmp_path):
    case=fixture_case()
    case['subjects'][0].update(aliases=['夜猫'],alias_source_entries={'夜猫':'local'})
    case['memories'].append({'id':'alias-only','content':'夜猫最近在学日语','speaker':'self',
        'importance':100,'created_at':'2026-10-01T00:00:00+00:00'})
    path=tmp_path/'tiny.json';path.write_text(dumps({'format_version':1,'cases':[case]}))
    model=_Responses([{'items':[{'kind':'fact','text':'公开机制测试内容','memory_refs':['M1']}]},
        {'items':[{'index':1,'supported':True,'scene_qualified':True,'violations':[],'reason':'机制检查'}],
         'change_degree':'small','reason':'机制检查'}])
    report=run_eval(path,tmp_path/'out',gateway_factory=lambda configs,store:model)
    row=report['rows'][0]; mid=row['memory_ids']['alias-only']
    material=json.loads(row['attempts'][0]['material_json'])
    assert mid not in {m['memory_id'] for m in material['evidence']['memories']}
    assert report['metrics']['scoped_leaks']==0
    material['evidence']['memories'].append({'memory_id':mid})
    row['attempts'][0]['material_json']=dumps(material)
    assert any(v['kind']=='restricted_identity_material' for v in runner._leaks(row)['violations'])
    del case['subjects'][0]['alias_source_entries']
    path.write_text(dumps({'format_version':1,'cases':[case]}))
    assert load_corpus(path)[0]['subjects'][0]['aliases']==['夜猫']


def test_private_alias_audit_does_not_use_ambiguous_role_name_as_public_link():
    case = fixture_case()
    case['subjects'][0].update(aliases=['夜猫', 'Iris'], alias_source_entries={'夜猫':'local'})
    case['memories'].append({'id':'alias-only', 'content':'夜猫最近在与 Iris 聊日语', 'speaker':'self'})
    identity, private = runner._private_identity(case)
    assert 'alias-only' in identity
    assert private == ['夜猫']


def write_corpus(tmp_path, cases):
    path=tmp_path/'cases.json'
    path.write_text(dumps({'format_version':1,'cases':cases}),encoding='utf-8')
    return path


def none_case():
    case=fixture_case()
    case.update(id='no-summary',expect_none=True,must_cover=[])
    case['memories']=case['memories'][:3]+case['memories'][-1:]
    return case


def good_responses():
    return _Responses([{'items':[{'kind':'fact','text':'公开机制测试内容','memory_refs':['M1']}]},
        {'items':[{'index':1,'supported':True,'scene_qualified':True,'violations':[],'reason':'机制检查'}],
         'change_degree':'small','reason':'机制检查'}])


@pytest.mark.parametrize('value',[None,0,1,'true',[],{}])
def test_expect_none_requires_a_real_boolean(tmp_path,value):
    case=none_case(); case['expect_none']=value
    with pytest.raises(ValueError,match='expect_none'):
        load_corpus(write_corpus(tmp_path,[case]))


def test_expect_none_cannot_have_must_cover_points(tmp_path):
    case=none_case(); case['must_cover']=['不应要求生成的要点']
    with pytest.raises(ValueError,match='must_cover'):
        load_corpus(write_corpus(tmp_path,[case]))


def test_expect_none_insufficient_is_complete_and_exports_strictly_empty_task(tmp_path):
    report=run_eval(write_corpus(tmp_path,[none_case()]),tmp_path/'out',
                    gateway_factory=lambda configs,store:_Responses([]))
    row=report['rows'][0]
    assert row['complete'] is True
    assert row['due']['global_memories']==3
    assert row['expect_none_check']['status']=='passed'
    assert row['material_diagnostics'] is None
    assert len(row['attempts'])==1 and row['attempts'][0]['state']=='skipped'
    assert json.loads(row['attempts'][0]['material_json'])=={}
    assert json.loads(row['attempts'][0]['outputs_json'])=={}
    assert row['generated_versions']==[]
    assert report['metrics']['expect_none_passed']==1
    assert report['metrics']['expect_none_violations']==0
    assert report['metrics']['incomplete_cases']==0
    payload=json.loads((Path(report['materials'])/'cases/0000.json').read_text())['input']
    assert payload['judgment_mode']=='empty'
    assert payload['must_cover']==payload['forbidden']==[]
    empty={'item_results':[],'must_cover':[],'forbidden':[]}
    assert runner._validate(empty,row)==empty
    with pytest.raises(ValueError):
        runner._validate({**empty,'forbidden':[False]},row)
    with pytest.raises(ValueError):
        runner._validate({**empty,'item_results':[{'index':1}]},row)


def none_row():
    return {'case':none_case(),'memory_ids':{'m0':1,'m1':2,'m2':3,'private':4},
            'result':{'status':'skipped','reason':'insufficient_memories','version':None},
            'due':{'global_memories':3},'attempts':[],'calls':[],'generated_versions':[],'complete':True}


def attempt_row(**updates):
    return {'id':1,'state':'skipped','version':None,'material_json':'{}',
            'outputs_json':'{}','calls_json':'[]',**updates}


@pytest.mark.parametrize('reason',['unconfigured','paused','usage_limit','temporary_unavailable',
                                    'call_budget','disabled','no_evidence','not_due'])
def test_expect_none_other_skip_reasons_stay_incomplete(reason):
    row=none_row(); row['result']['reason']=reason
    check=runner._expect_none_check(row)
    assert check['status']=='incomplete' and check['violations']==[]
    assert runner._row_complete(row) is False


@pytest.mark.parametrize('state',['running','published','candidate','rejected','failed','conflict'])
def test_any_non_skipped_attempt_is_an_expect_none_failure_even_without_output(state):
    row=none_row(); row['attempts']=[attempt_row(state=state)]
    check=runner._expect_none_check(row)
    assert check['status']=='failed' and check['violations']
    assert runner._metrics([row])['expect_none_violations']>0


@pytest.mark.parametrize('field,value',[
    ('material_json','{"evidence":{"memories":[]}}'),
    ('outputs_json','{"generate":"bad output"}'),
    ('calls_json','[{"result":"error"}]'),('version',1)])
def test_skip_label_cannot_hide_generation_artifacts(field,value):
    row=none_row(); row['attempts']=[attempt_row(**{field:value})]
    assert runner._expect_none_check(row)['status']=='failed'


@pytest.mark.parametrize('status',['published','candidate','rejected'])
def test_any_returned_version_fails_expect_none(status):
    row=none_row(); row['result'].update(status=status,version={'version':1,'status':status})
    assert runner._expect_none_check(row)['status']=='failed'


def test_persisted_version_cannot_be_hidden_by_skip_result(tmp_path,monkeypatch):
    case=fixture_case(); case.update(expect_none=True,must_cover=[])
    original=runner.PersonSummaryEngine.regenerate
    def hidden(self,*args,**kwargs):
        original(self,*args,**kwargs)
        # Simulate a regression losing the returned version and attempt record.
        with self.store.write() as conn:
            conn.execute('DELETE FROM person_summary_attempts')
        return {'status':'skipped','reason':'insufficient_memories','version':None}
    monkeypatch.setattr(runner.PersonSummaryEngine,'regenerate',hidden)
    report=run_eval(write_corpus(tmp_path,[case]),tmp_path/'out',gateway_factory=lambda configs,store:good_responses())
    row=report['rows'][0]
    assert row['generated_versions'][0]['status']=='published'
    assert row['expect_none_check']['status']=='failed'
    assert report['metrics']['expect_none_violations']>0


def test_expect_none_failed_provider_attempt_is_a_deterministic_failure(tmp_path):
    from iris.models import ModelError
    class Failed:
        def chat(self,*args,**kwargs):
            raise ModelError('server_error','synthetic failure')
    case=fixture_case(); case.update(expect_none=True,must_cover=[])
    report=run_eval(write_corpus(tmp_path,[case]),tmp_path/'out',gateway_factory=lambda configs,store:Failed())
    assert report['rows'][0]['result']['status']=='failed'
    assert report['rows'][0]['expect_none_check']['status']=='failed'
    assert report['metrics']['expect_none_violations']>0


def test_empty_tasks_do_not_dilute_grounding_or_coverage_denominators(tmp_path):
    report=run_eval(write_corpus(tmp_path,[fixture_case(),none_case()]),tmp_path/'out',
                    gateway_factory=lambda configs,store:good_responses())
    path=make_round(tmp_path/'round',report)
    (path/'0001.json').write_text(dumps({'item_results':[],'must_cover':[],'forbidden':[]}),encoding='utf-8')
    result=score(report['materials'],[path],judge_model='fake-format-test',out=tmp_path/'score')
    assert result['metrics']['meets_threshold'] is True
    assert result['metrics']['expect_none_passed']==1
    assert result['metrics']['must_cover_total']==1
    assert result['metrics']['model_items']==1
    # Even a fully supportive external judgment cannot erase an unexpected attempt.
    rows=result['rows']; rows[1]['attempts'].append(attempt_row(state='failed'))
    assert runner._metrics(rows,result['judgments'])['meets_threshold'] is False


def test_seeded_admin_summary_is_not_a_generated_version(tmp_path):
    case=none_case(); case['initial_summary']='管理员保留的说明。'
    report=run_eval(write_corpus(tmp_path,[case]),tmp_path/'out',gateway_factory=lambda configs,store:_Responses([]))
    assert report['rows'][0]['generated_versions']==[]
    assert report['rows'][0]['expect_none_check']['status']=='passed'


def test_material_truncation_lists_only_displaced_globally_available_memories(tmp_path):
    case=fixture_case()
    case['memories'].append({'id':'forgotten','content':'遗忘的机制夹具','speaker':'a','lifecycle':'forgotten',
                             'importance':100,'created_at':'2026-10-01T00:00:00+00:00'})
    def factory(configs,store):
        store.set_setting('person_summary',{'memory_tokens':800})
        return good_responses()
    report=run_eval(write_corpus(tmp_path,[case]),tmp_path/'out',gateway_factory=factory)
    row=report['rows'][0]; diagnostic=row['material_diagnostics']
    material=json.loads(row['attempts'][0]['material_json'])['evidence']['memories']
    selected={m['memory_id'] for m in material}
    expected=[key for key,mid in row['memory_ids'].items() if key.startswith('m') and mid not in selected]
    assert diagnostic['selection_source']=='attempt'
    assert diagnostic['global_memories']==row['due']['global_memories']==8
    assert diagnostic['selected_memories']==len(selected)<8
    assert diagnostic['material_truncated'] is True
    assert diagnostic['excluded_memory_ids']==expected
    assert len(diagnostic['excluded_database_memory_ids'])==8-len(selected)
    assert report['metrics']['material_truncated_cases']==1
    assert report['metrics']['material_truncated_memories']==len(expected)
    assert report['metrics']['material_truncations'][0]['case_id']==case['id']


def test_selection_snapshot_distinguishes_provider_skip_from_budget_truncation(tmp_path):
    from iris.models import ModelError
    class Health:
        def check(self,*args,**kwargs):
            raise ModelError('paused','synthetic pause',reason='paused')
    def factory(configs,store):
        model=_Responses([]); model.health=Health()
        return model
    report=run_eval(write_corpus(tmp_path,[fixture_case()]),tmp_path/'out',gateway_factory=factory)
    row=report['rows'][0]; diagnostic=row['material_diagnostics']
    assert row['complete'] is False
    assert diagnostic['selection_source']=='pre_generation_snapshot'
    assert diagnostic['selected_memories']==diagnostic['global_memories']==8
    assert diagnostic['material_truncated'] is False
    assert diagnostic['excluded_memory_ids']==[]


def test_no_evidence_budget_skip_still_reports_all_displaced_ids(tmp_path):
    def factory(configs,store):
        store.set_setting('person_summary',{'memory_tokens':1})
        return _Responses([])
    report=run_eval(write_corpus(tmp_path,[fixture_case()]),tmp_path/'out',gateway_factory=factory)
    row=report['rows'][0]; diagnostic=row['material_diagnostics']
    assert row['result']['reason']=='no_evidence'
    assert diagnostic['selected_memories']==0
    assert diagnostic['global_memories']==8
    assert diagnostic['material_truncated'] is True
    assert diagnostic['excluded_memory_ids']==[f'm{i}' for i in range(8)]


@pytest.mark.parametrize('lifecycle',['forgotten','deleted','purged'])
def test_non_active_lifecycle_seed_is_excluded_and_audited(tmp_path,lifecycle):
    case=fixture_case(); case['memories'].append({'id':'inactive','content':'失效的机制夹具','speaker':'a',
        'lifecycle':lifecycle,'importance':100,'created_at':'2026-10-01T00:00:00+00:00'})
    def factory(configs,store):
        with store.read() as conn:
            saved=conn.execute('SELECT lifecycle,purged_at,content FROM memories ORDER BY id DESC LIMIT 1').fetchone()
            assert saved['lifecycle']==('deleted' if lifecycle=='purged' else lifecycle)
            if lifecycle=='purged':
                assert saved['purged_at']==case['as_of'] and saved['content']==''
        return good_responses()
    report=run_eval(write_corpus(tmp_path,[case]),tmp_path/'out',gateway_factory=factory)
    row=report['rows'][0]
    assert row['complete'] is True
    assert row['due']['global_memories']==8
    assert report['metrics']['non_active_memories_checked']==1
    assert report['metrics']['non_active_violations']==0
    assert 'inactive' not in row['material_diagnostics']['excluded_memory_ids']
    check=runner._non_active(row)
    assert check['violations']==[]
    inactive=row['memory_ids']['inactive']
    row['result']['version']['sentences'][0]['basis'][0]['memory_id']=inactive
    assert any(v['kind']=='non_active_reference' and v['lifecycle']==lifecycle for v in runner._non_active(row)['violations'])


@pytest.mark.parametrize('lifecycle',['forgotten','deleted','purged'])
def test_non_active_gate_uses_corpus_state_independent_of_engine_and_grader(tmp_path,lifecycle):
    case=fixture_case(); case['memories'].append({'id':'inactive','content':'失效的机制夹具','speaker':'a',
        'lifecycle':lifecycle,'importance':100,'created_at':'2026-10-01T00:00:00+00:00'})
    def factory(configs,store):
        # Simulate an erroneous product selection. The corpus remains unchanged.
        with store.write() as conn:
            conn.execute("UPDATE memories SET lifecycle='active',purged_at=NULL,content='失效的机制夹具' WHERE importance=100 AND purged_at IS NOT NULL")
            conn.execute("UPDATE memories SET lifecycle='active' WHERE lifecycle!='active'")
        return good_responses()
    report=run_eval(write_corpus(tmp_path,[case]),tmp_path/'out',gateway_factory=factory)
    violations=runner._non_active(report['rows'][0])['violations']
    assert {'non_active_material','non_active_reference'}<={v['kind'] for v in violations}
    assert report['metrics']['non_active_violations']>=2
    path=make_round(tmp_path/'round',report)
    result=score(report['materials'],[path],judge_model='fake',out=tmp_path/'score')
    assert result['metrics']['grounded_item_ratio']==1
    assert result['metrics']['meets_threshold'] is False


def test_expect_none_audits_leaks_and_inactive_ids_even_in_skipped_attempt_material():
    row=none_row(); row['case']['memories'][0]['lifecycle']='deleted'
    material={'evidence':{'memories':[{'memory_id':4}], 'previous_basis':[{'memory_id':1}]}}
    row['attempts']=[attempt_row(material_json=dumps(material),outputs_json=dumps({'generate':'紫色密钥盒测试标记'}))]
    assert runner._expect_none_check(row)['status']=='failed'
    assert runner._leaks(row)['violations']
    assert any(v['kind']=='non_active_material' and v['memory_id']==1 for v in runner._non_active(row)['violations'])


def test_expect_none_handles_zero_memories_without_model_calls(tmp_path):
    case=none_case(); case.update(memories=[],leak_probes=[])
    report=run_eval(write_corpus(tmp_path,[case]),tmp_path/'out',gateway_factory=lambda configs,store:_Responses([]))
    assert report['rows'][0]['due']['global_memories']==0
    assert report['rows'][0]['expect_none_check']['status']=='passed'


def test_empty_task_can_score_an_unexpected_version_but_cannot_pass_gate(tmp_path):
    case=fixture_case(); case.update(expect_none=True,must_cover=[])
    report=run_eval(write_corpus(tmp_path,[case]),tmp_path/'out',gateway_factory=lambda configs,store:good_responses())
    assert report['rows'][0]['result']['version'] is not None
    path=tmp_path/'round'; path.mkdir()
    (path/'manifest.json').write_bytes((Path(report['materials'])/'round-template.json').read_bytes())
    (path/'0000.json').write_text(dumps({'item_results':[],'must_cover':[],'forbidden':[]}),encoding='utf-8')
    result=score(report['materials'],[path],judge_model='fake-format-test',out=tmp_path/'score')
    assert result['metrics']['expect_none_failed']==1
    assert result['metrics']['expect_none_violations']>0
    assert result['metrics']['model_items']==0
    assert result['metrics']['meets_threshold'] is False


def test_only_correct_expect_none_cases_do_not_claim_a_grounding_threshold(tmp_path):
    report=run_eval(write_corpus(tmp_path,[none_case()]),tmp_path/'out',gateway_factory=lambda configs,store:_Responses([]))
    path=tmp_path/'round'; path.mkdir()
    (path/'manifest.json').write_bytes((Path(report['materials'])/'round-template.json').read_bytes())
    (path/'0000.json').write_text(dumps({'item_results':[],'must_cover':[],'forbidden':[]}),encoding='utf-8')
    result=score(report['materials'],[path],judge_model='fake-format-test',out=tmp_path/'score')
    assert result['metrics']['expect_none_passed']==1
    assert result['metrics']['incomplete_cases']==result['metrics']['expect_none_violations']==0
    assert result['metrics']['grounded_item_ratio'] is None
    assert result['metrics']['meets_threshold'] is False


@pytest.mark.parametrize('lifecycle',['forgotten','deleted','purged'])
@pytest.mark.parametrize('location',['dependency_ids','admin_sentences'])
def test_non_active_check_includes_all_memory_ids_sent_in_generation_material(lifecycle,location):
    row=none_row(); row['case']['memories'][0]['lifecycle']=lifecycle
    material=({'dependency_ids':[1]} if location=='dependency_ids' else
              {'admin_sentences':[{'text':'历史条目','basis':[{'memory_id':1}]}]})
    row['attempts']=[attempt_row(material_json=dumps(material))]
    violations=runner._non_active(row)['violations']
    assert any(v['kind']=='non_active_material' and v['memory_id']==1 and v['lifecycle']==lifecycle for v in violations)
