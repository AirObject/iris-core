"""Technical failures stop model work and interruptions preserve completed evidence."""
import copy
import json

import pytest

from iris.db import dumps
from iris.persona_evaluation import run_persona_eval
from test_persona_evaluation import FakeGateway, TIMELINE, tmp_path


def inputs(tmp_path, cases=None):
    corpus=tmp_path/'small.jsonl';corpus.write_text('\n'.join(dumps(c) for c in cases or [TIMELINE])+'\n',encoding='utf-8')
    scoring=tmp_path/'scoring.md';scoring.write_text('格式测试',encoding='utf-8')
    return dict(corpus=corpus,out=tmp_path/'out',judge_mode='external',scoring_path=scoring)


def test_interruption_preserves_completed_checkpoint_raw_outputs_and_does_not_resume_partial(tmp_path):
    class Interrupted(FakeGateway):
        generation=0;interrupt=True
        def chat(self,messages,purpose,**kwargs):
            if purpose=='persona_generate':
                type(self).generation+=1
                if type(self).interrupt and type(self).generation==2:
                    raise KeyboardInterrupt()
            return super().chat(messages,purpose,**kwargs)
    kwargs=inputs(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_persona_eval({},tmp_path,gateway_factory=Interrupted,runner_identity='interruption',**kwargs)
    journals=list((tmp_path/'out').glob('.persona/*/timeline-progress-*.json'))
    assert len(journals)==1
    audit=json.loads(journals[0].read_text())
    assert audit['state']=='interrupted' and audit['interruption']=='KeyboardInterrupt'
    assert audit['inflight_usage_unknown'] and audit['row']['complete'] is False
    assert audit['row']['completed_events']==3 and audit['row']['remaining_events']==1
    assert audit['row']['checkpoints'][0]['result']['status']=='current'
    first=audit['row']['attempts'][0]
    assert json.loads(first['outputs_json'])['persona_generate']
    assert json.loads(first['outputs_json'])['persona_check']
    assert not list((tmp_path/'out').glob('.persona/*/timeline-*/iris.db'))
    Interrupted.interrupt=False
    _,report=run_persona_eval({},tmp_path,gateway_factory=Interrupted,runner_identity='interruption',**kwargs)
    assert report['resumed_cases']==0 and report['metrics']['run_valid']
    assert json.loads(journals[0].read_text())==audit


@pytest.mark.parametrize('format_failure', ['coverage', 'degree', 'output_limit'])
def test_schema_recovery_and_incomplete_checkpoints_execute_remaining_events_and_timelines(tmp_path, format_failure):
    class Broken(FakeGateway):
        generation=0
        def chat(self,messages,purpose,**kwargs):
            if purpose=='persona_generate':type(self).generation+=1
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_check' and json.loads(messages[-1]['content']).get('task')!='handwritten_review':
                value=json.loads(reply.content)
                if format_failure == 'coverage': value['sentences'] = []
                elif format_failure == 'degree': value['change_degree'] = 'unknown'
                else: reply.finish_reason = 'length'
                reply.content=dumps(value)
            return reply
    second=copy.deepcopy(TIMELINE);second['id']='another-format-case'
    _,report=run_persona_eval({},tmp_path,gateway_factory=Broken,runner_identity='missing-check',
        **inputs(tmp_path,[TIMELINE,second]))
    summary=next((tmp_path/'out').glob('persona-*.md')).read_text()
    if format_failure == 'output_limit':
        assert '| run_valid | False |' in summary and '| unexecuted_timelines | 0 |' in summary
        assert Broken.generation == 4 and not report['metrics']['run_valid']
        assert report['executed_timelines'] == 2 and report['unexecuted_timelines'] == 0
        assert all(row['remaining_events'] == 0 and row['complete'] for row in report['rows'])
        assert report['metrics']['incomplete_check_count'] == 4
        assert 'incomplete_check_limit_exceeded' in report['metrics']['run_invalid_reasons']
    else:
        assert '| run_valid | True |' in summary and '| unexecuted_timelines | 0 |' in summary
        assert Broken.generation == 4 and report['metrics']['run_valid']
        assert report['executed_timelines'] == 2 and report['unexecuted_timelines'] == 0
        assert all(row['remaining_events'] == 0 for row in report['rows'])
        initial = report['rows'][0]['checkpoints'][0]['result']['version']['checks']['initial_model']
        assert initial['invalid_batch_checks']
        assert all(not row['supported'] for row in initial['sentences'])
    assert report['planned_timelines'] == 2
    for path in (tmp_path/'out').glob('.persona/*/timeline-progress-*.json'):
        journal = json.loads(path.read_text())
        assert journal['state'] == 'complete'
        assert journal['row'] in report['rows']
        assert any(json.loads(a['outputs_json']).get('persona_check') for a in journal['row']['attempts'])


def test_complete_semantic_rejections_do_not_stop_the_timeline(tmp_path):
    class Rejected(FakeGateway):
        generation=0
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_generate':
                type(self).generation+=1
                value=json.loads(reply.content);value['sentences'][0]['basis']=['M404'];reply.content=dumps(value)
            return reply
    _,report=run_persona_eval({},tmp_path,gateway_factory=Rejected,runner_identity='negative-quality',**inputs(tmp_path))
    assert Rejected.generation==2 and report['metrics']['run_valid']
    assert report['rows'][0]['remaining_events']==0
    assert report['planned_timelines']==report['executed_timelines']==1 and report['unexecuted_timelines']==0


def test_failed_atomic_progress_replace_preserves_previous_complete_file(tmp_path,monkeypatch):
    import errno
    from iris import persona_evaluation as evaluation
    target=tmp_path/'progress.json';evaluation._write_progress(target,{'completed':1})
    before=target.read_bytes()
    def full_disk(*args):raise OSError(errno.ENOSPC,'disk full')
    monkeypatch.setattr(evaluation.os,'replace',full_disk)
    with pytest.raises(OSError) as failure:
        evaluation._write_progress(target,{'completed':2})
    assert failure.value.errno==errno.ENOSPC and target.read_bytes()==before
    assert not list(tmp_path.glob('*.tmp'))
