"""Duplicate handwriting must retain its own introduction across deletion and rollback."""
import json
from datetime import datetime, timezone

import pytest

from iris.db import Store
from iris.memory_ops import setup_role
from iris.persona import (PersonaEngine, _handwritten_snapshot, _insert, admin_edit,
                          confirm_candidate, current_persona, rollback)
from test_persona import add_self
from test_persona_handwritten_revision import ReviewModel

TEXT = '我从不喜欢合奏。'
KEEP = '我偏好淡蓝色。'
EARLY = '2026-10-01T12:00:00+00:00'
LATE = '2026-10-03T12:00:00+00:00'


def clock(stamp):
    return lambda: datetime.fromisoformat(stamp)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path/'provenance.db')
    setup_role(result,'Iris','我来自云城。')
    yield result
    result.close()


class CorrectionModel(ReviewModel):
    """The fixture grants correction only when this exact item has later evidence."""
    def __init__(self,store):
        super().__init__(store,TEXT,omit=True)

    def chat(self,messages,purpose,**kwargs):
        payload=json.loads(messages[-1]['content'])
        if payload.get('task')=='handwritten_review':
            self.decision=('explicit_correction' if payload['handwritten']['text']==TEXT
                           and payload['evidence_by_ref'] else 'keep')
        return super().chat(messages,purpose,**kwargs)


def duplicate_history(store):
    first=admin_edit(store,TEXT+KEEP,expected_version=1,clock=clock(EARLY))
    add_self(store,'此前说从不喜欢合奏并不准确，我喜欢合奏排练。',
             stamp='2026-10-02T12:00:00+00:00')
    return admin_edit(store,TEXT+KEEP+TEXT,expected_version=first['id'],clock=clock(LATE))


def publish(store,result,previous):
    assert result['version']['checks']['passed']
    if result['status']=='pending':
        return confirm_candidate(store,result['version']['id'],expected_version=previous['id'],
                                 clock=clock('2026-10-04T12:00:00+00:00'))
    assert result['status']=='current'
    return result['version']


def restore(store,version):
    other=admin_edit(store,KEEP,expected_version=version['id'],
                     clock=clock('2026-10-05T12:00:00+00:00'))
    return rollback(store,version['id'],expected_version=other['id'],
                    clock=clock('2026-10-06T12:00:00+00:00'))


@pytest.mark.parametrize('mode',['all_manual','small_medium_auto','all_auto'])
@pytest.mark.parametrize('via_rollback',[False,True])
def test_only_earlier_duplicate_is_deleted_and_later_item_stays_protected(store,mode,via_rollback):
    before=duplicate_history(store)
    store.set_setting('persona_publish_mode',mode)
    first=PersonaEngine(store,CorrectionModel(store),clock=clock('2026-10-04T12:00:00+00:00')).regenerate(
        expected_version=before['id'])
    assert [c['ref'] for c in first['version']['checks']['handwritten_changes']]==['A1']
    assert first['version']['content'].count(TEXT)==1
    published=publish(store,first,before)
    if via_rollback:
        published=restore(store,published)
    model=CorrectionModel(store)
    second=PersonaEngine(store,model,clock=clock('2026-10-07T12:00:00+00:00')).regenerate(
        expected_version=published['id'])
    assert second['version']['content'].count(TEXT)==1
    assert second['version']['checks']['handwritten_changes']==[]
    review,=[p for p in model.reviews if p['handwritten']['text']==TEXT]
    assert review['handwritten']['written_at']==LATE
    assert review['evidence_by_ref']=={}
    if mode=='all_auto':
        assert current_persona(store)['content'].count(TEXT)==1


@pytest.mark.parametrize('kind',[
    'legacy_auto','missing_removal','bad_reference','bad_sentence',
    'repeated_reference','count_mismatch','ambiguous_edit'])
@pytest.mark.parametrize('via_rollback',[False,True])
def test_ambiguous_duplicate_lineage_has_unknown_time_and_cannot_authorize_correction(store,kind,via_rollback):
    before=duplicate_history(store)
    store.set_setting('persona_publish_mode','all_auto')
    if kind=='ambiguous_edit':
        ambiguous=admin_edit(store,TEXT+KEEP,expected_version=before['id'],
                             clock=clock('2026-10-04T12:00:00+00:00'))
    else:
        change={'ref':'A1','sentence':before['sentences'][0],'action':'delete','basis':[]}
        material={} if kind=='legacy_auto' else {'handwritten_changes':[]}
        if kind=='bad_reference':
            material['handwritten_changes']=[{**change,'ref':'A99'}]
        elif kind=='bad_sentence':
            material['handwritten_changes']=[{**change,'sentence':before['sentences'][1]}]
        elif kind=='repeated_reference':
            material['handwritten_changes']=[change,change]
        elif kind=='count_mismatch':
            material['handwritten_changes']=[change]
        sentences=before['sentences'] if kind=='count_mismatch' else before['sentences'][:2]
        with store.write() as conn:
            ambiguous=_insert(conn,content=''.join(s['text'] for s in sentences),sentences=sentences,
                checks={'passed':True},degree='large',status='current',source='regenerate',
                stamp='2026-10-04T12:00:00+00:00',base=before['id'],material=material)
    if via_rollback:
        ambiguous=restore(store,ambiguous)
    with store.read() as conn:
        entries=[s for s in _handwritten_snapshot(conn,ambiguous).values() if s['text']==TEXT]
    assert entries and all(s['written_at'] is None for s in entries)
    model=CorrectionModel(store)
    result=PersonaEngine(store,model,clock=clock('2026-10-07T12:00:00+00:00')).regenerate(
        expected_version=ambiguous['id'])
    assert result['version']['content'].count(TEXT)==ambiguous['content'].count(TEXT)
    assert result['version']['checks']['handwritten_changes']==[]
    assert all(p['evidence_by_ref']=={} for p in model.reviews if p['handwritten']['text']==TEXT)
    with store.read() as conn:
        entries=[s for s in _handwritten_snapshot(conn,result['version']).values() if s['text']==TEXT]
    assert entries and all(s['written_at'] is None for s in entries)


def test_unique_legacy_carry_keeps_known_introduction_and_permitted_deletion(store):
    before=admin_edit(store,TEXT+KEEP,expected_version=1,clock=clock(EARLY))
    add_self(store,'此前说从不喜欢合奏并不准确，我喜欢合奏排练。',
             stamp='2026-10-02T12:00:00+00:00')
    store.set_setting('persona_publish_mode','all_auto')
    with store.write() as conn:
        carried=_insert(conn,content=before['content'],sentences=before['sentences'],checks={'passed':True},
            degree='small',status='current',source='regenerate',stamp=LATE,base=before['id'])
    model=CorrectionModel(store)
    result=PersonaEngine(store,model,clock=clock('2026-10-04T12:00:00+00:00')).regenerate(
        expected_version=carried['id'])
    assert result['status']=='current'
    assert TEXT not in result['version']['content']
    assert result['version']['checks']['handwritten_changes'][0]['written_at']==EARLY
