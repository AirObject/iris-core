"""Persona publication boundaries, tested without a network model."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from iris.db import Store, dumps
from iris.memory_ops import setup_role, update_role, edit_memory, delete_memory, manage_memory
from iris.models import ModelReply
from iris.persona import (
    PersonaEngine, PersonaConflict, admin_edit, confirm_candidate, reject_candidate,
    current_persona, list_versions, persona_due, pending_update, rollback,
    select_evidence, version_diff,
)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'persona.db')
    setup_role(result, 'Iris', '我来自云城。')
    yield result
    result.close()


def add_self(store, content='我在直播里耐心解释了规则。', *, stamp='2026-10-01T12:00:00+00:00',
             speaker='self', about=True, stance='亲历', importance=60, pinned=0, kind='live'):
    with store.write() as conn:
        conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind) VALUES(?,?,?,?)", (kind, kind, 'eval', kind))
        conn.execute("INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (speaker, speaker, stamp))
        msg = conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
            VALUES(?,'self_output',?,?,?,?,?)""", (kind, speaker, content, stamp, stamp, content + stamp)).lastrowid
        mid = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
            pinned,entry_id,created_at,updated_at,first_confirmed_at,last_confirmed_at)
            VALUES(?,'自我',?,?,80,?,60,?,?,?,?,?,?)""",
            (content, speaker, stance, importance, pinned, kind, stamp, stamp, stamp, stamp)).lastrowid
        if about:
            conn.execute("INSERT INTO memory_subjects VALUES(?,'self')", (mid,))
        conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)", (mid, msg, stamp))
    return mid


class FakeGateway:
    def __init__(self, store, generated, *, degree='small', supported=True, scene=True, callback=None):
        self.store, self.generated = store, generated
        self.degree, self.supported, self.scene = degree, supported, scene
        self.calls, self.callback = [], callback

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        self.calls.append((purpose, json.loads(messages[-1]['content'])))
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        if purpose == 'persona_generate':
            value = self.generated
        else:
            value = {'sentences': [{'index': i + 1, 'supported': self.supported,
                       'fabricated': False, 'scene_qualified': self.scene,
                       'violations': [], 'reason': '依据支持，保留场景。'}
                       for i, _ in enumerate(self.generated['sentences'])],
                     'change_degree': self.degree, 'reason': '措辞调整'}
        return ModelReply(dumps(value), 'stop', {})


def generate(store, clock, *, degree='small', supported=True, scene=True, text='初始设定中，我来自云城。', basis=None, **kwargs):
    output = {'sentences': [{'text': text, 'basis': basis or ['M1']}]}
    gateway = FakeGateway(store, output, degree=degree, supported=supported, scene=scene, **kwargs)
    old = current_persona(store)['id']
    return PersonaEngine(store, gateway, clock=clock).regenerate(expected_version=old), gateway


@pytest.mark.parametrize('name,background,expected', [
    ('Iris', '', '我是Iris。尚无预设经历，会在相处中逐渐认识自己。'),
    (' 星野 ', '我喜欢安静。\n我住在云城！', '我是星野。初始设定：我喜欢安静；我住在云城。'),
])
def test_s01_exact_initial_template(tmp_path, name, background, expected):
    store = Store(tmp_path / 'initial.db')
    try:
        assert setup_role(store, name, background) == expected
        version = current_persona(store)
        assert version['content'] == expected
        assert ''.join(s['text'] for s in version['sentences']) == expected
        assert version['source'] == 'initial_setting'
        with store.read() as conn:
            ids = [r[0] for r in conn.execute('SELECT id FROM memories')]
            assert not conn.execute('SELECT * FROM model_calls').fetchall()
        assert {b['memory_id'] for s in version['sentences'] for b in s['basis']} == set(ids)
        assert not pending_update(store)['pending']
    finally:
        store.close()


def test_selection_excludes_other_people_and_inactive(store):
    add_self(store, '别人觉得我急躁', speaker='audience')
    add_self(store, '缺少涉及我', about=False)
    gone = add_self(store, '被遗忘')
    manage_memory(store, gone, 1, action='forget')
    deleted = add_self(store, '被删除')
    delete_memory(store, deleted, 1)
    first = add_self(store, '置顶优先', importance=1, pinned=1)
    material = select_evidence(store)
    assert material['memories'][0]['memory_id'] == first
    assert {m['content'] for m in material['memories']} == {'我来自云城', '置顶优先'}
    assert material['estimated_tokens'] <= 6000


@pytest.mark.parametrize('degree,status', [('small', 'current'), ('medium', 'current'), ('large', 'pending')])
def test_s02_s03_publication(store, clock, degree, status):
    old = current_persona(store)['id']
    result, gateway = generate(store, clock, degree=degree)
    assert result['version']['status'] == status
    assert current_persona(store)['id'] == (old if status == 'pending' else result['version']['id'])
    assert [p for p, _ in gateway.calls] == ['persona_generate', 'persona_check']
    assert result['version']['sentences'][0]['basis'] == [{'memory_id': 1, 'revision': 1}]
    assert result['version']['checks']['deterministic']['warnings']  # under 300 is advisory


def test_s04_rejected_model_keeps_previous(store, clock):
    old = current_persona(store)
    result, _ = generate(store, clock, supported=False)
    assert result['version']['status'] == 'rejected'
    assert current_persona(store) == old
    assert not result['version']['checks']['passed']


@pytest.mark.parametrize('change', ['edit', 'delete', 'forget'])
def test_s05_pending_update_is_read_only(store, change):
    old = current_persona(store)
    if change == 'edit':
        edit_memory(store, 1, 1, content='我来自海城')
    elif change == 'delete':
        delete_memory(store, 1, 1)
    else:
        manage_memory(store, 1, 1, action='forget')
    result = pending_update(store)
    assert result['pending']
    assert result['basis'][0]['memory_id'] == 1
    assert current_persona(store) == old


def test_s17_deleted_basis_cannot_be_reused(store, clock):
    delete_memory(store, 1, 1)
    add_self(store, '我喜欢在阅读时独处。')
    output = {'sentences': [{'text': '我来自云城。', 'basis': ['M1']}]}
    gateway = FakeGateway(store, output, supported=False)
    result = PersonaEngine(store, gateway, clock=clock).regenerate(expected_version=1)
    assert '我来自云城' not in dumps(gateway.calls[0][1]['evidence'])
    assert result['version']['status'] == 'rejected'
    assert current_persona(store)['id'] == 1


def test_s18_single_date_requires_scene(store, clock):
    add_self(store, importance=99)
    result, _ = generate(store, clock, text='我一向很耐心。', scene=False)
    assert result['version']['status'] == 'rejected'
    result, _ = generate(store, clock, text='在这次直播中，我耐心解释了规则。', scene=True)
    assert result['version']['status'] == 'current'
    assert result['version']['sentences'][0]['date_count'] == 1


def test_s19_admin_removal_forces_large_and_pending(store, clock):
    manual = admin_edit(store, '我喜欢简短的表达。', expected_version=1, clock=clock)
    result, _ = generate(store, clock)
    assert result['version']['change_degree'] == 'large'
    assert result['version']['status'] == 'pending'
    assert current_persona(store)['id'] == manual['id']


def test_pending_replacement_confirmation_and_rejection(store, clock):
    first, _ = generate(store, clock, degree='large')
    second, _ = generate(store, clock, degree='large', text='设定中，我住在云城。')
    assert {v['id']: v['status'] for v in list_versions(store)}[first['version']['id']] == 'superseded'
    reject_candidate(store, second['version']['id'], expected_version=1, clock=clock)
    third, _ = generate(store, clock, degree='large')
    published = confirm_candidate(store, third['version']['id'], expected_version=1, clock=clock)
    assert published['status'] == 'current'
    with pytest.raises(PersonaConflict):
        confirm_candidate(store, first['version']['id'], expected_version=1, clock=clock)


@pytest.mark.parametrize('mode,degree,status', [('all_auto','large','current'), ('all_manual','small','pending')])
def test_publication_modes(store, clock, mode, degree, status):
    store.set_setting('persona_publish_mode', mode)
    result, _ = generate(store, clock, degree=degree)
    assert result['version']['status'] == status


def test_rollback_linear_history_and_diff(store, clock):
    initial = current_persona(store)
    manual = admin_edit(store, '我偏好直接表达。\n我也喜欢留白。', expected_version=1, clock=clock)
    diff = version_diff(store, 1, manual['id'])
    assert diff['changes']
    restored = rollback(store, 1, expected_version=manual['id'], clock=clock)
    assert restored['id'] > manual['id']
    assert restored['content'] == initial['content']
    assert restored['base_version_id'] == manual['id']
    assert restored['rollback_of'] == 1
    assert len(list_versions(store)) == 3
    with pytest.raises(PersonaConflict):
        admin_edit(store, '过期修改。', expected_version=manual['id'], clock=clock)


def test_inflight_current_and_basis_conflicts(store, clock):
    gateway = FakeGateway(store, {'sentences': [{'text':'设定中，我来自云城。', 'basis':['M1']}]},
                          callback=lambda: admin_edit(store, '并发编辑。', expected_version=1, clock=clock))
    with pytest.raises(PersonaConflict):
        PersonaEngine(store, gateway, clock=clock).regenerate(expected_version=1)
    assert current_persona(store)['content'] == '并发编辑。'
    result, _ = generate(store, clock, callback=lambda: delete_memory(store, 1, 1))
    assert result['status'] == 'conflict'
    assert current_persona(store)['content'] == '并发编辑。'


def test_due_counts_distinct_memory_changes_and_seven_days(store, clock):
    admin_edit(store, '我在慢慢了解自己。', expected_version=1, clock=clock)
    assert not persona_due(store, clock=clock)['due']
    for i in range(4):
        add_self(store, f'新的自我认识{i}')
    assert not persona_due(store, clock=clock)['due']
    clock.value += timedelta(days=7)
    assert persona_due(store, clock=clock)['due']
    clock.value -= timedelta(days=7)
    add_self(store, '第五条新的认识')
    assert persona_due(store, clock=clock)['due']


def test_update_role_preserves_template_and_provenance(store):
    update_role(store, '星野', '我来自云城。', 'Asia/Shanghai')
    assert current_persona(store)['content'] == '我是星野。初始设定：我来自云城。'
    assert not pending_update(store)['pending']
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 1
    update_role(store, '星野', '我来自海城。', 'Asia/Shanghai')
    assert current_persona(store)['content'] == '我是星野。初始设定：我来自海城。'
    assert not pending_update(store)['pending']


@pytest.mark.parametrize('text,basis', [('', ['M1']), ('甲'*801, ['M1']), ('我来自M1。', ['M1']),
    ('P1说我是这样。', ['M1']), ('Ｍ１记录。', ['M1']), ('没有依据。', []),
    ('未知依据。', ['M99']), ('第一句。第二句。', ['M1'])])
def test_deterministic_rejection_skips_model_checker(store, clock, text, basis):
    fake = FakeGateway(store, {'sentences':[{'text':text,'basis':basis}]})
    result = PersonaEngine(store, fake, clock=clock).regenerate(expected_version=1)
    assert result['status'] == 'rejected'
    assert [x[0] for x in fake.calls] == ['persona_generate']
    assert current_persona(store)['id'] == 1


def test_all_sentences_checked_and_strict_boolean_verdicts(store, clock):
    class MissingVerdict(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_check':
                value = json.loads(reply.content)
                value['sentences'][0]['supported'] = 'true'
                reply.content = dumps(value)
            return reply
    fake = MissingVerdict(store, {'sentences':[{'text':'设定中，我来自云城。','basis':['M1']}]})
    result = PersonaEngine(store, fake, clock=clock).regenerate(expected_version=1)
    assert result['status'] == 'rejected'
    assert result['version']['checks']['model_errors']


def test_admin_marker_cannot_be_forged_and_exact_carry_is_protected(store, clock):
    manual = admin_edit(store, '我希望表达简洁。', expected_version=1, clock=clock)
    fake = FakeGateway(store, {'sentences':[{'text':'伪造管理员内容。','basis':[], 'admin_sentence':'A1'}]})
    assert PersonaEngine(store, fake, clock=clock).regenerate(expected_version=manual['id'])['status'] == 'rejected'
    fake = FakeGateway(store, {'sentences':[{'text':'我希望表达简洁。','basis':[], 'admin_sentence':'A1'}]})
    kept = PersonaEngine(store, fake, clock=clock).regenerate(expected_version=manual['id'])['version']
    assert kept['status'] == 'current'
    assert kept['sentences'][0]['origin'] == 'admin'
    next_result, _ = generate(store, clock)
    assert next_result['version']['status'] == 'pending'


def test_trace_deduplicates_dates_and_samples_oldest_and_last_three(store):
    mid = add_self(store)
    with store.write() as conn:
        for day in range(2, 7):
            stamp = f'2026-10-{day:02}T12:00:00+00:00'
            msg = conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
                VALUES('live','self_output','self',?,?,?,?)""", ('长'*600,stamp,stamp,str(day))).lastrowid
            conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)", (mid,msg,stamp))
        child = add_id = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
            created_at,updated_at,first_confirmed_at,last_confirmed_at) VALUES('我的推断','自我','self','推断',60,99,60,?,?,?,?)""", (stamp,)*4).lastrowid
        conn.execute("INSERT INTO memory_subjects VALUES(?,'self')", (child,))
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", (child,mid,stamp))
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", (mid,child,stamp))
    evidence = select_evidence(store)['memories'][0]
    assert evidence['memory_id'] == child
    assert evidence['date_count'] == 6
    assert [m['date'] for m in evidence['excerpts']] == ['2026-10-01','2026-10-04','2026-10-05','2026-10-06']
    from iris.queue import estimate_tokens
    assert all(estimate_tokens(m['excerpt']) <= 300 for m in evidence['excerpts'])


def test_selection_budget_includes_metadata_and_excerpts(store):
    for i in range(12):
        add_self(store, f'{i}'+('长'*900), importance=80)
    material = select_evidence(store)
    from iris.queue import estimate_tokens
    assert material['estimated_tokens'] == estimate_tokens(dumps(material['memories'])) <= 6000
    assert 0 < len(material['memories']) < 13
    assert material == select_evidence(store)


def test_pending_cannot_publish_stale_evidence_and_rollback_exposes_stale(store, clock):
    pending, _ = generate(store, clock, degree='large')
    delete_memory(store, 1, 1)
    with pytest.raises(PersonaConflict):
        confirm_candidate(store, pending['version']['id'], expected_version=1, clock=clock)
    manual = admin_edit(store, '手写句子。', expected_version=1, clock=clock)
    rollback(store, 1, expected_version=manual['id'], clock=clock)
    assert pending_update(store)['pending']


def test_no_changes_and_no_evidence_skip_without_model(store, clock):
    admin_edit(store, '手写句子。', expected_version=1, clock=clock)
    fake = FakeGateway(store, {})
    result = PersonaEngine(store, fake, clock=clock).update(expected_version=2)
    assert result['status'] == 'skipped' and result['reason'] == 'not_due'
    assert not fake.calls


def test_model_health_usage_repair_gate_and_call_diagnostics(store, clock):
    import httpx
    from iris.model_health import ModelHealth
    from iris.models import ModelConfig, Gateway
    configs = {'chat':ModelConfig('https://model.test/v1', '', 'fake', reasoning_effort='high')}
    health = ModelHealth(store, configs, clock=clock)
    requests = []
    def transport(request):
        assert not store._writer.in_transaction
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices':[{'message':{'content':'broken json'},'finish_reason':'stop'}],
                                        'usage':{'prompt_tokens':10,'completion_tokens':5}})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    gateway = Gateway(configs, store, client=client, health=health, clock=clock)
    try:
        health.set_daily_token_limit(10)
        result = PersonaEngine(store,gateway,clock=clock).regenerate(expected_version=1)
        assert result['status'] == 'skipped' and result['reason'] == 'usage_limit'
        assert len(requests) == 1  # cannot spend another call repairing JSON
        with store.read() as conn:
            call = dict(conn.execute('SELECT * FROM model_calls').fetchone())
        assert call['purpose'] == 'persona_generate' and call['model_kind'] == 'chat'
        assert call['reasoning_effort'] == 'high' and call['finish_reason'] == 'stop'
        assert (call['prompt_tokens'],call['completion_tokens']) == (10,5)
        result = PersonaEngine(store,gateway,clock=clock).regenerate(expected_version=1)
        assert result['status'] == 'skipped' and len(requests) == 1
        health.set_daily_token_limit(None)
        token = health.check('chat','learning')
        health.observe('chat',token,'authentication','invalid key')
        result = PersonaEngine(store,gateway,clock=clock).regenerate(expected_version=1)
        assert result['status'] == 'skipped' and result['reason'] == 'invalid_key'
        assert len(requests) == 1
    finally:
        gateway.close()
        client.close()


@pytest.mark.parametrize('memory_delay',[0,0.25])
def test_migration_freezes_legacy_initial_basis_before_later_edit(tmp_path,memory_delay):
    import sqlite3
    from importlib.resources import files
    path = tmp_path/'legacy.db'
    conn = sqlite3.connect(path)
    conn.create_function('iris_terms',1,lambda text:text)
    conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
    for script in sorted(files('iris').joinpath('migrations').iterdir()):
        if script.name.endswith('.sql') and script.name < '013':
            conn.executescript(script.read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)',(script.name,'2026-10-01'))
            conn.commit()
    stamp = '2026-10-01T12:00:00+00:00'
    memory_stamp = (datetime.fromisoformat(stamp)+timedelta(seconds=memory_delay)).isoformat()
    conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('self','self','我',?)",(stamp,))
    conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
        created_at,updated_at,first_confirmed_at,last_confirmed_at) VALUES('我来自云城','自我','self','设定',80,65,56,?,?,?,?)""",(memory_stamp,)*4)
    conn.execute("INSERT INTO memory_subjects VALUES(1,'self')")
    conn.execute("INSERT INTO sources(memory_id,kind,created_at) VALUES(1,'initial_setting',?)",(memory_stamp,))
    conn.execute("INSERT INTO persona_versions(content,created_at,is_current) VALUES('我是Iris。初始设定：我来自云城。',?,1)",(stamp,))
    conn.commit()
    conn.close()
    store = Store(path)
    try:
        # Deliberately modify before the first persona read after upgrade.
        edit_memory(store,1,1,content='我来自海城')
        assert pending_update(store)['pending']
        version = current_persona(store)
        assert version['content'] == '我是Iris。初始设定：我来自云城。'
        assert ''.join(s['text'] for s in version['sentences']) == version['content']
        assert version['sentences'][-1]['basis'] == [{'memory_id':1,'revision':1}]
    finally:
        store.close()


def test_rollback_with_stale_basis_becomes_due_after_seven_days(store,clock):
    edit_memory(store,1,1,content='我来自海城')
    manual = admin_edit(store,'手写内容。',expected_version=1,clock=clock)
    rollback(store,1,expected_version=manual['id'],clock=clock)
    assert not persona_due(store,clock=clock)['due']
    clock.value += timedelta(days=7)
    assert persona_due(store,clock=clock)['due']


def test_pending_candidate_keeps_its_original_settings(store,clock):
    pending,_ = generate(store,clock,degree='large')
    original = pending['version']['material']['settings']
    store.set_setting('persona_rules','只保留新的自我证据。')
    confirmed = confirm_candidate(store,pending['version']['id'],expected_version=1,clock=clock)
    assert confirmed['status'] == 'current'
    assert confirmed['material']['settings'] == original
    assert confirmed['created_at'] == pending['version']['created_at']


@pytest.mark.parametrize('content', ['\n我喜欢简洁。', '我说过“先听完。”后来我又解释了理由。', 'I prefer concise words. I also listen.'])
def test_sentence_boundaries_preserve_admin_text(store,clock,content):
    edited = admin_edit(store,content,expected_version=1,clock=clock)
    assert ''.join(s['text'] for s in edited['sentences']) == content
    assert len(edited['sentences']) == (1 if content.startswith('\n') else 2)


def test_initial_english_sentences_share_the_correct_setting_basis(tmp_path):
    store = Store(tmp_path/'english.db')
    try:
        setup_role(store,'Iris','I prefer concise words. I also listen.')
        version = current_persona(store)
        assert len(version['sentences']) == 3
        assert all(s['basis']==[{'memory_id':1,'revision':1}] for s in version['sentences'][1:])
    finally:
        store.close()


def test_model_retry_pause_probe_and_resume(store,clock):
    import httpx
    from iris.model_health import ModelHealth
    from iris.models import ModelConfig,Gateway
    configs = {'chat':ModelConfig('https://model.test/v1','','fake')}
    health = ModelHealth(store,configs,clock=clock)
    calls = []
    def transport(request):
        calls.append(json.loads(request.content))
        if len(calls)<=3:
            return httpx.Response(503,json={'error':{'message':'temporary'}})
        request_data = calls[-1]
        if len(calls)==4:
            output = {'ok':True}
        elif 'candidate' in json.loads(request_data['messages'][-1]['content']):
            output = {'sentences':[{'index':1,'supported':True,'fabricated':False,'scene_qualified':False,
                'violations':[],'reason':'设定'}],'change_degree':'small','reason':'措辞'}
        else:
            output = {'sentences':[{'text':'初始设定中，我来自云城。','basis':['M1']}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':dumps(output)},'finish_reason':'stop'}]})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    gateway = Gateway(configs,store,client=client,health=health,clock=clock,sleeper=lambda _:None)
    try:
        result = PersonaEngine(store,gateway,clock=clock).regenerate(expected_version=1)
        assert result['status']=='skipped' and len(calls)==3
        assert health.snapshot()['chat']['state']=='temporarily_unavailable'
        result = PersonaEngine(store,gateway,clock=clock).regenerate(expected_version=1)
        assert result['status']=='skipped' and len(calls)==3
        clock.value += timedelta(seconds=61)
        assert gateway.probe('chat')
        result = PersonaEngine(store,gateway,clock=clock).regenerate(expected_version=1)
        assert result['status']=='current' and len(calls)==6
    finally:
        gateway.close()
        client.close()


def test_admin_protection_is_byte_exact_including_outer_whitespace(store,clock):
    manual = admin_edit(store,'\n我希望表达简洁。 ',expected_version=1,clock=clock)
    changed = FakeGateway(store,{'sentences':[{'text':'我希望表达简洁。','basis':[],'admin_sentence':'A1'}]})
    result = PersonaEngine(store,changed,clock=clock).regenerate(expected_version=manual['id'])
    assert result['status']=='rejected'
    assert result['version']['change_degree']=='large'
    unchanged = FakeGateway(store,{'sentences':[{'text':'\n我希望表达简洁。 ','basis':[],'admin_sentence':'A1'}]})
    result = PersonaEngine(store,unchanged,clock=clock).regenerate(expected_version=manual['id'])
    assert result['status']=='current'
    assert result['version']['content'] == manual['content']


def test_admin_edit_preserves_unchanged_sentence_provenance(store,clock):
    previous = current_persona(store)
    text = previous['content'] + '我用手写补充这句话。'
    published = admin_edit(store,text,expected_version=previous['id'],clock=clock)
    assert published['sentences'][:-1] == previous['sentences']
    assert published['sentences'][-1]['origin'] == 'admin'
    assert published['sentences'][-1]['basis'] == []


def test_due_counts_168_hours_from_generation_not_confirmation(store,clock):
    generated,_ = generate(store,clock,degree='large')
    created = clock.value
    add_self(store,'我在另一日说明规则。',stamp=(created+timedelta(hours=1)).isoformat())
    clock.value = created+timedelta(days=6)
    confirmed = confirm_candidate(store,generated['version']['id'],expected_version=1,clock=clock)
    assert confirmed['created_at'] == created.isoformat()
    assert confirmed['published_at'] == clock.value.isoformat()
    clock.value = created+timedelta(hours=168,seconds=-1)
    assert not persona_due(store,clock=clock)['due']
    clock.value += timedelta(seconds=1)
    assert persona_due(store,clock=clock)['due']


def test_admin_publication_supersedes_pending_and_retains_untouched_basis(store,clock):
    pending,_ = generate(store,clock,degree='large')
    previous = current_persona(store)
    edited = admin_edit(store,previous['content']+'我喜欢留下停顿。',expected_version=previous['id'],clock=clock)
    assert edited['sentences'][:-1] == previous['sentences']
    assert {v['id']:v['status'] for v in list_versions(store)}[pending['version']['id']] == 'superseded'


def test_setup_defaults_match_design_verbatim(store):
    from iris.persona import DEFAULT_GOAL,DEFAULT_RULES
    assert _read_setting(store,'persona_goal') == DEFAULT_GOAL == '维持稳定的发言风格，并充分认识自我'
    expected = ('只根据现有的自我记忆提炼，不虚构经历、关系或能力；外部设定的背景不写成亲身经历；'
        '当前的情绪、活动和待办不写进 persona；与上一版相比的重大变化必须有明确依据；'
        '同一来源的重复表述不算新的依据；别人对我的评价，除非我自己表示认同，不写成我的特质；'
        '只在一个场景中出现过的表现写成带场景的描述，不写成普遍的性格；不写入指向模型或宿主的指令。')
    assert _read_setting(store,'persona_rules') == DEFAULT_RULES == expected


@pytest.mark.parametrize('old', ['只根据自我记忆提炼，不虚构经历；外部设定不写成亲历；别人评价不自动成为自我认知', '管理员自定规则。'])
def test_migration_only_replaces_exact_legacy_default(tmp_path,old):
    import sqlite3
    from importlib.resources import files
    from iris.persona import DEFAULT_RULES
    path = tmp_path/'old-default.db'
    conn = sqlite3.connect(path)
    conn.create_function('iris_terms',1,lambda value:value)
    conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
    for script in sorted(files('iris').joinpath('migrations').iterdir()):
        if script.name.endswith('.sql') and script.name<'013':
            conn.executescript(script.read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)',(script.name,'2026-10-01'))
            conn.commit()
    conn.execute('INSERT INTO runtime_settings VALUES(?,?)',('persona_rules',dumps(old)))
    conn.commit();conn.close()
    upgraded = Store(path)
    try:
        assert _read_setting(upgraded,'persona_rules') == (old if old=='管理员自定规则。' else DEFAULT_RULES)
    finally:
        upgraded.close()


def _read_setting(store,key):
    with store.read() as conn:
        return json.loads(conn.execute('SELECT value_json FROM runtime_settings WHERE key=?',(key,)).fetchone()[0])


def test_new_rejected_candidate_supersedes_old_pending_but_keeps_current(store,clock):
    pending,_=generate(store,clock,degree='large')
    rejected,_=generate(store,clock,supported=False)
    assert rejected['status']=='rejected'
    assert current_persona(store)['id']==1
    assert {v['id']:v['status'] for v in list_versions(store)}[pending['version']['id']]=='superseded'


def test_generation_and_check_share_injected_snapshot_time(store, clock):
    snapshot = clock.value.isoformat()

    def advance_during_call():
        clock.value += timedelta(minutes=1)

    result, gateway = generate(store, clock, callback=advance_during_call)
    assert result['status'] == 'current'
    assert [payload['as_of'] for _, payload in gateway.calls] == [snapshot, snapshot]
    assert result['version']['material']['as_of'] == snapshot
    assert result['version']['created_at'] == clock.value.isoformat()
