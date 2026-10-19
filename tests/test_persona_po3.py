"""PO3: durable wording and operationally invalid evaluation runs."""
import copy
import json

import pytest

from iris.db import dumps
from iris.persona import PersonaEngine, _check_verdicts
from iris.persona_evaluation import run_persona_eval
from test_persona_deterministic_guards import approving_verdict
from test_persona_evaluation import FakeGateway, TIMELINE, tmp_path
from test_persona_optimization import Model, prepared


@pytest.mark.parametrize('text', [
    '我今天在拼图直播中选择了灰底。', '在昨晚的共读里，我先确认了页码。',
    '最近我更倾向先听同伴的解释。', '今年我在共读中更愿意等待描述。',
    '我目前更喜欢合作解谜。',
])
@pytest.mark.parametrize('date_count', [1, 3])
def test_relative_time_is_not_durable_even_with_multiple_source_dates(text, date_count):
    sentence = {'text':text, 'origin':'memory', 'initial_setting':False, 'date_count':date_count}
    _, errors, problems = _check_verdicts(approving_verdict([sentence]), [sentence])
    assert not errors and 'relative time' in ' '.join(problems[1])


@pytest.mark.parametrize('text', [
    '在2027年10月3日的共读中，我先确认了页码。',
    '在那次拼图直播中，我选择了灰底。',
    '在一起读图时，我倾向先听同伴的解释。',
])
def test_explicit_event_and_non_calendar_scene_remain_available(text):
    sentence = {'text':text, 'origin':'memory', 'initial_setting':False, 'date_count':3}
    assert not _check_verdicts(approving_verdict([sentence]), [sentence])[2]


@pytest.mark.parametrize('exemption', [
    {'origin':'admin'}, {'origin':'initial_template'}, {'initial_setting':True},
])
def test_relative_time_guard_does_not_rewrite_manual_or_setting_text(exemption):
    sentence = {'text':'设定中，我今天在云城。', 'origin':'memory',
                'initial_setting':False, 'date_count':1, **exemption}
    assert not _check_verdicts(approving_verdict([sentence]), [sentence])[2]


def test_relative_time_override_survives_an_approving_model(prepared):
    class Permissive(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            payload = json.loads(messages[-1]['content'])
            if purpose == 'persona_generate':
                reply.content = dumps({'sentences':[{'text':'我今天在直播中解释了规则。',
                    'basis':[payload['evidence']['memories'][0]['ref']]}]})
            elif purpose in ('persona_check','persona_recheck'):
                reply.content = dumps(approving_verdict(payload['candidate'], payload))
            return reply
    result = PersonaEngine(prepared, Permissive(prepared,repair=None)).regenerate(expected_version=1)
    version = result['version']
    assert version['checks']['passed']
    assert len(version['checks']['repaired_sentences']) == 1
    assert 'relative time' in ' '.join(version['checks']['precheck_rejections'][0]['reasons'])
    assert '今天' not in version['content']


@pytest.mark.parametrize('status,timed_out,reason', [
    (429,0,'rate_limited'), (None,1,'timeout'), (503,0,'transport_failure'),
])
def test_missing_success_record_cannot_be_reused_as_recovered_eval(tmp_path,status,timed_out,reason):
    class Recovered(FakeGateway):
        def __init__(self, configs, store, **kwargs):
            super().__init__(configs,store,**kwargs)
            self.recorded = False
            self.clock = kwargs['clock']

        def chat(self,messages,purpose,**kwargs):
            if not self.recorded:
                with self.store.write() as conn:
                    conn.execute('''INSERT INTO model_calls
                        (purpose,model,duration_ms,result_category,status_code,timed_out,created_at)
                        VALUES(?, 'fake', 1, 'retryable', ?, ?, ?)''',
                        (purpose,status,timed_out,self.clock().isoformat()))
                self.recorded = True
            # A returned fake reply without a recorded successful HTTP call is not proof of recovery.
            return super().chat(messages,purpose,**kwargs)

    case = copy.deepcopy(TIMELINE)
    case['events'] = case['events'][:2]
    corpus = tmp_path/'format-only.json'
    corpus.write_text(dumps(case),encoding='utf-8')
    for _ in range(2):
        _, report = run_persona_eval({},tmp_path,corpus=corpus,out=tmp_path/'out',
            judge_mode='external',gateway_factory=Recovered,runner_identity='recovered-operational-error')
        assert report['rows'][0]['checkpoints'][0]['result']['status'] == 'current'
        assert report['resumed_cases'] == 0
        assert report['rows'][0]['complete'] is False
        assert report['metrics']['run_valid'] is False
        assert report['rows'][0]['invalid_calls'][0]['reason'] == reason
        assert report['metrics']['calls']['rate_limited_calls'] == int(status == 429)
    assert not list((tmp_path/'out/.persona').glob('*/0000.json'))


@pytest.mark.parametrize('text', [
    '我的耐心体现在那次共读时的等待。',
    '在接近日落的那次共读中，我先确认了页码。',
])
def test_relative_time_guard_respects_word_boundaries(text):
    sentence = {'text':text,'origin':'memory','initial_setting':False,'date_count':3}
    assert not _check_verdicts(approving_verdict([sentence]),[sentence])[2]


@pytest.mark.parametrize('text', [
    '在今早的共读中，我确认了页码。', '在这周的共读中，我确认了页码。',
    '在本星期的共读中，我确认了页码。', '在这个月的共读中，我确认了页码。',
])
def test_split_relative_time_phrases_still_require_durable_wording(text):
    sentence = {'text':text,'origin':'memory','initial_setting':False,'date_count':3}
    assert 'relative time' in ' '.join(_check_verdicts(approving_verdict([sentence]),[sentence])[2][1])


def test_stage_timeout_without_http_record_is_invalid_and_not_reused(tmp_path):
    from iris.models import ModelError

    class Expired(FakeGateway):
        def chat(self, *args, **kwargs):
            raise ModelError('timeout', 'total timeout', reason='timeout')

    case = copy.deepcopy(TIMELINE)
    case['events'] = case['events'][:2]
    corpus = tmp_path/'expired.json'
    corpus.write_text(dumps(case), encoding='utf-8')
    for _ in range(2):
        _, report = run_persona_eval({}, tmp_path, corpus=corpus, out=tmp_path/'out',
            judge_mode='external', gateway_factory=Expired, runner_identity='expired-stage')
        assert not report['metrics']['run_valid']
        assert report['metrics']['timed_out_attempts'] == 1
        assert report['metrics']['calls']['timed_out_calls'] == 0
        assert report['resumed_cases'] == 0
        assert report['check_batch_size'] == 4
        assert report['max_check_stage_seconds'] == 900
