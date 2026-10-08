"""M2 people: identity changes, durable denials, and recall annotations."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from conftest import FakeGateway
from iris import people
from iris.db import now
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.queue import add_message, form_batch
from iris.retrieval import Retrieval
from test_retrieval import put


def person(store, sid, name, *, parent=None):
    with store.write() as conn:
        conn.execute('INSERT INTO subjects(id,kind,name,parent_id,created_at) VALUES(?,\'person\',?,?,?)',
                     (sid, name, parent, now()))
        conn.execute('INSERT INTO platform_identities(subject_id,platform,account_id,display_name) VALUES(?,?,?,?)',
                     (sid, 'test', sid, name))
    return sid


def link(store, a, b, *, kind='same_as', status='possible', belief=70, world=None, evidence=None):
    if kind == 'same_as':
        a, b = sorted((a, b))
    with store.write() as conn:
        return conn.execute('''INSERT INTO subject_links(subject_a,subject_b,kind,status,belief,world,
            source_message_id,created_at) VALUES(?,?,?,?,?,?,?,?)''',
            (a, b, kind, status, belief, world, evidence, now())).lastrowid


def say(store, sid='b', key='1', content='我喜欢猫，大家叫我林林', *, entry='chat', quote=None):
    with store.read() as conn:
        name = conn.execute('SELECT name FROM subjects WHERE id=?', (sid,)).fetchone()[0]
    return add_message(store, entry_id=entry, entry_name=entry, platform='test', entry_kind='group',
                       kind='message', sender=name, account_id=sid, content=content,
                       occurred_at='2026-10-09T10:00:00+08:00', dedupe_key=key,
                       quote_author=quote)


@pytest.fixture
def pair(store):
    person(store, 'a', '小林')
    person(store, 'b', '林同学')
    person(store, 'c', '远客')
    return link(store, 'a', 'b')


def confirm(store, lid, target='a'):
    with store.read() as conn:
        rel = conn.execute('SELECT * FROM subject_links WHERE id=?', (lid,)).fetchone()
        source = rel['subject_b'] if rel['subject_a'] == target else rel['subject_a']
        revisions = {r['id']: r['revision'] for r in conn.execute('SELECT id,revision FROM subjects')}
    return people.confirm_link(store, lid, target_id=target, expected_revision=rel['revision'],
                               expected_source_revision=revisions[source], expected_target_revision=revisions[target])


def revision(store, sid):
    with store.read() as conn:
        return conn.execute('SELECT revision FROM subjects WHERE id=?', (sid,)).fetchone()[0]


def test_merge_moves_relations_keeps_memory_and_source_history(store, pair):
    message = say(store)
    mid = put(store, '林同学喜欢猫', about=['a', 'b'], speaker='b', evidence=[message], vector=[1, 0])
    alias = people.add_alias(store, 'b', '阿林', expected_revision=revision(store, 'b'))
    person(store, 'child', '林同学的妈妈', parent='b')
    person(store, 'character', '船长')
    role = link(store, 'b', 'character', kind='roleplay', world='海岛游戏', evidence=message)
    with store.read() as conn:
        before = dict(conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())
    result = confirm(store, pair)
    with store.read() as conn:
        after = dict(conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())
        assert after == {**before, 'speaker_subject_id': 'a'}
        assert [r[0] for r in conn.execute('SELECT subject_id FROM memory_subjects WHERE memory_id=?', (mid,))] == ['a']
        assert conn.execute("SELECT subject_id FROM platform_identities WHERE account_id='b'").fetchone()[0] == 'a'
        assert {r[0] for r in conn.execute("SELECT alias FROM subject_aliases WHERE subject_id='a'")} == {'阿林', '林同学'}
        assert conn.execute("SELECT merged_into FROM subjects WHERE id='b'").fetchone()[0] == 'a'
        assert conn.execute("SELECT parent_id FROM subjects WHERE id='child'").fetchone()[0] == 'a'
        assert conn.execute('SELECT subject_a FROM subject_links WHERE id=?', (role,)).fetchone()[0] == 'a'
        assert conn.execute('SELECT sender_subject_id FROM messages WHERE id=?', (message,)).fetchone()[0] == 'b'
        assert conn.execute('SELECT COUNT(*) FROM memory_revisions').fetchone()[0] == 0
        audit = json.loads(conn.execute("SELECT details_json FROM admin_operations WHERE action='subjects_merged'").fetchone()[0])
        assert audit['moved']['speaker_memory_ids'] == [mid]
        assert audit['moved']['about_memory_ids'] == [mid]
        assert alias['id'] in audit['moved']['alias_ids']
        assert audit['moved']['child_subject_ids'] == ['child']
        assert '林同学喜欢猫' not in json.dumps(audit, ensure_ascii=False)
        assert people.canonical_subject(conn, 'b') == 'a'
    assert result['source_id'] == 'b' and result['target_id'] == 'a'


def test_denial_survives_learning_reverse_insert_update_and_delete(store, pair):
    people.deny_link(store, pair, expected_revision=1)
    say(store)
    formed = form_batch(store, 'chat', PROMPT_VERSION, target_count=1)
    output = {'people': [{'name': '林同学', 'same_as': '小林', 'belief': 95, 'evidence': [1]}]}
    assert not LearningEngine(store, FakeGateway(output)).run_batch(formed.id)['dropped']
    with store.write() as conn:
        conn.execute("INSERT INTO subject_links(subject_a,subject_b,kind,belief,created_at) VALUES('b','a','same_as',99,?)", (now(),))
        conn.execute("UPDATE subject_links SET status='possible',belief=99 WHERE id=?", (pair,))
    with store.write() as conn, pytest.raises(sqlite3.IntegrityError, match='denied'):
        conn.execute('DELETE FROM subject_links WHERE id=?', (pair,))
    with store.read() as conn:
        rows = conn.execute('SELECT status,belief FROM subject_links').fetchall()
        assert [tuple(r) for r in rows] == [('denied', 70)]
    with pytest.raises(people.PeopleConflict):
        confirm(store, pair)


@pytest.mark.parametrize('denied_on', ['a', 'b'])
def test_link_conflicts_fold_with_denial_priority_and_keep_evidence(store, pair, denied_on):
    first = say(store, 'a', content='小林的证据', entry='private-a')
    second = say(store, 'b', content='林同学的证据', entry='private-b')
    left = link(store, 'a', 'c', status='denied' if denied_on == 'a' else 'possible', evidence=first)
    right = link(store, 'b', 'c', status='denied' if denied_on == 'b' else 'possible', evidence=second)
    confirm(store, pair)
    detail = people.person_detail(store, 'a')
    merged = next(x for x in detail['same_as'] if x['id'] == left)
    assert merged['status'] == 'denied'
    assert {m['id'] for m in merged['evidence_messages']} == {first, second}
    with store.read() as conn:
        assert conn.execute('SELECT folded_into FROM subject_links WHERE id=?', (right,)).fetchone()[0] == left
        assert conn.execute('SELECT source_message_id FROM subject_links WHERE id=?', (right,)).fetchone()[0] == second
    mid = put(store, '小林喜欢猫', speaker='a', about=['a'])
    result = Retrieval(store).search(text='小林 猫')['memories']
    assert result[0]['id'] == mid and result[0]['subject_annotations']['possible_same_as'] == []


def test_alias_delete_blocks_automatic_readdition_but_manual_add_can_restore(store, pair):
    mid = say(store)
    alias = people.add_alias(store, 'b', '林林', expected_revision=revision(store, 'b'))
    people.delete_alias(store, 'b', alias['id'], expected_revision=revision(store, 'b'))
    formed = form_batch(store, 'chat', PROMPT_VERSION, target_count=1)
    LearningEngine(store, FakeGateway({'people': [{'name': '林同学', 'alias': '林林', 'evidence': [1]}]})).run_batch(formed.id)
    with store.read() as conn:
        assert not conn.execute("SELECT 1 FROM subject_aliases WHERE alias='林林'").fetchone()
    restored = people.add_alias(store, 'b', '林林', expected_revision=revision(store, 'b'))
    assert restored['id'] > alias['id']
    with pytest.raises(people.PeopleConflict):
        people.delete_alias(store, 'b', restored['id'], expected_revision=1)
    with pytest.raises(ValueError):
        people.add_alias(store, 'b', '林同学', expected_revision=revision(store, 'b'))


def test_merge_transfers_alias_denials_and_avoids_duplicate_aliases(store, pair):
    for sid in ('a', 'b'):
        people.add_alias(store, sid, '共同别名', expected_revision=revision(store, sid))
    blocked = people.add_alias(store, 'b', '旧错误别名', expected_revision=revision(store, 'b'))
    people.delete_alias(store, 'b', blocked['id'], expected_revision=revision(store, 'b'))
    people.add_alias(store, 'a', '旧错误别名', expected_revision=revision(store, 'a'))
    confirm(store, pair)
    with store.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('a','旧错误别名')")
    with store.read() as conn:
        assert [r[0] for r in conn.execute("SELECT alias FROM subject_aliases WHERE subject_id='a' ORDER BY alias")] == ['共同别名', '林同学']


@pytest.mark.parametrize('text', ['小林', '林同学', '小林 天文摄影', '林同学 天文摄影'])
def test_after_merge_both_names_find_moved_and_body_only_memories(store, pair, text):
    moved = put(store, '林同学每周练习天文摄影', speaker='b', about=['b'])
    mentioned = put(store, '远客说林同学修好了天文摄影赤道仪', speaker='c', about=['c'])
    unrelated = put(store, '远客喜欢天文摄影', speaker='c', about=['c'])
    confirm(store, pair)
    ids = [m['id'] for m in Retrieval(store).search(text=text)['memories']]
    assert moved in ids and mentioned in ids and unrelated not in ids
    assert [m['id'] for m in Retrieval(store).search(people=['b'])['memories']] == [moved]


def test_intake_merged_account_and_quote_author_resolve_to_survivor(store, pair):
    say(store)
    confirm(store, pair)
    mid = say(store, 'b', key='2', quote='林同学')
    with store.read() as conn:
        row = conn.execute('SELECT sender_subject_id,quote_author_subject_id FROM messages WHERE id=?', (mid,)).fetchone()
        assert tuple(row) == ('a', 'a')
    assert people.person_detail(store, 'b')['canonical_id'] == 'a'
    assert [p['id'] for p in people.list_people(store, text='林同学')['items']] == ['a']


def test_merge_chain_flattens_at_resolution_and_rejects_repeated_mutation(store, pair):
    confirm(store, pair)
    next_link = link(store, 'a', 'c')
    confirm(store, next_link, target='c')
    with store.read() as conn:
        assert people.canonical_subject(conn, 'b') == 'c'
        assert people.canonical_subject(conn, 'a') == 'c'
    with pytest.raises(people.PeopleConflict):
        people.add_alias(store, 'b', '不能写占位', expected_revision=revision(store, 'b'))


@pytest.mark.parametrize('table,sql', [
    ('memory', "INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,created_at,updated_at,first_confirmed_at,last_confirmed_at) VALUES('x','事实','b','亲历',50,50,50,'t','t','t','t')"),
    ('about', "INSERT INTO memory_subjects(memory_id,subject_id) VALUES(1,'b')"),
    ('alias', "INSERT INTO subject_aliases(subject_id,alias) VALUES('b','新名字')"),
    ('identity', "INSERT INTO platform_identities(subject_id,platform,account_id,display_name) VALUES('b','new','new','new')"),
    ('link', "INSERT INTO subject_links(subject_a,subject_b,kind,belief,created_at) VALUES('b','c','same_as',50,'t')"),
    ('child', "INSERT INTO subjects(id,name,kind,parent_id,created_at) VALUES('new','new','person','b','t')"),
])
def test_database_rejects_new_references_to_merged_placeholder(store, pair, table, sql):
    put(store, '原记忆')
    confirm(store, pair)
    with pytest.raises(sqlite3.IntegrityError, match='merged'), store.write() as conn:
        conn.execute(sql)


def test_conflicting_admin_merge_writes_commit_once(store, pair):
    source_rev, target_rev = revision(store, 'b'), revision(store, 'a')
    def execute():
        try:
            people.confirm_link(store, pair, target_id='a', expected_revision=1,
                                expected_source_revision=source_rev, expected_target_revision=target_rev)
            return 'ok'
        except people.PeopleConflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: execute(), range(2))) == ['conflict', 'ok']
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='subjects_merged'").fetchone()[0] == 1


@pytest.mark.parametrize('invalid', ['self', 'scene', 'roleplay', 'ancestor'])
def test_merges_reject_special_subjects_actor_character_and_family_cycles(store, pair, invalid):
    if invalid in ('self', 'scene'):
        lid = link(store, 'b', invalid)
        target = invalid
    else:
        lid, target = pair, 'a'
        if invalid == 'roleplay':
            link(store, 'a', 'b', kind='roleplay', world='游戏')
        else:
            with store.write() as conn:
                conn.execute("UPDATE subjects SET parent_id='b' WHERE id='a'")
    with pytest.raises((ValueError, people.PeopleConflict)):
        confirm(store, lid, target=target)
    with store.read() as conn:
        assert conn.execute("SELECT merged_into FROM subjects WHERE id='b'").fetchone()[0] is None


def test_annotations_after_selection_preserve_budget_order_reasons_and_learning_context(store, pair):
    say(store, 'a', content='天文摄影使用什么设备')
    first = put(store, '小林喜欢天文摄影，使用双筒望远镜', speaker='a', about=['a'])
    second = put(store, '小林用赤道仪练习天文摄影', speaker='a', about=['a'])
    r = Retrieval(store, clock=lambda: datetime(2026, 10, 9, tzinfo=timezone.utc))
    baseline = r.prepare('chat', text='天文摄影', participants=[], recent_limit=0, judge=False)
    learning = r.learning_context('天文摄影', ['a'])
    person(store, 'character', '远航船长')
    secret = say(store, 'a', key='secret', content='绝不能作为标注返回的跨入口原文', entry='private')
    role = link(store, 'a', 'character', kind='roleplay', world='游戏海岛', evidence=secret)
    found = r.prepare('chat', text='天文摄影', participants=[], recent_limit=0, judge=False)
    assert [(m['id'], m['reason']) for m in found['memories']] == [(m['id'], m['reason']) for m in baseline['memories']]
    assert r.learning_context('天文摄影', ['a']) == learning
    note = found['memories'][0]['subject_annotations']
    assert note['possible_same_as'][0]['subjects'] == [{'id': 'a', 'name': '小林'}, {'id': 'b', 'name': '林同学'}]
    assert note['roleplay'][0]['actor']['id'] == 'a'
    assert note['roleplay'][0]['character']['id'] == 'character'
    assert note['roleplay'][0]['worlds'] == ['游戏海岛'] and note['roleplay'][0]['fictional'] is True
    assert '绝不能作为标注返回的跨入口原文' not in json.dumps(found, ensure_ascii=False)
    budget = 1
    # Use the exact pre-annotation candidate payload to locate its existing budget boundary.
    for budget in range(100, 1501, 20):
        if r.prepare('chat', text='天文摄影', participants=[], recent_limit=0, judge=False, token_budget=budget)['memories']:
            break
    before = r.prepare('chat', text='天文摄影', participants=[], recent_limit=0, judge=False, token_budget=budget)
    for i in range(6):
        person(store, f'p{i}', f'候选{i}')
        link(store, 'a', f'p{i}')
    after = r.prepare('chat', text='天文摄影', participants=[], recent_limit=0, judge=False, token_budget=budget)
    assert [m['id'] for m in after['memories']] == [m['id'] for m in before['memories']]
    char_mid = put(store, '远航船长找到海岛藏宝图', speaker='character', about=['character'])
    char = r.search(text='远航船长 藏宝图')['memories'][0]
    assert char['id'] == char_mid and char['speaker_subject_id'] == 'character'
    assert char['subject_annotations']['roleplay'][0]['actor']['id'] == 'a'


LEARNING_PENDING = pytest.mark.xfail(strict=True, raises=(AssertionError, sqlite3.IntegrityError), reason='等待学习提示词 v7 合入后，在 learning.py 接入最终主体解析；人物线第 1 步不改该文件')


@LEARNING_PENDING
def test_learning_fresh_snapshot_excludes_merged_tombstone(store, pair):
    say(store)
    confirm(store, pair)
    formed = form_batch(store, 'chat', PROMPT_VERSION, target_count=1)
    engine = LearningEngine(store, FakeGateway())
    snapshot = engine._snapshot(formed)
    engine._material(formed, snapshot, [])
    assert engine._known_subject('林同学', snapshot) == 'a'
    assert 'b' not in {s['id'] for s in snapshot['subjects']}


@LEARNING_PENDING
@pytest.mark.parametrize('mode', ['memory', 'alias', 'link', 'parent', 'same_subject', 'duplicate'])
def test_learning_inflight_merge_uses_current_subject_before_dedupe(store, pair, mode):
    say(store)
    output = {'memories': [{'content': '林同学喜欢猫', 'type': '偏好', 'speaker': 'P1', 'about': ['P1'],
                           'stance': '亲历', 'importance': 60, 'evidence': [1]}]}
    if mode == 'alias':
        output = {'people': [{'name': 'P1', 'alias': '林林', 'evidence': [1]}]}
    elif mode in ('link', 'same_subject'):
        output = {'people': [{'name': 'P1', 'same_as': '远客' if mode == 'link' else '小林', 'evidence': [1]}]}
    elif mode == 'parent':
        output['memories'][0].update(about=['P1的妈妈'], stance='转述')
    if mode == 'duplicate':
        old = put(store, '林同学喜欢猫', about=['b'], speaker='b', kind='偏好')
    formed = form_batch(store, 'chat', PROMPT_VERSION, target_count=1)
    result = LearningEngine(store, FakeGateway(output, hook=lambda _: confirm(store, pair))).run_batch(formed.id)
    with store.read() as conn:
        assert not conn.execute("SELECT 1 FROM memories WHERE speaker_subject_id='b'").fetchone()
        assert not conn.execute("SELECT 1 FROM memory_subjects WHERE subject_id='b'").fetchone()
        assert not conn.execute("SELECT 1 FROM subject_aliases WHERE subject_id='b'").fetchone()
        assert not conn.execute("SELECT 1 FROM subjects WHERE parent_id='b' AND merged_into IS NULL").fetchone()
        assert not conn.execute("SELECT 1 FROM subject_links WHERE folded_into IS NULL AND status='possible' AND (subject_a='b' OR subject_b='b')").fetchone()
        if mode == 'duplicate':
            assert result['created'] == [] and result['confirmed'] == [old]
        elif mode == 'same_subject':
            assert any(x['reason'] == 'same subject on both sides' for x in result['dropped'])
        elif mode in ('memory', 'parent'):
            assert len(result['created']) == 1
        else:
            assert not result['dropped']


def test_subject_names_exclude_tombstones_and_removed_merge_name_alias(store, pair):
    from iris.query_analysis import SubjectNames
    confirm(store, pair)
    with store.read() as conn:
        names = SubjectNames(conn)
        assert names.mentioned('林同学') == ('a',)
        assert 'b' not in names.names
        alias_id = conn.execute("SELECT id FROM subject_aliases WHERE subject_id='a' AND alias='林同学'").fetchone()[0]
    people.delete_alias(store, 'a', alias_id, expected_revision=revision(store, 'a'))
    with store.read() as conn:
        assert SubjectNames(conn).mentioned('林同学') == ()


def test_merge_rolls_back_every_move_if_audit_cannot_commit(store, pair):
    mid = put(store, '林同学喜欢猫', about=['b'], speaker='b')
    people.add_alias(store, 'b', '阿林', expected_revision=revision(store, 'b'))
    with store.write() as conn:
        conn.execute("""CREATE TRIGGER reject_merge_audit BEFORE INSERT ON admin_operations
            WHEN new.action='subjects_merged' BEGIN SELECT RAISE(ABORT,'test audit failure'); END""")
    tables = ('subjects', 'subject_aliases', 'platform_identities', 'memories', 'memory_subjects', 'subject_links')
    with store.read() as conn:
        before = {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}
    with pytest.raises(sqlite3.IntegrityError, match='test audit failure'):
        confirm(store, pair)
    with store.read() as conn:
        assert before == {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}


def test_roleplay_conflict_keeps_scenes_and_evidence_protection(store, pair):
    from iris.memory_ops import message_references
    person(store, 'character', '船长')
    first = say(store, 'a', entry='world-one')
    second = say(store, 'b', entry='world-two')
    left = link(store, 'a', 'character', kind='roleplay', world='海岛', evidence=first)
    right = link(store, 'b', 'character', kind='roleplay', world='星际', evidence=second)
    confirm(store, pair)
    mid = put(store, '船长收集地图', about=['character'], speaker='character')
    item = Retrieval(store).search(text='船长 地图')['memories'][0]
    assert item['id'] == mid
    assert item['subject_annotations']['roleplay'][0]['worlds'] == ['海岛', '星际']
    with store.read() as conn:
        assert 'subject_link' in message_references(conn, second)
        assert conn.execute('SELECT folded_into FROM subject_links WHERE id=?', (right,)).fetchone()[0] == left


def test_duplicate_alias_fold_keeps_evidence_until_explicit_removal(store, pair):
    from iris.memory_ops import message_references
    first = say(store, 'a', entry='first')
    second = say(store, 'b', entry='second')
    with store.write() as conn:
        for sid, mid in [('a', first), ('b', second)]:
            conn.execute("INSERT INTO subject_aliases(subject_id,alias,source_message_id) VALUES(?,'共同别名',?)", (sid, mid))
    confirm(store, pair)
    aliases = people.person_detail(store, 'a')['aliases']
    duplicate = next(a for a in aliases if a['alias'] == '共同别名')
    assert duplicate['evidence_message_ids'] == [first, second]
    with store.read() as conn:
        assert 'subject_alias' in message_references(conn, second)
    people.delete_alias(store, 'a', duplicate['id'], expected_revision=revision(store, 'a'))
    with store.read() as conn:
        assert 'subject_alias' not in message_references(conn, second)


@pytest.mark.parametrize('action', ['deny', 'merge'])
def test_judgment_inflight_identity_change_only_refreshes_annotations(store, pair, action):
    import httpx
    from test_recall_judge import gateway, scores
    from test_models import response
    say(store, 'b', content='摄影需要什么')
    mid = put(store, '林同学的摄影设备是望远镜', speaker='b', about=['b'])
    def handler(request):
        body = json.loads(request.content)
        payload = json.loads(body['messages'][1]['content'])
        assert 'subject_annotations' not in json.dumps(payload)
        assert not store._writer.in_transaction
        if action == 'deny':
            people.deny_link(store, pair, expected_revision=1)
        else:
            confirm(store, pair)
        return httpx.Response(200, json=response(scores([c['id'] for c in payload['candidates']])))
    with gateway(store, handler) as g:
        result = Retrieval(store, g).prepare('chat', text='摄影 望远镜', participants=[], recent_limit=0)
    assert result['judgment']['status'] == 'applied'
    assert [(m['id'], m['reason']) for m in result['memories']] == [(mid, 'relevant')]
    assert result['memories'][0]['subject_annotations']['possible_same_as'] == []
    assert result['memories'][0]['speaker_subject_id'] == ('a' if action == 'merge' else 'b')
    with store.read() as conn:
        assert conn.execute('SELECT revision FROM memories WHERE id=?', (mid,)).fetchone()[0] == 1
        assert conn.execute('SELECT memory_id FROM recall_items WHERE recall_id=?', (result['recall_id'],)).fetchone()[0] == mid


def test_merge_includes_memory_committed_before_its_write_transaction(store, pair):
    a_revision, b_revision = revision(store, 'a'), revision(store, 'b')
    mid = put(store, '并发学习刚写入的事实', speaker='b', about=['b'])
    people.confirm_link(store, pair, target_id='a', expected_revision=1,
                        expected_source_revision=b_revision, expected_target_revision=a_revision)
    with store.read() as conn:
        assert conn.execute('SELECT speaker_subject_id FROM memories WHERE id=?', (mid,)).fetchone()[0] == 'a'
        assert conn.execute('SELECT subject_id FROM memory_subjects WHERE memory_id=?', (mid,)).fetchone()[0] == 'a'
        details = json.loads(conn.execute("SELECT details_json FROM admin_operations WHERE action='subjects_merged'").fetchone()[0])
        assert mid in details['moved']['speaker_memory_ids']


def test_queue_unmerged_alias_does_not_change_original_quote_resolution(store, pair):
    people.add_alias(store, 'a', '阿林', expected_revision=revision(store, 'a'))
    mid = say(store, 'a', quote='阿林')
    with store.read() as conn:
        quoted = conn.execute('SELECT quote_author_subject_id FROM messages WHERE id=?', (mid,)).fetchone()[0]
        assert quoted != 'a'


def test_link_database_canonical_key_and_immutable_merge_map(store, pair):
    with store.write() as conn:
        conn.execute("INSERT INTO subject_links(subject_a,subject_b,kind,belief,created_at) VALUES('c','b','same_as',60,?)", (now(),))
    with store.read() as conn:
        assert tuple(conn.execute("SELECT subject_a,subject_b FROM subject_links WHERE subject_b='c'").fetchone()) == ('b', 'c')
    confirm(store, pair)
    with pytest.raises(sqlite3.IntegrityError), store.write() as conn:
        conn.execute("UPDATE subjects SET merged_into=NULL WHERE id='b'")
    with pytest.raises(sqlite3.IntegrityError), store.write() as conn:
        conn.execute("UPDATE subjects SET merged_into='b' WHERE id='a'")


@pytest.mark.parametrize('other', ['self', 'scene'])
def test_can_deny_invalid_identity_suggestion_to_special_subject(store, pair, other):
    lid = link(store, 'b', other)
    result = people.deny_link(store, lid, expected_revision=1)
    assert result['status'] == 'denied'
