"""Conservative, restartable model consolidation. All network IO is outside transactions."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from importlib.resources import files

from .db import dumps
from .memory_ops import lifecycle_settings, operation
from .model_health import utc_now
from .models import Gateway, ModelError
from .search_text import match_query, terms

# Candidate order and parameters are frozen before the first real-model probe.
METHODS = {'balanced': 0.40, 'strict': 0.65, 'broad': 0.20}
DEFAULTS = {'enabled': True, 'max_calls': 50, 'method': 'balanced'}
PROMPT_VERSION = 'consolidation_v1'
MAX_ANCESTORS = 64


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def last_edit(conn, mid):
    row = conn.execute("""SELECT actor,reason FROM memory_revisions WHERE memory_id=?
        AND json_extract(before_json,'$.content') IS NOT json_extract(after_json,'$.content')
        ORDER BY revision_after DESC,id DESC LIMIT 1""", (mid,)).fetchone()
    return 'admin' if row and row['actor'] != 'learning' and row['reason'] == 'manual edit' else 'learning'


def snapshot(conn, mid):
    row = conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone()
    if row is None:
        return None
    result = {k: row[k] for k in ('id','content','belief','importance','retention','lifecycle','revision',
              'pinned','event_time','created_at','updated_at','world','merged_into')}
    result.update(type=row['kind'], speaker=row['speaker_subject_id'], stance=row['stance'],
                  pinned=bool(row['pinned']), last_edit=last_edit(conn, mid))
    result['about'] = [r[0] for r in conn.execute('SELECT subject_id FROM memory_subjects WHERE memory_id=? ORDER BY subject_id',(mid,))]
    result['sources'] = [dict(r) for r in conn.execute("""SELECT DISTINCT m.id,m.dedupe_key AS key,m.entry_id,
        e.kind AS entry_kind,m.sender_subject_id AS sender,m.kind,m.occurred_at AS at,m.content AS text,
        m.quote_author_subject_id AS quote_author,m.quote_content
        FROM sources s JOIN messages m ON m.id=s.message_id JOIN entries e ON e.id=m.entry_id
        WHERE s.memory_id=? AND s.kind='message' ORDER BY julianday(m.occurred_at),m.id""", (mid,))]
    result['derived_from'] = [dict(r) for r in conn.execute("""SELECT source_memory_id AS id,source_revision AS revision
        FROM sources WHERE memory_id=? AND kind='memory' ORDER BY source_memory_id,id""", (mid,))]
    result['initial_setting'] = bool(conn.execute("SELECT 1 FROM sources WHERE memory_id=? AND kind='initial_setting'", (mid,)).fetchone())
    result['annotations'] = [dict(text=r['text'], evidence=json.loads(r['evidence_json']), source_keys=[s[0] for s in conn.execute('SELECT dedupe_key FROM messages WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id',(r['evidence_json'],))]) for r in conn.execute(
        'SELECT * FROM consolidation_annotations WHERE memory_id=? ORDER BY id', (mid,))]
    return result


def semantic(memory):
    # Retention and annotations do not manufacture a new fact or invalidate receipts.
    return {k: v for k,v in memory.items() if k not in ('retention','importance','updated_at','revision','annotations')}


def protected(m):
    return m['pinned'] or m['last_edit'] == 'admin'


def numeric(text):
    digits = dict(zip('零〇一二两三四五六七八九', (0,0,1,2,2,3,4,5,6,7,8,9)))
    def number(s):
        if s[0].isascii():
            return s
        total, part = 0, 0
        for ch in s:
            if ch in digits:
                part = digits[ch]
            else:
                total += (part or 1)*{'十':10,'百':100,'千':1000,'万':10000}[ch]
                part = 0
        return str(total+part)
    # Numeric quantities, dates, ordinal numbers; do not interpret 一直/一点 as quantities.
    values = {}
    for m in re.finditer(r'(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)(年|月|日|号|点|时|分|次|个|箱|支|件|米|楼|层|周|天)', text):
        unit, value = m[2], number(m[1])
        if unit in ('点','时'):
            prefix = text[max(0,m.start()-3):m.start()]
            if any(p in prefix for p in ('下午','晚上')) and int(value) < 12:
                value = str(int(value)+12)
            unit = 'hour'
        if unit in ('楼','层'):
            unit = 'floor'
        values.setdefault(unit,set()).add(value)
    return values


def merge_exclusion(a,b):
    if any(m['lifecycle']=='deleted' or m['merged_into'] for m in (a,b)):
        return 'deleted'
    if protected(a) or protected(b):
        return 'protected'
    if any(a[k]!=b[k] for k in ('speaker','stance','about','world')):
        return 'attribution'
    if a['event_time'] and b['event_time'] and datetime.fromisoformat(a['event_time']) != datetime.fromisoformat(b['event_time']):
        return 'event_time'
    na,nb=numeric(a['content']),numeric(b['content'])
    if any(not (na[k] <= nb[k] or nb[k] <= na[k]) for k in na.keys() & nb.keys()):
        return 'numbers'
    negative = r'不|没|未|无|非|\b(?:not|never|no)\b'
    if bool(re.search(negative,a['content'],re.I)) != bool(re.search(negative,b['content'],re.I)):
        return 'negation'
    if any(s['id']==b['id'] for s in a['derived_from']) or any(s['id']==a['id'] for s in b['derived_from']):
        return 'dependency'
    return None


def similarity(a,b):
    a,b = re.sub(r'\W','',a).casefold(),re.sub(r'\W','',b).casefold()
    aa,bb=set(a[i:i+2] for i in range(len(a)-1)),set(b[i:i+2] for i in range(len(b)-1))
    dice = 2*len(aa & bb)/max(1,len(aa)+len(bb))
    return max(dice,SequenceMatcher(None,a,b,autojunk=False).ratio())


def excerpt(text):
    # At most 300 UTF-8 bytes: conservative upper bound of 300 byte-level tokens.
    return text.encode('utf-8')[:300].decode('utf-8',errors='ignore')


def material(m):
    result = {k:v for k,v in m.items() if k not in ('updated_at','annotations','last_edit')}
    result['protected'] = protected(m)
    sources = m['sources']
    selected = sources if len(sources)<=4 else [sources[0],*sources[-3:]]
    result['sources'] = [{**s,'text':excerpt(s['text']), 'quote_content':excerpt(s['quote_content'] or '')} for s in selected]
    if m['lifecycle']=='deleted':
        result['content'],result['sources'] = None,[]
    return result


def dependency_material(conn, target):
    visited, stack, memories, losses = {target['id']}, [(s,target['id']) for s in target['derived_from']], [], []
    while stack:
        ref,child=stack.pop()
        mid=ref['id']
        if mid in visited:
            continue
        visited.add(mid)
        if len(visited)>MAX_ANCESTORS:
            return None
        m=snapshot(conn,mid)
        if m is None:
            continue
        memories.append(m)
        changed = bool(conn.execute("""SELECT 1 FROM memory_revisions WHERE memory_id=? AND revision_after>?
            AND json_extract(before_json,'$.content') IS NOT json_extract(after_json,'$.content') LIMIT 1""",
            (mid,ref['revision'] or 0)).fetchone())
        if m['lifecycle']!='active' or changed:
            losses.append({'id':mid,'dependent_id':child,'state':m['lifecycle'],'corrected':changed})
        stack.extend((s,mid) for s in m['derived_from'])
    if not losses:
        return None
    return {'target_id':target['id'],'memories':[target,*memories], 'losses':losses}


def work_fingerprint(kind,payload,method):
    return digest({'version':PROMPT_VERSION,'method':method,'kind':kind,
                   'memories':[semantic(m) for m in payload['memories']],
                   'losses':payload.get('losses',[])})


class _CallHealth:
    """Purpose-local view; never mutate the shared Gateway or its health object."""
    def __init__(self, owner, health, work):
        self.owner,self.real,self.work=owner,health,work
        self.call_id=None

    def check(self, kind, purpose, *, probe=False):
        # learning's existing admission includes the common daily generation limit.
        token=self.real.check('chat','learning') if self.real else None
        self.call_id=self.owner.reserve(self.work,purpose)
        return token

    def observe(self,*args,**kwargs):
        return self.real.observe(*args,**kwargs) if self.real else False


class _CallGateway(Gateway):
    def _record(self,*args,**kwargs):
        super()._record(*args,**kwargs)
        purpose,model,duration,category,error,usage=args[:6]
        self._co.finish_call(self.health.call_id,category,duration,usage,kwargs.get('finish_reason'),error,
                             kwargs.get('reasoning_effort'))


class Consolidation:
    def __init__(self, store, gateway=None, *, clock=utc_now):
        self.store,self.gateway,self.clock=store,gateway,clock
        self.run_id=None

    def reserve(self, work, purpose):
        with self.store.write() as conn:
            settings=json.loads(conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?',(self.run_id,)).fetchone()[0])
            count=conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE run_id=?',(self.run_id,)).fetchone()[0]
            if count>=settings['max_calls']:
                raise ModelError('budget','call budget',reason='call_budget')
            return conn.execute('INSERT INTO consolidation_calls(run_id,work_id,purpose,created_at) VALUES(?,?,?,?)',
                (self.run_id,work['id'],purpose,self.clock().isoformat())).lastrowid

    def finish_call(self,cid,result,duration,usage,finish=None,error=None,effort=None,raw=None):
        with self.store.write() as conn:
            conn.execute('''UPDATE consolidation_calls SET result=?,duration_ms=?,prompt_tokens=?,completion_tokens=?,
                finish_reason=?,error=?,reasoning_effort=?,raw_output=COALESCE(?,raw_output) WHERE id=?''',
                (result,duration,usage.get('prompt_tokens'),usage.get('completion_tokens'),finish,error,effort,raw,cid))

    def call(self,work,payload):
        purpose='consolidation_dependency' if work['kind']=='dependency' else ('consolidation_merge' if payload['merge_allowed'] else 'consolidation_conflict')
        name='dependency' if work['kind']=='dependency' else 'pair'
        prompt=files('iris').joinpath(f'prompts/consolidation_{name}_v1.md').read_text(encoding='utf-8')
        public={**payload,'memories':[material(m) for m in payload['memories']]}
        messages=[{'role':'system','content':prompt},{'role':'user','content':dumps(public)}]
        def unique(pairs):
            value={}
            for k,v in pairs:
                if k in value:
                    raise ValueError('duplicate_key')
                value[k]=v
            return value
        if isinstance(self.gateway,Gateway):
            # Shallow facade shares live config/client/pools, but has local admission.
            # It must never close the shared pools or replace their health object.
            gateway=object.__new__(_CallGateway)
            gateway.__dict__=self.gateway.__dict__.copy()
            gateway._co=self
            gateway.health=_CallHealth(self,getattr(self.gateway,'health',None),work)
        else:
            gateway=self.gateway
        deadline=time.monotonic()+120
        for repair in (False,True):
            call_purpose=purpose+('_repair' if repair else '')
            if isinstance(gateway,Gateway):
                reply=gateway.chat(messages,call_purpose,16000,_deadline=gateway.monotonic()+max(0,deadline-time.monotonic()))
                cid=gateway.health.call_id
                with self.store.write() as conn:
                    conn.execute('UPDATE consolidation_calls SET raw_output=? WHERE id=?',(reply.content,cid))
            else:
                health=_CallHealth(self,getattr(gateway,'health',None),work)
                health.check('chat',call_purpose)
                started=time.monotonic()
                try:
                    reply=gateway.chat(messages,call_purpose,16000,_deadline=deadline)
                except ModelError as exc:
                    self.finish_call(health.call_id,exc.category,round((time.monotonic()-started)*1000),{},error=exc.reason or exc.category)
                    raise
                self.finish_call(health.call_id,'success',round((time.monotonic()-started)*1000),reply.usage,reply.finish_reason,raw=reply.content)
            try:
                if reply.finish_reason!='stop':
                    raise ValueError('incomplete_output')
                result=json.loads(reply.content,object_pairs_hook=unique)
                self.validate(result,work,payload)
                return result
            except (ValueError,TypeError,KeyError) as error:
                if repair:
                    raise ValueError('invalid_output') from None
                # Explain the mechanical rejection, not a preferred semantic answer.
                visible=sorted({s['id'] for m in payload['memories'] for s in material(m)['sources']})
                category=str(error) if type(error) is ValueError and str(error) in {
                    'invalid_fields','invalid_decision','invalid_evidence','missing_evidence','invalid_updates',
                    'invalid_update','non_target_update','invalid_belief','invalid_text','invalid_merge',
                    'unexpected_updates','weakening_cannot_rewrite','duplicate_key','incomplete_output'} else 'invalid_json'
                repair_note=f'校验失败类型：{category}。evidence 只能引用可见来源消息 id：{dumps(visible)}；不要把记忆 id 当作消息 id。依照原始指令和材料完整修正一次，只输出 JSON。'
                messages += [{'role':'assistant','content':reply.content},
                    {'role':'user','content':repair_note}]
        raise AssertionError('unreachable')

    @staticmethod
    def validate(value,work,payload):
        allowed={'decision','reason','evidence','updates','keep_id'}
        if not isinstance(value,dict) or set(value)-allowed or not {'decision','reason','evidence','updates'}<=set(value):
            raise ValueError('invalid_fields')
        decisions=('keep','weaken','review') if work['kind']=='dependency' else ('merge','conflict','separate','uncertain')
        if value['decision'] not in decisions or not isinstance(value['reason'],str) or not value['reason'].strip():
            raise ValueError('invalid_decision')
        ids=set(payload.get('pair_ids',[payload.get('target_id')]))
        evidence={s['id'] for m in payload['memories'] for s in material(m)['sources']}
        if not isinstance(value['evidence'],list) or any(type(i) is not int or i not in evidence for i in value['evidence']):
            raise ValueError('invalid_evidence')
        if value['decision'] in ('merge','conflict') and not value['evidence']:
            raise ValueError('missing_evidence')
        if not isinstance(value['updates'],list):
            raise ValueError('invalid_updates')
        seen=set()
        for update in value['updates']:
            if not isinstance(update,dict) or set(update)-{'id','content','belief','annotation'} or type(update.get('id')) is not int or update['id'] not in ids or update['id'] in seen:
                raise ValueError('invalid_update')
            seen.add(update['id'])
            if work['kind']=='dependency' and update['id']!=payload['target_id']:
                raise ValueError('non_target_update')
            if 'belief' in update and (type(update['belief']) is not int or not 0<=update['belief']<=100):
                raise ValueError('invalid_belief')
            for key in ('content','annotation'):
                if key in update and (not isinstance(update[key],str) or not 0<len(update[key].strip())<=1000):
                    raise ValueError('invalid_text')
        if value['decision']=='merge' and (type(value.get('keep_id')) is not int or value['keep_id'] not in ids or value['updates']):
            raise ValueError('invalid_merge')
        if value['decision'] in ('keep','separate','uncertain') and value['updates']:
            raise ValueError('unexpected_updates')
        if value['decision']=='weaken' and any('content' in u for u in value['updates']):
            raise ValueError('weakening_cannot_rewrite')

    def plan(self,run,settings):
        candidates=[]
        with self.store.read() as conn:
            rows=conn.execute("""SELECT id FROM memories WHERE lifecycle!='deleted' AND id<=?
                ORDER BY importance DESC,julianday(updated_at) DESC,id""",(run['memory_through'],)).fetchall()
            seen_pairs=set()
            scanned=[]
            cache={}
            def get(mid):
                if mid not in cache:
                    cache[mid]=snapshot(conn,mid)
                return cache[mid]
            new_seeds=0
            for row in rows:
                m=get(row[0])
                # Dependencies are selected independently of text similarity.
                if m['derived_from']:
                    dep=dependency_material(conn,m)
                    if dep:
                        candidates.append(('dependency',dep))
                fp=digest([settings['method'],semantic(m)])
                old=conn.execute('SELECT fingerprint FROM consolidation_scanned WHERE memory_id=?',(m['id'],)).fetchone()
                if old and old[0]==fp:
                    continue
                if new_seeds>=128:
                    continue
                new_seeds+=1
                query=match_query(terms(m['content'])[:32])
                peers=conn.execute('SELECT rowid FROM memory_fts_jieba WHERE memory_fts_jieba MATCH ? ORDER BY rank LIMIT 64',(query,)).fetchall() if query else []
                for peer in peers:
                    n=get(peer[0])
                    pair=tuple(sorted((m['id'],n['id'])))
                    if m['id']==n['id'] or n['id']>run['memory_through'] or pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    # A proposition and its own supporting chain are reviewed by
                    # the dependency phase, never merged with one another.
                    def ancestors(value):
                        found=set()
                        stack=[s['id'] for s in value['derived_from']]
                        while stack and len(found)<MAX_ANCESTORS:
                            parent=stack.pop()
                            if parent in found:
                                continue
                            found.add(parent)
                            base=get(parent)
                            if base:
                                stack.extend(s['id'] for s in base['derived_from'])
                        return found
                    ma,na=ancestors(m),ancestors(n)
                    if m['id'] in na or n['id'] in ma:
                        continue
                    # Broad conflict discovery is shared by every merge candidate.
                    related=(bool(set(m['about']) & set(n['about'])) or m['about']==n['about']) and m['world']==n['world']
                    sim=similarity(m['content'],n['content'])
                    if not related or sim<0.20:
                        continue
                    a,b=sorted((m,n),key=lambda x:x['id'])
                    exclusion=merge_exclusion(a,b)
                    eligible=not exclusion and sim>=METHODS[settings['method']]
                    # Below the method threshold, still inspect genuinely incompatible
                    # facts; compatible low-similarity pairs wait for other evidence.
                    if not eligible and not exclusion:
                        continue
                    preferred=sorted((a,b),key=lambda x:(-len(x['content']),x['created_at'],x['id']))[0]['id']
                    candidates.append(('pair',{'pair_ids':[a['id'],b['id']],'memories':[a,b,*[get(i) for i in sorted(ma | na) if i not in pair]],'merge_allowed':eligible,
                        'merge_exclusion':exclusion or ('similarity' if not eligible else None),'preferred_keep_id':preferred}))
                scanned.append((m['id'],fp))
        for kind,payload in candidates:
            with self.store.write() as conn:
                fp=work_fingerprint(kind,payload,settings['method'])
                if conn.execute('SELECT 1 FROM consolidation_receipts WHERE fingerprint=?',(fp,)).fetchone():
                    continue
                conn.execute('''INSERT OR IGNORE INTO consolidation_work(fingerprint,kind,payload_json,importance,changed_at,created_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET payload_json=excluded.payload_json,state='pending',result_json=NULL WHERE consolidation_work.state='obsolete' ''',(fp,kind,dumps(payload),max(m['importance'] for m in payload['memories']),
                    max(m['updated_at'] for m in payload['memories']),self.clock().isoformat()))
        with self.store.write() as conn:
            conn.executemany('INSERT OR REPLACE INTO consolidation_scanned VALUES(?,?)',scanned)
            # A bounded run snapshot; the durable pending queue remains ordered.
            conn.execute('''INSERT OR IGNORE INTO consolidation_run_work(run_id,work_id)
                SELECT ?,id FROM consolidation_work WHERE state IN ('pending','decided')
                ORDER BY importance DESC,julianday(changed_at) DESC,id LIMIT 256''',(run['id'],))
            conn.execute('UPDATE consolidation_runs SET planned=1 WHERE run_id=?',(run['id'],))

    def _stale(self,conn,payload):
        for old in payload['memories']:
            current=snapshot(conn,old['id'])
            if current is None or digest(semantic(current))!=digest(semantic(old)):
                return True
            if current['revision'] != old['revision']:
                # A dependency annotation committed earlier by this run does not
                # alter support. External learning/admin revisions always conflict.
                source_only = payload.get('target_id') and old['id'] != payload['target_id']
                foreign = conn.execute('''SELECT 1 FROM memory_revisions WHERE memory_id=?
                    AND revision_after>? AND actor!='consolidation' LIMIT 1''', (old['id'],old['revision'])).fetchone()
                if not source_only or foreign:
                    return True
        return False

    def record(self,conn,work,outcome,reason=None,details=None,*,done=True):
        details=details or {}
        conn.execute('UPDATE consolidation_run_work SET outcome=?,reason=?,details_json=? WHERE run_id=? AND work_id=?',
            (outcome,reason,dumps(details),self.run_id,work['id']))
        if done:
            conn.execute("UPDATE consolidation_work SET state=? WHERE id=?",('obsolete' if reason=='revision_conflict' else 'done',work['id']))
        # No audit row for unchanged successful checks. Skips are aggregate only.
        if outcome not in ('checked','skipped'):
            payload=json.loads(work['payload_json'])
            mid=payload.get('target_id',payload['memories'][0]['id'])
            conn.execute('''INSERT OR IGNORE INTO maintenance_items(run_id,phase,item_key,memory_id,object_id,outcome,reason,details_json,created_at)
                VALUES(?,'consolidation',?,?,?,?,?,?,?)''',
                (self.run_id,str(work['id']),mid,mid,outcome,reason,dumps(details),self.clock().isoformat()))

    def halt(self,reason):
        with self.store.write() as conn:
            conn.execute('UPDATE consolidation_runs SET finished=1,skip_reason=? WHERE run_id=?',(reason,self.run_id))

    def run(self,run,*,stop=None):
        self.run_id=run['id']
        with self.store.write() as conn:
            config={**DEFAULTS,**self.store.setting('consolidation',{})}
            if config['method'] not in METHODS or type(config['max_calls']) is not int or not 0<=config['max_calls']<=50:
                raise ValueError('invalid consolidation settings')
            conn.execute('INSERT OR IGNORE INTO consolidation_runs(run_id,settings_json) VALUES(?,?)',(self.run_id,dumps(config)))
            state=dict(conn.execute('SELECT * FROM consolidation_runs WHERE run_id=?',(self.run_id,)).fetchone())
        if state['finished']:
            return True
        config=json.loads(state['settings_json'])
        if not config['enabled'] or not self.gateway or not callable(getattr(self.gateway,'chat',None)):
            self.halt('disabled' if not config['enabled'] else 'unconfigured')
            return True
        if not state['planned']:
            self.plan(run,config)
        with self.store.read() as conn:
            worklist=[dict(r) for r in conn.execute('''SELECT w.* FROM consolidation_work w JOIN consolidation_run_work rw ON rw.work_id=w.id
                WHERE rw.run_id=? AND rw.outcome IS NULL ORDER BY w.importance DESC,julianday(w.changed_at) DESC,w.id''',(self.run_id,))]
        budget_reason=None
        for work in worklist:
            if stop and stop():
                return False
            if work['state']!='pending':
                continue
            payload=json.loads(work['payload_json'])
            with self.store.write() as conn:
                stale=self._stale(conn,payload)
                if stale:
                    self.record(conn,work,'skipped','revision_conflict')
                    conn.executemany('DELETE FROM consolidation_scanned WHERE memory_id=?',[(m['id'],) for m in payload['memories']])
            if stale:
                continue
            try:
                result=self.call(work,payload)
            except ModelError as exc:
                reason=exc.reason or exc.category
                with self.store.write() as conn:
                    self.record(conn,work,'skipped' if reason in ('call_budget','usage_limit','paused','temporary_unavailable') or exc.paused else 'failed',reason,done=False)
                budget_reason=reason
                break
            except (ValueError,TypeError,KeyError) as exc:
                with self.store.write() as conn:
                    self.record(conn,work,'failed','invalid_output',done=False)
                continue
            with self.store.write() as conn:
                conn.execute("UPDATE consolidation_work SET state='decided',result_json=? WHERE id=?",(dumps(result),work['id']))
        if stop and stop():
            return False
        # Freeze every contradiction before applying any merge; unresolved incident
        # candidates conservatively block a merge until a later run finishes them.
        with self.store.read() as conn:
            all_work=[dict(r) for r in conn.execute('''SELECT w.*,rw.outcome FROM consolidation_work w JOIN consolidation_run_work rw ON rw.work_id=w.id
                WHERE rw.run_id=? ORDER BY w.id''',(self.run_id,))]
        blocked=set()
        for work in all_work:
            p=json.loads(work['payload_json'])
            r=json.loads(work['result_json']) if work['result_json'] else {}
            if work['kind']=='pair' and (work['state']=='pending' or r.get('decision')=='conflict'):
                blocked.add(frozenset(p['pair_ids']))
        all_work.sort(key=lambda w:(json.loads(w['result_json'] or '{}').get('decision')=='merge',w['id']))
        for work in all_work:
            if stop and stop():
                return False
            if work['state']!='decided' or work['outcome'] is not None:
                continue
            payload=json.loads(work['payload_json'])
            result=json.loads(work['result_json'])
            ids=set(payload.get('pair_ids',[payload.get('target_id')]))
            if result['decision']=='merge' and any(ids & pair for pair in blocked):
                with self.store.write() as conn:
                    self.record(conn,work,'skipped','unresolved_conflict',done=False)
                continue
            with self.store.write() as conn:
                if self._stale(conn,payload):
                    self.record(conn,work,'skipped','revision_conflict')
                    conn.executemany('DELETE FROM consolidation_scanned WHERE memory_id=?',[(m['id'],) for m in payload['memories']])
                    continue
                try:
                    conn.execute('SAVEPOINT consolidation_item')
                    outcome,details=self.apply(conn,work,payload,result)
                    self.record(conn,work,outcome,details=details)
                    conn.execute('INSERT OR IGNORE INTO consolidation_receipts VALUES(?)',(work['fingerprint'],))
                    after={**payload,'memories':[snapshot(conn,m['id']) for m in payload['memories']]}
                    conn.execute('INSERT OR IGNORE INTO consolidation_receipts VALUES(?)',(work_fingerprint(work['kind'],after,config['method']),))
                    conn.execute('RELEASE consolidation_item')
                except (ValueError,KeyError,TypeError):
                    conn.execute('ROLLBACK TO consolidation_item')
                    conn.execute('RELEASE consolidation_item')
                    self.record(conn,work,'failed','invalid_action',done=False)
        self.halt(budget_reason)
        return True

    def _revision(self,conn,old,changes,reason):
        if not changes:
            return
        fields=tuple(changes)
        conn.execute('UPDATE memories SET '+','.join(f'{k}=?' for k in fields)+',revision=revision+1,updated_at=? WHERE id=? AND revision=?',
                     (*[changes[k] for k in fields],self.clock().isoformat(),old['id'],old['revision']))
        after=snapshot(conn,old['id'])
        conn.execute('''INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,reason,actor,created_at)
            VALUES(?,?,?,?,?,?,'consolidation',?)''',(old['id'],old['revision'],after['revision'],dumps(old),dumps(after),reason,self.clock().isoformat()))

    def apply(self,conn,work,payload,result):
        before=[snapshot(conn,m['id']) for m in payload['memories']]
        decision=result['decision']
        details={'action_id':f'co-{work["id"]}','decision':decision,'before_memories':before,
                 'report':result['reason'],'evidence':result['evidence']}
        if decision in ('separate','uncertain','keep'):
            return 'checked',{}
        if decision=='merge':
            a,b=[m for m in before if m['id'] in payload['pair_ids']]
            if not payload['merge_allowed'] or merge_exclusion(a,b):
                raise ValueError('merge_forbidden')
            keep=next(m for m in before if m['id']==result['keep_id'])
            lose=next(m for m in (a,b) if m['id']!=result['keep_id'])
            # Preserve existing fuller wording, never synthesize a third proposition.
            # Incoming dependents keep real support after its duplicate is absorbed.
            inherited=[dict(r) for r in conn.execute('SELECT * FROM sources WHERE memory_id=?',(lose['id'],))]
            for source in inherited:
                exists=conn.execute('''SELECT 1 FROM sources WHERE memory_id=? AND kind=? AND message_id IS ? AND source_memory_id IS ?''',
                    (keep['id'],source['kind'],source['message_id'],source['source_memory_id'])).fetchone()
                if not exists:
                    conn.execute('''INSERT INTO sources(memory_id,kind,message_id,source_memory_id,source_revision,note,created_at)
                        VALUES(?,?,?,?,?,?,?)''',(keep['id'],source['kind'],source['message_id'],source['source_memory_id'],source['source_revision'],source['note'],source['created_at']))
            conn.execute('INSERT OR IGNORE INTO memory_tags SELECT ?,tag FROM memory_tags WHERE memory_id=?',(keep['id'],lose['id']))
            self._revision(conn,keep,{'belief':max(a['belief'],b['belief']),'importance':max(a['importance'],b['importance']),
                'lifecycle':'active','forgotten_at':None,'retention':max(a['retention'],b['retention'],lifecycle_settings(conn)['restore_threshold'])},result['reason'])
            redirected=conn.execute("UPDATE sources SET source_memory_id=?,source_revision=?,note=COALESCE(note,'')||? WHERE kind='memory' AND source_memory_id=?",
                (keep['id'],keep['revision']+1,f' [merged source {lose["id"]}]',lose['id'])).rowcount
            self._revision(conn,lose,{'lifecycle':'deleted','merged_into':keep['id'],'embedding':None,'embedding_model':None},result['reason'])
            details.update(result_id=keep['id'],absorbed_ids=[lose['id']],source_messages=sorted({s['id'] for m in before for s in m['sources']}),redirected_dependencies=redirected)
            outcome='merged'
        else:
            updates={u['id']:u for u in result['updates']}
            targets=[payload['target_id']] if work['kind']=='dependency' else payload['pair_ids']
            for mid in targets:
                m=next(m for m in before if m['id']==mid)
                u=updates.get(mid,{})
                changes={k:u[k] for k in ('content','belief') if k in u and u[k]!=m[k]}
                if work['kind']=='dependency':
                    only_forgotten=all(l['state']=='forgotten' and not l['corrected'] for l in payload['losses'])
                    if only_forgotten:
                        changes.pop('belief',None)
                    if decision=='weaken':
                        changes.pop('content',None)
                    if 'belief' in changes:
                        changes['belief']=min(changes['belief'],m['belief'])
                if protected(m):
                    changes={}
                annotation=u.get('annotation',result['reason'])
                inserted=conn.execute('''INSERT OR IGNORE INTO consolidation_annotations(memory_id,work_id,text,evidence_json,created_at)
                    VALUES(?,?,?,?,?)''',(mid,work['id'],annotation,dumps(result['evidence']),self.clock().isoformat())).rowcount
                if inserted and not changes:
                    # Annotation is a judgment change and invalidates in-flight feedback.
                    changes={'belief':m['belief']}
                self._revision(conn,m,changes,result['reason'])
            outcome='dependencies_reviewed' if work['kind']=='dependency' else 'conflicts'
        details['after_memories']=[snapshot(conn,m['id']) for m in before]
        operation(conn,'consolidation_'+('merge' if decision=='merge' else 'dependency' if work['kind']=='dependency' else 'conflict'),
                  'memory',before[0]['id'],{'run_id':self.run_id,'work_id':work['id'],'memory_ids':[m['id'] for m in before]},
                  actor='consolidation',stamp=self.clock().isoformat())
        return outcome,details
