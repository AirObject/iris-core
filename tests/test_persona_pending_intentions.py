"""An explicitly unimplemented intention stays out even when its speech is true."""
import json

import pytest

from iris.persona import PersonaEngine, _surface_issues, current_persona, select_evidence
from test_persona import FakeGateway, add_self, store


def sentence(text, *, dates=2, origin='memory', setting=False):
    return {'text':text, 'origin':origin, 'basis':[], 'date_count':dates, 'initial_setting':setting}


@pytest.mark.parametrize('text', [
    '在那次讨论游园路线时，我宣布绘制每张路线图前先勾轮廓，这只是一个新计划。',
    '在那次合奏里，我承诺逐段录下练习，这还只是新的打算。',
    '在那次共读里，我提出逐页校对页码，这仅是一项意向。',
    '在那次讨论里，我提出分类所有书签，这只是个设想。',
    '在那次练习里，我说要逐幅先勾轮廓，还只是新的打算。',
    '在那次排练里，我承诺记录每段节拍，这仅仅是一项承诺而已。',
])
@pytest.mark.parametrize('dates', [1, 2])
def test_explicit_pending_intention_is_not_rescued_by_dates(text, dates):
    assert any('explicit pending intention' in x for x in _surface_issues(sentence(text, dates=dates)))


@pytest.mark.parametrize('text', [
    '在那次合奏里，我按练习计划记录了错拍。',
    '在那次游园里，我完成了原定的路线。',
    '在那次讨论里，我体会到计划不等于结果。',
    '在那次讨论里，我说如果只是计划就还不算成果。',
    '在那次演练里，这不只是一个计划，试演已经录好。',
    '在那次演练里，排练不仅仅是新的计划，试演已经录好。',
    '在那次共读里，我提到名为只是一个计划的短篇。',
    '我重视将计划付诸实践。',
    '我长期希望创作能让人安心的故事。',
    '在那次排练里，当时只是一个计划，后来试演已完成。',
])
def test_plan_words_negation_values_and_resolved_history_are_not_literal_pending_fragments(text):
    # These still need semantic checking. The wording guard is not an intent parser.
    assert not _surface_issues(sentence(text))


@pytest.mark.parametrize('origin,setting', [('admin',False), ('memory',True)])
def test_handwritten_and_setting_wording_still_use_their_semantic_rule_checks(origin, setting):
    assert not _surface_issues(sentence('这只是新的打算。', origin=origin, setting=setting))


def test_truthful_dated_announcement_cannot_be_published_as_an_episode(store):
    pending='在那次讨论游园路线时，我宣布绘制每张路线图前先勾轮廓，这只是一个新计划。'
    fact='在那次游园里，我画了码头旁的桥。'
    pending_id=add_self(store, pending)
    fact_id=add_self(store, fact)
    refs={m['memory_id']:m['ref'] for m in select_evidence(store)['memories']}
    gateway=FakeGateway(store, {'sentences':[
        {'text':pending, 'basis':[refs[pending_id]]},
        {'text':fact, 'basis':[refs[fact_id]]},
    ]})
    result=PersonaEngine(store, gateway).regenerate(expected_version=current_persona(store)['id'])
    version=result['version'];checks=version['checks']
    assert checks['passed'] and pending not in version['content'] and fact in version['content']
    assert any('explicit pending intention' in ' '.join(x['reasons']) for x in checks['deleted_sentences'])
    assert any('explicit pending intention' in ' '.join(x['reasons']) for x in checks['repair_options_rejected'])
    assert not any(purpose=='persona_sentence_repair' for purpose,_ in gateway.calls)
    with store.read() as conn:
        outputs=json.loads(conn.execute('SELECT outputs_json FROM persona_attempts ORDER BY id DESC LIMIT 1').fetchone()[0])
    assert pending in outputs['persona_generate']  # Raw candidate retained, never rewritten in place.
