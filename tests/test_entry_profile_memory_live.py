"""Production adapter/projection contracts; no live providers."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from iris import entry_profile as ep
from iris.db import dumps
from iris.memory_ops import write_entry_profile_memory, edit_memory, manage_memory, purge_memory
from iris.queue import add_message
from iris.retrieval import Retrieval

CLOCK = lambda: datetime(2026, 10, 14, tzinfo=timezone.utc)


def message(store, text='本群长期禁止广告。', key='one', at='2026-10-01T12:00:00+08:00'):
    mid=add_message(store, entry_id='group', entry_name='测试群', platform='test', entry_kind='group',
        kind='message', sender='测试者', account_id='person', content=text, occurred_at=at, dedupe_key=key)
    with store.write() as conn:
        conn.execute('UPDATE messages SET received_at=? WHERE id=?',(at,mid))
    return mid


def manual(store, content='禁止广告。', category='rule'):
    message(store)
    return write_entry_profile_memory(store, 'group', op='new', content=content, category=category,
        manual=True, current=CLOCK())['memory_id']


def context(store, **kwargs):
    with store.read() as conn:
        return ep.profile_context(conn, 'group', **kwargs)


def test_projection_changes_once_per_transaction_and_reads_do_not_write(store):
    mid = manual(store)
    first = context(store)
    assert first is not None and first['memory_ids'] == [mid]
    with store.write() as conn:
        conn.execute("UPDATE memories SET content='禁止广告和刷屏。',revision=revision+1 WHERE id=?", (mid,))
        conn.execute("UPDATE memories SET content='禁止广告及刷屏。' WHERE id=?", (mid,))
    second = context(store)
    assert second['version'] == first['version'] + 1
    assert '禁止广告及刷屏。' in second['text']
    with store.read() as conn:
        before = conn.execute('SELECT * FROM entry_profile_memory_state').fetchall()
    for _ in range(3):
        assert context(store) == second
    with store.read() as conn:
        assert [tuple(r) for r in conn.execute('SELECT * FROM entry_profile_memory_state')] == [tuple(r) for r in before]
    assert manage_memory(store, mid, 2, retention=90)
    assert context(store)['version'] == second['version']


def test_projection_lifecycle_known_ids_and_clear_are_consistent(store):
    mid = manual(store)
    first = context(store)
    assert first is not None
    assert context(store, known_memory_ids=[mid]) is None
    assert manage_memory(store, mid, 1, action='forget')
    assert context(store) is None
    assert manage_memory(store, mid, 1, action='restore')
    assert context(store)['version'] == first['version'] + 2
    assert purge_memory(store, mid, 1, confirm=True)
    assert context(store) is None
    with store.read() as conn:
        assert '禁止广告' not in dumps([dict(r) for r in conn.execute('SELECT * FROM entry_profile_memory_state')])


def test_new_manual_profile_preserves_unknown_category_and_body(store):
    mid = manual(store, '管理员留下的\n未分类说明。', None)
    value = context(store)
    assert value is not None and value['memory_ids'] == [mid]
    assert value['text'] == '管理员说明：管理员留下的 未分类说明。'


class RuleGateway:
    def __init__(self, store, hook=None):
        self.store, self.hook, self.calls = store, hook, []

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[-1]['content'])
        self.calls.append((purpose, payload))
        if purpose.endswith('_observe'):
            source=payload['messages'][0]
            output={'observations':[{'op':'propose','category':'rule','body':'禁止广告。',
                'saying':'','meaning':'','definition_refs':[],
                'source_refs':[{'message_id':source['message_id'],'field':'content','excerpt':source['content']}]}]}
        elif purpose.endswith('_admit'):
            output={'decision':'accept','violations':[],'reason':'明确的长期群规',
                'safe_sources':[{k:s[k] for k in ('message_id','field','excerpt')} for s in payload['memory']['sources']]}
            if self.hook: self.hook()
        elif purpose.endswith('_generate'):
            source = payload['messages'][0]
            output = {'operations': [{'op':'new', 'category':'rule', 'body':'禁止广告。',
                'saying':'', 'meaning':'', 'importance':69, 'sources':[
                    {'ref':source['ref'], 'field':'content', 'excerpt':source['content'], 'via_memory':None}]}]}
        else:
            output = {'operations':[{'index':op['index'], 'supported':True, 'stable':True,
                'independent':True, 'independent_from_previous':True, 'privacy_safe':True,
                'meaning_supported':True, 'meaning_checks':[], 'evidence_groups':[[0]],
                'violations':[], 'reason':'明确的长期群规'} for op in payload['operations']]}
            if self.hook:
                self.hook()
        return SimpleNamespace(content=dumps(output), finish_reason='stop', usage={})


def test_default_engine_uses_memory_writer_and_durable_cursor(store):
    message(store)
    gateway = RuleGateway(store)
    engine = ep.EntryProfileEngine(store, gateway, clock=CLOCK)
    result = engine.regenerate('group', expected_version=0)
    assert result['status'] == 'published', result
    profile = context(store)
    assert profile and len(profile['memory_ids']) == 1
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM entry_profile_versions').fetchone()[0] == 0
        row = conn.execute('SELECT * FROM memories WHERE id=?', (profile['memory_ids'][0],)).fetchone()
        assert row['importance'] == 50 and not row['pinned']
        assert conn.execute('SELECT cursor FROM entry_profile_memory_state').fetchone()[0] > 0
    again = engine.regenerate('group', expected_version=profile['version'])
    assert again['status'] == 'skipped'
    assert len(gateway.calls) == 2
    assert Retrieval(store).prepare('group', text='', judge=False)['entry_profile'] == profile


def test_mutated_source_blocks_admission_but_preserves_observation_commit(store):
    message(store)
    def mutate():
        with store.write() as conn:
            conn.execute("UPDATE messages SET content='本条已被编辑。' WHERE entry_id='group'")
    result = ep.EntryProfileEngine(store, RuleGateway(store, mutate), clock=CLOCK).regenerate('group', expected_version=0)
    assert result['status'] == 'published' and result['reason']=='check_source_changed', result
    assert context(store) is None
    with store.read() as conn:
        assert conn.execute('SELECT cursor FROM entry_profile_memory_state').fetchone()[0] > 0
        assert conn.execute("SELECT importance FROM memories WHERE kind='入口画像'").fetchone()[0] == 30


def test_legacy_write_functions_are_retired(store):
    message(store)
    with pytest.raises(ep.EntryProfileRetired):
        ep.admin_edit(store, 'group', '旧整版编辑。' * 30, expected_version=0)


def test_lost_response_plan_replay_and_restart_do_not_confirm_twice(store):
    from iris.db import Store
    message(store)
    repo=ep.SQLiteMemoryProfileRepository(store,clock=CLOCK)
    saved=[]; original=repo.commit
    def capture(plan):
        saved.append(plan)
        return original(plan)
    repo.commit=capture
    result=ep.EntryProfileMemoryTask(repo,RuleGateway(store)).run('group')
    assert result['status']=='committed'
    mid=result['receipt']['applied'][0]['memory_id']
    with store.read() as conn:
        before=dict(conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone())
    assert original(saved[0])['replayed'] is True
    reopened=Store(store.path)
    try:
        assert ep.SQLiteMemoryProfileRepository(reopened,clock=CLOCK).commit(saved[0])['replayed'] is True
        with reopened.read() as conn:
            assert dict(conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone())==before
    finally:
        reopened.close()


def test_atomic_projection_rolls_back_with_memory_mutation(store):
    mid=manual(store); before=context(store)
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            conn.execute("UPDATE memories SET content='这次必须回滚。' WHERE id=?",(mid,))
            raise RuntimeError('rollback')
    assert context(store)==before


def test_invalid_current_evidence_withdraws_projection_without_read_mutation(store):
    message(store)
    result=ep.EntryProfileEngine(store,RuleGateway(store),clock=CLOCK).regenerate('group',expected_version=0)
    assert result['status']=='published'
    before=context(store)
    with store.write() as conn:
        conn.execute("UPDATE messages SET content='已修改成不相关材料。' WHERE entry_id='group'")
    assert context(store) is None
    with store.read() as conn:
        assert ep._memory_state(conn,'group')['version']==before['version']+1


def test_handwritten_rules_are_not_cut_by_model_projection_budget(store):
    first=manual(store, '人工规则一。')
    ids=[first]
    for text in ['人工规则二。','人工规则三。','人工规则四。','人工规则五。','人工规则六。','人工规则七。']:
        ids.append(write_entry_profile_memory(store,'group',op='new',content=text,category='rule',manual=True)['memory_id'])
    assert context(store)['memory_ids']==ids


def test_real_attempt_and_projection_are_redacted_by_purge(store):
    message(store)
    result=ep.EntryProfileEngine(store,RuleGateway(store),clock=CLOCK).regenerate('group',expected_version=0)
    mid=context(store)['memory_ids'][0]
    purge_memory(store,mid,2,confirm=True)
    assert context(store) is None
    with store.read() as conn:
        attempt=conn.execute('SELECT * FROM entry_profile_attempts WHERE id=?',(result['attempt_id'],)).fetchone()
        assert attempt['outputs_json']=='{}' and attempt['material_json']=='{}'


def test_memory_task_runs_in_existing_maintenance_schedule_with_two_call_budget(store):
    from iris.maintenance import Maintenance
    message(store)
    store.set_setting('consolidation',{'enabled':True,'merge_enabled':False,'conflict_enabled':False,'dependency_enabled':False,'persona_enabled':False,'goal_review_enabled':False,'entry_profile_enabled':True,'person_summary_enabled':False,'max_calls':2})
    gateway=RuleGateway(store)
    maintenance=Maintenance(store,gateway=gateway,clock=CLOCK)
    run=maintenance.request()
    maintenance.run(run)
    assert context(store) is not None
    assert len(gateway.calls)==2
    maintenance.run(run)
    assert len(gateway.calls)==2


def test_future_message_is_not_permanently_skipped_by_cursor(store):
    message(store,at='2027-01-01T12:00:00+08:00')
    gateway=RuleGateway(store)
    ep.EntryProfileEngine(store,gateway,clock=CLOCK).regenerate('group',expected_version=0)
    with store.read() as conn:
        assert ep._memory_state(conn,'group')['cursor']==0
    assert not gateway.calls


def test_timezone_change_during_check_invalidates_date_based_snapshot(store):
    message(store)
    gateway=RuleGateway(store,lambda:store.set_setting('timezone','UTC'))
    result=ep.EntryProfileEngine(store,gateway,clock=CLOCK).regenerate('group',expected_version=0)
    assert result['status']=='published' and result['reason']=='check_snapshot_changed'
    assert context(store) is None


def test_indirect_topic_sources_remain_normalized_in_persisted_projection(store):
    from iris.memory_ops import entry_profile_source
    from test_retrieval import put
    a=message(store,'讨论徒步装备的背包选型。',key='a')
    b=message(store,'雨天行走的鞋子怎么挑？',key='b',at='2026-10-05T12:00:00+08:00')
    fact=put(store,'讨论徒步装备。',evidence=[a,b])
    with store.read() as conn:
        sources=[entry_profile_source(conn,x,source_memory_id=fact,source_revision=1) for x in (a,b)]
    result=write_entry_profile_memory(store,'group',op='new',category='topic',content='常聊徒步装备。',sources=sources)
    admit_existing(store)
    assert context(store)['memory_ids']==[result['memory_id']]
    with store.read() as conn:
        rows=ep.memory_profile_rows(conn,'group',as_of=CLOCK().isoformat())
        assert [s['message']['date'] for s in rows[0]['sources']]==['2026-10-01','2026-10-05']
    assert edit_memory(store,fact,1,content='材料已修订。')
    assert context(store) is None


def test_default_engine_reports_actual_memory_prompts_and_refuses_legacy_override(store):
    engine=ep.EntryProfileEngine(store,None)
    assert engine.prompt_metadata['prompt_versions']==['entry_profile_observe_v1','entry_profile_admit_v1']
    with pytest.raises(ep.EntryProfileRetired):
        ep.EntryProfileEngine(store,None,prompt_version='v2')


def test_timezone_change_rechecks_cross_day_sources_in_same_write(store):
    from iris.memory_ops import entry_profile_source
    a=message(store,'背包肩带怎么选？',key='near-midnight',at='2026-10-01T23:00:00+08:00')
    b=message(store,'雨天徒步穿什么鞋？',key='after-midnight',at='2026-10-02T00:00:00+08:00')
    with store.read() as conn:
        sources=[entry_profile_source(conn,x) for x in (a,b)]
    write_entry_profile_memory(store,'group',op='new',category='topic',content='常聊徒步装备。',sources=sources)
    admit_existing(store)
    before=context(store)
    assert before is not None
    store.set_setting('timezone','UTC')
    assert context(store) is None
    with store.read() as conn:
        assert ep._memory_state(conn,'group')['version']==before['version']+1


def test_attempt_persists_generation_before_the_check_call(store):
    from iris.admin_data import entry_profile_snapshot
    message(store)
    observed=[]
    def during_check():
        latest=entry_profile_snapshot(store,'group')['latest_attempt']
        observed.append(latest['stage'])
        assert latest['stage']=='checking'
    result=ep.EntryProfileEngine(store,RuleGateway(store,during_check),clock=CLOCK).regenerate('group',expected_version=0)
    assert result['status']=='published' and observed==['checking']


def admit_existing(store):
    class AdmissionGateway(RuleGateway):
        def chat(self,messages,purpose,**kwargs):
            if purpose.endswith('_observe'):
                return SimpleNamespace(content='{"observations":[]}',finish_reason='stop',usage={})
            return super().chat(messages,purpose,**kwargs)
    ep.EntryProfileEngine(store,AdmissionGateway(store),clock=CLOCK).regenerate('group',expected_version=0)
