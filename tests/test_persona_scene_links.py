"""A sentence checked in isolation cannot borrow a scene from its neighbour."""
import pytest

from iris.persona import PersonaEngine, _surface_issues, current_persona, select_evidence
from test_persona import FakeGateway, add_self, store


def sentence(text, *, dates=1, origin='memory', setting=False):
    return {'text':text, 'origin':origin, 'basis':[], 'date_count':dates, 'initial_setting':setting}


@pytest.mark.parametrize('text', [
    '也是在那次配音里，我核对了稿页。',
    '同样是在那场联机中，我标出了路线。',
    '又在那次共读里，我问过页码。',
    '在同一场线上共读里，我标出了页码。',
])
@pytest.mark.parametrize('dates', [1, 2])
def test_automatic_scene_links_need_a_self_contained_replacement(text, dates):
    assert any('cross-sentence scene reference' in p for p in _surface_issues(sentence(text, dates=dates)))


@pytest.mark.parametrize('text', [
    '在那次配音里，我核对了稿页。',
    '在2026年9月3日的配音里，我核对了稿页。',
    '在那次配音里，我也核对了稿页。',
])
def test_independent_event_scope_and_local_also_are_not_scene_links(text):
    assert not _surface_issues(sentence(text))


@pytest.mark.parametrize('origin,setting', [('admin',False), ('memory',True)])
def test_handwritten_and_fictional_setting_voice_keep_their_own_wording(origin, setting):
    assert not _surface_issues(sentence('也是在那次配音里，我核对了稿页。',origin=origin,setting=setting))


def test_positive_model_verdict_cannot_retain_cross_sentence_scene_link(store):
    mid=add_self(store,'在那次配音里，我核对了稿页。')
    ref=next(m['ref'] for m in select_evidence(store)['memories'] if m['memory_id']==mid)
    text='也是在那次配音里，我核对了稿页。'
    model=FakeGateway(store,{'sentences':[{'text':text,'basis':[ref]}]})
    result=PersonaEngine(store,model).regenerate(expected_version=current_persona(store)['id'])
    assert result['version']['checks']['passed']
    assert text not in result['version']['content']
    deleted=result['version']['checks']['deleted_sentences']
    assert any('cross-sentence scene reference' in ' '.join(d['reasons']) for d in deleted)
    assert any(s['text']==text for _,request in model.calls if request.get('candidate') for s in request['candidate'])
