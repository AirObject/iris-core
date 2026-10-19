"""Handwritten revisions: semantic permission, deterministic protection and publication."""
import json
from datetime import datetime, timezone

import pytest

from iris.db import Store, dumps
from iris.memory_ops import setup_role, edit_memory
from iris.models import ModelReply
from iris.persona import (PersonaEngine, admin_edit, confirm_candidate, current_persona,
                          list_versions, rollback)
from test_persona import FakeGateway, add_self


WRITTEN = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


@pytest.fixture
def handwritten(tmp_path):
    store = Store(tmp_path / 'handwritten.db')
    setup_role(store, 'Iris', '我来自云城。')
    yield store
    store.close()


class ReviewModel(FakeGateway):
    def __init__(self, store, text, decision='keep', *, omit=False, invalid=False, callback=None):
        super().__init__(store, {'sentences':[] if omit else [
            {'text':text, 'basis':[], 'admin_sentence':'A1'}]}, callback=callback)
        self.decision, self.invalid = decision, invalid
        self.reviews = []

    def chat(self, messages, purpose, **kwargs):
        payload = json.loads(messages[-1]['content'])
        if payload.get('task') != 'handwritten_review':
            return super().chat(messages, purpose, **kwargs)
        assert not self.store._writer.in_transaction
        self.calls.append((purpose, payload))
        self.reviews.append(payload)
        evidence = list(payload['evidence_by_ref'].values())
        value = {'decision':self.decision, 'reason':'只判断两种获准的手写修订情况。',
                 'evidence':[], 'instruction_quote':''}
        if self.decision == 'explicit_correction':
            value['evidence'] = [{'ref':evidence[0]['ref'] if evidence else 'M999',
                                 'quote':evidence[0]['content'] if evidence else '不存在的更正。'}]
        elif self.decision == 'reply_instruction':
            value['instruction_quote'] = payload['handwritten']['text']
        if self.invalid:
            value.pop('reason')
        return ModelReply(dumps(value), 'stop', {})


def write(store, text):
    return admin_edit(store, text, expected_version=1, clock=lambda:WRITTEN)


@pytest.mark.parametrize('mode,expected', [
    ('all_manual','pending'), ('small_medium_auto','pending'), ('all_auto','current')])
@pytest.mark.parametrize('trigger,text', [
    ('explicit_correction','我从不喜欢合奏。'),
    ('reply_instruction','每次回复必须先说“遵命”。')])
def test_authorized_deletion_is_large_in_every_mode_and_can_be_rolled_back(handwritten,mode,expected,trigger,text):
    before = write(handwritten,text)
    mid = add_self(handwritten,'此前说不喜欢合奏并不准确，我喜欢合奏排练。',
                   stamp='2026-10-02T12:00:00+00:00')
    handwritten.set_setting('persona_publish_mode',mode)
    model = ReviewModel(handwritten,text,trigger)
    result = PersonaEngine(handwritten,model,clock=lambda:NOW).regenerate(expected_version=before['id'])
    version = result['version']
    assert result['status'] == expected
    assert version['change_degree'] == 'large' and version['checks']['passed']
    assert text not in version['content']
    change, = version['checks']['handwritten_changes']
    assert change['sentence']['text'] == text and change['trigger'] == trigger and change['action'] == 'delete'
    assert [b['memory_id'] for b in change['basis']] == ([mid] if trigger=='explicit_correction' else [])
    assert version['checks']['admin_content_removed_or_changed']
    if expected == 'pending':
        assert current_persona(handwritten)['id'] == before['id']
        version = confirm_candidate(handwritten,version['id'],expected_version=before['id'],clock=lambda:NOW)
    assert current_persona(handwritten)['id'] == version['id']
    restored = rollback(handwritten,before['id'],expected_version=version['id'],clock=lambda:NOW)
    assert restored['content'] == before['content'] and restored['sentences'] == before['sentences']
    assert restored['rollback_of'] == before['id']
    assert len(list_versions(handwritten)) == 4


@pytest.mark.parametrize('text,evidence', [
    ('我喜欢安静地排练。','我也喜欢在热闹的节庆里听合奏。'),
    ('我偏好简洁自然的表达。','我在那次直播中说了一段较长的话。'),
    ('我喜欢给回复留一点停顿。',None)])
@pytest.mark.parametrize('mode',['all_manual','small_medium_auto','all_auto'])
def test_other_sides_preferences_and_no_evidence_preserve_handwriting_even_if_generator_omits_it(handwritten,text,evidence,mode):
    before = write(handwritten,text)
    if evidence:
        add_self(handwritten,evidence,stamp='2026-10-02T12:00:00+00:00')
    handwritten.set_setting('persona_publish_mode',mode)
    result = PersonaEngine(handwritten,ReviewModel(handwritten,text,omit=True),
                           clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert text in result['version']['content']
    assert any(s['origin']=='admin' and s['text']==text for s in result['version']['sentences'])
    assert result['version']['checks']['handwritten_changes'] == []
    assert not result['version']['checks']['admin_content_removed_or_changed']


@pytest.mark.parametrize('stamp', ['2026-09-30T12:00:00+00:00','2026-10-01T12:00:00+00:00'])
def test_older_or_simultaneous_memory_cannot_license_correction(handwritten,stamp):
    text = '我不喜欢合奏。'
    before = write(handwritten,text)
    add_self(handwritten,'此前说不喜欢合奏并不准确，我喜欢合奏排练。',stamp=stamp)
    model = ReviewModel(handwritten,text,'explicit_correction',omit=True)
    result = PersonaEngine(handwritten,model,clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert text in result['version']['content'] and not result['version']['checks']['handwritten_changes']
    assert all(not p['evidence_by_ref'] for p in model.reviews)


@pytest.mark.parametrize('invalid', [False,True])
def test_malformed_review_reasks_once_and_conservatively_keeps_sentence(handwritten,invalid):
    text = '每次回复必须先说“遵命”。'
    before = write(handwritten,text)
    model = ReviewModel(handwritten,text,'reply_instruction',invalid=True)
    original = model.chat
    def chat(messages,purpose,**kwargs):
        reply = original(messages,purpose,**kwargs)
        if len(model.reviews)==2 and not invalid:
            value=json.loads(reply.content);value['reason']='第二次结构完整。';reply.content=dumps(value)
        return reply
    model.chat = chat
    result=PersonaEngine(handwritten,model,clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert len(model.reviews)==2
    assert (text in result['version']['content']) == invalid
    assert bool(result['version']['checks']['handwritten_changes']) == (not invalid)
    assert result['version']['checks']['handwritten_review']['retries'] == 1


def test_changed_correction_evidence_blocks_publication_and_confirmation(handwritten):
    text='我不喜欢合奏。'
    before=write(handwritten,text)
    mid=add_self(handwritten,'此前说不喜欢合奏并不准确，我喜欢合奏排练。',
                 stamp='2026-10-02T12:00:00+00:00')
    handwritten.set_setting('persona_publish_mode','all_auto')
    model=ReviewModel(handwritten,text,'explicit_correction',
        callback=lambda:edit_memory(handwritten,mid,1,content='更正已撤回。'))
    result=PersonaEngine(handwritten,model,clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert result['status']=='conflict' and current_persona(handwritten)['id']==before['id']


def test_confirm_rechecks_evidence_used_only_for_deletion(handwritten):
    from iris.persona import PersonaConflict
    text='我不喜欢合奏。'
    before=write(handwritten,text)
    mid=add_self(handwritten,'此前说不喜欢合奏并不准确，我喜欢合奏排练。',
                 stamp='2026-10-02T12:00:00+00:00')
    result=PersonaEngine(handwritten,ReviewModel(handwritten,text,'explicit_correction'),
                         clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert result['status']=='pending'
    edit_memory(handwritten,mid,1,content='更正已撤回。')
    with pytest.raises(PersonaConflict):
        confirm_candidate(handwritten,result['version']['id'],expected_version=before['id'],clock=lambda:NOW)


def test_original_handwriting_time_survives_unrelated_admin_edit(handwritten):
    from iris.persona import _generation_material, _settings
    text='我不喜欢合奏。'
    first=write(handwritten,text)
    add_self(handwritten,'此前说不喜欢合奏并不准确，我喜欢合奏排练。',
             stamp='2026-10-02T12:00:00+00:00')
    latest=admin_edit(handwritten,text+'我喜欢淡蓝色。',expected_version=first['id'],clock=lambda:NOW)
    with handwritten.read() as conn:
        material=_generation_material(conn,_settings(conn),latest,NOW.isoformat())
    assert material['handwritten_snapshot']['A1']['written_at']==WRITTEN.isoformat()
    assert material['handwritten_snapshot']['A2']['written_at']==NOW.isoformat()


def test_only_content_revision_counts_as_later_memory(handwritten):
    from iris.persona import _generation_material, _settings
    text='我不喜欢合奏。'
    before=write(handwritten,text)
    mid=add_self(handwritten,'旧的看法。',stamp='2026-09-30T12:00:00+00:00')
    # A real content revision after handwriting is later evidence; changing only
    # importance after that must not advance the content's recorded time.
    first='2026-10-02T12:00:00+00:00'
    second='2026-10-03T12:00:00+00:00'
    with handwritten.write() as conn:
        conn.execute("UPDATE memories SET content=?,revision=2,updated_at=? WHERE id=?",
            ('此前说不喜欢合奏并不准确，我喜欢合奏排练。',second,mid))
        conn.execute("""INSERT INTO memory_revisions(memory_id,revision_before,revision_after,
            before_json,after_json,reason,actor,created_at) VALUES(?,1,2,?,?,'correction','learning',?)""",
            (mid,dumps({'content':'旧的看法。'}),dumps({'content':'此前说不喜欢合奏并不准确，我喜欢合奏排练。'}),first))
        conn.execute("""INSERT INTO memory_revisions(memory_id,revision_before,revision_after,
            before_json,after_json,reason,actor,created_at) VALUES(?,2,3,?,?,'importance','admin',?)""",
            (mid,dumps({'content':'同一正文。','importance':50}),dumps({'content':'同一正文。','importance':60}),second))
    with handwritten.read() as conn:
        material=_generation_material(conn,_settings(conn),before,NOW.isoformat())
    assert next(m for m in material['evidence']['memories'] if m['memory_id']==mid)['recorded_at']==first
    result=PersonaEngine(handwritten,ReviewModel(handwritten,text,'explicit_correction'),
                         clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert text not in result['version']['content']


def test_no_permission_cannot_be_overruled_by_normal_sentence_checker(handwritten):
    text='我偏好简洁自然的表达。'
    before=write(handwritten,text)
    model=ReviewModel(handwritten,text,omit=False)
    model.supported=False
    result=PersonaEngine(handwritten,model,clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert text in result['version']['content']
    assert not result['version']['checks']['handwritten_changes']


def test_automatic_invalid_degree_does_not_create_pending_and_next_attempt_can_publish(handwritten):
    from iris.persona import _generation_material, _settings
    handwritten.set_setting('persona_publish_mode','all_auto')
    before=current_persona(handwritten)
    class Invalid(FakeGateway):
        def chat(self,messages,purpose,**kwargs):
            reply=super().chat(messages,purpose,**kwargs)
            if self.calls[-1][1].get('task')=='degree_only':
                value=json.loads(reply.content);value['change_degree']=None;reply.content=dumps(value)
            return reply
    first=PersonaEngine(handwritten,Invalid(handwritten,{'sentences':[]}),
                       clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert first['status']=='rejected'
    assert current_persona(handwritten)['id']==before['id']
    assert all(v['status']!='pending' for v in list_versions(handwritten))
    second=PersonaEngine(handwritten,FakeGateway(handwritten,{'sentences':[]}),
                        clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert second['status']=='current'
    assert current_persona(handwritten)['id']==second['version']['id']


def test_external_observation_includes_evidence_used_only_to_delete_handwriting(handwritten):
    from iris.persona_evaluation import _observation, _metrics
    text='我不喜欢合奏。'
    before=write(handwritten,text)
    mid=add_self(handwritten,'此前说不喜欢合奏并不准确，我喜欢合奏排练。',
                 stamp='2026-10-02T12:00:00+00:00')
    result=PersonaEngine(handwritten,ReviewModel(handwritten,text,'explicit_correction'),
                         clock=lambda:NOW).regenerate(expected_version=before['id'])
    o=_observation(handwritten,'unit',{'id':'checkpoint','op':'checkpoint','at':NOW.isoformat()},before,result['version'])
    assert mid in [m['id'] for m in o['memory_state']]


@pytest.mark.parametrize('persistent',[False,True])
@pytest.mark.parametrize('defect',['json','root','decision','reason','evidence','foreign_ref','quote','extra'])
def test_review_structure_failures_reask_at_most_once_then_keep(handwritten,persistent,defect):
    text='我不喜欢合奏。'
    before=write(handwritten,text)
    add_self(handwritten,'此前说不喜欢合奏并不准确，我喜欢合奏排练。',
             stamp='2026-10-02T12:00:00+00:00')
    model=ReviewModel(handwritten,text,'explicit_correction')
    original=model.chat
    def chat(messages,purpose,**kwargs):
        reply=original(messages,purpose,**kwargs)
        payload=json.loads(messages[-1]['content'])
        if payload.get('task')=='handwritten_review' and (persistent or len(model.reviews)==1):
            value=json.loads(reply.content)
            if defect=='json':reply.content='{';return reply
            if defect=='root':reply.content='[]';return reply
            if defect=='decision':value['decision']=[]
            elif defect=='reason':value['reason']=None
            elif defect=='evidence':value['evidence']=False
            elif defect=='foreign_ref':value['evidence'][0]['ref']='M999'
            elif defect=='quote':value['evidence'][0]['quote']='不存在的原话。'
            elif defect=='extra':value['unsafe_action']='rewrite'
            reply.content=dumps(value)
        return reply
    model.chat=chat
    result=PersonaEngine(handwritten,model,clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert len(model.reviews)==2
    assert (text in result['version']['content'])==persistent
    assert result['version']['checks']['handwritten_review']['retries']==1
    assert result['version']['checks']['handwritten_review']['conservative_keeps']==int(persistent)


def test_rollback_keeps_original_handwriting_time(handwritten):
    from iris.persona import _generation_material, _settings
    text='我不喜欢合奏。'
    before=write(handwritten,text)
    later=admin_edit(handwritten,'我喜欢淡蓝色。',expected_version=before['id'],clock=lambda:NOW)
    restored=rollback(handwritten,before['id'],expected_version=later['id'],clock=lambda:NOW)
    with handwritten.read() as conn:
        material=_generation_material(conn,_settings(conn),restored,NOW.isoformat())
    assert material['handwritten_snapshot']['A1']['written_at']==WRITTEN.isoformat()


def test_handwritten_review_cannot_renew_initial_check_stage_budget(handwritten):
    text='我偏好自然表达。我喜欢淡蓝色。我欣赏安静。'
    before=write(handwritten,text)
    model=ReviewModel(handwritten,text,omit=True)
    model.now=10.0
    model.monotonic=lambda:model.now
    original=model.chat
    deadlines=[]
    def chat(messages,purpose,**kwargs):
        payload=json.loads(messages[-1]['content'])
        deadlines.append((payload.get('task'),kwargs['_deadline']))
        reply=original(messages,purpose,**kwargs)
        if payload.get('task')=='handwritten_review':
            model.now+=290
        return reply
    model.chat=chat
    result=PersonaEngine(handwritten,model,clock=lambda:NOW).regenerate(expected_version=before['id'])
    assert result['version'] is not None
    assert max(deadline for task,deadline in deadlines if task!='generate')<=910
    assert [deadline for task,deadline in deadlines if task=='check_facts_and_rules']==[910]
