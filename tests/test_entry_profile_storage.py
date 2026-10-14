"""Real SQLite foundation; no model calls or production task switch."""
import sqlite3

import pytest

from iris import memory_ops
from iris.db import Store, now
from conftest import msg


def row(store, mid):
    with store.read() as conn:
        return dict(conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone())


def source(store, mid, excerpt=None, **kwargs):
    with store.read() as conn:
        return memory_ops.entry_profile_source(conn,mid,excerpt=excerpt,**kwargs)


def write(store, **kwargs):
    return memory_ops.write_entry_profile_memory(store,'A',**kwargs)


def test_entry_subject_is_stable_unique_and_tracks_name(store):
    msg(store,1,'hello')
    with store.write() as conn:
        sid=conn.execute('SELECT subject_id FROM entry_subjects WHERE entry_id=?',('A',)).fetchone()[0]
        assert conn.execute('SELECT kind FROM subjects WHERE id=?',(sid,)).fetchone()[0]=='entry'
        conn.execute("UPDATE entries SET name='新名字' WHERE id='A'")
        assert conn.execute('SELECT name FROM subjects WHERE id=?',(sid,)).fetchone()[0]=='新名字'
        assert conn.execute("SELECT subject_id FROM entry_subjects WHERE entry_id='A'").fetchone()[0]==sid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE entry_subjects SET subject_id='self' WHERE entry_id='A'")
    reopened=Store(store.path)
    try:
        with reopened.read() as conn:
            assert conn.execute("SELECT subject_id FROM entry_subjects WHERE entry_id='A'").fetchone()[0]==sid
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.close()


def test_writer_cap_identity_scope_and_replay_receipt(store):
    a=msg(store,1,'本群讨论徒步装备')
    b=msg(store,2,'常来交流轻量背包选择',at='2026-09-30T09:00:00+08:00')
    result=write(store,op='new',category='topic',content='常讨论徒步装备取舍',importance=100,sources=[source(store,a),source(store,b)])
    value=row(store,result['memory_id'])
    assert (value['kind'],value['speaker_subject_id'],value['stance'],value['importance'],value['event_time'],value['owner_entry_id'])==('入口画像','self','推断',69,None,'A')
    with store.read() as conn:
        assert [r[0] for r in conn.execute('SELECT subject_id FROM memory_subjects WHERE memory_id=?',(value['id'],))]==[conn.execute("SELECT subject_id FROM entry_subjects WHERE entry_id='A'").fetchone()[0]]
        visibility=memory_ops.Visibility(conn)
        assert visibility.memory(value['id'])==frozenset({'A'})
        assert not visibility.memory_visible(value['id'])
    same=write(store,op='new',category='topic',content='常讨论徒步装备取舍',sources=[source(store,a),source(store,b)])
    assert same['memory_id']==value['id'] and not same['confirmed']
    assert row(store,value['id'])['retention']==value['retention']
    c=msg(store,3,'背包的肩带该怎样选',at='2026-10-03T09:00:00+08:00')
    update=write(store,op='confirm',memory_id=value['id'],expected_revision=1,sources=[source(store,c)])
    assert update['confirmed']
    assert row(store,value['id'])['revision']==1
    assert row(store,value['id'])['retention']==value['retention']+5
    assert not write(store,op='confirm',memory_id=value['id'],expected_revision=1,sources=[source(store,c)])['confirmed']


def test_stale_sources_cas_manual_protection_and_deleted_ids(store):
    a=msg(store,1,'请不要发布广告')
    old=source(store,a)
    with store.write() as conn:
        conn.execute("UPDATE messages SET content='编辑过的原话' WHERE id=?",(a,))
    with pytest.raises(ValueError,match='source'):
        write(store,op='new',category='rule',content='禁止广告',sources=[old])
    manual=write(store,op='new',category='rule',content='禁止广告',manual=True)
    mid=manual['memory_id']
    with pytest.raises(ValueError,match='protected'):
        write(store,op='revise',memory_id=mid,expected_revision=1,category='rule',content='允许广告',sources=[source(store,a)])
    b=msg(store,2,'请遵守禁广告规则',at='2026-10-04T09:00:00+08:00')
    assert write(store,op='confirm',memory_id=mid,expected_revision=1,sources=[source(store,b)])['confirmed']
    with pytest.raises(ValueError,match='revision'):
        write(store,op='confirm',memory_id=mid,expected_revision=0,sources=[source(store,b)])
    assert memory_ops.delete_memory(store,mid,1)
    with pytest.raises(ValueError,match='deleted'):
        write(store,op='confirm',memory_id=mid,expected_revision=2,sources=[source(store,b)])


def test_hard_scope_inherits_and_survives_source_unlink(store):
    msg(store,1,'a'); msg(store,2,'b',entry='B')
    a=write(store,op='new',category='rule',content='规则',manual=True)['memory_id']
    stamp=now()
    with store.write() as conn:
        b=conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
            created_at,updated_at,first_confirmed_at,last_confirmed_at) VALUES('派生内容','事实','self','推断',60,50,50,?,?,?,?)''', (stamp,)*4).lastrowid
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",(b,a,stamp))
        assert memory_ops.Visibility(conn).memory(b)==frozenset({'A'})
        assert conn.execute('SELECT owner_entry_id FROM memories WHERE id=?',(b,)).fetchone()[0]=='A'
        conn.execute('DELETE FROM sources WHERE memory_id=?',(b,))
        assert memory_ops.Visibility(conn).memory(b)==frozenset({'A'})
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE memories SET owner_entry_id=NULL WHERE id=?',(b,))


def test_concurrent_confirmation_once_and_transaction_rollback(store):
    from concurrent.futures import ThreadPoolExecutor
    a=msg(store,1,'请勿广告')
    mid=write(store,op='new',category='rule',content='禁止广告',sources=[source(store,a)])['memory_id']
    b=msg(store,2,'再次提醒禁广告规则')
    bound=source(store,b)
    before=row(store,mid)['retention']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:write(store,op='confirm',memory_id=mid,expected_revision=1,sources=[bound]),range(2)))
    assert sum(r['confirmed'] for r in results)==1
    assert row(store,mid)['retention']==before+5
    c=msg(store,3,'又有新的规则确认')
    bound=source(store,c)
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            write(store,op='confirm',memory_id=mid,expected_revision=1,sources=[bound],_conn=conn)
            raise RuntimeError('interrupt commit')
    assert write(store,op='confirm',memory_id=mid,expected_revision=1,sources=[bound])['confirmed']
    assert row(store,mid)['retention']==before+10


def test_scope_intersection_and_original_scope_survive_merge_and_recreate(store):
    from test_retrieval import put
    from iris.memory_ops import recreate_memory, set_entry_visibility
    a=msg(store,1,'A 材料'); b=msg(store,1,'B 材料',entry='B')
    pa=write(store,op='new',category='rule',content='A 规则',manual=True)['memory_id']
    pb=memory_ops.write_entry_profile_memory(store,'B',op='new',category='rule',content='B 规则',manual=True)['memory_id']
    child=put(store,'双入口派生内容')
    with store.write() as conn:
        for parent in (pa,pb):
            conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",(child,parent,now()))
        assert memory_ops.Visibility(conn).memory(child)==frozenset()
    copy=recreate_memory(store,child,1,source_revision=1)
    with store.read() as conn:
        assert memory_ops.Visibility(conn).memory(copy)==frozenset()
    copied_profile=recreate_memory(store,pa,1,source_revision=1)
    with store.read() as conn:
        assert memory_ops.Visibility(conn).memory(copied_profile)==frozenset({'A'})
        assert conn.execute('SELECT manual_created FROM entry_profile_memories WHERE memory_id=?',(copied_profile,)).fetchone()[0]==1
    # A shared owner cannot override a B-only source's restriction.
    other=put(store,'另外的派生内容',evidence=[b])
    set_entry_visibility(store,'B','entry_only')
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)",(other,pa,now()))
        assert memory_ops.Visibility(conn).memory(other)==frozenset()


def test_revision_protection_is_not_removed_by_strength_or_score_changes(store):
    from iris.memory_ops import edit_memory, manage_memory
    a=msg(store,1,'最初群规')
    mid=write(store,op='new',category='rule',content='最初规则',sources=[source(store,a)])['memory_id']
    assert edit_memory(store,mid,1,content='手写的新规则')
    assert manage_memory(store,mid,2,importance=60)
    b=msg(store,2,'新证据与手写规则不同')
    with pytest.raises(ValueError,match='protected'):
        write(store,op='retire',memory_id=mid,expected_revision=3,sources=[source(store,b)])
    assert row(store,mid)['content']=='手写的新规则'


def test_backup_round_trip_keeps_new_kind_owner_subject_and_receipts(store,tmp_path):
    from iris.backup import export_archive, import_archive
    a=msg(store,1,'请勿广告')
    write(store,op='new',category='rule',content='禁止广告',sources=[source(store,a)])
    archive=tmp_path/'profiles.zip'
    export_archive(store,archive)
    target=tmp_path/'restored'/'iris.db'
    import_archive(archive,target)
    with store.read() as conn, sqlite3.connect(target) as restored:
        for table in ('subjects','entry_subjects','memories','memory_owner_scopes','entry_profile_memories',
                      'entry_profile_evidence_receipts','entry_profile_revision_sources','sources'):
            assert [tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]==restored.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall(),table
        assert not restored.execute('PRAGMA foreign_key_check').fetchall()


def test_upgrade_030_preserves_people_triggers_and_foreign_keys(tmp_path,monkeypatch):
    import iris.db as db
    from iris.search_text import segmented
    path=tmp_path/'old.db'
    root=db.files('iris')
    class OldScripts:
        def iterdir(self):
            return [p for p in root.joinpath('migrations').iterdir() if p.name<'031']
    class OldRoot:
        def joinpath(self,*args):
            return OldScripts() if args==('migrations',) else root.joinpath(*args)
    with monkeypatch.context() as patch:
        patch.setattr(db,'files',lambda _:OldRoot())
        old=Store(path)
        msg(old,1,'升级前的消息')
        with old.write() as conn:
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('friend','person','旧人物',?)",(now(),))
            triggers={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='subjects'")}
            assert memory_ops.Visibility(conn).memory(999) is None
        old.close()
    upgraded=Store(path)
    try:
        with upgraded.read() as conn:
            assert triggers <= {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='subjects'")}
            assert conn.execute("SELECT name FROM subjects WHERE id='friend'").fetchone()[0]=='旧人物'
            assert conn.execute("SELECT COUNT(*) FROM entry_subjects WHERE entry_id='A'").fetchone()[0]==1
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        with upgraded.write() as conn:
            assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
            conn.execute("UPDATE subjects SET name='新人物' WHERE id='friend'")
            assert conn.execute("SELECT revision FROM subjects WHERE id='friend'").fetchone()[0]==2
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('missing','不合法')")
        assert list(tmp_path.glob('old.db.*.bak'))
    finally:
        upgraded.close()


def test_automatic_profiles_use_ordinary_decay_forgetting_and_restore(store):
    import json
    from datetime import datetime,timezone
    from iris.maintenance import Maintenance
    a=msg(store,1,'本群规则')
    mid=write(store,op='new',category='rule',content='规则',importance=100,sources=[source(store,a)])['memory_id']
    before=row(store,mid)['retention']
    engine=Maintenance(store,clock=lambda:datetime(2026,10,13,tzinfo=timezone.utc))
    with store.write() as conn:
        settings=memory_ops.lifecycle_settings(conn)
        run={'created_at':'2026-10-13T00:00:00+00:00','settings_json':json.dumps(settings)}
        for _ in range(3):
            engine._apply(conn,run,'decay',{'id':mid,'revision':1})
        assert conn.execute('SELECT retention FROM memories WHERE id=?',(mid,)).fetchone()[0]==before-1
        memory_ops.adjust_retention(store,mid,value=19,_conn=conn)
    assert row(store,mid)['lifecycle']=='forgotten'
    for i in range(4):
        message=msg(store,i+2,f'第{i}份独立新证据',at=f'2026-10-{i+1:02}T10:00:00+08:00')
        write(store,op='confirm',memory_id=mid,expected_revision=1,sources=[source(store,message)])
    assert row(store,mid)['lifecycle']=='active'
    assert row(store,mid)['revision']==1


def test_source_memory_revision_scope_and_quoted_replay_are_rechecked(store):
    from test_retrieval import put
    from iris.memory_ops import edit_memory, set_entry_visibility
    a=msg(store,1,'禁止在群内发布广告')
    fact=put(store,'明确禁止广告',evidence=[a])
    evidence=source(store,a,source_memory_id=fact,source_revision=1)
    assert edit_memory(store,fact,1,content='修改过的证据记忆')
    with pytest.raises(ValueError,match='source memory'):
        write(store,op='new',category='rule',content='禁止广告',sources=[evidence])
    mid=write(store,op='new',category='rule',content='禁止广告',sources=[source(store,a)])['memory_id']
    quote=msg(store,2,'转引一次',sender='小王')
    with store.write() as conn:
        author=conn.execute('SELECT sender_subject_id FROM messages WHERE id=?',(a,)).fetchone()[0]
        conn.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?',(author,'禁止在群内发布广告',quote))
    assert not write(store,op='confirm',memory_id=mid,expected_revision=1,sources=[source(store,quote,field='quote')])['confirmed']
    other=msg(store,1,'别处规定',entry='B')
    with pytest.raises(ValueError,match='entry mismatch'):
        write(store,op='confirm',memory_id=mid,expected_revision=1,sources=[source(store,other)])


def test_revise_binds_only_new_current_sources_and_retire_is_cas(store):
    a=msg(store,1,'旧群规说允许广告')
    mid=write(store,op='new',category='rule',content='允许广告',sources=[source(store,a)])['memory_id']
    b=msg(store,2,'新群规禁止广告')
    value=write(store,op='revise',memory_id=mid,expected_revision=1,category='rule',content='禁止广告',sources=[source(store,b)])
    assert value['revision']==2 and row(store,mid)['content']=='禁止广告'
    with store.read() as conn:
        assert [r[0] for r in conn.execute('SELECT s.message_id FROM entry_profile_revision_sources r JOIN sources s ON s.id=r.source_id WHERE r.memory_id=? AND r.revision=2',(mid,))]==[b]
        assert conn.execute('SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?',(mid,)).fetchone()[0]==1
    c=msg(store,3,'本条群规已经撤销')
    with pytest.raises(ValueError,match='revision'):
        write(store,op='retire',memory_id=mid,expected_revision=1,sources=[source(store,c)])
    write(store,op='retire',memory_id=mid,expected_revision=2,sources=[source(store,c)])
    assert row(store,mid)['lifecycle']=='forgotten'
    assert row(store,mid)['revision']==2


def test_empty_entry_delete_preserves_existing_behavior(store):
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('empty','empty','test','group')")
        sid=conn.execute("SELECT subject_id FROM entry_subjects WHERE entry_id='empty'").fetchone()[0]
        conn.execute("DELETE FROM entries WHERE id='empty'")
        assert not conn.execute('SELECT 1 FROM subjects WHERE id=?',(sid,)).fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_manual_recreate_and_importance_revision_preserve_source_receipts(store):
    from iris.memory_ops import recreate_memory, manage_memory
    a=msg(store,1,'群规禁止广告')
    evidence=source(store,a)
    mid=write(store,op='new',category='rule',content='禁止广告',sources=[evidence])['memory_id']
    assert manage_memory(store,mid,1,importance=60)
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM entry_profile_revision_sources WHERE memory_id=? AND revision=2',(mid,)).fetchone()[0]==1
    copied=recreate_memory(store,mid,2,source_revision=1)
    assert not write(store,op='confirm',memory_id=copied,expected_revision=1,sources=[evidence])['confirmed']
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM entry_profile_revision_sources WHERE memory_id=? AND revision=1',(copied,)).fetchone()[0]==1


@pytest.mark.parametrize('name,background', [('Iris新版','最初角色背景'), ('Iris','新的角色背景')])
def test_role_settings_do_not_replace_or_project_manual_entry_profiles(store,name,background):
    import json
    memory_ops.setup_role(store,'Iris','最初角色背景','Asia/Shanghai')
    msg(store,1,'入口消息')
    mid=write(store,op='new',category='rule',content='人工群规保持有效',manual=True)['memory_id']
    memory_ops.update_role(store,name,background,'Asia/Shanghai')
    assert row(store,mid)['lifecycle']=='active'
    assert row(store,mid)['revision']==1
    with store.read() as conn:
        version=conn.execute('SELECT sentences_json FROM persona_versions WHERE is_current=1').fetchone()
        sentences=json.loads(version[0])
        assert mid not in {b['memory_id'] for s in sentences for b in s.get('basis',[])}
        assert '人工群规' not in json.dumps(sentences,ensure_ascii=False)


def test_missing_hard_scope_table_after_031_fails_closed(store):
    with store.write() as conn:
        conn.execute('DROP TABLE memory_owner_scopes')
        with pytest.raises(sqlite3.OperationalError,match='memory_owner_scopes'):
            memory_ops.Visibility(conn)
