"""Daily review through the real maintenance, identity, intake and admin paths."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from iris import people
from iris.db import now
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.maintenance import Maintenance, PHASES
from iris.models import ModelError
from iris.people_review import SETTING, material_for_link, verified_evidence
from iris.people_split import split_merge
from iris.queue import add_message, form_batch
from test_people import confirm, link, person, revision
from test_people_api import client  # noqa: F401
from test_retrieval import put
from conftest import FakeGateway


def clock():
    return datetime.fromisoformat('2026-11-17T12:00:00+08:00')


def message(store, sender, content, at='2026-11-14T12:00:00+08:00', *, entry='chat', key=None, **extra):
    author = {'a': '小林', 'b': '林同学', 'c': '远客', 'd': '小周', 'self': 'Iris'}[sender]
    return add_message(store, entry_id=entry, entry_name=entry, platform='test', entry_kind='group',
                       kind=extra.pop('kind', 'message'), sender=author, account_id=sender,
                       content=content, occurred_at=at, dedupe_key=key or content, **extra)


@pytest.fixture
def scenario(store):
    for sid, name in (('a', '小林'), ('b', '林同学'), ('c', '远客'), ('d', '小周')):
        person(store, sid, name)
    with store.write() as conn:
        conn.execute("UPDATE subjects SET created_at='2026-01-01T11:00:00+08:00' WHERE id='a'")
        conn.execute("UPDATE subjects SET created_at='2026-01-01T02:59:59+00:00' WHERE id='b'")
    first = message(store, 'b', '我原来用林同学账号，现在换成小林了。')
    second = message(store, 'c', '我昨晚核对过，小林是林同学自己的新号。',
                     '2026-11-15T12:00:00+08:00', entry='other')
    return link(store, 'a', 'b', evidence=first), first, second


class Judge:
    def __init__(self, ids=(), *, action=None, decision='same_person', counters=()):
        self.ids, self.action, self.decision, self.counters = ids, action, decision, counters
        self.calls = []

    def json_chat(self, payload, purpose, max_tokens):
        prompt = json.loads(payload[-1]['content'])
        self.calls.append((purpose, prompt))
        if self.action:
            self.action()
        evidence = [{'id': row['id'], 'excerpt': row['text']} for row in prompt['messages'] if row['id'] in self.ids]
        return ({'decision': self.decision, 'evidence': evidence, 'counter_evidence': list(self.counters)},)


def enable(store, value=True):
    store.set_setting('lifecycle', {SETTING: value})


def maintain(store, judge, *, stop=None, run_id=None):
    maintenance = Maintenance(store, gateway=judge, clock=clock)
    if run_id is None:
        run_id = maintenance.request()
        with store.write() as conn:
            conn.execute('UPDATE maintenance_runs SET phase=? WHERE id=?', (PHASES.index('goals'), run_id))
    maintenance.run(run_id, stop=stop)
    return maintenance.report(run_id)


def test_daily_merge_switch_older_instant_and_audit(store, scenario):
    lid, first, second = scenario
    judge = Judge((first, second))
    assert not maintain(store, judge)['items'] and not judge.calls
    enable(store)
    result = maintain(store, judge)['items'][0]
    assert result['phase'] == 'people' and result['outcome'] == 'people_merged'
    assert result['object_id'] == lid and result['details']['evidence_message_ids'] == [first, second]
    with store.read() as conn:
        assert people.canonical_subject(conn, 'a') == 'b'  # One second older despite the timestamp offset.
        assert conn.execute("SELECT actor FROM admin_operations WHERE action='subjects_merged'").fetchone()[0] == 'maintenance'
    assert maintain(store, judge, run_id=result['run_id'])['items'][0] == result
    assert len(judge.calls) == 1


def test_skips_fingerprints_and_refreshes_on_new_counterevidence(store, scenario):
    lid, _, _ = scenario
    enable(store)
    judge = Judge(decision='keep_possible')
    assert maintain(store, judge)['items'][0]['outcome'] == 'kept_possible'
    assert maintain(store, judge)['items'][0]['reason'] == 'already_reviewed'
    message(store, 'b', '小林不是我，我也没有换号。', '2026-11-16T12:00:00+08:00')
    assert maintain(store, judge)['items'][-1]['object_id'] == lid
    assert len(judge.calls) == 2


@pytest.mark.parametrize('change', ['deny', 'counter', 'disable', 'stop', 'timeout'])
def test_review_call_is_outside_write_and_revalidates_before_merge(store, scenario, change):
    lid, first, second = scenario
    enable(store)

    def mutate():
        if change == 'deny':
            people.deny_link(store, lid, expected_revision=1)
        if change == 'counter':
            message(store, 'b', '小林和林同学是两个人。', '2026-11-16T12:00:00+08:00')
        if change == 'disable':
            enable(store, False)
        if change == 'timeout':
            raise ModelError('retryable', 'simulated timeout', reason='timeout')

    judge = Judge((first, second), action=mutate)
    result = maintain(store, judge, stop=(lambda: bool(judge.calls)) if change == 'stop' else None)
    with store.read() as conn:
        assert not conn.execute('SELECT 1 FROM subject_merges').fetchone()
    if change == 'stop':
        assert result['state'] == 'running'
        result = maintain(store, Judge((first, second)), run_id=result['id'])
        assert result['items'][0]['outcome'] == 'people_merged'
    elif change == 'timeout':
        assert result['items'][0]['reason'] == 'timeout'
        assert maintain(store, Judge((first, second)))['items'][0]['outcome'] == 'people_merged'
    elif change != 'disable':
        assert result['items'][0]['reason'] == 'stale'


@pytest.mark.parametrize('guard', ['denied_history', 'roleplay', 'ancestor'])
def test_structural_guard_blocks_associations_before_model_call(store, scenario, guard):
    lid, first, second = scenario
    if guard == 'denied_history':
        denied = link(store, 'b', 'd', status='denied')
        folded_into = link(store, 'a', 'c', status='confirmed')
        with store.write() as conn:
            conn.execute('UPDATE subject_links SET folded_into=? WHERE id=?', (folded_into, denied))
            conn.execute("UPDATE subjects SET merged_into='a' WHERE id='d'")
    if guard == 'roleplay':
        link(store, 'a', 'b', kind='roleplay')
    if guard == 'ancestor':
        with store.write() as conn:
            conn.execute("UPDATE subjects SET parent_id='b' WHERE id='a'")
    enable(store)
    judge = Judge((first, second))
    assert maintain(store, judge)['items'][0]['reason'] == guard
    assert not judge.calls


def test_chat_actor_and_character_are_blocked_without_a_roleplay_link(store, scenario):
    lid, first, second = scenario
    message(store, 'a', '今天我演林同学，台词放在群文件里。', '2026-11-16T12:00:00+08:00')
    enable(store)
    judge = Judge((first, second))
    assert maintain(store, judge)['items'][0]['reason'] == 'roleplay_evidence'
    assert not judge.calls


@pytest.mark.parametrize('bad_proof', ['same_day', 'same_witness', 'role', 'quote', 'copy', 'unknown', 'counter'])
def test_only_independent_direct_original_citations_can_pass(store, scenario, bad_proof):
    lid, first, second = scenario
    replacement = message(store, 'd', '我亲眼见他用小林收到了原账号的核验短信。', '2026-11-16T12:00:00+08:00')
    extra = message(store, 'b', '林同学的另一个说明。', '2026-11-16T12:05:00+08:00')
    changed = {
        'same_day': ('occurred_at', '2026-11-13T20:01:00-08:00'),  # Nov 14 in the role's timezone.
        'same_witness': ('sender_subject_id', 'c'),
        'role': ('sender_subject_id', 'self'),
        'copy': ('content', '我昨晚核对过，小林是林同学自己的新号。'),
        'quote': ('quote_content', '我亲眼见他用小林收到了原账号的核验短信。'),
    }
    if bad_proof in changed:
        col, value = changed[bad_proof]
        with store.write() as conn:
            conn.execute(f'UPDATE messages SET {col}=? WHERE id=?', (value, replacement))
            if bad_proof == 'same_day':
                conn.execute("UPDATE messages SET occurred_at='2026-11-14T11:00:00+08:00' WHERE id=?", (second,))
    with store.read() as conn:
        material, _ = material_for_link(conn, lid, through=extra, zone=ZoneInfo('Asia/Shanghai'))
    rows = {m['id']: m for m in material['payload']['messages']}
    ids = [second, replacement] if bad_proof != 'unknown' else [first, replacement + 100]
    proof = [{'id': mid, 'excerpt': rows[mid]['text'] if mid in rows else '凭空编造的证据'} for mid in ids]
    result, _ = verified_evidence(material, {'decision': 'same_person', 'evidence': proof,
                                            'counter_evidence': [first] if bad_proof == 'counter' else []})
    assert result == []


def test_split_restores_associations_reassigns_later_messages_and_keeps_edits(store, client, scenario):  # noqa: F811
    lid, first, second = scenario
    enable(store)
    people.add_alias(store, 'a', '共同称呼', expected_revision=revision(store, 'a'))
    people.add_alias(store, 'b', '共同称呼', expected_revision=revision(store, 'b'))
    person(store, 'child', '小林的妈妈', parent='a')
    left = link(store, 'a', 'c', status='denied')
    right = link(store, 'b', 'c')
    shared = put(store, '共有人物', speaker='a', about=['a', 'b'])
    own = put(store, '单方人物', speaker='a', about=['a'])
    result = next(item for item in maintain(store, Judge((first, second)))['items'] if item['object_id'] == lid)
    merge_id = result['details']['merge_id']
    mid = message(store, 'a', '我每天骑车。', quote_author='小林', quote_author_account_id='a', quote_content='小林骑车。')
    from_name = message(store, 'a', '我还去过旧码头。', quote_author='小林')
    recent = put(store, '新消息形成的记忆', speaker='b', about=['b'], evidence=[mid])
    with store.write() as conn:
        conn.execute("UPDATE memories SET content='管理员编辑过的记忆',belief=91,revision=revision+1 WHERE id=?", (own,))
        assert tuple(conn.execute("SELECT sender_subject_id,quote_author_subject_id FROM messages WHERE id=?", (mid,)).fetchone()) == ('b', 'b')
    def recent_message():
        response = client.post('/api/v1/entries/chat/prepare', json={'text': '', 'judge': False})
        assert response.status_code == 200
        return next(row for row in response.json()['recent_messages'] if row['id'] == mid)

    projected = recent_message()
    assert projected['sender_subject_id'] == 'b'
    assert not {'original_sender_subject_id', 'original_quote_author_subject_id'} & projected.keys()
    detail = client.get('/admin/api/people/b').json()
    action = detail['merges'][0]
    assert action['actor'] == 'maintenance' and action['can_split']
    assert [m['id'] for m in action['evidence_messages']] == [first, second]
    url = f'/admin/api/people/merges/{merge_id}/split'
    assert client.post(url, json={'expected_revision': action['target_revision'] - 1}).status_code == 409
    assert client.post(url, json={'expected_revision': action['target_revision']}).status_code == 200
    assert recent_message()['sender_subject_id'] == 'a'
    assert client.post(url, json={'expected_revision': revision(store, 'b')}).status_code == 409
    with store.read() as conn:
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        assert people.canonical_subject(conn, 'a') == 'a'
        assert conn.execute("SELECT subject_id FROM platform_identities WHERE account_id='a'").fetchone()[0] == 'a'
        assert conn.execute("SELECT parent_id FROM subjects WHERE id='child'").fetchone()[0] == 'a'
        assert tuple(conn.execute('SELECT speaker_subject_id,content,belief,revision FROM memories WHERE id=?', (own,)).fetchone()) == (
            'a', '管理员编辑过的记忆', 91, 2)
        assert [row[0] for row in conn.execute('SELECT subject_id FROM memory_subjects WHERE memory_id=?', (shared,))] == ['a', 'b']
        assert [row[0] for row in conn.execute('SELECT subject_id FROM memory_subjects WHERE memory_id=?', (own,))] == ['a']
        assert conn.execute('SELECT speaker_subject_id FROM memories WHERE id=?', (recent,)).fetchone()[0] == 'a'
        assert [tuple(row) for row in conn.execute('SELECT sender_subject_id,quote_author_subject_id FROM messages WHERE id IN (?,?)', (mid, from_name))] == [('a','a'), ('a','a')]
        assert [tuple(row) for row in conn.execute('SELECT id,status,folded_into FROM subject_links WHERE id IN (?,?,?) ORDER BY id', (lid,left,right))] == [
            (lid,'denied',None), (left,'denied',None), (right,'possible',None)]
    with store.write() as conn:
        conn.execute("INSERT INTO subject_links(subject_a,subject_b,kind,belief,created_at) VALUES('a','b','same_as',99,?)", (now(),))
        assert conn.execute("SELECT status FROM subject_links WHERE subject_a='a' AND subject_b='b'").fetchone()[0] == 'denied'


def test_split_nested_merges_recovers_older_edge_after_later_denial(store, scenario):
    lid, _, _ = scenario
    other = link(store, 'a', 'c')
    first = confirm(store, lid, 'b')
    second = confirm(store, other, 'b')
    with pytest.raises(people.PeopleConflict):
        split_merge(store, first['merge_id'], expected_revision=revision(store, 'b'))
    split_merge(store, second['merge_id'], expected_revision=revision(store, 'b'))
    split_merge(store, first['merge_id'], expected_revision=revision(store, 'b'))
    with store.read() as conn:
        assert all(people.canonical_subject(conn, sid) == sid for sid in 'abc')
        assert [tuple(row) for row in conn.execute("SELECT subject_a,subject_b FROM subject_links WHERE status='denied' ORDER BY 1,2")] == [
            ('a','b'), ('a','c'), ('b','c')]


def test_learning_with_pre_split_attribution_is_retried_without_spending_attempt(store, scenario):
    lid, _, _ = scenario
    merge = confirm(store, lid, 'b')
    message(store, 'a', '我周末做了面包。', entry='batch')
    batch = form_batch(store, 'batch', PROMPT_VERSION, target_count=1)

    class SplitDuringLearning(FakeGateway):
        def json_chat(self, *args, **kwargs):
            split_merge(store, merge['merge_id'], expected_revision=revision(store, 'b'))
            return super().json_chat(*args, **kwargs)

    result = LearningEngine(store, SplitDuringLearning({})).run_batch(batch.id)
    assert result['state'] == 'waiting'
    with store.read() as conn:
        assert conn.execute('SELECT attempt_count FROM batches WHERE id=?', (batch.id,)).fetchone()[0] == 0
    assert 'created' in LearningEngine(store, FakeGateway({})).run_batch(batch.id)
