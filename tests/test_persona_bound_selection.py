"""Bound extraction needs one semantic check, not a second model selector."""
import json

import pytest

from iris.db import dumps
from iris.memory_ops import edit_memory
from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine, PersonaConflict, admin_edit, current_persona
from test_persona import add_self
from test_persona_deterministic_guards import approving_verdict
from test_persona_optimization import prepared


class SelectionModel:
    def __init__(self, store, basis, *, reject=False, failure=None, hook=None):
        self.store, self.basis = store, basis
        self.reject, self.failure, self.hook = reject, failure, hook
        self.calls = []

    def chat(self, messages, purpose, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[-1]['content'])
        self.calls.append((purpose, payload))
        assert purpose != 'persona_sentence_repair', 'selection must not require a model request'
        if purpose == 'persona_generate':
            refs = {m['memory_id']:m['ref'] for m in payload['evidence']['memories']}
            result = {'sentences':[{'text':'我一向会先询问对方。',
                                   'basis':[refs[mid] for mid in self.basis]}]}
        else:
            if purpose == 'persona_recheck':
                if self.failure:
                    raise self.failure
                if self.hook:
                    self.hook()
            result = approving_verdict(payload['candidate'], payload)
            if purpose == 'persona_recheck' and self.reject:
                for row in result['sentences']:
                    row.update(supported=False, reason='The original source does not prove this claim.')
                    for part in row['clauses']:
                        part.update(supported=False, reason='Source does not prove this clause.')
        return ModelReply(dumps(result), 'stop', {})


def test_unique_bound_option_is_selected_without_model_vote(prepared):
    text='在那次直播中，我先询问了对方的看法。'
    mid=add_self(prepared,text)
    model=SelectionModel(prepared,[mid])
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert result['status']=='current' and text in result['version']['content']
    assert [p for p,_ in model.calls]==['persona_generate','persona_check','persona_recheck']
    assert checks['repair_policy']=='first_eligible_bound_memory_v2'
    selection=checks['repair_selections'][0]
    assert selection['index']==2 and selection['selected']==selection['eligible_options'][0]
    assert selection['selected']['text']==text
    assert checks['repair_outputs']==[] and 'repair_output' not in checks
    assert checks['repaired_sentences'][0]['after']['basis']==[{'memory_id':mid,'revision':1}]
    with prepared.read() as conn:
        outputs=json.loads(conn.execute('SELECT outputs_json FROM persona_attempts').fetchone()[0])
    assert outputs['persona_repair_selections']==checks['repair_selections']
    assert 'persona_sentence_repair_batches' not in outputs


def test_multiple_options_follow_snapshot_order_not_generated_reference_order(prepared):
    first='在那次直播中，我先询问了对方的看法。'
    second='在那次共读中，我先询问了对方的理解。'
    one=add_self(prepared,first,importance=95)
    two=add_self(prepared,second,importance=60)
    model=SelectionModel(prepared,[two,one])
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    selection=result['version']['checks']['repair_selections'][0]
    assert [x['text'] for x in selection['eligible_options']]==[first,second]
    assert selection['selected']['text']==first
    request=next(r for p,r in model.calls if p=='persona_recheck')
    assert [s['text'] for s in request['candidate']]==[first]
    assert len(request['evidence_by_ref'])==1
    assert next(iter(request['evidence_by_ref'].values()))['memory_id']==one
    assert result['version']['checks']['repaired_sentences'][0]['after']['basis']==[{'memory_id':one,'revision':1}]


def test_selection_skips_deterministically_ineligible_option(prepared):
    bad=add_self(prepared,'我一向喜欢替对方作决定。',importance=99)
    good_text='在那次共读中，我先询问了对方的理解。'
    good=add_self(prepared,good_text,importance=60)
    model=SelectionModel(prepared,[bad,good])
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert checks['repair_options_rejected']
    assert checks['repair_selections'][0]['selected']['text']==good_text
    assert len(checks['repair_selections'][0]['eligible_options'])==1
    assert good_text in result['version']['content']


def test_negative_recheck_deletes_once_without_trying_next_option(prepared):
    one=add_self(prepared,'在那次直播中，我先询问了对方的看法。',importance=95)
    two=add_self(prepared,'在那次共读中，我先询问了对方的理解。',importance=60)
    model=SelectionModel(prepared,[one,two],reject=True)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert not checks['repaired_sentences'] and checks['deleted_sentences'][0]['stage']=='recheck'
    assert len([r for p,r in model.calls if p=='persona_recheck'])==1
    assert '询问' not in result['version']['content'] and '初始设定：' in result['version']['content']
    assert len(checks['repair_selections'][0]['eligible_options'])==2
    assert checks['degree_override']=='sentences_removed_after_recheck'


@pytest.mark.parametrize('failure', [ModelError('timeout','deadline exceeded'),
                                     ModelError('paused','not admitted',paused=True,reason='call_budget')])
def test_real_recheck_failure_is_not_hidden_by_deterministic_selection(prepared,failure):
    mid=add_self(prepared,'在那次直播中，我先询问了对方的看法。')
    model=SelectionModel(prepared,[mid],failure=failure)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['status']==('skipped' if failure.paused else 'failed')
    assert result['version'] is None and current_persona(prepared)['id']==1
    with prepared.read() as conn:
        outputs=json.loads(conn.execute('SELECT outputs_json FROM persona_attempts').fetchone()[0])
    assert outputs['persona_repair_selections'][0]['selected']['text'].startswith('在那次直播中')


def test_selected_memory_revision_still_checked_at_writeback(prepared):
    mid=add_self(prepared,'在那次直播中，我先询问了对方的看法。')
    model=SelectionModel(prepared,[mid],hook=lambda:edit_memory(prepared,mid,1,content='我更正先前的记录。'))
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['status']=='conflict' and result['version'] is None
    assert current_persona(prepared)['id']==1


def test_current_version_changed_during_recheck_is_never_overwritten(prepared):
    mid=add_self(prepared,'在那次直播中，我先询问了对方的看法。')
    model=SelectionModel(prepared,[mid],hook=lambda:admin_edit(prepared,'管理员新内容。',expected_version=1))
    with pytest.raises(PersonaConflict):
        PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert current_persona(prepared)['content']=='管理员新内容。'


def test_same_bound_replacement_is_checked_once_for_duplicate_problem_sentences(prepared):
    text='在那次直播中，我先询问了对方的看法。'
    mid=add_self(prepared,text)
    class Repeated(SelectionModel):
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_generate':
                value=json.loads(reply.content)
                value['sentences']*=2
                reply.content=dumps(value)
            return reply
    model=Repeated(prepared,[mid])
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert result['version']['content'].count(text)==1
    assert len(checks['repair_selections'])==2
    assert len(checks['repaired_sentences'])==1
    assert checks['deleted_sentences'][0]['index']==3
    assert checks['deleted_sentences'][0]['stage']=='repair_duplicate'
    assert [s['text'] for p,r in model.calls if p=='persona_recheck' for s in r['candidate']]==[text]


def test_identical_text_from_distinct_bound_memories_keeps_separate_checks(prepared):
    text='在那次直播中，我先询问了对方的看法。'
    one=add_self(prepared,text,importance=95)
    two=add_self(prepared,text,importance=60,stamp='2026-10-02T12:00:00+00:00')
    class Separate(SelectionModel):
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_generate':
                value=json.loads(reply.content)
                refs=value['sentences'][0]['basis']
                value['sentences']=[{**value['sentences'][0],'basis':[ref]} for ref in refs]
                reply.content=dumps(value)
            return reply
    model=Separate(prepared,[one,two])
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert len(checks['repaired_sentences'])==2 and not checks['deleted_sentences']
    assert [r['after']['basis'] for r in checks['repaired_sentences']]==[
        [{'memory_id':one,'revision':1}],[{'memory_id':two,'revision':1}]]
    requests=[r for p,r in model.calls if p=='persona_recheck']
    assert sum(len(r['candidate']) for r in requests)==2
    assert result['version']['content'].count(text)==2
