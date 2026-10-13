"""Planner review regressions: old snapshots, fresh references, bounded cleanup."""
import json
from datetime import datetime, timezone

import pytest

from conftest import msg
from iris import admin_data, memory_ops
from iris.db import dumps, now
from iris.memory_ops import purge_memory, setup_role, update_role
from test_purge_raw_outputs import insert, learned, record, PROSE
from test_retrieval import put


def derived(store, parent, text='保留的独立推断'):
    mid = put(store, text, kind='自我', about=('self',), speaker='self')
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", (mid,parent,now()))
    return mid


def run_row(conn, mid=0):
    return insert(conn, 'maintenance_runs', trigger='manual', settings_json='{}', timezone='UTC',
                  memory_through=mid, message_through=0, batch_through=0, created_at=now())


def test_fresh_persona_with_purged_trace_reference_keeps_content(store):
    from iris.persona import PersonaEngine, current_persona, select_evidence
    from test_persona import FakeGateway
    setup_role(store, 'Iris')
    parent = put(store, PROSE)
    target = derived(store, parent)
    safe = put(store, '新的独立自我依据', kind='自我')
    purge_memory(store, parent, 1, confirm=True)
    store.set_setting('persona_publish_mode', 'all_auto')
    for _ in range(2):
        material = select_evidence(store)
        item = next(m for m in material['memories'] if m['memory_id'] == target)
        assert any(r['memory_id'] == parent and r['revision'] == 2 for r in item['trace_refs'])
        safe_ref = next(m['ref'] for m in material['memories'] if m['memory_id'] == safe)
        generated = {'sentences': [{'text': '在这次交流中，我提出了独立的推断。', 'basis': [safe_ref]}]}
        result = PersonaEngine(store, FakeGateway(store,generated)).regenerate(expected_version=current_persona(store)['id'])
        assert result['status'] == 'current'
        assert result['version']['content'] == generated['sentences'][0]['text']
        assert current_persona(store)['content'] == result['version']['content']


def test_fresh_dependency_work_annotation_and_penalty_remain_effective(store):
    from iris.consolidation import dependency_material, snapshot
    parent = put(store, PROSE)
    target = derived(store,parent)
    purge_memory(store,parent,1,confirm=True)
    with store.write() as conn:
        material=dependency_material(conn,snapshot(conn,target))
        run=run_row(conn,target)
        work=insert(conn,'consolidation_work',fingerprint='fresh-dependency',kind='dependency',payload_json=dumps(material),importance=50,changed_at=now(),created_at=now())
        annotation=insert(conn,'consolidation_annotations',memory_id=target,memory_revision=1,work_id=work,kind='insufficient_support',text='原依据已不可用，保留待核对标记。',evidence_json='[]',related_ids_json=dumps([parent]),created_at=now())
        detail={'source_memory_id':parent,'before':80,'after':70}
        conn.execute("INSERT INTO maintenance_items(run_id,phase,item_key,memory_id,object_id,outcome,details_json,created_at) VALUES(?,'dependency',?,?,?,'adjusted',?,?)",(run,f'{target}:{parent}',target,target,dumps(detail),now()))
    assert record(store,'consolidation_work',work)['state']=='pending'
    assert json.loads(record(store,'consolidation_work',work)['payload_json'])==material
    assert record(store,'consolidation_annotations',annotation)['cleared_at'] is None
    assert record(store,'consolidation_annotations',annotation)['text']
    with store.read() as conn:
        assert json.loads(conn.execute('SELECT details_json FROM maintenance_items WHERE run_id=?',(run,)).fetchone()[0])==detail


def test_retired_initial_setting_does_not_clear_current_background_or_persona(store):
    from iris.persona import current_persona
    setup_role(store,'Iris','旧背景。')
    update_role(store,'Iris','新背景。','Asia/Shanghai')
    before=current_persona(store)
    purge_memory(store,1,2,confirm=True)
    assert store.setting('background')=='新背景。'
    assert current_persona(store)==before


def test_current_persona_removes_only_dependent_sentences_as_new_version(store):
    from iris.persona import current_persona
    setup_role(store,'Iris')
    parent=put(store,PROSE); child=derived(store,parent); safe=put(store,'保留依据')
    sentences=[{'text':'依赖待清除的依据。','origin':'model','basis':[{'memory_id':child,'revision':1}]},
               {'text':'这句及其依据原样保留。','origin':'model','basis':[{'memory_id':safe,'revision':1}]}]
    with store.write() as conn:
        conn.execute("UPDATE persona_versions SET is_current=0,status='history' WHERE is_current=1")
        old=insert(conn,'persona_versions',content=''.join(s['text'] for s in sentences),created_at=now(),is_current=1,status='current',source='regenerate',sentences_json=dumps(sentences))
    preview=admin_data.memory_detail(store,parent)['purge_preview']
    assert preview['persona']['versions']==1 and preview['persona']['sentences']==1
    purge_memory(store,parent,1,confirm=True)
    got=current_persona(store)
    assert got['id']!=old and got['source']=='purge' and got['author']=='purge'
    assert got['sentences']==sentences[1:] and got['content']==sentences[1]['text']
    assert record(store,'persona_versions',old)['content']==''
    assert record(store,'memories',child)['content']=='保留的独立推断'


def test_persona_call_redaction_does_not_touch_other_purposes_in_same_run(store):
    source,_,mid=learned(store)
    with store.write() as conn:
        run=run_row(conn,mid)
        version=insert(conn,'persona_versions',content=PROSE,created_at=now(),is_current=0,sentences_json=dumps([{'text':PROSE,'basis':[{'memory_id':mid,'revision':1}]}]))
        attempt=insert(conn,'persona_attempts',source='periodic',base_version_id=version,state='running',material_json=dumps({'memory_id':mid,'revision':1}),created_at=now())
        conn.execute('INSERT INTO maintenance_persona(run_id,attempt_id) VALUES(?,?)',(run,attempt))
        calls={purpose:insert(conn,'consolidation_calls',run_id=run,purpose=purpose,raw_output=purpose,created_at=now()) for purpose in ('persona_generate','persona_check','consolidation_pair','consolidation_dependency','consolidation_entry_profile_generate')}
    purge_memory(store,mid,1,confirm=True)
    for purpose,cid in calls.items():
        assert record(store,'consolidation_calls',cid)['raw_output']==(None if purpose.startswith('persona_') else purpose)


def test_late_known_memory_id_does_not_redact_new_recall(store):
    _,_,mid=learned(store)
    purge_memory(store,mid,1,confirm=True)
    with store.write() as conn:
        conn.execute('INSERT INTO recalls VALUES(?,?,?,?)',('fresh','A',dumps({'text':'之后的正常查询','known_memory_ids':[mid]}),now()))
    with store.read() as conn:
        assert json.loads(conn.execute("SELECT request_json FROM recalls WHERE id='fresh'").fetchone()[0])['text']=='之后的正常查询'


def test_protected_message_only_marks_batches_whose_results_name_the_memory(store):
    source,bid,mid=learned(store)
    put(store,'另一条记忆仍保护同一消息',evidence=[source])
    with store.write() as conn:
        other=insert(conn,'batches',entry_id='A',history_ids=dumps([source]),target_ids='[]',future_ids='[]',state='succeeded',prompt_version='test',created_at=now())
        attempt=insert(conn,'batch_attempts',batch_id=other,number=1,started_at=now(),finished_at=now(),parse_status='direct',duration_ms=1,raw_output='独立批次输出')
    result=purge_memory(store,mid,1,confirm=True)
    assert result['retained_messages']
    assert record(store,'batch_attempts',attempt)['raw_output']=='独立批次输出'


def test_backup_notice_is_available_before_single_purge_and_mentions_upgrade_backups(store):
    _,_,mid=learned(store)
    store.set_setting('scheduled_backup',{'keep':4})
    notice=admin_data.memory_detail(store,mid)['purge_preview']['backup_notice']
    assert notice['scheduled_backup_keep']==4 and notice['upgrade_backup_keep']==3
    assert '.bak' in notice['message']


def test_bulk_plans_each_short_chunk_once_and_summary_hook_shares_transaction(store, monkeypatch):
    ids=[put(store,f'批量记忆 {i}') for i in range(19)]
    observed=[]; hooks=[]
    original=memory_ops._purge_linked_outputs
    def track(conn, records, stamp):
        assert conn.in_transaction
        observed.append(len(records))
        return original(conn,records,stamp)
    def summary_hook(conn, chunk):
        assert conn.in_transaction
        hooks.extend(chunk)
    monkeypatch.setattr(memory_ops,'_purge_linked_outputs',track)
    monkeypatch.setattr(memory_ops,'_purge_person_summaries',summary_hook)
    preview=admin_data.bulk_memory_preview(store,subject_id='self',action='purge')
    result=memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)
    assert result['memory_ids']==ids and hooks==ids
    assert len(observed)==3  # 8 + 8 + 3, with one indexed plan per transaction.


def test_partial_purge_result_has_backup_notice_and_exact_receipt(store,monkeypatch):
    from contextlib import contextmanager
    ids=[put(store,'部分清除一'),put(store,'部分清除二')]
    monkeypatch.setattr(memory_ops,'BULK_BATCH_SIZE',1)
    preview=admin_data.bulk_memory_preview(store,subject_id='self',action='purge')
    original=store.write; changed=False
    @contextmanager
    def interleave():
        nonlocal changed
        with original() as conn:
            yield conn
        if not changed and record(store,'memories',ids[0])['purged_at']:
            changed=True
            with original() as conn:
                conn.execute('UPDATE memories SET revision=revision+1 WHERE id=?',(ids[1],))
    monkeypatch.setattr(store,'write',interleave)
    with pytest.raises(memory_ops.BulkMemoryConflict) as exc:
        memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)
    assert exc.value.result['memory_ids']==[ids[0]]
    assert exc.value.result['backup_notice']['upgrade_backup_keep']==3
    assert not record(store,'memories',ids[1])['purged_at']


def test_purge_structure_and_indexed_runtime_predicates(store):
    tables=set(memory_ops._PURGE_TOUCH)
    with store.read() as conn:
        schema={r[0]:r[1] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'purge_%'")}
        expected={f'purge_{table}_{event}' for table in tables for event in ('insert','update','delete')}
        assert expected | {'purge_stale_references','purge_recall_items_insert'} <= schema.keys()
        for table in tables:
            assert 'WHERE CAST(id AS TEXT)' not in schema[f'purge_{table}_update']
        plan=[r[3] for r in conn.execute("EXPLAIN QUERY PLAN SELECT table_name,row_id FROM purge_record_refs WHERE ref_kind='memory' AND ref_id IN ('1','2')")]
        assert any('purge_refs_target' in detail and 'SEARCH' in detail for detail in plan)
        for query,index in (("SELECT recall_id FROM recall_items WHERE memory_id=1",'recall_items_by_memory'),
                            ("SELECT batch_id FROM batch_message_refs WHERE message_id=1",'batch_message_refs_by_message'),
                            ("SELECT id FROM model_calls WHERE batch_id=1",'model_calls_by_batch')):
            assert any(index in r[3] and 'SEARCH' in r[3] for r in conn.execute('EXPLAIN QUERY PLAN '+query))
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_existing_annotation_on_other_memory_keeps_kind_and_effective_state(store):
    parent=put(store,PROSE); other=put(store,'另一个独立对象')
    with store.write() as conn:
        work=insert(conn,'consolidation_work',fingerprint='existing-note',kind='pair',payload_json=dumps({'memories':[{'id':parent,'revision':1,'content':PROSE}]}),importance=50,changed_at=now(),created_at=now())
        note=insert(conn,'consolidation_annotations',memory_id=other,memory_revision=1,work_id=work,text=PROSE,evidence_json='[]',related_ids_json=dumps([parent,other]),kind='insufficient_support',created_at=now())
    purge_memory(store,parent,1,confirm=True)
    got=record(store,'consolidation_annotations',note)
    assert got['text']=='' and got['evidence_json']=='[]'
    assert got['kind']=='insufficient_support' and got['cleared_at'] is None


def test_persona_engine_inflight_output_cannot_repopulate_cleared_attempt(store):
    from iris.persona import PersonaEngine, PersonaConflict, current_persona
    from test_persona import FakeGateway
    setup_role(store,'Iris',PROSE)
    gateway=FakeGateway(store,{'sentences':[{'text':PROSE+'。','basis':['M1']}]},callback=lambda:purge_memory(store,1,1,confirm=True))
    with pytest.raises(PersonaConflict):
        PersonaEngine(store,gateway).regenerate(expected_version=1)
    with store.read() as conn:
        attempt=conn.execute('SELECT * FROM persona_attempts').fetchone()
        assert attempt['outputs_json']=='{}' and attempt['material_json']=='{}'
    assert current_persona(store)['content']=='我是Iris。'


def test_consolidation_engine_inflight_output_and_payload_stay_cleared(store):
    from test_consolidation import Model,pair,merge_answer,run
    first,second=pair(store)
    seen=[]
    def during_call(payload):
        if not seen:
            seen.append(True)
            purge_memory(store,first,1,confirm=True)
    _,_,report=run(store,Model(store,merge_answer,hook=during_call))
    assert seen
    with store.read() as conn:
        calls=conn.execute('SELECT raw_output FROM consolidation_calls WHERE work_id IS NOT NULL').fetchall()
        assert calls and all(r[0] is None for r in calls)
        assert conn.execute("SELECT COUNT(*) FROM consolidation_work WHERE state='obsolete' AND payload_json='{}'").fetchone()[0]
    assert record(store,'memories',second)['lifecycle']=='active'


def test_entry_profile_engine_inflight_attempt_and_source_are_cleared(store):
    from iris.entry_profile import EntryProfileEngine,EntryProfileConflict,current_profile
    from test_entry_profile import Clock,messages,memory,Model
    clock=Clock();mids=messages(store,clock);mid=memory(store,clock,mids[:1],text=PROSE)
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='learned'")
    gateway=Model(store,refs=['M1'],hook=lambda:purge_memory(store,mid,1,confirm=True))
    with pytest.raises(EntryProfileConflict):
        EntryProfileEngine(store,gateway,clock=clock).regenerate('group',expected_version=0)
    with store.read() as conn:
        attempt=conn.execute('SELECT * FROM entry_profile_attempts').fetchone()
        assert attempt['outputs_json']=='{}' and attempt['material_json']=='{}'
        assert not conn.execute('SELECT 1 FROM messages WHERE id=?',(mids[0],)).fetchone()
    assert current_profile(store,'group') is None


def test_image_understanding_late_write_after_source_deletion_stays_empty(store):
    from iris.media import prepare_media,get_media
    from test_media import saved,send,Vision
    item=saved(store);source=send(store,[item['id']]);mid=put(store,PROSE,evidence=[source])
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='learned' WHERE id=?",(source,))
    vision=Vision(PROSE,hook=lambda:purge_memory(store,mid,1,confirm=True))
    prepare_media(store,vision,[source])
    assert vision.images
    assert get_media(store,item['id'])['understanding_text']==''
    with store.read() as conn:
        assert not conn.execute('SELECT 1 FROM messages WHERE id=?',(source,)).fetchone()


def test_current_profile_selective_version_and_empty_regeneration_flag(store):
    from iris import entry_profile
    from test_entry_profile import Clock,messages,memory
    clock=Clock();mids=messages(store,clock)
    private=memory(store,clock,mids[:1],text=PROSE,importance=90)
    safe=memory(store,clock,mids[1:2],text='保留依据',importance=50)
    material=entry_profile.select_material(store,'group',clock=clock)
    indexed={m['memory_id']:m for m in material['memories']}
    sentences=[{'text':'要删除的条目','author':'model','basis':[{k:indexed[private][k] for k in ('memory_id','revision','sha256')}]},
               {'text':'保留的条目','author':'model','basis':[{k:indexed[safe][k] for k in ('memory_id','revision','sha256')}]}]
    with store.write() as conn:
        settings=entry_profile._ensure(conn,'group',now())
        entry_profile._insert(conn,'group',previous=0,content='\n'.join(s['text'] for s in sentences),sentences=sentences,
            checks={'passed':True,'format_version':2},degree='small',status='published',stamp=now(),author='model',source='regenerate',
            material={'evidence':material},message_through=max(mids),settings_revision=settings['revision'])
    preview=admin_data.memory_detail(store,private)['purge_preview']
    assert preview['entry_profiles']=={'versions':1,'sentences':1,'retained_unbased_sentences':0}
    purge_memory(store,private,1,confirm=True)
    current=entry_profile.current_profile(store,'group')
    assert current['version']==2 and current['author']==current['source']=='purge'
    assert current['sentences']==sentences[1:] and current['content']=='保留的条目'
    assert current['checks']['format_version']==2
    assert admin_data.entry_profile_snapshot(store,'group')['current']['format_version']==2
    purge_memory(store,safe,1,confirm=True)
    current=entry_profile.current_profile(store,'group')
    assert current['version']==3 and current['content']==''
    assert current['checks']['needs_regeneration'] and current['checks']['reason']=='需要重新生成'


def test_read_projections_explain_redaction_without_original_outputs(store):
    from iris.media import get_media
    from test_media import saved,send
    source,bid,mid=learned(store)
    with store.write() as conn:
        call=insert(conn,'model_calls',purpose='learning',model='fake',duration_ms=1,result_category='success',created_at=now(),batch_id=bid,error_summary=PROSE)
    purge_memory(store,mid,1,confirm=True)
    detail=admin_data.batch_detail(store,bid)
    assert detail['redacted'] and detail['redaction_reason']==memory_ops.PURGE_REASON
    assert detail['attempts'][0]['redacted']
    calls=[*detail['unassigned_calls'],*[c for a in detail['attempts'] for c in a['calls']]]
    assert next(c for c in calls if c['id']==call)['redacted']
    item=saved(store);message=send(store,[item['id']]);memory=put(store,PROSE,evidence=[message])
    purge_memory(store,memory,1,confirm=True)
    projected=admin_data.media_projection(get_media(store,item['id']),store=store)
    assert projected['redacted'] and projected['redaction_reason']==memory_ops.PURGE_REASON


def before_purge_store(path, monkeypatch):
    from importlib.resources import files
    from types import SimpleNamespace
    from iris.db import Store
    root=files('iris')
    class Resources:
        def joinpath(self,name):
            return (SimpleNamespace(iterdir=lambda:[p for p in root.joinpath(name).iterdir()
                    if p.name < '029']) if name=='migrations' else root.joinpath(name))
    with monkeypatch.context() as patch:
        patch.setattr('iris.db.files',lambda _:Resources())
        return Store(path)


def test_upgrade_does_not_erase_post_purge_references_or_dependency_details(tmp_path,monkeypatch):
    from iris.db import Store
    from iris.consolidation import snapshot,dependency_material
    path=tmp_path/'before-upgrade.db';store=before_purge_store(path,monkeypatch)
    parent=put(store,PROSE);target=derived(store,parent)
    with store.write() as conn:
        conn.execute("UPDATE memories SET content='',lifecycle='deleted',purged_at=?,revision=2 WHERE id=?",(now(),parent))
        payload=dependency_material(conn,snapshot(conn,target))
        run=run_row(conn,target)
        work=insert(conn,'consolidation_work',fingerprint='upgrade-fresh',kind='dependency',payload_json=dumps(payload),importance=50,changed_at=now(),created_at=now())
        annotation=insert(conn,'consolidation_annotations',memory_id=target,memory_revision=1,work_id=work,kind='insufficient_support',text='正常的新复核结论',evidence_json='[]',related_ids_json=dumps([parent]),created_at=now())
        persona=insert(conn,'persona_versions',is_current=1,status='current',content='正常新版本',created_at=now(),source='regenerate',material_json=dumps({'evidence':{'memories':[{'memory_id':target,'revision':1,'trace_refs':[{'memory_id':parent,'revision':2}]}]}}))
        conn.execute("INSERT INTO maintenance_items(run_id,phase,item_key,memory_id,object_id,outcome,details_json,created_at) VALUES(?,'dependency','loss',?,?,'adjusted',?,?)",(run,target,target,dumps({'source_memory_id':parent,'before':80,'after':70}),now()))
    store.close();store=Store(path)
    try:
        assert record(store,'consolidation_work',work)['state']=='pending'
        assert record(store,'consolidation_work',work)['payload_json']==dumps(payload)
        assert record(store,'consolidation_annotations',annotation)['text']=='正常的新复核结论'
        assert record(store,'persona_versions',persona)['content']=='正常新版本'
        with store.read() as conn:
            assert json.loads(conn.execute('SELECT details_json FROM maintenance_items').fetchone()[0])['before']==80
            assert conn.execute('SELECT COUNT(*) FROM purge_redactions').fetchone()[0]==0
    finally:
        store.close()


def test_upgrade_selectively_preserves_current_persona_and_clears_old_background(tmp_path,monkeypatch):
    from iris.db import Store
    from iris.persona import current_persona
    path=tmp_path/'current-upgrade.db';store=before_purge_store(path,monkeypatch)
    setup_role(store,'Iris',PROSE)
    with store.write() as conn:
        conn.execute("UPDATE memories SET content='',lifecycle='deleted',purged_at=?,revision=2 WHERE id=1",(now(),))
        conn.execute('DELETE FROM sources WHERE memory_id=1')
    store.close();store=Store(path)
    try:
        current=current_persona(store)
        assert current['source']=='purge' and current['content']=='我是Iris。'
        assert PROSE not in dumps(current)
        assert store.setting('background')==''
        assert record(store,'persona_versions',1)['content']==''
    finally:
        store.close()


def test_small_scale_unrelated_history_is_not_visited_or_cleared(store):
    source,bid,mid=learned(store)
    with store.write() as conn:
        conn.executemany('''INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,prompt_version,created_at)
            VALUES('A','[]','[]','[]','succeeded','scale',?)''',[(now(),)]*300)
        batches=[r[0] for r in conn.execute('SELECT id FROM batches WHERE id!=?',(bid,))]
        conn.executemany('''INSERT INTO model_calls(purpose,model,duration_ms,result_category,created_at,batch_id,error_summary)
            VALUES('learning','fake',1,'success',?,?,'unrelated')''',[(now(),batches[i%300]) for i in range(3000)])
    result=purge_memory(store,mid,1,confirm=True)
    assert result['redaction_counts'].get('model_calls',0)==0
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE error_summary='unrelated'").fetchone()[0]==3000
        details=[r[3] for r in conn.execute("EXPLAIN QUERY PLAN UPDATE model_calls SET error_summary=error_summary WHERE id IN (SELECT value FROM json_each('[1,2]'))")]
        assert any('SEARCH model_calls USING INTEGER PRIMARY KEY' in detail for detail in details)
        assert not any('SCAN model_calls' in detail for detail in details)


def test_normal_dependency_review_after_purge_calls_model_and_is_not_repeated(store):
    from test_consolidation import Model, run
    from iris.consolidation import snapshot
    parent=put(store,'待清除的厨房依据',importance=80)
    target=put(store,'我可能经常烘焙',stance='推断',importance=80)
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",(target,parent,now()))
    purge_memory(store,parent,1,confirm=True)
    answer={'decision':'review','reason':'原依据已清除，当前依据不足','evidence':[],
            'updates':[{'id':target,'annotation':'原依据已清除，保留待核对标记'}]}
    model=Model(store,answer)
    run(store,model)
    assert any('target_id' in payload for _,payload in model.requests)
    with store.read() as conn:
        got=snapshot(conn,target)
        assert got['annotations'] and got['retention']==40
        work=conn.execute("SELECT * FROM consolidation_work WHERE kind='dependency'").fetchone()
        assert work['state']=='done' and work['payload_json']!='{}'
    calls=len(model.requests)
    run(store,model)
    assert len(model.requests)==calls


def test_interrupted_bulk_purge_returns_committed_ids_and_can_resume(store,monkeypatch):
    ids=[put(store,'中断前已提交'),put(store,'中断后可续跑')]
    monkeypatch.setattr(memory_ops,'BULK_BATCH_SIZE',1)
    original=memory_ops._purge_memories
    def interrupt(conn, expected):
        if ids[1] in expected:
            raise RuntimeError('simulated interruption')
        return original(conn,expected)
    preview=admin_data.bulk_memory_preview(store,subject_id='self',action='purge')
    with monkeypatch.context() as patch:
        patch.setattr(memory_ops,'_purge_memories',interrupt)
        with pytest.raises(memory_ops.BulkMemoryConflict) as exc:
            memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)
    receipt=exc.value.result
    assert receipt['status']=='interrupted' and receipt['memory_ids']==[ids[0]]
    assert receipt['backup_notice']['upgrade_backup_keep']==3
    preview=admin_data.bulk_memory_preview(store,subject_id='self',action='purge')
    assert preview['total']==1
    done=memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)
    assert done['memory_ids']==[ids[1]]
    assert memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)==done


def test_maintenance_read_projection_marks_cleared_calls(store):
    _,_,mid=learned(store)
    with store.write() as conn:
        rid=run_row(conn,mid)
        work=insert(conn,'consolidation_work',fingerprint='read-projection',kind='dependency',payload_json=dumps({'memories':[{'id':mid,'revision':1,'content':PROSE}]}),importance=50,changed_at=now(),created_at=now())
        call=insert(conn,'consolidation_calls',run_id=rid,work_id=work,purpose='consolidation_dependency',raw_output=PROSE,created_at=now())
    purge_memory(store,mid,1,confirm=True)
    report=admin_data.maintenance_redactions(store,{'id':rid,'items':[]})
    assert report['consolidation_calls'][0]['id']==call
    assert report['consolidation_calls'][0]['redacted']
    assert report['consolidation_calls'][0]['redaction_reason']==memory_ops.PURGE_REASON


def test_bulk_purge_does_not_leave_marker_for_deleted_source_row(store):
    parent=put(store,'同批清除的依据')
    child=put(store,'同批清除的派生记忆')
    with store.write() as conn:
        source=conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,note,created_at) VALUES(?,'memory',?,1,'旧来源说明',?)",(child,parent,now())).lastrowid
    preview=admin_data.bulk_memory_preview(store,subject_id='self',action='purge')
    memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)
    safe=put(store,'后来创建的独立记忆')
    with store.write() as conn:
        assert not conn.execute("SELECT 1 FROM purge_redactions WHERE table_name='sources' AND row_id=?",(str(source),)).fetchone()
        conn.execute("INSERT INTO sources(id,memory_id,kind,source_memory_id,source_revision,note,created_at) VALUES(?,?,'memory',?,1,'新来源说明',?)",(source,safe,safe,now()))
        assert conn.execute('SELECT note FROM sources WHERE id=?',(source,)).fetchone()[0]=='新来源说明'


@pytest.mark.parametrize('family',['persona','entry_profile'])
def test_current_version_with_only_unused_cleared_material_keeps_all_sentences(store,family):
    from iris.persona import current_persona
    from iris import entry_profile
    setup_role(store,'Iris')
    private=put(store,PROSE);safe=put(store,'独立的保留依据')
    sentence={'text':'这句只依据保留的记忆。','basis':[{'memory_id':safe,'revision':1}]}
    material={'evidence':{'memories':[{'memory_id':private,'revision':1,'content':PROSE},
                                     {'memory_id':safe,'revision':1,'content':'独立的保留依据'}]}}
    with store.write() as conn:
        if family=='persona':
            conn.execute("UPDATE persona_versions SET is_current=0,status='history' WHERE is_current=1")
            old=insert(conn,'persona_versions',content=sentence['text'],created_at=now(),is_current=1,status='current',source='regenerate',sentences_json=dumps([sentence]),material_json=dumps(material))
        else:
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('group','入口','test','group')")
            setting=entry_profile._ensure(conn,'group',now())
            row=conn.execute('SELECT * FROM memories WHERE id=?',(safe,)).fetchone()
            sentence['basis'][0]['sha256']=entry_profile._memory_hash(row,entry_profile._direct_sources(conn,safe,'group'))
            entry_profile._insert(conn,'group',previous=0,content=sentence['text'],sentences=[sentence],checks={'passed':True},degree='small',status='published',stamp=now(),author='model',source='regenerate',material=material,message_through=0,settings_revision=setting['revision'])
            old=conn.execute('SELECT id FROM entry_profile_versions').fetchone()[0]
    preview=admin_data.memory_detail(store,private)['purge_preview']
    assert preview['persona' if family=='persona' else 'entry_profiles']=={'versions':1,'sentences':0,'retained_unbased_sentences':0}
    purge_memory(store,private,1,confirm=True)
    current=current_persona(store) if family=='persona' else entry_profile.current_profile(store,'group')
    assert current['content']==sentence['text'] and current['sentences']==[sentence]
    assert current['source']=='purge' and PROSE not in dumps(current)
    assert record(store,family+'_versions',old)['content']==''


def test_write_failure_after_first_chunk_preserves_receipt_when_audit_is_unavailable(store,monkeypatch):
    from contextlib import contextmanager
    import sqlite3
    ids=[put(store,'写故障前已提交'),put(store,'写故障后未提交')]
    monkeypatch.setattr(memory_ops,'BULK_BATCH_SIZE',1)
    preview=admin_data.bulk_memory_preview(store,subject_id='self',action='purge')
    original=store.write;writes=0
    @contextmanager
    def unavailable():
        nonlocal writes
        writes+=1
        if writes>1:
            raise sqlite3.OperationalError('simulated write failure')
        with original() as conn:
            yield conn
    with monkeypatch.context() as patch:
        patch.setattr(store,'write',unavailable)
        with pytest.raises(memory_ops.BulkMemoryConflict) as exc:
            memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)
    assert exc.value.result['memory_ids']==[ids[0]]
    assert exc.value.result['status']=='interrupted'
    assert exc.value.result['backup_notice']['upgrade_backup_keep']==3
    assert record(store,'memories',ids[0])['purged_at']
    assert not record(store,'memories',ids[1])['purged_at']
    with store.read() as conn:
        audit=json.loads(conn.execute("SELECT details_json FROM admin_operations WHERE action='memory_bulk_purge'").fetchone()[0])
        assert audit['memory_ids']==[ids[0]] and 'backup_notice' not in audit


@pytest.mark.parametrize('source,author',[('initial_setting','admin'),('admin_edit','admin'),('rollback','admin'),
                                         ('regenerate','model'),('periodic','model'),('purge','purge')])
def test_persona_author_metadata_matches_source(store,source,author):
    with store.write() as conn:
        vid=insert(conn,'persona_versions',content='独立正文。',created_at=now(),is_current=0,source=source)
        assert conn.execute('SELECT author FROM persona_versions WHERE id=?',(vid,)).fetchone()[0]==author


def test_profile_format_survives_purge_upgrade(tmp_path,monkeypatch):
    from iris.db import Store
    from iris import entry_profile
    path=tmp_path/'profile-upgrade.db';store=before_purge_store(path,monkeypatch)
    private=put(store,PROSE);safe=put(store,'保留的画像依据')
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('group','入口','test','group')")
        setting=entry_profile._ensure(conn,'group',now())
        sentences=[]
        for mid,text in ((private,'清除的条目'),(safe,'保留的条目')):
            row=conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
            basis={'memory_id':mid,'revision':1,'sha256':entry_profile._memory_hash(row,[])}
            sentences.append({'text':text,'author':'model','basis':[basis],'format_version':2})
        entry_profile._insert(conn,'group',previous=0,content='清除的条目；保留的条目',sentences=sentences,
            checks={'passed':True,'format_version':2},degree='small',status='published',stamp=now(),author='model',
            source='regenerate',material={},message_through=0,settings_revision=setting['revision'])
        conn.execute("UPDATE memories SET content='',lifecycle='deleted',purged_at=?,revision=2 WHERE id=?",(now(),private))
    store.close();store=Store(path)
    try:
        current=entry_profile.current_profile(store,'group')
        assert current['source']=='purge' and current['sentences']==sentences[1:]
        assert current['checks']['format_version']==2
        assert admin_data.entry_profile_snapshot(store,'group')['current']['format_version']==2
    finally:
        store.close()


@pytest.mark.parametrize('upgrade',[False,True])
def test_profile_purge_keeps_item_text_without_adding_line_breaks(tmp_path,monkeypatch,upgrade):
    from iris.db import Store
    from iris import entry_profile
    path=tmp_path/'line-breaks.db'
    store=before_purge_store(path,monkeypatch) if upgrade else Store(path)
    try:
        private=put(store,PROSE);safe=put(store,'其余画像依据保持原样')
        with store.write() as conn:
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('group','入口','test','group')")
            setting=entry_profile._ensure(conn,'group',now())
            sentences=[]
            for mid,text in ((private,'被删除条目。\n'),(safe,'第一条保留文字。\n'),(safe,'第二条保留文字。\n')):
                row=conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
                sentences.append({'text':text,'author':'model','format_version':2,
                    'basis':[{'memory_id':mid,'revision':1,'sha256':entry_profile._memory_hash(row,[])}]})
            entry_profile._insert(conn,'group',previous=0,content=''.join(s['text'] for s in sentences),sentences=sentences,
                checks={'passed':True,'format_version':2},degree='small',status='published',stamp=now(),author='model',
                source='regenerate',material={},message_through=0,settings_revision=setting['revision'])
            if upgrade:
                conn.execute("UPDATE memories SET content='',lifecycle='deleted',purged_at=?,revision=2 WHERE id=?",(now(),private))
        if upgrade:
            store.close();store=Store(path)
        else:
            purge_memory(store,private,1,confirm=True)
        current=entry_profile.current_profile(store,'group')
        assert current['sentences']==sentences[1:]
        assert current['content']=='第一条保留文字。\n第二条保留文字。\n'
    finally:
        store.close()


def test_upgrade_clears_pre_hook_person_summary_copies(tmp_path,monkeypatch):
    from iris.db import Store
    from test_person_summary import Clock,seed,generate,current_summary
    path=tmp_path/'summary-upgrade.db';store=before_purge_store(path,monkeypatch)
    clock=Clock();mids=seed(store,clock);generate(store,clock)
    with store.write() as conn:
        run=run_row(conn,mids[-1])
        conn.execute('UPDATE person_summary_attempts SET run_id=?',(run,))
        summary_call=insert(conn,'consolidation_calls',run_id=run,purpose='consolidation_person_summary_generate',raw_output=PROSE,created_at=now())
        other_call=insert(conn,'consolidation_calls',run_id=run,purpose='consolidation_dependency',raw_output='独立的依赖建议',created_at=now())
        conn.execute("UPDATE memories SET content='',lifecycle='deleted',purged_at=?,revision=2 WHERE id=?",(now(),mids[0]))
    store.close();store=Store(path)
    try:
        value=current_summary(store,'a')
        assert value['content']=='' and value['material']=={}
        assert value['sentences'][0]['kind']=='fact'
        assert value['sentences'][0]['basis'][0]=={'memory_id':mids[0],'revision':1}
        assert value['checks']['cleared_reason']==memory_ops.PURGE_REASON
        with store.read() as conn:
            row=conn.execute('SELECT * FROM person_summary_attempts').fetchone()
            assert row['material_json']==row['outputs_json']=='{}' and row['cleared_at']
            assert conn.execute("SELECT needs_regeneration FROM person_summary_settings WHERE subject_id='a'").fetchone()[0]==1
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert record(store,'consolidation_calls',summary_call)['raw_output'] is None
        assert record(store,'consolidation_calls',other_call)['raw_output']=='独立的依赖建议'
    finally:
        store.close()
