"""Known deterministic failures must not consume semantic-check requests."""
import json

import pytest

from iris.db import dumps
from iris.memory_ops import setup_role, edit_memory
from iris.models import ModelReply
from iris.persona import PersonaEngine, current_persona
from iris.persona_evaluation import _incomplete_checks
from test_persona import add_self
from test_persona_deterministic_guards import approving_verdict
from test_persona_optimization import prepared


class PrecheckModel:
    def __init__(self, store, rows, *, repair=None, reject_text=None, limit=False):
        self.store, self.rows = store, rows
        self.repair, self.reject_text, self.limit = repair, reject_text, limit
        self.calls = []

    def chat(self, messages, purpose, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[-1]['content'])
        self.calls.append((purpose, payload))
        if purpose == 'persona_generate':
            refs = {m['memory_id']:m['ref'] for m in payload['evidence']['memories']}
            value = {'sentences':[{'text':text,'basis':[refs[mid]]} for text,mid in self.rows]}
        elif purpose == 'persona_sentence_repair':
            value = {'repairs':[{'index':row['index'],'text':self.repair} for row in payload['problems']]}
        else:
            # These facts are already deterministically ineligible. Sending them
            # to a permissive judge would waste an expensive, possibly long call.
            assert all('她' not in row['text'] and '一向' not in row['text']
                       for row in payload['candidate'])
            if purpose == 'persona_check':
                assert '她' not in payload.get('candidate_text', '')
                assert '一向' not in payload.get('candidate_text', '')
            if self.limit and payload['candidate']:
                return ModelReply('', 'length', {})
            value = approving_verdict(payload['candidate'], payload)
            for row,candidate in zip(value['sentences'],payload['candidate']):
                if candidate['text'] == self.reject_text:
                    row.update(supported=False,reason='semantic rejection')
                    for part in row['clauses']:
                        part.update(supported=False,reason='semantic rejection')
        return ModelReply(dumps(value), 'stop', {})


@pytest.mark.parametrize('bad_position', [0,1,2])
def test_precheck_filters_requests_and_preserves_original_indices(prepared,bad_position):
    good1='在那次直播中，我解释了规则。'
    good2='在那次直播中，我选择了浅灰底。'
    bad='在那次直播中，我把她的答复记下。'
    one,two,three=[add_self(prepared,text) for text in (good1,good2,bad)]
    rows=[(good1,one),(good2,two)];rows.insert(bad_position,(bad,three))
    model=PrecheckModel(prepared,rows)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    version=result['version'];checks=version['checks']
    # The immutable template is first; row numbers refer to the full proposal.
    bad_index=bad_position+2
    assert checks['precheck_rejections'][0]['index']==bad_index
    assert 'neutral person reference' in ' '.join(checks['precheck_rejections'][0]['reasons'])
    assert checks['initial_model_sentence_indices']==[i for i in range(1,5) if i!=bad_index]
    assert len(checks['initial_model']['sentences'])==3
    assert all(row['supported'] for row in checks['initial_model']['sentences'])
    assert checks['deleted_sentences'][0]['index']==bad_index
    assert good1 in version['content'] and good2 in version['content'] and bad not in version['content']
    assert [row['index'] for row in checks['model']['sentences']]==[1,2,3]
    assert checks['passed'] and result['status']=='current'


def test_all_precheck_failures_make_no_check_or_degree_call(store):
    setup_role(store,'Iris','')
    text='我一向喜欢浅灰底。';mid=add_self(store,text)
    model=PrecheckModel(store,[(text,mid)])
    result=PersonaEngine(store,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert [purpose for purpose,_ in model.calls]==['persona_generate']
    assert checks['initial_model'] is None and checks['initial_model_sentence_indices']==[]
    assert checks['precheck_rejections'][0]['index']==1
    assert len(checks['deleted_sentences'])==1
    assert result['status']=='rejected' and current_persona(store)['id']==1
    assert not _incomplete_checks([{'timeline_id':'test','checkpoint_id':'test','candidate':result['version']}])


def test_precheck_failure_can_use_eligible_whole_memory_repair_and_recheck(prepared):
    original='在那次直播中，我把对方的答复记下。'
    bad='在那次直播中，我把她的答复记下。'
    mid=add_self(prepared,original)
    model=PrecheckModel(prepared,[(bad,mid)],repair=original)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert original in result['version']['content'] and bad not in result['version']['content']
    assert checks['precheck_rejections'][0]['index']==2
    assert checks['repaired_sentences'][0]['index']==2
    rechecks=[p for purpose,p in model.calls if purpose=='persona_recheck']
    assert len(rechecks)==1 and [s['text'] for s in rechecks[0]['candidate']]==[original]
    assert result['status']=='current'


def test_semantic_rejection_after_filtering_maps_to_the_original_sentence(prepared):
    bad='在那次直播中，我把她的答复记下。'
    unknown='在那次直播中，我解释了规则。'
    first,second=[add_self(prepared,text) for text in (bad,unknown)]
    model=PrecheckModel(prepared,[(bad,first),(unknown,second)],reject_text=unknown)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert checks['initial_model_sentence_indices']==[1,3]
    assert {s['index'] for s in checks['deleted_sentences']}=={2,3}
    assert bad not in result['version']['content'] and unknown not in result['version']['content']
    assert '初始设定：' in result['version']['content']


def test_precheck_cannot_hide_a_length_failure_on_a_remaining_sentence(prepared):
    bad='在那次直播中，我把她的答复记下。'
    good='在那次直播中，我解释了规则。'
    first,second=[add_self(prepared,text) for text in (bad,good)]
    model=PrecheckModel(prepared,[(bad,first),(good,second)],limit=True)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    checks=result['version']['checks']
    assert result['status']=='rejected' and current_persona(prepared)['id']==1
    assert 'output_limit' in ' '.join(checks['model_errors'])
    assert _incomplete_checks([{'timeline_id':'test','checkpoint_id':'test','candidate':result['version']}])
    assert not any(purpose in ('persona_sentence_repair','persona_recheck') for purpose,_ in model.calls)
    assert checks['initial_model_sentence_indices']==[1,3]


def test_prechecked_repair_still_checks_revision_at_writeback(prepared):
    original='在那次直播中，我把对方的答复记下。'
    bad='在那次直播中，我把她的答复记下。'
    mid=add_self(prepared,original)
    class Concurrent(PrecheckModel):
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if purpose=='persona_recheck':
                edit_memory(self.store,mid,1,content='我改正了先前的记录。')
            return reply
    model=Concurrent(prepared,[(bad,mid)],repair=original)
    result=PersonaEngine(prepared,model).regenerate(expected_version=1)
    assert result['status']=='conflict' and result['version'] is None
    assert current_persona(prepared)['id']==1
