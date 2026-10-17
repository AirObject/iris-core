"""Daily observations, evidence accumulation and per-memory admission; fake models only."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from iris import entry_profile as ep
from iris.db import Store, dumps
from iris.memory_ops import edit_memory, manage_memory, purge_memory, write_entry_profile_memory
from iris.models import ModelError
from iris.queue import add_message
from iris.retrieval import Retrieval


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
TOPIC = '讨论徒步装备的取舍。'


@pytest.fixture(autouse=True)
def fixed_projection_clock(monkeypatch):
    monkeypatch.setattr(ep, 'utc_now', lambda: NOW)


def add(store, text, day=1, *, sender='甲', key=None, minute=0):
    at = f'2026-10-{day:02}T12:{minute:02}:00+08:00'
    mid = add_message(store, entry_id='group', entry_name='测试群', platform='test', entry_kind='group',
        kind='message', sender=sender, account_id=sender, content=text, occurred_at=at,
        dedupe_key=key or text)
    with store.write() as conn:
        conn.execute('UPDATE messages SET received_at=? WHERE id=?', (at, mid))
    return mid


def profiles(store):
    with store.read() as conn:
        return ep.memory_profile_rows(conn, 'group', as_of=NOW.isoformat())


def projection(store):
    with store.read() as conn:
        return ep.profile_context(conn, 'group')


def run(store, gateway, *, periodic=False, now=NOW):
    engine = ep.EntryProfileEngine(store, gateway, clock=lambda: now)
    with store.read() as conn:
        version = ep._memory_state(conn, 'group')['version']
    return (engine.update if periodic else engine.regenerate)('group', expected_version=version)


def source(message):
    return {'message_id': message['message_id'], 'field': 'content', 'excerpt': message['content']}


def propose(payload, *, category='topic', body=TOPIC):
    return {'observations': [{'op': 'propose', 'category': category, 'body': body,
        'saying': '', 'meaning': '', 'definition_refs': [],
        'source_refs': [source(m) for m in payload['messages']]}]}


def accept(payload):
    value = {'decision': 'accept', 'violations': [], 'reason': '逐条核对通过。',
        'safe_sources': [{k: s[k] for k in ('message_id', 'field', 'excerpt')}
                         for s in payload['memory']['sources']]}
    if payload.get('review_contract')=='groups_and_meaning_v1':
        value['group_reviews']=[dict(group_id=g['group_id'],focus='同一主题的独立讨论。',
            same_topic=True,independent=g['eligible'],reason='假模型确认该组的独立性。')
            for g in payload['memory'].get('discussion_groups',[])]
        meaning=payload['memory'].get('meaning','')
        value['meaning_support']=[dict(point=meaning,source_refs=[ref for ref in value['safe_sources']
            if ref['message_id'] in payload['memory'].get('definition_refs',[])])] if meaning else []
    return value


class Gateway:
    def __init__(self, store, observations=None, checks=None, hook=None):
        self.store = store
        self.observations = observations or propose
        self.checks = checks or accept
        self.hook = hook
        self.calls = []
        self.tick = 0.0

    def monotonic(self):
        return self.tick

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[1]['content'])
        self.calls.append({'purpose': purpose, 'payload': payload, 'deadline': kwargs.get('_deadline')})
        if self.hook:
            self.hook(self, purpose, payload)
        handler = self.observations if 'observe' in purpose else self.checks
        result = handler(payload) if callable(handler) else handler.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(content=result if isinstance(result, str) else dumps(result),
                               finish_reason='stop', usage={})


def test_one_observation_creates_low_strength_hidden_candidate(store):
    add(store, '轻量背包与结实背包各有什么取舍？')
    gateway = Gateway(store)
    result = run(store, gateway)
    assert result['status'] == 'published', result
    rows = profiles(store)
    assert len(rows) == 1 and rows[0]['importance'] == 30 and rows[0]['retention'] == 42
    assert rows[0]['admission_state'] == 'candidate' and not rows[0]['usable']
    assert rows[0]['independent_days'] == 1 and rows[0]['independent_uses'] == 1
    assert projection(store) is None
    assert len(gateway.calls) == 1 and 'observe' in gateway.calls[0]['purpose']
    assert not Retrieval(store).search(text='徒步装备', kinds=['入口画像'], entry_id='group')['memories']


def test_cross_day_confirmation_checks_only_one_item_and_promotes_to_50(store):
    add(store, '轻量背包与结实背包各有什么取舍？')
    gateway = Gateway(store)
    run(store, gateway)
    mid = profiles(store)[0]['memory_id']
    add(store, '长途徒步时，我想讨论防水鞋和透气鞋怎么选。', 3, sender='乙')
    gateway.observations = lambda p: {'observations': [{'op': 'confirm', 'memory_id': mid,
        'source_refs': [source(p['messages'][0])]}]}
    result = run(store, gateway)
    assert result['status'] == 'published', result
    row = profiles(store)[0]
    assert row['importance'] == 50 and row['retention'] == 50
    assert row['admission_state'] == 'accepted' and row['independent_days'] == 2
    assert projection(store)['memory_ids'] == [mid]
    check = gateway.calls[-1]['payload']
    assert check['memory']['memory_id'] == mid and 'operations' not in check
    assert len(gateway.calls) == 3
    assert set(projection(store)) == {'text','version','generated_at','possibly_stale','memory_ids'}


def test_same_day_topic_does_not_reach_cross_day_gate(store):
    add(store, '轻量背包与结实背包各有什么取舍？')
    add(store, '防水鞋和透气鞋谁更适合长途徒步？', sender='乙', minute=40)
    gateway = Gateway(store)
    run(store, gateway)
    assert not profiles(store)[0]['usable']
    assert len(gateway.calls) == 1


def test_daily_chunks_do_not_include_future_day_or_prior_sources_in_directory(store):
    add(store, '轻量背包与结实背包各有什么取舍？')
    add(store, '防水鞋和透气鞋谁更适合长途徒步？', 3, sender='乙')
    gateway = Gateway(store)
    run(store, gateway)
    observations = [c['payload'] for c in gateway.calls if 'observe' in c['purpose']]
    assert len(observations) == 2
    assert all(len({m['date'] for m in p['messages']}) == 1 for p in observations)
    assert len(observations[0]['messages']) == 1
    assert observations[1]['profiles'] and 'sources' not in observations[1]['profiles'][0]
    assert observations[1]['profiles'][0]['independent_days'] == 1


def test_periodic_defers_current_day_but_immediate_flushes_it(store):
    add(store, '本群长期禁止发布广告。', 14)
    gateway = Gateway(store, lambda p: propose(p, category='rule', body='禁止发布广告。'))
    result = run(store, gateway, periodic=True)
    assert result['status'] == 'skipped' and not gateway.calls
    assert run(store, gateway)['status'] == 'published'
    assert projection(store)


def test_rule_requires_single_item_check_and_rejection_does_not_hide_manual(store):
    add(store, '本群长期禁止发布广告。')
    manual = write_entry_profile_memory(store, 'group', op='new', manual=True,
        content='管理员说明第一行。\n第二行。', category=None)['memory_id']
    gateway = Gateway(store, lambda p: propose(p, category='rule', body='禁止发布广告。'),
        lambda p: {'decision':'reject','violations':['无依据'],'reason':'不支持。','safe_sources':[]})
    run(store, gateway)
    assert projection(store)['memory_ids'] == [manual]
    assert len(projection(store)['text'].splitlines()) == 1
    assert profiles(store)[1]['admission_state'] == 'rejected'
    assert profiles(store)[1]['importance'] == 30


def test_one_bad_checker_does_not_rollback_other_admitted_items(store):
    add(store, '本群长期禁止广告，也禁止谩骂。')
    def observe(p):
        first = propose(p, category='rule', body='禁止广告。')['observations'][0]
        second = {**first, 'body':'禁止谩骂。'}
        return {'observations':[first,second]}
    gateway = Gateway(store, observe, checks=[accept, '{', '{'])
    # Function-valued scripted replies keep this fake independent of product code.
    pending = gateway.checks
    gateway.checks = lambda p: (lambda x: x(p) if callable(x) else x)(pending.pop(0))
    result = run(store, gateway)
    assert result['status'] == 'published', result
    assert len(projection(store)['memory_ids']) == 1
    rows = profiles(store)
    assert sum(r['usable'] for r in rows) == 1
    with store.read() as conn:
        assert ep.incremental_progress(conn, 'group')['failed_checks'] == 1
        assert conn.execute('SELECT cursor FROM entry_profile_memory_state').fetchone()[0] > 0


def test_timeout_one_check_keeps_other_success_and_does_not_write_late_admission(store):
    add(store, '本群长期禁止广告，也禁止谩骂。')
    def observe(p):
        one = propose(p, category='rule', body='禁止广告。')['observations'][0]
        return {'observations':[one,{**one,'body':'禁止谩骂。'}]}
    checks = 0
    def hook(g, purpose, payload):
        nonlocal checks
        if 'admit' in purpose:
            checks += 1
            if checks == 2:
                g.tick += 181
    gateway = Gateway(store, observe, hook=hook)
    run(store, gateway)
    assert len(projection(store)['memory_ids']) == 1
    assert len([r for r in profiles(store) if r['importance'] == 50]) == 1


def test_json_repair_shares_deadline_and_missing_fields_are_not_accepted(store):
    add(store, '本群长期禁止广告。')
    replies = []
    def observe(p):
        replies.append(p)
        return '{' if len(replies) == 1 else propose(p, category='rule', body='禁止广告。')
    gateway = Gateway(store, observe)
    run(store, gateway)
    assert gateway.calls[0]['deadline'] == gateway.calls[1]['deadline'] == 180
    assert gateway.calls[1]['purpose'].endswith('_repair')
    assert projection(store)


def test_confirmations_do_not_publish_unchecked_new_excerpts_or_bump_projection(store):
    add(store, '本群长期禁止广告。')
    gateway = Gateway(store, lambda p: propose(p, category='rule', body='禁止广告。'))
    run(store, gateway)
    before = projection(store)
    mid = before['memory_ids'][0]
    add(store, '长期的广告禁令继续有效；同事家里遭遇的私事不必转发。', 4, sender='乙')
    gateway.observations = lambda p: {'observations':[{'op':'confirm','memory_id':mid,
        'source_refs':[source(p['messages'][0])]}]}
    run(store, gateway)
    assert projection(store)['version'] == before['version']
    row = profiles(store)[0]
    assert len(row['sources']) == 1 and '同事' not in dumps(row['sources'])
    assert row['retention'] == 47 and len(gateway.calls) == 3


def test_replay_and_restart_do_not_duplicate_candidate_or_confirmation(store):
    add(store, '轻量背包与结实背包各有什么取舍？')
    gateway = Gateway(store)
    run(store, gateway)
    before = profiles(store)
    assert run(store, gateway)['status'] == 'skipped'
    reopened = Store(store.path)
    try:
        assert run(reopened, Gateway(reopened))['status'] == 'skipped'
        assert profiles(reopened) == before
    finally:
        reopened.close()
    assert len(gateway.calls) == 1


def test_source_mutation_revokes_admission_without_lowering_importance(store):
    mid = add(store, '本群长期禁止广告。')
    gateway = Gateway(store, lambda p: propose(p, category='rule', body='禁止广告。'))
    run(store, gateway)
    before = projection(store)
    with store.write() as conn:
        conn.execute("UPDATE messages SET content='这条消息已经变更。' WHERE id=?", (mid,))
    assert projection(store) is None
    row = profiles(store)[0]
    assert row['importance'] == 50 and row['admission_state'] == 'candidate'
    with store.read() as conn:
        assert ep._memory_state(conn, 'group')['version'] == before['version'] + 1


def test_manual_edit_during_check_wins_and_is_not_automatically_rewritten(store):
    add(store, '本群长期禁止广告。')
    def hook(g, purpose, payload):
        if 'admit' in purpose:
            m = payload['memory']
            assert edit_memory(store, m['memory_id'], m['revision'], content='管理员手写规则。')
    run(store, Gateway(store, lambda p: propose(p, category='rule', body='禁止广告。'), hook=hook))
    row = profiles(store)[0]
    assert row['body'] == '管理员手写规则。' and row['manual_protected']
    assert '管理员手写规则' in projection(store)['text']


def test_clear_during_check_cannot_leave_candidate_prose_or_late_raw_output(store):
    add(store, '本群长期禁止广告。')
    def hook(g, purpose, payload):
        if 'admit' in purpose:
            m = payload['memory']
            assert purge_memory(store, m['memory_id'], m['revision'], confirm=True)
    run(store, Gateway(store, lambda p: propose(p, category='rule', body='禁止广告。'), hook=hook))
    assert not profiles(store) and projection(store) is None
    with store.read() as conn:
        for table in ('entry_profile_incremental_work', 'entry_profile_attempts'):
            assert '本群长期禁止广告' not in dumps([dict(r) for r in conn.execute('SELECT * FROM '+table)])


@pytest.mark.parametrize('bad', ['她经常聊登山。', '群里他经常聊装备。'])
def test_gendered_candidate_is_discarded_before_storage(store, bad):
    add(store, '轻量背包与结实背包各有什么取舍？')
    gateway = Gateway(store, lambda p: propose(p, body=bad))
    run(store, gateway)
    assert not profiles(store) and len(gateway.calls) == 1


def test_saying_independent_uses_need_no_cross_day_but_bursts_do_not_count(store):
    add(store, '这个填空先留着，叫作纸洞。', minute=0)
    add(store, '纸洞就是没有补完的缺口。', sender='乙', minute=5)
    def observe(p):
        return {'observations':[{'op':'propose','category':'saying','body':'纸洞',
            'saying':'纸洞','meaning':'','definition_refs':[], 'source_refs':[source(m) for m in p['messages']]}]}
    gateway=Gateway(store,observe)
    run(store,gateway)
    row=profiles(store)[0]
    assert row['usable'] and row['independent_days']==1 and row['independent_uses']==2
    before=row['retention']
    add(store,'纸洞又来了。',sender='乙',minute=6)
    gateway.observations=lambda p:{'observations':[{'op':'confirm','memory_id':row['memory_id'],
        'source_refs':[source(p['messages'][0])]}]}
    run(store,gateway)
    assert profiles(store)[0]['retention']==before


def test_repeated_copy_registration_and_reported_text_never_add_strength(store):
    first=add(store,'轻量背包与结实背包各有什么取舍？')
    run(store,Gateway(store))
    mid=profiles(store)[0]['memory_id']
    add(store,'轻量背包与结实背包各有什么取舍？',day=3,key='copy')
    add(store,'报名接龙：也想讨论轻量背包取舍，加我。',day=4,key='register')
    add(store,'转述一下之前提过的背包取舍。',day=5,key='reported')
    gateway=Gateway(store,lambda p:{'observations':[{'op':'confirm','memory_id':mid,
        'source_refs':[source(m) for m in p['messages']]}]})
    run(store,gateway)
    row=profiles(store)[0]
    assert row['retention']==42 and row['independent_uses']==1 and not row['usable']


def test_failed_observation_is_a_durable_gap_but_later_day_can_succeed(store):
    first=add(store,'本群长期禁止广告。')
    last=add(store,'本群的长期群规也禁止谩骂。',day=3)
    def observe(p):
        if p['messages'][0]['message_id']==first: return '{'
        return propose(p,category='rule',body='禁止谩骂。')
    gateway=Gateway(store,observe)
    run(store,gateway)
    assert projection(store)
    with store.read() as conn:
        progress=ep.incremental_progress(conn,'group')
    assert progress['scan_cursor']==last and progress['applied_cursor']==0 and progress['observation_gaps']==1
    assert len(gateway.calls)==4


def test_unusually_long_message_is_split_without_truncation_or_double_confirmation(store):
    text='本群长期禁止广告。' + '这是完整保留的背景说明。'*700
    mid=add(store,text)
    add(store,'以后也不要在群里谩骂，这是长期规则。',day=2)
    gateway=Gateway(store,lambda p:{'observations':[]})
    run(store,gateway)
    chunks=[c['payload']['messages'][0] for c in gateway.calls if 'observe' in c['purpose']
            and c['payload']['messages'][0]['message_id']==mid]
    assert len(chunks)>1
    assert ''.join(c['content'] for c in chunks)==text
    with store.read() as conn:
        assert ep.incremental_progress(conn,'group')['observation_gaps']==0


def test_rejected_recheck_leaves_importance_50_but_reverts_to_candidate(store):
    source_id=add(store,'本群长期禁止广告。')
    gateway=Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。'))
    run(store,gateway)
    mid=profiles(store)[0]['memory_id']
    with store.write() as conn:
        conn.execute("UPDATE messages SET content='原公告已编辑。' WHERE id=?",(source_id,))
    add(store,'新消息里提到广告，但并不是明确禁止。',day=4)
    gateway.observations=lambda p:{'observations':[{'op':'confirm','memory_id':mid,'source_refs':[source(p['messages'][0])]}]}
    gateway.checks=lambda p:{'decision':'reject','violations':['无依据'],'reason':'没有规则公告。','safe_sources':[]}
    run(store,gateway)
    row=profiles(store)[0]
    assert not row['usable'] and row['importance']==50
    assert row['admission_state']=='candidate'


def test_restoring_forgotten_candidate_keeps_gate_and_never_revives_deleted(store):
    add(store,'轻量背包与结实背包各有什么取舍？')
    run(store,Gateway(store))
    row=profiles(store)[0]
    assert manage_memory(store,row['memory_id'],row['revision'],action='forget')
    assert manage_memory(store,row['memory_id'],row['revision'],action='restore')
    assert projection(store) is None


def test_smaller_excerpt_must_keep_support_and_cannot_borrow_neighbor(store):
    add(store,'本群长期禁止广告；另外同事的事请勿转发。')
    def check(p):
        return {'decision':'accept','violations':[],'reason':'保留规则原句。',
            'safe_sources':[{'message_id':p['memory']['sources'][0]['message_id'],'field':'content','excerpt':'本群长期禁止广告'}]}
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。'),check))
    row=profiles(store)[0]
    assert row['usable'] and [s['excerpt'] for s in row['sources']]==['本群长期禁止广告']


def test_all_runtime_revision_and_scope_guards_hold_while_check_is_in_flight(store):
    from iris.memory_ops import set_entry_visibility
    add(store,'本群长期禁止广告。')
    def hook(g,purpose,payload):
        if 'admit' in purpose:
            set_entry_visibility(store,'group','entries',visible_in=['some-other-entry'])
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。'),hook=hook))
    assert projection(store) is None


def test_maintenance_round_robins_entries_before_draining_backlog(store):
    from iris.maintenance import Maintenance
    for entry in ('a','b'):
        for day in (1,2,3):
            at=f'2026-10-{day:02}T12:00:00+08:00'
            mid=add_message(store,entry_id=entry,entry_name=entry,platform='test',entry_kind='group',
                kind='message',sender='甲',account_id='甲',content='当天的普通消息。',occurred_at=at,dedupe_key=str(day))
            with store.write() as conn: conn.execute('UPDATE messages SET received_at=? WHERE id=?',(at,mid))
    store.set_setting('consolidation',dict(max_calls=50,merge_enabled=False,conflict_enabled=False,
        dependency_enabled=False,persona_enabled=False,goal_review_enabled=False,person_summary_enabled=False))
    gateway=Gateway(store,lambda p:{'observations':[]})
    maintenance=Maintenance(store,gateway=gateway,clock=lambda:NOW)
    maintenance.run(maintenance.request())
    assert [c['payload']['entry_id'] for c in gateway.calls]==['a','b','a','b','a','b']


def test_admin_progress_and_pending_basis_are_distinct_from_public_sources(store):
    from iris.admin_data import entry_profile_snapshot
    add(store,'轻量背包与结实背包各有什么取舍？')
    run(store,Gateway(store))
    value=entry_profile_snapshot(store,'group')
    assert value['progress']['completed_chunks']==1
    assert value['memories'][0]['sources']==[]
    assert len(value['memories'][0]['pending_sources'])==1
    assert value['latest_attempt']['progress']['completed_chunks']==1


def test_cleared_queued_work_never_calls_network(store):
    mid=add(store,'本群长期禁止广告。')
    from test_retrieval import put
    ordinary=put(store,'本群禁止广告。',evidence=[mid])
    gateway=Gateway(store)
    task=ep.IncrementalProfileTask(store,gateway,clock=lambda:NOW)
    wid=task._freeze_chunk('group')
    assert purge_memory(store,ordinary,1,confirm=True)
    with store.read() as conn: work=dict(conn.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?',(wid,)).fetchone())
    task._run_one(work)
    assert not gateway.calls


def test_saved_observation_recovers_without_second_model_call(store):
    add(store,'轻量背包与结实背包各有什么取舍？')
    gateway=Gateway(store)
    task=ep.IncrementalProfileTask(store,gateway,clock=lambda:NOW)
    wid=task._freeze_chunk('group')
    with store.write() as conn:
        work=conn.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?',(wid,)).fetchone()
        parsed=propose(ep._inc_public_payload(json.loads(work['payload_json'])))
        conn.execute("UPDATE entry_profile_incremental_work SET state='running',parsed_json=? WHERE id=?",(dumps(parsed),wid))
    run(store,gateway)
    assert not gateway.calls
    assert len(profiles(store))==1 and profiles(store)[0]['retention']==42


def test_multiple_observations_of_same_target_in_one_chunk_confirm_once(store):
    add(store,'本群长期禁止广告。')
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。')))
    row=profiles(store)[0]
    add(store,'本群的群规也适用于商业链接，禁止广告。',day=3,sender='乙')
    add(store,'群公告说不要投广告，大家遵守。',day=3,sender='丙',minute=30)
    def observe(p):
        return {'observations':[{'op':'confirm','memory_id':row['memory_id'],'source_refs':[source(m)]} for m in p['messages']]}
    run(store,Gateway(store,observe))
    assert profiles(store)[0]['retention']==row['retention']+5


def test_backup_round_trip_keeps_admission_pending_sources_and_journal(store,tmp_path):
    from iris.backup import export_archive,import_archive
    import sqlite3
    add(store,'本群长期禁止广告。')
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。')))
    archive=tmp_path/'profiles.zip';export_archive(store,archive)
    target=tmp_path/'restored'/'iris.db';import_archive(archive,target)
    with store.read() as conn,sqlite3.connect(target) as copy:
        for table in ('entry_profile_admissions','entry_profile_incremental_work','entry_profile_confirmation_groups','entry_profile_source_groups'):
            assert [tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]==copy.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
    restored=Store(target)
    try: assert projection(restored)==projection(store)
    finally: restored.close()


def test_candidates_decay_daily_but_accepted_50_every_third_maintenance(store):
    from iris.maintenance import Maintenance
    add(store,'轻量背包与结实背包各有什么取舍？')
    run(store,Gateway(store))
    add(store,'本群长期禁止广告。',day=2)
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。')))
    before={r['memory_id']:r for r in profiles(store)}
    maintenance=Maintenance(store,clock=lambda:NOW)
    maintenance.run(maintenance.request())
    for row in profiles(store):
        old=before[row['memory_id']]
        assert row['retention']==old['retention']-(1 if row['importance']==30 else 0)


def test_unsafe_observation_is_resnapshotted_on_later_run(store):
    mid=add(store,'修改之前的普通消息。')
    def mutate(g,purpose,payload):
        if 'observe' in purpose:
            with store.write() as conn: conn.execute("UPDATE messages SET content='修改之后的主题讨论。' WHERE id=?",(mid,))
    first=Gateway(store,lambda p:{'observations':[]},hook=mutate)
    result=run(store,first)
    assert result['reason']=='source_changed'
    second=Gateway(store,lambda p:{'observations':[]})
    run(store,second)
    assert second.calls[0]['payload']['messages'][0]['content']=='修改之后的主题讨论。'
    with store.read() as conn: assert ep.incremental_progress(conn,'group')['observation_gaps']==0


def test_revision_proposal_keeps_old_body_until_separate_check_accepts(store):
    add(store,'本群长期禁止广告。')
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。')))
    row=profiles(store)[0];version=projection(store)['version']
    add(store,'群规修订：广告和商业链接都禁止。',day=4)
    def observe(p):
        return {'observations':[{'op':'change','memory_id':row['memory_id'],'action':'revise',
            'category':'rule','body':'禁止广告与商业链接。','saying':'','meaning':'','definition_refs':[],
            'source_refs':[source(p['messages'][0])]}]}
    def check(p):
        assert projection(store)['text'].endswith('禁止广告。')
        assert len(p['memory']['sources'])==2
        return accept(p)
    run(store,Gateway(store,observe,check))
    assert projection(store)['version']==version+1
    assert profiles(store)[0]['body']=='禁止广告与商业链接。'


def test_self_only_and_unparseable_turns_advance_without_model(store):
    mid=add(store,'这是角色自己的输出。')
    bad=add(store,'旧导入的不可解析日期消息。',key='bad')
    with store.write() as conn:
        conn.execute("UPDATE messages SET sender_subject_id='self',kind='self_output' WHERE id=?",(mid,))
        conn.execute("UPDATE messages SET occurred_at='下个月' WHERE id=?",(bad,))
    gateway=Gateway(store,lambda p:(_ for _ in ()).throw(AssertionError('empty evidence must not call')))
    run(store,gateway)
    assert not gateway.calls
    with store.read() as conn: assert ep.incremental_progress(conn,'group')['applied_cursor']==bad


def test_stale_queued_check_is_cancelled_before_spending_a_model_call(store):
    add(store,'本群长期禁止广告。')
    gateway=Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。'),
        checks=lambda p:{'decision':'reject','violations':['无依据'],'reason':'暂不通过。','safe_sources':[]})
    run(store,gateway)
    row=profiles(store)[0]
    with store.write() as conn:
        work=dict(conn.execute("SELECT * FROM entry_profile_incremental_work WHERE kind='admit'").fetchone())
        conn.execute("UPDATE entry_profile_incremental_work SET state='queued' WHERE id=?",(work['id'],))
        conn.execute('UPDATE memories SET revision=revision+1 WHERE id=?',(row['memory_id'],))
    fresh=Gateway(store)
    ep.IncrementalProfileTask(store,fresh,clock=lambda:NOW)._run_one(work)
    assert not fresh.calls
    with store.read() as conn:
        state=conn.execute('SELECT state,reason FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()
        assert tuple(state)==('cancelled','stale_check')


def test_change_check_cannot_remove_the_actual_change_announcement(store):
    first=add(store,'本群长期禁止广告。')
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。')))
    row=profiles(store)[0];before=projection(store)['text']
    add(store,'群公告：广告已允许。',day=4)
    def observe(p):
        return {'observations':[{'op':'change','memory_id':row['memory_id'],'action':'retire',
            'category':'rule','body':'','saying':'','meaning':'','definition_refs':[],
            'source_refs':[source(p['messages'][0])]}]}
    def check(p):
        value=accept(p);value['safe_sources']=[s for s in value['safe_sources'] if s['message_id']==first];return value
    result=run(store,Gateway(store,observe,check))
    assert projection(store)['text']==before
    assert result['reason']=='change_basis_removed'


def test_nested_wrong_category_type_gets_format_repair_not_an_uncaught_error(store):
    add(store,'本群长期禁止广告。')
    def observe(p):
        value=propose(p,category='rule',body='禁止广告。')
        if len(gateway.calls)==1: value['observations'][0]['category']=[]
        return value
    gateway=Gateway(store,observe)
    run(store,gateway)
    assert profiles(store)[0]['usable'] and len(gateway.calls)==3


def test_settings_change_reschedules_a_stale_candidate_check(store):
    add(store,'本群长期禁止广告。')
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。'),
        checks=lambda p:{'decision':'reject','violations':['无依据'],'reason':'暂不通过。','safe_sources':[]}))
    with store.write() as conn:
        work=dict(conn.execute("SELECT * FROM entry_profile_incremental_work WHERE kind='admit'").fetchone())
        conn.execute("UPDATE entry_profile_incremental_work SET state='queued' WHERE id=?",(work['id'],))
        conn.execute("UPDATE entry_profile_settings SET revision=revision+1 WHERE entry_id='group'")
    fresh=Gateway(store)
    ep.IncrementalProfileTask(store,fresh,clock=lambda:NOW)._run_one(work)
    assert not fresh.calls
    run(store,fresh)
    assert len(fresh.calls)==1 and profiles(store)[0]['usable']


def test_progress_counts_only_live_automatic_waiting_candidates(store):
    add(store,'轻量背包与结实背包各有什么取舍？')
    run(store,Gateway(store))
    write_entry_profile_memory(store,'group',op='new',manual=True,content='管理员说明。',category=None)
    with store.read() as conn: assert ep.incremental_progress(conn,'group')['waiting_candidates']==1
    row=next(r for r in profiles(store) if not r['manual_protected'])
    assert manage_memory(store,row['memory_id'],row['revision'],action='forget')
    with store.read() as conn: assert ep.incremental_progress(conn,'group')['waiting_candidates']==0


def test_manual_body_remains_visible_without_publishing_unchecked_confirmation_quote(store):
    add(store,'本群长期禁止广告。另外，同事最近离婚的事情不要外传。')
    manual=write_entry_profile_memory(store,'group',op='new',manual=True,
        content='禁止广告。',category='rule')['memory_id']
    before=projection(store)
    gateway=Gateway(store,lambda p:{'observations':[{'op':'confirm','memory_id':manual,
        'source_refs':[source(p['messages'][0])]}]})
    run(store,gateway)
    row=profiles(store)[0]
    assert row['manual_protected'] and row['usable'] and row['admission_state']=='manual'
    assert row['sources']==[]
    assert len(row['pending_sources'])==1
    assert projection(store)['text']==before['text'] and projection(store)['version']==before['version']
    assert len(gateway.calls)==1
