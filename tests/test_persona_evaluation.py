import copy
import json
import tempfile
from pathlib import Path

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona_evaluation import run_persona_eval, score_persona_judgments


# A tiny handwritten format fixture, not a quality-evaluation corpus.
TIMELINE = {'id':'format-only', 'role':{'name':'Iris','background':'我来自云城。','timezone':'Asia/Shanghai'},
    'start_at':'2026-10-01T12:00:00+00:00', 'events':[
        {'at':'2026-10-02T12:00:00+00:00','op':'add','memory':{'key':'patience','content':'我在直播中耐心解释规则。',
          'speaker':'self','about':['self'],'stance':'亲历','importance':99,
          'sources':[{'content':'我会耐心解释规则。','entry':{'id':'live','kind':'live'},'sender':'self','kind':'self_output'}]}},
        {'at':'2026-10-08T12:00:00+00:00','op':'checkpoint','id':'c1','action':'update',
         'must_reflect':['直播中的耐心'],'must_not':['天生耐心'],'expected_change':'small','scenarios':['S18']},
        {'at':'2026-10-09T12:00:00+00:00','op':'persona_edit','content':'我喜欢简短的表达。'},
        {'at':'2026-10-10T12:00:00+00:00','op':'checkpoint','id':'c2','action':'regenerate',
         'must_reflect':[],'must_not':[],'expected_change':'large','scenarios':['S19']},
    ]}


class FakeGateway:
    calls = 0
    def __init__(self, configs, store, **kwargs):
        self.store = store

    def chat(self, messages, purpose, **kwargs):
        assert not self.store._writer.in_transaction
        FakeGateway.calls += 1
        if purpose == 'persona_generate':
            value = {'sentences':[{'text':'在那次直播中，我耐心解释了规则。','basis':['M1']}]}
        elif purpose == 'persona_check':
            value = {'sentences':[{'index':1,'supported':True,'fabricated':False,
                'scene_qualified':True,'violations':[],'reason':'场景明确'}], 'change_degree':'small','reason':'单场经历'}
        else:
            raise AssertionError('external mode must not call a model judge')
        return ModelReply(dumps(value),'stop',{})

    def close(self):
        pass


@pytest.fixture
def tmp_path():
    # Full evaluation bundles must stay outside the source checkout.
    with tempfile.TemporaryDirectory(prefix='iris-persona-format-') as directory:
        yield Path(directory)


@pytest.fixture
def material(tmp_path):
    corpus = tmp_path/'small.jsonl'
    corpus.write_text(dumps(TIMELINE)+'\n',encoding='utf-8')
    scoring = tmp_path/'scoring.md'
    scoring.write_text('仅用于格式测试的评分占位。',encoding='utf-8')
    FakeGateway.calls = 0
    manifest, report = run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',
        judge_mode='external',gateway_factory=FakeGateway,runner_identity='fake-format-v1',scoring_path=scoring)
    return manifest.parent, report, corpus, scoring


def make_round(materials, target, *, disagree=False):
    manifest = json.loads((materials/'manifest.json').read_text(encoding='utf-8'))
    target.mkdir()
    (target/'manifest.json').write_bytes((materials/'round-template.json').read_bytes())
    for case in manifest['cases']:
        data = json.loads((materials/case['file']).read_text(encoding='utf-8'))['input']
        rows = [{'index':i,'supported':not disagree,'violations':['虚构'] if disagree else [],'reason':'格式测试'}
                for i, sentence in enumerate(data['candidate']['sentences'],1)]
        judgment = {'sentence_results':rows, 'has_basis':not disagree,
            'must_reflect':[not disagree for _ in data['must_reflect']],
            'must_not':[disagree for _ in data['must_not']], 'level_reasonable':not disagree, 'reason':'格式测试'}
        (target/case['judgment_file']).write_text(dumps(judgment),encoding='utf-8')
    return target


def test_timeline_external_export_and_cache(material, tmp_path):
    directory, report, corpus, scoring = material
    assert FakeGateway.calls == 4
    rows = report['rows'][0]['observations']
    assert [r['candidate']['status'] for r in rows] == ['current','current','current','pending']
    assert rows[1]['candidate']['sentences'][0]['date_count'] == 1
    assert rows[-1]['candidate']['change_degree'] == 'large'
    assert report['metrics']['changes'] == 2
    assert report['metrics']['admin_publications'] == 1
    _, repeat = run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',judge_mode='external',
        gateway_factory=FakeGateway,runner_identity='fake-format-v1',scoring_path=scoring)
    assert FakeGateway.calls == 4 and repeat['resumed_cases'] == 1
    assert directory.joinpath('run.json').is_file()


def test_score_two_rounds_adverse_and_reports_disagreements(material, tmp_path):
    directory, _, _, _ = material
    first = make_round(directory,tmp_path/'first')
    second = make_round(directory,tmp_path/'second',disagree=True)
    path, report = score_persona_judgments(directory,[first,second],tmp_path,judge_model='executor-test',out=tmp_path/'scores')
    assert path.is_file()
    assert report['judge_runs'] == 2
    assert report['metrics']['grounded_change_ratio'] == 0
    assert report['metrics']['must_not_occurrences'] == 1
    assert report['disagreements']
    assert report['metrics']['violations']['虚构'] > 0


def test_rejects_material_tampering(material, tmp_path):
    directory, _, _, _ = material
    first = make_round(directory,tmp_path/'first')
    path = directory/'cases/0000.json'
    path.write_text(path.read_text()+' ',encoding='utf-8')
    with pytest.raises(ValueError,match='fingerprint'):
        score_persona_judgments(directory,[first],tmp_path,judge_model='test',out=tmp_path/'scores')


@pytest.mark.parametrize('mutation', ['bool','index','length','round_fingerprint','formula','fields','order','level'])
def test_strict_external_judgments(material,tmp_path,mutation):
    directory, _, _, _ = material
    first = make_round(directory,tmp_path/'first')
    p = first/('manifest.json' if mutation=='round_fingerprint' else '0000.json')
    data = json.loads(p.read_text())
    if mutation == 'bool':
        data['sentence_results'][0]['supported'] = 'true'
    elif mutation == 'index':
        data['sentence_results'][0]['index'] = 12345
    elif mutation == 'length':
        data['sentence_results'].pop()
    elif mutation == 'formula':
        data['has_basis'] = False
    elif mutation == 'fields':
        data['extra'] = 'not allowed'
    elif mutation == 'order':
        data['sentence_results'][0]['violations'] = ['指令','虚构']
        data['has_basis'] = False
    elif mutation == 'level':
        data['level_reasonable'] = 1
    else:
        data['materials_sha256'] = 'wrong'
    p.write_text(dumps(data),encoding='utf-8')
    with pytest.raises(ValueError):
        score_persona_judgments(directory,[first],tmp_path,judge_model='test',out=tmp_path/'scores')


def test_cli_persona_score_does_not_load_models(material,tmp_path,monkeypatch,capsys):
    import iris.cli as cli
    directory, _, _, _ = material
    first = make_round(directory,tmp_path/'first')
    monkeypatch.setattr(cli,'load_test_models',lambda *args: pytest.fail('offline score loaded credentials'))
    assert cli.main(['eval','persona-score','--materials',str(directory),'--judgments',str(first),
        '--judge-model','executor-test','--out',str(tmp_path/'scores')]) == 0


def test_invalid_timeline_rejected_before_any_calls(tmp_path):
    bad = copy.deepcopy(TIMELINE)
    bad['events'][1]['at'] = '2026-09-01T00:00:00+00:00'
    corpus = tmp_path/'bad.jsonl'
    corpus.write_text(dumps(bad),encoding='utf-8')
    with pytest.raises(ValueError):
        run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',gateway_factory=FakeGateway,runner_identity='fake')


def test_no_due_checkpoint_is_recorded_without_fabricating_judgment(tmp_path):
    case = copy.deepcopy(TIMELINE)
    case['events'] = [{'op':'checkpoint','id':'too-early','at':'2026-10-02T00:00:00+00:00',
                       'action':'update','must_reflect':['云城设定'],'must_not':['额外经历']}]
    corpus = tmp_path/'small.jsonl'
    corpus.write_text(dumps(case),encoding='utf-8')
    scoring = tmp_path/'scoring.md'
    scoring.write_text('格式测试',encoding='utf-8')
    FakeGateway.calls = 0
    materials,report = run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',judge_mode='external',
        gateway_factory=FakeGateway,runner_identity='fake',scoring_path=scoring)
    assert FakeGateway.calls == 0
    assert len(report['rows'][0]['observations']) == 2
    assert report['metrics']['changes'] == 0
    assert report['metrics']['generated_candidates'] == 0
    manifest = json.loads(materials.read_text())
    assert manifest['cases'] == []
    checkpoint = report['rows'][0]['checkpoints'][0]
    assert checkpoint['checkpoint']['must_reflect'] == ['云城设定']
    assert report['metrics']['untriggered_checkpoints'] == 1


def test_rejected_candidates_export_but_do_not_count_as_changes(tmp_path):
    class Rejected(FakeGateway):
        def chat(self,messages,purpose,**kwargs):
            reply = super().chat(messages,purpose,**kwargs)
            if purpose=='persona_check':
                output = json.loads(reply.content)
                output['sentences'][0]['supported'] = False
                reply.content = dumps(output)
            return reply
    corpus = tmp_path/'small.jsonl'
    case = copy.deepcopy(TIMELINE)
    case['events'] = case['events'][:2]
    corpus.write_text(dumps(case),encoding='utf-8')
    scoring = tmp_path/'scoring.md'
    scoring.write_text('格式测试',encoding='utf-8')
    materials,report = run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',judge_mode='external',
        gateway_factory=Rejected,runner_identity='reject',scoring_path=scoring)
    assert report['metrics']['changes'] == 0
    assert report['metrics']['check_rejected_ratio'] == 1
    assert len(json.loads(materials.read_text())['cases']) == 1


def test_paused_timelines_are_recorded_but_never_reused(tmp_path):
    from iris.models import ModelError
    class Paused(FakeGateway):
        attempts = 0
        def chat(self,*args,**kwargs):
            Paused.attempts += 1
            raise ModelError('paused','model unavailable',paused=True,reason='temporarily_unavailable')
    corpus = tmp_path/'small.jsonl'
    case = copy.deepcopy(TIMELINE)
    case['events'] = case['events'][:2]
    corpus.write_text(dumps(case),encoding='utf-8')
    scoring = tmp_path/'scoring.md'
    scoring.write_text('格式测试',encoding='utf-8')
    for _ in range(2):
        _,report = run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',judge_mode='external',
            gateway_factory=Paused,runner_identity='paused',scoring_path=scoring)
        assert report['resumed_cases'] == 0
        assert report['metrics']['incomplete_timelines'] == 1
    assert Paused.attempts == 2


def test_call_percentiles_use_percent_not_fraction():
    from iris.persona_evaluation import _call_metrics
    calls = [dict(duration_ms=t,purpose='persona_generate',prompt_tokens=1,completion_tokens=2,reasoning_tokens=0) for t in (10,20)]
    metric = _call_metrics(calls)
    assert metric['duration_ms_p50'] == 15
    assert metric['duration_ms_p95'] == 19.5


def test_round_manifest_must_be_an_object(material,tmp_path):
    directory,_,_,_ = material
    first = make_round(directory,tmp_path/'first')
    (first/'manifest.json').write_text('[]',encoding='utf-8')
    with pytest.raises(ValueError,match='manifest'):
        score_persona_judgments(directory,[first],tmp_path,judge_model='test',out=tmp_path/'scores')


# Handwritten contract fixture, independent of the frozen quality cases.
FROZEN_FORMAT = {'format_version':1, 'name':'format-only', 'timezone':'Asia/Shanghai', 'timelines':[
    {'id':'tiny','split':'dev','role_name':'Iris','background':'','persona_goal':'记住有界经历',
     'persona_rules':None,'initialized_at':'2026-10-01T23:00:00+08:00',
     'subjects':[{'id':'friend','name':'同伴','aliases':['伙伴']}],
     'entries':[{'id':'room','name':'测试房间','kind':'game'}],
     'days':[
       {'date':'2026-10-02','ops':[{'op':'add','key':'one','content':'我解释了规则','type':'自我',
         'speaker':'self','stance':'亲历','about':['self','friend'],'belief':90,'importance':70,'pinned':False,
         'event_time':'2026-10-02T20:00:00+08:00',
         'sources':[{'entry_id':'room','sender':'self','kind':'self_output','at':'2026-10-02T20:00:00+08:00','text':'我解释规则。'}]}],
        'checkpoint':{'at':'2026-10-02T23:00:00+08:00','run':'regenerate',
          'expect':{'generate':True,'change':True,'level':'小','publication':'published','must_reflect':['解释规则'],'must_not':[]}}},
       {'date':'2026-10-09','ops':[{'op':'edit','key':'one','actor':'admin','content':'我跟着同伴解释规则',
         'event_time':'2026-10-09T20:00:00+08:00',
         'sources':[{'entry_id':'room','sender':'self','kind':'self_output','at':'2026-10-02T20:00:00+08:00','text':'我解释规则。'},
                    {'entry_id':'room','sender':'scene','kind':'event','at':'2026-10-09T20:00:00+08:00','text':'同伴先示范了规则。'}]}],
        'checkpoint':{'at':'2026-10-09T23:00:00+08:00','run':'update',
          'expect':{'generate':True,'change':False,'level':'小','publication':'published','needs_update':False,
                    'must_reflect':[],'must_not':[]}}}]}]}


def test_frozen_contract_clock_sources_actor_and_evidence(tmp_path):
    corpus = tmp_path/'tiny.json'
    corpus.write_text(dumps(FROZEN_FORMAT),encoding='utf-8')
    materials,report = run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',judge_mode='external',
        gateway_factory=FakeGateway,runner_identity='format-only')
    assert len(json.loads(materials.read_text())['cases']) == 2
    row = report['rows'][0]
    assert row['observations'][0]['candidate']['created_at'] == '2026-10-01T23:00:00+08:00'
    assert row['checkpoints'][1]['result']['due']['elapsed_days'] == 7
    assert all(item['matches'] for item in report['metrics']['checkpoint_expectations'])
    data = json.loads((materials.parent/'cases/0001.json').read_text())['input']
    assert data['settings']['goal'] == '记住有界经历'
    assert data['timezone'] == 'Asia/Shanghai'
    memory = data['memory_state'][0]
    assert memory['created_at'] == '2026-10-02T22:00:00+08:00'
    assert memory['revision'] == 2 and memory['revisions'][0]['actor'] == 'admin'
    assert memory['event_time'] == '2026-10-09T20:00:00+08:00'
    assert len(memory['trace']['excerpts']) == 2  # identical source is not a new occurrence
    assert memory['trace']['excerpts'][1]['speaker_subject_id'] == 'scene'
    assert data['evidence']['memories'][0]['speaker_subject_id'] == 'self'
    assert data['evidence']['memories'][0]['about'] == ['persona-eval:friend','self']


def test_frozen_contract_rejects_future_source_and_unknown_entry(tmp_path):
    from iris.persona_evaluation import load_corpus
    for field,value in [('entry_id','missing'),('at','2026-10-03T20:00:00+08:00')]:
        case = copy.deepcopy(FROZEN_FORMAT)
        case['timelines'][0]['days'][0]['ops'][0]['sources'][0][field] = value
        path = tmp_path/'bad.json'
        path.write_text(dumps(case),encoding='utf-8')
        with pytest.raises(ValueError):
            load_corpus(path)


@pytest.mark.parametrize('operation', ['persona_confirm','persona_reject'])
def test_timeline_pending_controls_keep_actual_current_baseline(tmp_path,operation):
    case = copy.deepcopy(TIMELINE)
    case['events'].append({'op':operation,'at':'2026-10-11T12:00:00+00:00'})
    corpus = tmp_path/'control.jsonl'
    corpus.write_text(dumps(case),encoding='utf-8')
    _,report = run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',judge_mode='external',
        gateway_factory=FakeGateway,runner_identity='format-control')
    control = report['rows'][0]['control_operations'][0]
    assert control['version']['status'] == ('current' if operation=='persona_confirm' else 'rejected')
    assert report['metrics']['changes'] == 2  # confirmation does not count the same candidate twice


def test_double_judgment_recomputes_basis_and_keeps_frozen_violation_order():
    from iris.persona_evaluation import _combine
    first = {'sentence_results':[{'index':1,'supported':True,'violations':['指令'],'reason':'一'}],
             'has_basis':False,'must_reflect':[True],'must_not':[False],'level_reasonable':True,'reason':'一'}
    second = {'sentence_results':[{'index':1,'supported':False,'violations':['虚构'],'reason':'二'}],
              'has_basis':False,'must_reflect':[False],'must_not':[True],'level_reasonable':False,'reason':'二'}
    combined,differences = _combine([first,second])
    assert combined['sentence_results'][0]['violations'] == ['虚构','指令']
    assert combined['has_basis'] is False and combined['level_reasonable'] is False
    assert combined['must_reflect'] == [False] and combined['must_not'] == [True]
    assert differences
