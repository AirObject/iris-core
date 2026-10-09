from datetime import datetime, timezone
import json

import pytest

from iris.goals import Goals, write_learning_goal
from iris.models import ModelError


class Judge:
    def __init__(self,store,verdict='same',hook=None):
        self.store,self.verdict,self.hook=store,verdict,hook
        self.calls=[]

    def goal_dedup_judge(self,messages,ids,*,budget_seconds):
        assert not self.store._writer.in_transaction
        self.calls.append(json.loads(messages[-1]['content']))
        if self.hook:
            self.hook()
        if isinstance(self.verdict,Exception):
            raise self.verdict
        return {'decisions':{gid:self.verdict for gid in ids}}


@pytest.fixture
def setup(store):
    store.set_setting('goal_dedup_judge',{'enabled':True,'method':'C','budget_seconds':1})
    clock=lambda:datetime(2026,10,9,tzinfo=timezone.utc)
    judge=Judge(store)
    return Goals(store,gateway=judge,clock=clock),judge



def test_selected_default_checks_identical_text_before_merging_and_allows_opt_out(store):
    judge=Judge(store,verdict='different')
    goals=Goals(store,gateway=judge)
    old=goals.create(content='整理操作手册')
    new=goals.create(content='整理操作手册')
    assert new['dedup']['status']=='created'
    assert new['goal']['id']!=old['goal']['id']
    assert len(judge.calls)==1
    store.set_setting('goal_dedup_judge',{'enabled':False})
    fallback=goals.create(content='整理操作手册')
    assert fallback['dedup']['status']=='merged'
    assert fallback['goal']['id']==old['goal']['id']
    assert len(judge.calls)==1


def test_save_then_judge_and_merge_with_revision_checks(store,setup):
    goals,judge=setup
    old=goals.create(content='整理操作手册')
    def saved():
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM goals').fetchone()[0]==2
    judge.hook=saved
    new=goals.create(content='整理操作说明手册')
    assert new['dedup']['status']=='merged'
    assert new['dedup']['target_id']==old['submitted_id']
    assert len(judge.calls)==1


@pytest.mark.parametrize('edit',['target','incoming'])
def test_revision_change_during_judgment_keeps_pending(store,setup,edit):
    goals,judge=setup
    old=goals.create(content='整理操作手册')
    def change():
        with store.write() as conn:
            gid=old['submitted_id'] if edit=='target' else conn.execute('SELECT MAX(id) FROM goals').fetchone()[0]
            conn.execute('UPDATE goals SET revision=revision+1 WHERE id=?',(gid,))
    judge.hook=change
    new=goals.create(content='整理操作说明手册')
    assert new['dedup']['status']=='pending' and new['dedup']['reason']=='stale'
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM goals WHERE merged_into IS NOT NULL').fetchone()[0]==0


def test_learning_transaction_only_stages_then_same_review_path_runs_after_commit(store,setup):
    goals,judge=setup
    old=goals.create(content='整理操作手册')
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','unit','private')")
        result=write_learning_goal(conn,content='整理操作说明手册',kind='normal',deadline=None,
                                   entry_id='A',evidence=[],current=goals.clock())
        assert result['dedup']['status']=='pending' and not judge.calls
    result=goals.review(result['submitted_id'])
    assert result['dedup']['target_id']==old['submitted_id'] and len(judge.calls)==1


def test_admin_same_verdict_marks_possible_without_merging(store,setup):
    goals,judge=setup
    old=goals.create(content='整理操作手册')
    result=goals.create(content='整理操作说明手册',origin='admin',actor='admin')
    assert result['dedup']['status']=='possible_duplicate'
    assert result['goal']['id']!=old['goal']['id']
    assert goals.get(result['goal']['id'])['possible_duplicate_ids']==[old['goal']['id']]


def test_failed_judgment_is_pending_and_host_receipt_is_idempotent(store,setup):
    goals,judge=setup
    goals.create(content='整理操作手册')
    judge.verdict=ModelError('retryable','test timeout',reason='timeout')
    result=goals.create(content='整理操作说明手册',host_key='unit-key')
    assert result['dedup']['status']=='pending'
    judge.verdict='same'
    assert goals.create(content='ignored retry body',host_key='unit-key')==result
    completed=goals.review(result['submitted_id'])
    assert completed['dedup']['status']=='merged'
    assert goals.create(content='ignored retry body',host_key='unit-key')==result


def test_missing_people_can_be_confirmed_without_discarding_known_people(store,setup):
    goals,judge=setup
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('lin','person','林',?)",(goals.clock().isoformat(),))
    old=goals.create(content='帮林整理操作手册',people=['lin'])
    result=goals.create(content='帮林整理操作说明手册')
    assert result['dedup']['status']=='merged'
    assert goals.get(old['submitted_id'])['people']==['lin']


def test_ambiguous_verdicts_preserve_all_possible_targets(store,setup):
    goals,judge=setup
    store.set_setting('goal_dedup_judge',{'enabled':False})
    a=goals.create(content='整理相机操作手册')
    b=goals.create(content='整理望远镜操作手册')
    store.set_setting('goal_dedup_judge',{'enabled':True,'method':'C'})
    judge.verdict='uncertain'
    result=goals.create(content='整理那个操作手册')
    assert result['dedup']['status']=='possible_duplicate'
    assert set(result['dedup']['target_ids'])=={a['submitted_id'],b['submitted_id']}


def test_dismissed_pair_is_not_automatically_reopened(store,setup):
    goals,judge=setup
    old=goals.create(content='整理操作手册')
    judge.verdict='uncertain'
    result=goals.create(content='整理操作说明手册')
    a,b=goals.get(old['submitted_id']),goals.get(result['submitted_id'])
    goals.dismiss_duplicate(a['id'],b['id'],expected_revision=a['revision'],other_revision=b['revision'])
    before=len(judge.calls)
    assert goals.review(result['submitted_id'])['dedup']['status']!='merged'
    assert len(judge.calls)==before


def test_unresolved_learning_deadline_is_preserved_as_model_data(store,setup):
    goals,judge=setup
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','unit','private')")
        a=write_learning_goal(conn,content='整理操作手册',kind='normal',deadline='等对方确定日期',
                              entry_id='A',evidence=[],current=goals.clock())
        b=write_learning_goal(conn,content='整理操作说明手册',kind='normal',deadline='等对方确定日期',
                              entry_id='A',evidence=[],current=goals.clock())
    result=goals.review(b['submitted_id'])
    assert result['dedup']['status']=='merged'
    assert judge.calls[-1]['incoming']['deadline']=='等对方确定日期'
    assert judge.calls[-1]['incoming']['content']['at'].endswith('+08:00')
    assert result['goal']['deadline_unresolved']


def test_review_notifications_survive_schedule_updates_and_closure(store,setup):
    goals,judge=setup
    goals.create(content='整理操作手册')
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','unit','private')")
        b=write_learning_goal(conn,content='整理操作说明手册',kind='normal',deadline=None,
                              entry_id='A',evidence=[],current=goals.clock())
    result=goals.review(b['submitted_id'])
    goals.update(result['goal']['id'],deadline='2026-10-12T15:00:00+08:00')
    goals.update(result['goal']['id'],content='手工更新正文')
    goals.update(result['goal']['id'],state='completed')
    notes=goals.pull()['items']
    status=[note for note in notes if note['kind']=='goal_dedup_result']
    assert len(status)==1 and status[0]['status']=='taken'
    assert str(b['submitted_id']) in status[0]['content']
