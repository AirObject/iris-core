"""Upgrade actual 031 data: no model calls and no model paragraph import."""
import json
from iris import db, entry_profile as ep
from iris.db import Store, dumps
from iris.memory_ops import purge_memory
from test_entry_profile_memory_live import message, CLOCK


def test_031_upgrade_converts_only_current_manual_paragraphs_and_keeps_history(tmp_path,monkeypatch):
    root=db.files('iris')
    class Scripts:
        def iterdir(self):
            return [p for p in root.joinpath('migrations').iterdir() if p.name<'032']
    class Root:
        def joinpath(self,*parts):
            return Scripts() if parts==('migrations',) else root.joinpath(*parts)
    path=tmp_path/'legacy.db'
    with monkeypatch.context() as patch:
        patch.setattr(db,'files',lambda _:Root())
        old=Store(path)
        message(old)
        with old.write() as conn:
            settings=ep._ensure(conn,'group',CLOCK().isoformat())
            ep._insert(conn,'group',previous=0,content='旧主题。\n规则。\n未分类\n说明。',sentences=[
                {'text':'旧主题。','author':'model','basis':[],'message_count':2},
                {'text':'规则。','author':'admin','category':'rule','basis':[],'message_count':0},
                {'text':'未分类\n说明。','author':'admin','basis':[],'message_count':0}],
                checks={'passed':True},degree='large',status='published',stamp=CLOCK().isoformat(),
                author='admin',source='edit',material={},message_through=1,settings_revision=settings['revision'])
            conn.execute("UPDATE entry_profile_settings SET publish_mode='all_manual' WHERE entry_id='group'")
        old.close()
    upgraded=Store(path)
    with upgraded.read() as conn:
        rows=[dict(r) for r in conn.execute("SELECT * FROM memories WHERE kind='入口画像' ORDER BY id")]
        assert [r['content'] for r in rows]==['规则。','未分类\n说明。']
        assert all(r['pinned'] for r in rows)
        report=[dict(r) for r in conn.execute('SELECT * FROM entry_profile_legacy_migration ORDER BY sentence_index')]
        assert [r['disposition'] for r in report]==['model_rebuild_pending','manual_imported','manual_unclassified']
        state=conn.execute('SELECT * FROM entry_profile_memory_state').fetchone()
        assert state['cursor']==0 and state['rebuild_pending']==1 and state['version']>1
        assert ep._settings(conn,'group')['publish_mode']=='check_auto'
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='entry_profile_publish_mode_migrated'").fetchone()[0]==1
        assert ep.get_version(upgraded,'group',1)['legacy'] is True
        assert '旧主题。' not in ep.profile_context(conn,'group')['text']
    upgraded.close()
    reopened=Store(path)
    with reopened.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE kind='入口画像'").fetchone()[0]==2
    purge_memory(reopened,rows[0]['id'],1,confirm=True)
    with reopened.read() as conn:
        version=conn.execute('SELECT content,material_json FROM entry_profile_versions WHERE version=1').fetchone()
        assert '规则。' not in dumps(dict(version))
        assert '规则。' not in ep.profile_context(conn,'group')['text']
    reopened.close()
