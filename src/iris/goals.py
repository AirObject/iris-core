"""Shared character goals, conservative deduplication and pull notifications.

Decision and persistence are separate. Learning joins its existing transaction;
model calls run after commit. Model decisions are applied in a second short
transaction against both observed revisions.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import time as timer
import weakref
from uuid import uuid4
from contextlib import nullcontext
from datetime import datetime, time, timedelta, timezone

from .claim_sequences import normalize_claim, same_claim_sequences
from .db import dumps
from .memory_ops import operation
from .model_health import utc_now
from .models import ModelError
from . import goal_dedup_judge as dedup_judge
from .people import canonical_subject
from .query_analysis import SubjectNames
from .state import local_time, role_zone

DEFAULTS = {'default_reminder_minutes': 60, 'overdue_reminders': True}


class GoalError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


class GoalConflict(Exception):
    pass


class GoalMerged(Exception):
    def __init__(self, canonical_id):
        self.canonical_id = canonical_id
        super().__init__('目标已合并')


def goal_settings(conn):
    row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='goals'").fetchone()
    return {**DEFAULTS, **(json.loads(row[0]) if row else {})}


def _current(value=None):
    return (value or utc_now()).astimezone(timezone.utc)


def _deadline(value, zone, *, strict=True):
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError()
        if len(value) == 10:
            stamp = datetime.combine(datetime.fromisoformat(value).date(), time(23, 59, 59), zone)
        else:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                raise ValueError()
        stamp.astimezone(zone)
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        if strict:
            raise GoalError('deadline', '截止时间须为 ISO 日期或带时区的 ISO 时间') from None
        return None


def _content(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
        raise GoalError('content', '正文须为 1—4000 个字符且不为空白')
    return value.strip()


def _soon(deadline, minutes):
    try:
        return deadline-timedelta(minutes=minutes)
    except OverflowError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _lead(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 525600):
        raise GoalError('reminder_minutes', '提醒提前量须为 0—525600 的整数或 null')
    return value


def _canonical_people(conn, values, *, limited=True):
    if not isinstance(values, (list, tuple, set)) or limited and len(values) > 100:
        raise GoalError('people', '涉及的人最多 100 个主体 ID')
    result = set()
    for value in values:
        if not isinstance(value, str) or not conn.execute('SELECT 1 FROM subjects WHERE id=?', (value,)).fetchone():
            raise GoalError('people', '涉及的主体不存在')
        person = canonical_subject(conn, value)
        if person in ('self', 'scene'):
            raise GoalError('people', '涉及的人不包含角色自己或场景主体')
        result.add(person)
    return sorted(result)


def _people(conn, goal_ids):
    result = {gid: [] for gid in goal_ids}
    if not result:
        return result
    cache = {}
    for row in conn.execute('SELECT goal_id,subject_id FROM goal_people WHERE goal_id IN (' +
                            ','.join('?' for _ in result) + ')', list(result)):
        sid = row['subject_id']
        if sid not in cache:
            cache[sid] = canonical_subject(conn, sid)
        if cache[sid] not in ('self', 'scene'):
            result[row['goal_id']].append(cache[sid])
    return {gid: sorted(set(ids)) for gid, ids in result.items()}


def _rows(conn, where='1', args=()):
    rows = [dict(r) for r in conn.execute('SELECT * FROM goals WHERE ' + where, args)]
    people = _people(conn, [r['id'] for r in rows])
    for row in rows:
        row['people'] = people[row['id']]
    return rows


def _row(conn, goal_id):
    rows = _rows(conn, 'id=?', (goal_id,))
    if not rows:
        raise KeyError(goal_id)
    return rows[0]


def _canonical_id(conn, goal_id):
    seen = set()
    while goal_id not in seen:
        seen.add(goal_id)
        row = conn.execute('SELECT merged_into FROM goals WHERE id=?', (goal_id,)).fetchone()
        if row is None:
            raise KeyError(goal_id)
        if row[0] is None:
            return goal_id
        goal_id = row[0]
    raise GoalError('goal_id', '目标合并关系成环')


def _check(row, revision=None):
    if row['merged_into'] is not None:
        raise GoalMerged(row['merged_into'])
    if revision is not None and row['revision'] != revision:
        raise GoalConflict()


def _equivalent_text(value):
    # Interior punctuation can encode decimals, signs, alternatives or a scene.
    return re.sub(r'\s+', ' ', value.strip().casefold()).rstrip('。！？.!?')


def _compatible(a, b):
    return (a['kind'] == b['kind'] and set(a.get('people', [])) == set(b.get('people', []))
            and (not a.get('deadline') or not b.get('deadline') or a['deadline'] == b['deadline'])
            and same_claim_sequences(a['content'], b['content']))


def decide_dedup(candidates, proposed):
    """Pure replaceable step: created | merged(target) | possible_duplicate | pending.

    Deterministic execution never needs pending; it is reserved for unavailable
    future judgments. Similarity is only a review hint, never proof of identity.
    """
    possible = None
    candidates = sorted(candidates, key=lambda r: (r.get('created_at', ''), r['id']))
    for candidate in candidates:
        if (candidate['id'] == proposed.get('id') or candidate.get('state', 'open') != 'open'
                or candidate.get('merged_into') is not None or not _compatible(candidate, proposed)):
            continue
        if _equivalent_text(candidate['content']) == _equivalent_text(proposed['content']):
            return {'status': 'merged', 'target_id': candidate['id']}
        left, right = normalize_claim(candidate['content']), normalize_claim(proposed['content'])
        if possible is None and left and right and difflib.SequenceMatcher(None, left, right).ratio() >= .88:
            possible = candidate['id']
    return {'status': 'possible_duplicate' if possible is not None else 'created', 'target_id': possible}


def _duplicate_ids(conn, goal_id):
    return [r[0] for r in conn.execute('''SELECT CASE WHEN goal_a=? THEN goal_b ELSE goal_a END
        FROM goal_duplicates WHERE status='possible' AND (goal_a=? OR goal_b=?) ORDER BY goal_a,goal_b''',
        (goal_id, goal_id, goal_id))]


def _project(conn, row, *, current, zone=None, settings=None):
    zone, settings = zone or role_zone(conn), settings or goal_settings(conn)
    result = {k: row[k] for k in ('id','content','kind','origin','state','deadline','reminder_minutes',
              'entry_id','host_key','revision','created_at','updated_at','closed_at','closed_by','merged_into','people')}
    deadline = row['deadline_at'] or _deadline(row['deadline'], zone, strict=False)
    result['deadline_unresolved'] = bool(row['deadline'] and not deadline)
    lead = row['reminder_minutes'] if row['reminder_minutes'] is not None else settings['default_reminder_minutes']
    result['effective_reminder_minutes'] = lead if row['kind'] == 'normal' else None
    active = row['state'] == 'open' and row['merged_into'] is None
    due = datetime.fromisoformat(deadline) if deadline else None
    result['overdue'] = bool(active and due and current > due)
    result['due_soon'] = bool(active and due and current >= _soon(due,lead))
    result['possible_duplicate_ids'] = _duplicate_ids(conn, row['id'])
    result['possible_duplicate'] = bool(result['possible_duplicate_ids'])
    if deadline:
        result['deadline'] = local_time(deadline, zone)
    for key in ('created_at','updated_at','closed_at'):
        result[key] = local_time(result[key], zone)
    return result


def goal_list(conn, *, state=None, kind=None, overdue=None, due_soon=None, entry_id=None,
              possible_duplicate=None, deadline_from=None, deadline_to=None, limit=30, offset=0, current=None):
    current, zone, settings = _current(current), role_zone(conn), goal_settings(conn)
    where, args = ['merged_into IS NULL'], []
    for key, value in (('state',state),('kind',kind),('entry_id',entry_id)):
        if value is not None:
            where.append(key+'=?')
            args.append(value)
    rows = [_project(conn, row, current=current,zone=zone,settings=settings) for row in _rows(conn,' AND '.join(where),args)]
    for key, value in (('overdue',overdue),('due_soon',due_soon),('possible_duplicate',possible_duplicate)):
        if value is not None:
            rows = [r for r in rows if r[key] == value]
    for boundary, lower in ((deadline_from,True),(deadline_to,False)):
        if boundary is not None:
            stamp = _deadline(boundary,zone)
            if lower and len(boundary) == 10:
                try:
                    stamp = datetime.combine(datetime.fromisoformat(boundary).date(),time.min,zone).astimezone(timezone.utc).isoformat()
                except (ValueError,OverflowError):
                    raise GoalError('deadline_from','日期下界超出角色时区支持的范围') from None
            rows = [r for r in rows if r['deadline'] and not r['deadline_unresolved'] and
                    ((datetime.fromisoformat(r['deadline']) >= datetime.fromisoformat(stamp)) if lower else
                     (datetime.fromisoformat(r['deadline']) <= datetime.fromisoformat(stamp)))]
    rows.sort(key=lambda r: (r['created_at'],r['id']),reverse=True)
    return {'items':rows[offset:offset+limit], 'total':len(rows), 'limit':limit, 'offset':offset}


def goal_partition(conn, *, entry_id=None, participants=(), limit=10, current=None):
    limit = max(0, min(limit, 10))
    if not limit:
        return []
    current, zone, settings = _current(current), role_zone(conn), goal_settings(conn)
    participants = {canonical_subject(conn,p) for p in participants if p not in ('self','scene')
                    and conn.execute('SELECT 1 FROM subjects WHERE id=?',(p,)).fetchone()}
    # Existing databases may be read before the scheduler normalizes legacy dates.
    # New goals use deadline_at directly, without a Python callback per row.
    conn.create_function('iris_goal_deadline', 1, lambda value: _deadline(value,zone,strict=False), deterministic=True)
    since_epoch = current - datetime(1970,1,1,tzinfo=timezone.utc)
    rows = [dict(r) for r in conn.execute("""WITH RECURSIVE participant_tree(id) AS (
            SELECT value FROM json_each(:participants)
            UNION SELECT s.id FROM subjects s JOIN participant_tree p ON s.merged_into=p.id
        ), dated AS (
            SELECT g.*, COALESCE(deadline_at,
                CASE WHEN deadline IS NOT NULL THEN iris_goal_deadline(deadline) END) AS sort_deadline
            FROM goals g WHERE state='open' AND merged_into IS NULL
        ), timed AS (
            SELECT dated.*, unixepoch(substr(sort_deadline,1,19))
                - COALESCE(reminder_minutes,:default_lead)*60 AS soon_seconds FROM dated
        ) SELECT * FROM timed ORDER BY
            CASE WHEN soon_seconds<:now_seconds OR (soon_seconds=:now_seconds AND
                CASE WHEN substr(sort_deadline,20,1)='.' THEN substr(sort_deadline,21,6)
                     ELSE '000000' END <= :now_fraction) THEN 0 ELSE 1 END,
            CASE WHEN entry_id=:entry_id THEN 0 ELSE 1 END,
            CASE WHEN EXISTS(SELECT 1 FROM goal_people gp WHERE gp.goal_id=timed.id
                AND gp.subject_id IN (SELECT id FROM participant_tree)) THEN 0 ELSE 1 END,
            COALESCE(sort_deadline,'9999'), created_at, id LIMIT :limit""", {
                'participants':dumps(sorted(participants)), 'entry_id':entry_id, 'limit':limit,
                'default_lead':settings['default_reminder_minutes'],
                'now_seconds':since_epoch.days*86400+since_epoch.seconds,
                'now_fraction':f'{current.microsecond:06d}',
            })]
    # Integer seconds plus the original fraction preserve exact microsecond
    # boundaries, which SQLite's floating date arithmetic would round.
    people = _people(conn, [row['id'] for row in rows])
    for row in rows:
        row['people'] = people[row['id']]
    return [_project(conn,row,current=current,zone=zone,settings=settings) for row in rows]


def _notification(row, zone):
    result = dict(row)
    for key in ('deadline_at','scheduled_at','published_at','taken_at','cancelled_at'):
        result[key] = local_time(result[key],zone)
    return result


def notification_list(conn, *, status=None, goal_id=None, limit=30, offset=0):
    clauses, args = ['1'], []
    for key, value in (('status',status),('goal_id',goal_id)):
        if value is not None:
            clauses.append(key+'=?')
            args.append(value)
    where, zone = ' AND '.join(clauses), role_zone(conn)
    total = conn.execute('SELECT COUNT(*) FROM notifications WHERE '+where,args).fetchone()[0]
    items = [_notification(r,zone) for r in conn.execute('SELECT * FROM notifications WHERE '+where+
              ' ORDER BY id DESC LIMIT ? OFFSET ?',(*args,limit,offset))]
    return {'items':items,'total':total,'limit':limit,'offset':offset}


def goal_detail(conn, goal_id, *, current=None):
    row, zone = _row(conn,goal_id), role_zone(conn)
    result = _project(conn,row,current=_current(current),zone=zone)
    if row['merged_into'] is not None:
        result['merged_into'] = _canonical_id(conn,goal_id)
    result['sources'] = [dict(r) for r in conn.execute('''SELECT m.id,m.entry_id,m.sender_subject_id,m.kind,m.content,
        m.occurred_at FROM goal_sources gs JOIN messages m ON m.id=gs.message_id WHERE gs.goal_id=? ORDER BY m.id''',(goal_id,))]
    for source in result['sources']:
        source['occurred_at'] = local_time(source['occurred_at'],zone)
    result['promise_memories'] = [dict(r) for r in conn.execute('''SELECT m.id,m.content,gm.memory_revision AS revision,
        m.revision AS current_revision,m.lifecycle FROM goal_memories gm JOIN memories m ON m.id=gm.memory_id
        WHERE gm.goal_id=? ORDER BY m.id''',(goal_id,))]
    result['merged_goals'] = [_project(conn,r,current=_current(current),zone=zone) for r in _rows(conn,'merged_into=?',(goal_id,))]
    result['notifications'] = [_notification(r,zone) for r in conn.execute('SELECT * FROM notifications WHERE goal_id=? ORDER BY id',(goal_id,))]
    review = conn.execute('SELECT * FROM goal_dedup_jobs WHERE goal_id=?',(goal_id,)).fetchone()
    result['dedup_review'] = None
    if review:
        result['dedup_review'] = {key:review[key] for key in ('state','method','input_revision','attempts')}
        result['dedup_review'].update(result=json.loads(review['result_json']),
            next_attempt_at=local_time(review['available_at'],zone) if review['state']=='pending' else None,
            updated_at=local_time(review['updated_at'],zone))
    result['reminder_plans'] = [dict(r) for r in conn.execute('SELECT * FROM goal_reminder_plans WHERE goal_id=? ORDER BY id',(goal_id,))]
    for plan in result['reminder_plans']:
        for key in ('scheduled_at','created_at'):
            plan[key] = local_time(plan[key],zone)
    return result


def _cancel(conn, goal_id, current, *, notifications=True):
    conn.execute("UPDATE goal_reminder_plans SET status='cancelled' WHERE goal_id=? AND status='scheduled'",(goal_id,))
    if notifications:
        conn.execute("UPDATE notifications SET status='cancelled',cancelled_at=? WHERE goal_id=? AND kind='goal_reminder' AND status='pending'",(current.isoformat(),goal_id))


def _publish(conn, row, kind, scheduled, current):
    overdue = row['deadline_at'] and datetime.fromisoformat(row['deadline_at']) < current
    if kind == 'overdue' or overdue:
        content = '已过期，请选择放弃或修改截止时间：'+row['content']
        conn.execute('UPDATE goals SET last_overdue_date=? WHERE id=?',
                     (current.astimezone(role_zone(conn)).date().isoformat(),row['id']))
    elif kind == 'due' or (row['deadline_at'] and datetime.fromisoformat(row['deadline_at']) == current):
        content = '目标已到期：'+row['content']
    else:
        content = '目标临近截止：'+row['content']
    return conn.execute('''INSERT INTO notifications(kind,goal_id,reminder_kind,deadline_at,scheduled_at,published_at,content)
        VALUES('goal_reminder',?,?,?,?,?,?)''',(row['id'],kind,row['deadline_at'],scheduled,current.isoformat(),content)).lastrowid


def _plan(conn, goal_id, kind, scheduled, current):
    conn.execute('''INSERT INTO goal_reminder_plans(goal_id,reminder_kind,scheduled_at,created_at)
        VALUES(?,?,?,?)''',(goal_id,kind,scheduled.astimezone(timezone.utc).isoformat(),current.isoformat()))


def _next_overdue(conn, row, current):
    if not goal_settings(conn)['overdue_reminders']:
        return
    zone = role_zone(conn)
    tomorrow = current.astimezone(zone).date()+timedelta(days=1)
    _plan(conn,row['id'],'overdue',datetime.combine(tomorrow,time.min,zone),current)


def _schedule(conn, row, current, *, immediate=True):
    """Reset the future schedule; callers decide which published items to cancel."""
    conn.execute('UPDATE goals SET schedule_initialized=1 WHERE id=?',(row['id'],))
    if row['kind']=='question' or row['state']!='open' or row['merged_into'] is not None or not row['deadline_at']:
        return
    deadline = datetime.fromisoformat(row['deadline_at'])
    minutes = row['reminder_minutes'] if row['reminder_minutes'] is not None else goal_settings(conn)['default_reminder_minutes']
    soon = _soon(deadline,minutes)
    if soon <= current:
        if immediate:
            _publish(conn,row,'immediate',current.isoformat(),current)
        if deadline > current:
            _plan(conn,row['id'],'due',deadline,current)
        else:
            _next_overdue(conn,row,current)
    else:
        _plan(conn,row['id'],'soon',soon,current)
        _plan(conn,row['id'],'due',deadline,current)


def _bump(conn, goal_id, current):
    conn.execute('UPDATE goals SET revision=revision+1,updated_at=? WHERE id=?',(current.isoformat(),goal_id))


def _possible(conn,a,b,current):
    a,b = sorted((a,b))
    inserted = conn.execute("INSERT OR IGNORE INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible',?)",(a,b,current.isoformat())).rowcount
    if inserted:
        _bump(conn,a,current)
        _bump(conn,b,current)


def _cancel_review(conn, goal_id, current, decision):
    conn.execute("""UPDATE goal_dedup_jobs SET state='cancelled',lease_token=NULL,lease_until=NULL,
        result_json=?,updated_at=? WHERE goal_id=? AND state IN ('pending','running')""",
        (dumps(decision),current.isoformat(),goal_id))


def _merge(conn,a,b,current,*,actor,semantic=False):
    # All callers must have checked both observed revisions before arriving here.
    if a['state']!='open' or b['state']!='open' or not (dedup_judge.compatible(a,b) if semantic else _compatible(a,b)):
        raise GoalError('goal_id','只能合并未结束且类型、人物、截止时间、数字和否定兼容的目标')
    target,source = sorted((a,b),key=lambda r:(r['created_at'],r['id']))
    tid,sid = target['id'],source['id']
    deadline = target['deadline'] or source['deadline']
    deadline_at = target['deadline_at'] or source['deadline_at']
    # Preserve an explicit lead; with two different leads use the earlier warning.
    leads = [r['reminder_minutes'] for r in (target,source) if r['reminder_minutes'] is not None]
    lead = max(leads) if leads else None
    conn.execute('UPDATE goals SET deadline=?,deadline_at=?,reminder_minutes=? WHERE id=?',(deadline,deadline_at,lead,tid))
    for table, columns in (('goal_sources','message_id'),('goal_people','subject_id'),('goal_memories','memory_id,memory_revision')):
        conn.execute(f'INSERT OR IGNORE INTO {table}(goal_id,{columns}) SELECT ?,{columns} FROM {table} WHERE goal_id=?',(tid,sid))
    conn.execute('UPDATE goals SET merged_into=? WHERE id=?',(tid,sid))
    _cancel_review(conn,sid,current,{'status':'merged','target_id':tid})
    # Flatten existing redirects; a published receipt itself stays immutable.
    conn.execute('UPDATE goals SET merged_into=? WHERE merged_into=?',(tid,sid))
    conn.execute("UPDATE notifications SET goal_id=? WHERE goal_id=? AND status='pending'",(tid,sid))
    _cancel(conn,sid,current,notifications=False)
    _cancel(conn,tid,current,notifications=False)
    for other in _duplicate_ids(conn,sid):
        if other!=tid:
            _possible(conn,tid,other,current)
    conn.execute("UPDATE goal_duplicates SET status='merged',resolved_at=?,resolved_by=? WHERE (goal_a=? OR goal_b=?) AND status='possible'",(current.isoformat(),actor,sid,sid))
    row = _row(conn,tid)
    # Keep one unclaimed notification per stage when both targets already emitted it.
    seen = set()
    for note in conn.execute("SELECT id,reminder_kind,scheduled_at FROM notifications WHERE goal_id=? AND kind='goal_reminder' AND status='pending' ORDER BY id",(tid,)).fetchall():
        kind = note['reminder_kind']
        when = datetime.fromisoformat(note['scheduled_at'])
        due = datetime.fromisoformat(row['deadline_at']) if row['deadline_at'] else None
        if kind == 'immediate' and due:
            kind = 'soon' if when < due else ('due' if when == due else 'overdue')
        key = (kind,when.astimezone(role_zone(conn)).date() if kind=='overdue' else None)
        if key in seen:
            conn.execute("UPDATE notifications SET status='cancelled',cancelled_at=? WHERE id=?",(current.isoformat(),note['id']))
        else:
            content=conn.execute('SELECT content FROM notifications WHERE id=?',(note['id'],)).fetchone()[0]
            conn.execute('UPDATE notifications SET content=? WHERE id=?',(content.split('：',1)[0]+'：'+row['content'],note['id']))
        seen.add(key)
    # Only the same deadline and reached phase can suppress an immediate alert.
    # Taken notifications stay with their original goal but still count here.
    due = datetime.fromisoformat(deadline_at) if deadline_at else None
    reached = 'soon' if due and current<due else 'due'
    already_reported = False
    for note in conn.execute("SELECT * FROM notifications WHERE goal_id IN (?,?) AND deadline_at IS ? AND status IN ('pending','taken')",(tid,sid,deadline_at)):
        phase = note['reminder_kind']
        if phase=='immediate':
            phase='soon' if due and datetime.fromisoformat(note['published_at'])<due else 'due'
        if phase=='overdue':
            phase='due'
        if phase==reached:
            already_reported=True
    _schedule(conn,row,current,immediate=not already_reported)
    _bump(conn,tid,current)
    _bump(conn,sid,current)
    operation(conn,'goal_merge','goal',tid,{'merged_id':sid},actor=actor,stamp=current.isoformat())
    return tid


def _sequence_conflicts(conn,candidates,proposed,options,*,dismissed=()):
    if dedup_judge.candidate_method(options)!='C2':
        return []
    speakers=dedup_judge.source_speakers(conn,[proposed,*candidates])
    return dedup_judge.select_sequence_conflicts(candidates,proposed,source_speakers=speakers,dismissed=dismissed)


def _create(conn, *, content, kind='normal', deadline=None, reminder_minutes=None, people=(), entry_id=None,
            host_key=None, origin='host', actor='host', current=None, evidence=(), learning=False):
    current = _current(current)
    if host_key is not None:
        previous = conn.execute('SELECT receipt_json FROM goals WHERE host_key=?',(host_key,)).fetchone()
        if previous and previous[0]:
            return json.loads(previous[0])
    content, lead = (str(content).strip() if learning else _content(content)), _lead(reminder_minutes)
    if kind not in ('normal','question'):
        raise GoalError('kind','目标类型须为 normal 或 question')
    if origin not in ('internal','host','admin'):
        raise GoalError('origin','目标来源不合法')
    if kind=='question' and (deadline is not None or lead is not None):
        raise GoalError('deadline' if deadline is not None else 'reminder_minutes','询问没有截止时间和提醒')
    for key,value in (('host_key',host_key),('entry_id',entry_id)):
        if value is not None and (not isinstance(value,str) or not value.strip() or len(value)>200):
            raise GoalError(key,'标识须为 1—200 个字符')
    if entry_id is not None and not conn.execute('SELECT 1 FROM entries WHERE id=?',(entry_id,)).fetchone():
        raise GoalError('entry_id','产生目标的入口不存在')
    people = _canonical_people(conn,people,limited=not learning)
    deadline_at = _deadline(deadline,role_zone(conn),strict=not learning)
    raw_deadline = deadline_at or (str(deadline) if deadline else None)
    stamp = current.isoformat()
    gid = conn.execute('''INSERT INTO goals(content,kind,origin,deadline,deadline_at,reminder_minutes,entry_id,host_key,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)''',(content,kind,origin,raw_deadline,deadline_at,lead,entry_id,host_key,stamp,stamp)).lastrowid
    conn.executemany('INSERT INTO goal_people(goal_id,subject_id) VALUES(?,?)',((gid,p) for p in people))
    conn.executemany('INSERT OR IGNORE INTO goal_sources(goal_id,message_id) VALUES(?,?)',((gid,m) for m in evidence))
    if evidence:
        # No semantic promise classifier exists. Keep self-authored memories
        # sharing the actual evidence, with the revision that supported this goal.
        conn.execute('''INSERT OR IGNORE INTO goal_memories(goal_id,memory_id,memory_revision)
            SELECT ?,m.id,m.revision FROM memories m JOIN sources s ON s.memory_id=m.id
            WHERE m.speaker_subject_id='self' AND m.lifecycle!='deleted' AND s.kind='message'
            AND s.message_id IN ('''+','.join('?' for _ in evidence)+')',(gid,*evidence))
    proposed = _row(conn,gid)
    candidates = _rows(conn,"state='open' AND merged_into IS NULL AND kind=? AND id!=?",(kind,gid))
    # Dates in older databases may not yet have been normalized by the scheduler.
    for candidate in candidates:
        candidate['deadline'] = _deadline(candidate['deadline'],role_zone(conn),strict=False) or candidate['deadline']
    options = dedup_judge.connection_settings(conn)
    conflicts = _sequence_conflicts(conn,candidates,proposed,options)
    if dedup_judge.method(options) == 'A':
        decision = decide_dedup(candidates,proposed)
    else:
        # Even equal short bodies can refer to different events in their sources.
        # Inside learning's transaction only stage deterministic candidates.
        pending = dedup_judge.select_candidates(candidates,proposed)
        decision = ({'status':'pending','target_id':None} if pending else
                    dedup_judge.decision_from_verdicts({},origin=origin,possible_ids=[r['id'] for r in conflicts]))
    if origin=='admin' and decision['status']=='merged':
        # An administrator's new item stays separate until explicitly resolved.
        decision = {**decision, 'status':'possible_duplicate'}
    target = gid
    if decision['status']=='merged':
        observed = next(r for r in candidates if r['id']==decision['target_id'])
        latest = _row(conn,observed['id'])
        _check(latest,observed['revision'])
        _check(_row(conn,gid),proposed['revision'])
        # Use normalized legacy values for compatibility and adopt the actual time.
        latest['deadline'] = observed['deadline']
        latest['deadline_at'] = latest['deadline_at'] or _deadline(latest['deadline'],role_zone(conn),strict=False)
        target = _merge(conn,latest,proposed,current,actor=actor)
        decision['target_id'] = target
    else:
        for row in conflicts:
            _possible(conn,gid,row['id'],current)
        if decision['status']=='possible_duplicate':
            for target_id in decision.get('target_ids',[decision['target_id']]):
                _possible(conn,gid,target_id,current)
        _schedule(conn,proposed,current)
    if decision['status']=='pending':
        # Give the first host/admin request its full inline budget before the
        # scheduler may claim the job. An interrupted request is still durable.
        available = current if origin=='internal' else current+timedelta(seconds=options['budget_seconds']+1)
        conn.execute('''INSERT INTO goal_dedup_jobs(goal_id,state,method,input_revision,available_at,
            result_json,created_at,updated_at) VALUES(?,'pending',?,?,?,?,?,?)''',
            (gid,dedup_judge.candidate_method(options),_row(conn,gid)['revision'],available.isoformat(),dumps(decision),stamp,stamp))
    operation(conn,'goal_create','goal',gid,{'origin':origin,'dedup':decision},actor=actor,stamp=stamp)
    receipt = {'goal':_project(conn,_row(conn,target),current=current),'submitted_id':gid,'dedup':decision}
    if host_key is not None:
        conn.execute('UPDATE goals SET receipt_json=? WHERE id=?',(dumps(receipt),gid))
    return receipt


def write_learning_goal(conn, *, content, kind, deadline, entry_id, evidence, current=None):
    people = set(SubjectNames(conn).mentioned(content)) - {'self','scene'}
    if evidence:
        people.update(r[0] for r in conn.execute('SELECT sender_subject_id FROM messages WHERE id IN ('+
                      ','.join('?' for _ in evidence)+") AND sender_subject_id NOT IN ('self','scene')",evidence))
    return _create(conn,content=content,kind=kind,deadline=None if kind=='question' else deadline,
                   entry_id=entry_id,people=people,evidence=evidence,origin='internal',actor='learning',current=current,learning=True)


class Goals:
    _key_guard = threading.Lock()
    _key_locks = weakref.WeakKeyDictionary()

    def __init__(self,store,*,clock=utc_now,gateway=None):
        self.store,self.clock,self.gateway = store,clock,gateway
        self._first_generation = True
        with self._key_guard:
            self._host_locks = self._key_locks.setdefault(store,weakref.WeakValueDictionary())

    def create(self,**fields):
        key = fields.get('host_key')
        with self._key_guard:
            lock = self._host_locks.get(key) if key is not None else None
            if key is not None and lock is None:
                lock = threading.RLock()
                self._host_locks[key] = lock
        # Serialize only identical host keys through the first response. No DB
        # lock spans the network; different keys use purpose-local admission.
        with lock if lock is not None else nullcontext():
            with self.store.write() as conn:
                if key is not None:
                    old = conn.execute('SELECT receipt_json FROM goals WHERE host_key=?',(key,)).fetchone()
                    if old and old[0]:
                        return json.loads(old[0])
                result = _create(conn,current=self.clock(),**fields)
            if result['dedup']['status']=='pending' and fields.get('origin','host')!='internal':
                result = self.review(result['submitted_id'],actor=fields.get('actor','host'))
                if key is not None:
                    with self.store.write() as conn:
                        conn.execute('UPDATE goals SET receipt_json=? WHERE id=?',(dumps(result),result['submitted_id']))
            return result

    @staticmethod
    def _decision(conn,goal_id):
        job = conn.execute('SELECT result_json FROM goal_dedup_jobs WHERE goal_id=?',(goal_id,)).fetchone()
        if job:
            return json.loads(job[0])
        row = conn.execute("""SELECT details_json FROM admin_operations WHERE object_type='goal'
            AND object_id=? AND action IN ('goal_create','goal_dedup_judged') ORDER BY id DESC LIMIT 1""",
            (str(goal_id),)).fetchone()
        return json.loads(row[0])['dedup'] if row else {'status':'created','target_id':None}

    def _receipt(self,conn,goal_id,decision):
        target = _canonical_id(conn,goal_id)
        return {'goal':_project(conn,_row(conn,target),current=_current(self.clock())),
                'submitted_id':goal_id,'dedup':decision}

    def pending_reviews(self, *, limit=8):
        """Indexed due work, including expired leases after a process restart."""
        stamp = _current(self.clock()).isoformat()
        with self.store.read() as conn:
            rows = conn.execute("""SELECT j.goal_id FROM (
                SELECT goal_id,available_at AS due_at FROM goal_dedup_jobs
                    WHERE state='pending' AND available_at<=?
                UNION ALL
                SELECT goal_id,lease_until AS due_at FROM goal_dedup_jobs
                    WHERE state='running' AND lease_until<=?
                ) j JOIN goals g ON g.id=j.goal_id
                WHERE g.state='open' AND g.merged_into IS NULL
                ORDER BY j.due_at,j.goal_id LIMIT ?""",(stamp,stamp,max(0,min(limit,8))))
            return [row[0] for row in rows]

    def _claim_review(self, goal_id):
        with self.store.write() as conn:
            current = _current(self.clock())
            options = dedup_judge.connection_settings(conn)
            proposed = _row(conn,goal_id)
            before = self._decision(conn,goal_id)
            if proposed['merged_into'] is not None:
                return None,self._receipt(conn,goal_id,{'status':'merged','target_id':_canonical_id(conn,goal_id)})
            if proposed['state']!='open':
                _cancel_review(conn,goal_id,current,{'status':'created','target_id':None,'reason':'goal_closed'})
                return None,self._receipt(conn,goal_id,self._decision(conn,goal_id))
            job = conn.execute('SELECT * FROM goal_dedup_jobs WHERE goal_id=?',(goal_id,)).fetchone()
            if not job or job['state'] in ('done','cancelled') or before['status']!='pending':
                return None,self._receipt(conn,goal_id,before)
            if not options['enabled'] or dedup_judge.method(options) not in ('B','C'):
                return None,self._receipt(conn,goal_id,{**before,'reason':'disabled'})
            if job['state']=='running' and job['lease_until'] and datetime.fromisoformat(job['lease_until'])>current:
                return None,self._receipt(conn,goal_id,{**before,'reason':'in_progress'})
            token = uuid4().hex
            until = current+timedelta(seconds=dedup_judge.validate_budget(options['budget_seconds'])+5)
            conn.execute("""UPDATE goal_dedup_jobs SET state='running',method=?,input_revision=?,attempts=attempts+1,
                lease_until=?,lease_token=?,candidate_revisions_json='{}',updated_at=? WHERE goal_id=?""",
                (dedup_judge.candidate_method(options),proposed['revision'],until.isoformat(),token,current.isoformat(),goal_id))
            return (token,options,job['attempts']+1),None

    def review(self,goal_id,*,actor='scheduler'):
        started = timer.monotonic()
        claimed,receipt = self._claim_review(goal_id)
        if claimed is None:
            return receipt
        token,options,attempt = claimed
        # Snapshot every candidate used by the model: changing a distractor may
        # remove the ambiguity that prevented a merge, or introduce a new one.
        with self.store.read() as conn:
            proposed = _row(conn,goal_id)
            candidates = _rows(conn,"state='open' AND merged_into IS NULL AND kind=? AND id!=?",(proposed['kind'],goal_id))
            zone = role_zone(conn)
            for row in [proposed,*candidates]:
                row['deadline'] = _deadline(row['deadline'],zone,strict=False) or row['deadline']
                row['deadline_at'] = row['deadline_at'] or _deadline(row['deadline'],zone,strict=False)
            dismissed = {r[0] for r in conn.execute("""SELECT CASE WHEN goal_a=? THEN goal_b ELSE goal_a END
                FROM goal_duplicates WHERE status='dismissed' AND (goal_a=? OR goal_b=?)""",(goal_id,goal_id,goal_id))}
            selected = dedup_judge.select_candidates(candidates,proposed,dismissed=dismissed,limit=dedup_judge.MAX_CANDIDATES+1)
            overflow = len(selected)>dedup_judge.MAX_CANDIDATES
            selected = selected[:dedup_judge.MAX_CANDIDATES]
            conflicts = _sequence_conflicts(conn,candidates,proposed,options,dismissed=dismissed)
            observed = [proposed,*selected,*conflicts]
            snapshots = [dedup_judge.material(conn,row) for row in observed]
            incoming,materials = snapshots[0],snapshots[1:1+len(selected)]
            source_snapshot = dedup_judge.source_speakers(conn,observed) if conflicts else {}
        revisions = {str(row['id']):row['revision'] for row in [*selected,*conflicts]}
        with self.store.write() as conn:
            retained = conn.execute("""UPDATE goal_dedup_jobs SET input_revision=?,candidate_revisions_json=?
                WHERE goal_id=? AND state='running' AND lease_token=?""",
                (proposed['revision'],dumps(revisions),goal_id,token)).rowcount
            if not retained:
                return self._receipt(conn,goal_id,self._decision(conn,goal_id))
        try:
            remaining = options['budget_seconds']-(timer.monotonic()-started)
            if remaining<=0:
                raise ModelError('retryable','goal judgment total timeout',reason='timeout')
            verdicts = dedup_judge.judge(self.gateway,incoming,materials,method=dedup_judge.method(options),
                         budget_seconds=remaining) if selected else {}
            decision = dedup_judge.decision_from_verdicts(verdicts,origin=proposed['origin'],overflow=overflow,
                         possible_ids=[row['id'] for row in conflicts])
        except ModelError as exc:
            decision = {'status':'pending','target_id':None,'reason':exc.reason or exc.category}
        with self.store.write() as conn:
            current = _current(self.clock())
            job = conn.execute('SELECT * FROM goal_dedup_jobs WHERE goal_id=?',(goal_id,)).fetchone()
            if job['state']!='running' or job['lease_token']!=token:
                return self._receipt(conn,goal_id,self._decision(conn,goal_id))
            active_options = dedup_judge.connection_settings(conn)
            latest = {row['id']:_row(conn,row['id']) for row in observed}
            stale = any(latest[row['id']]['revision']!=row['revision'] or latest[row['id']]['state']!='open'
                        or latest[row['id']]['merged_into'] is not None for row in observed)
            # Subject aliases and source excerpts are not covered by goal revisions.
            if not stale:
                for row,snapshot in zip(observed,snapshots):
                    check = latest[row['id']]
                    check['deadline'] = _deadline(check['deadline'],role_zone(conn),strict=False) or check['deadline']
                    if dedup_judge.material(conn,check)!=snapshot:
                        stale = True
                        break
            if conflicts and not stale:
                stale = dedup_judge.source_speakers(conn,observed)!=source_snapshot
            if stale:
                decision = {'status':'pending','target_id':None,'reason':'stale'}
            elif datetime.fromisoformat(job['lease_until'])<=current:
                decision = {'status':'pending','target_id':None,'reason':'lease_expired'}
            elif not active_options['enabled'] or dedup_judge.candidate_method(active_options)!=dedup_judge.candidate_method(options):
                decision = {'status':'pending','target_id':None,'reason':'configuration_changed'}
            elif decision['status']=='merged':
                target = next(row for row in selected if row['id']==decision['target_id'])
                decision['target_id'] = _merge(conn,target,proposed,current,actor=actor,semantic=True)
            elif decision['status']=='possible_duplicate':
                for target_id in decision['target_ids']:
                    _possible(conn,goal_id,target_id,current)
            pending = decision['status']=='pending'
            # Purpose health supplies the 429 Retry-After/pause/daily-limit gate;
            # this local backoff also prevents busy retries of stale/invalid data.
            available = current+timedelta(seconds=min(300,2**min(attempt,9))) if pending else current
            conn.execute("""UPDATE goal_dedup_jobs SET state=?,result_json=?,available_at=?,lease_token=NULL,
                lease_until=NULL,updated_at=? WHERE goal_id=?""",
                ('pending' if pending else 'done',dumps(decision),available.isoformat(),current.isoformat(),goal_id))
            operation(conn,'goal_dedup_judged','goal',goal_id,{'dedup':decision,
                'method':dedup_judge.candidate_method(options),'revision':proposed['revision'],
                'candidate_revisions':revisions},actor=actor,stamp=current.isoformat())
            if actor=='scheduler' and not pending:
                target = _canonical_id(conn,goal_id)
                labels={'merged':'目标已合并','possible_duplicate':'目标可能重复，请复核','created':'目标复核完成，保留独立目标'}
                conn.execute("""INSERT INTO notifications(kind,goal_id,scheduled_at,published_at,content)
                    VALUES('goal_dedup_result',?,?,?,?)""",(target,current.isoformat(),current.isoformat(),
                    labels[decision['status']]+'：'+str(goal_id)+((' → '+str(target)) if target!=goal_id else '')))
            return self._receipt(conn,goal_id,decision)

    def get_receipt(self,goal_id,decision):
        with self.store.read() as conn:
            return self._receipt(conn,goal_id,decision)

    def get(self,goal_id):
        with self.store.read() as conn:
            return goal_detail(conn,goal_id,current=self.clock())

    def update(self,goal_id,*,expected_revision=None,actor='host',**changes):
        if set(changes)-{'state','content','deadline','reminder_minutes'}:
            raise GoalError('body','存在未知字段')
        if not changes:
            raise GoalError('body','至少提供一个修改字段')
        with self.store.write() as conn:
            current = _current(self.clock())
            row = _row(conn,goal_id)
            if row['merged_into'] is not None:
                raise GoalMerged(_canonical_id(conn,goal_id))
            _check(row,expected_revision)
            if 'state' in changes and changes['state'] not in ('completed','abandoned'):
                raise GoalError('state','只能明确完成或放弃目标')
            if 'content' in changes:
                changes['content'] = _content(changes['content'])
            if 'reminder_minutes' in changes:
                changes['reminder_minutes'] = _lead(changes['reminder_minutes'])
            if row['kind']=='question' and (changes.get('deadline') is not None or changes.get('reminder_minutes') is not None):
                raise GoalError('deadline' if changes.get('deadline') is not None else 'reminder_minutes','询问没有截止时间和提醒')
            if 'deadline' in changes:
                changes['deadline'] = _deadline(changes['deadline'],role_zone(conn))
                changes['deadline_at'] = changes['deadline']
            if 'state' in changes and changes['state']!=row['state']:
                changes.update(closed_at=current.isoformat(),closed_by=actor)
            changed = {k:v for k,v in changes.items() if row[k]!=v}
            if changed:
                conn.execute('UPDATE goals SET '+','.join(k+'=?' for k in changed)+' WHERE id=?',(*changed.values(),goal_id))
                _bump(conn,goal_id,current)
                if 'state' in changed:
                    _cancel_review(conn,goal_id,current,{'status':'created','target_id':None,'reason':'goal_closed'})
                if {'deadline','reminder_minutes','state'}.intersection(changed):
                    _cancel(conn,goal_id,current)
                    _schedule(conn,_row(conn,goal_id),current)
                elif 'content' in changed:
                    # Text edits make existing unclaimed reminder text obsolete.
                    for note in conn.execute("SELECT * FROM notifications WHERE goal_id=? AND kind='goal_reminder' AND status='pending'",(goal_id,)).fetchall():
                        prefix=note['content'].split('：',1)[0]
                        conn.execute('UPDATE notifications SET content=? WHERE id=?',(prefix+'：'+changed['content'],note['id']))
                operation(conn,'goal_update','goal',goal_id,{'fields':sorted(changed),'revision_before':row['revision']},actor=actor,stamp=current.isoformat())
            return _project(conn,_row(conn,goal_id),current=current)

    def merge(self,goal_id,other_id,*,expected_revision,other_revision,actor='admin'):
        with self.store.write() as conn:
            current = _current(self.clock())
            a,b = _row(conn,goal_id),_row(conn,other_id)
            _check(a,expected_revision)
            _check(b,other_revision)
            if other_id not in _duplicate_ids(conn,goal_id):
                raise GoalError('other_id','两个目标没有待处理的可能重复标记')
            target = _merge(conn,a,b,current,actor=actor,semantic=True)
            return _project(conn,_row(conn,target),current=current)

    def dismiss_duplicate(self,goal_id,other_id,*,expected_revision,other_revision,actor='admin'):
        with self.store.write() as conn:
            current = _current(self.clock())
            _check(_row(conn,goal_id),expected_revision)
            _check(_row(conn,other_id),other_revision)
            if other_id not in _duplicate_ids(conn,goal_id):
                raise GoalError('other_id','两个目标没有待处理的可能重复标记')
            a,b = sorted((goal_id,other_id))
            conn.execute("UPDATE goal_duplicates SET status='dismissed',resolved_at=?,resolved_by=? WHERE goal_a=? AND goal_b=?",(current.isoformat(),actor,a,b))
            _bump(conn,a,current)
            _bump(conn,b,current)
            operation(conn,'goal_duplicate_dismiss','goal',goal_id,{'other_id':other_id},actor=actor,stamp=current.isoformat())
            return _project(conn,_row(conn,goal_id),current=current)

    def generate_notifications(self):
        current = _current(self.clock())
        # Avoid a writer transaction on idle scheduler ticks.
        with self.store.read() as conn:
            needed = conn.execute("SELECT 1 FROM goals WHERE schedule_initialized=0 LIMIT 1").fetchone() or conn.execute("SELECT 1 FROM goal_reminder_plans WHERE status='scheduled' AND scheduled_at<=? LIMIT 1",(current.isoformat(),)).fetchone()
        if not needed:
            self._first_generation = False
            return 0
        with self.store.write() as conn:
            current = _current(self.clock())
            before = conn.execute('SELECT COALESCE(MAX(id),0) FROM notifications').fetchone()[0]
            for row in _rows(conn,'schedule_initialized=0'):
                row['deadline_at'] = _deadline(row['deadline'],role_zone(conn),strict=False)
                conn.execute('UPDATE goals SET deadline_at=? WHERE id=?',(row['deadline_at'],row['id']))
                _schedule(conn,row,current)
            ids = [r[0] for r in conn.execute("SELECT DISTINCT goal_id FROM goal_reminder_plans WHERE status='scheduled' AND scheduled_at<=?",(current.isoformat(),))]
            for gid in ids:
                row = _row(conn,gid)
                plans = conn.execute("SELECT * FROM goal_reminder_plans WHERE goal_id=? AND status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at,id",(gid,current.isoformat())).fetchall()
                if row['state']!='open' or row['merged_into'] is not None:
                    _cancel(conn,gid,current)
                    continue
                latest = plans[-1]
                missed_on_start = (self._first_generation and latest['reminder_kind']!='overdue'
                                   and datetime.fromisoformat(latest['scheduled_at'])<current)
                kind = latest['reminder_kind'] if len(plans)==1 and not missed_on_start else 'immediate'
                local_date = current.astimezone(role_zone(conn)).date().isoformat()
                allowed = kind!='overdue' or (goal_settings(conn)['overdue_reminders'] and row['last_overdue_date']!=local_date)
                if allowed:
                    _publish(conn,row,kind,latest['scheduled_at'],current)
                conn.executemany("UPDATE goal_reminder_plans SET status=? WHERE id=?",(('published' if p['id']==latest['id'] and allowed else 'skipped',p['id']) for p in plans))
                if row['deadline_at'] and datetime.fromisoformat(row['deadline_at'])<=current:
                    _next_overdue(conn,row,current)
            after = conn.execute('SELECT COALESCE(MAX(id),0) FROM notifications').fetchone()[0]
            self._first_generation = False
            return after-before

    def pull(self,*,after=0,limit=30):
        if type(after) is not int or after<0:
            raise GoalError('after','游标须为非负整数')
        if type(limit) is not int or not 1<=limit<=100:
            raise GoalError('limit','每页数量须为 1—100')
        with self.store.write() as conn:
            rows = conn.execute("SELECT * FROM notifications WHERE id>? AND status!='cancelled' ORDER BY id LIMIT ?",(after,limit+1)).fetchall()
            ids=[r['id'] for r in rows[:limit]]
            stamp=_current(self.clock()).isoformat()
            conn.executemany("UPDATE notifications SET status='taken',taken_at=? WHERE id=? AND status='pending'",((stamp,n) for n in ids))
            zone=role_zone(conn)
            items=[_notification(conn.execute('SELECT * FROM notifications WHERE id=?',(n,)).fetchone(),zone) for n in ids]
            return {'items':items,'next_cursor':ids[-1] if ids else after,'has_more':len(rows)>limit}

    def configure(self,*,default_reminder_minutes=None,overdue_reminders=None,actor='admin'):
        if default_reminder_minutes is not None:
            _lead(default_reminder_minutes)
        if overdue_reminders is not None and type(overdue_reminders) is not bool:
            raise GoalError('overdue_reminders','过期提醒开关须为布尔值')
        with self.store.write() as conn:
            current=_current(self.clock())
            settings=goal_settings(conn)
            changes={k:v for k,v in {'default_reminder_minutes':default_reminder_minutes,'overdue_reminders':overdue_reminders}.items() if v is not None and v!=settings[k]}
            settings.update(changes)
            conn.execute("INSERT INTO runtime_settings(key,value_json) VALUES('goals',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",(dumps(settings),))
            if 'default_reminder_minutes' in changes:
                for row in _rows(conn,"state='open' AND merged_into IS NULL AND kind='normal' AND reminder_minutes IS NULL"):
                    if row['deadline_at'] and datetime.fromisoformat(row['deadline_at'])>current:
                        _cancel(conn,row['id'],current)
                        _schedule(conn,row,current)
            if 'overdue_reminders' in changes:
                conn.execute("UPDATE goal_reminder_plans SET status='cancelled' WHERE reminder_kind='overdue' AND status='scheduled'")
                if not settings['overdue_reminders']:
                    conn.execute("UPDATE notifications SET status='cancelled',cancelled_at=? WHERE reminder_kind='overdue' AND status='pending'",(current.isoformat(),))
                else:
                    for row in _rows(conn,"state='open' AND merged_into IS NULL AND deadline_at IS NOT NULL AND deadline_at<=?",(current.isoformat(),)):
                        _next_overdue(conn,row,current)
            operation(conn,'settings_goals','settings','goals',changes,actor=actor,stamp=current.isoformat())
            return settings
