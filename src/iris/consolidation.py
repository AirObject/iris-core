"""Conservative, restartable model consolidation. All network IO is outside transactions."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
from importlib.resources import files

from .db import dumps
from .learning import LearningEngine, PARTICIPANT_NUMBER, _same_claim_sequences
from .memory_ops import lifecycle_settings, operation
from .model_health import utc_now
from .models import Gateway, ModelError
from .search_text import match_query, terms, words

# Candidate order and parameters are frozen before the first real-model probe.
METHODS = {'balanced': 0.40, 'strict': 0.65, 'broad': 0.20}
RESOLUTIONS = ('report_only_v1',)
DEFAULTS = {'enabled': True, 'max_calls': 50, 'method': 'broad', 'resolution': 'report_only_v1',
            'merge_enabled': True, 'conflict_enabled': True, 'dependency_enabled': True,
            'persona_enabled': True, 'goal_review_enabled': True}
PROMPT_VERSION = 'consolidation_report_only_v1'
MAX_ANCESTORS = 64


class UnsafeWrite(ValueError):
    """A semantic safety rejection is final for these inputs, never a JSON retry."""


# The learning helpers are read-only imports. Learning does not itself expose a
# gender guard: consolidation refuses gendered pronouns in retained merge bodies.
GENDERED = re.compile(r"\b(?:he|him|his|she|her|hers)\b", re.I)


def consolidation_settings(conn):
    """One snapshot for new runs; evaluation-only method knobs remain internal."""
    row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='consolidation'").fetchone()
    value = {**DEFAULTS, **(json.loads(row[0]) if row else {}), 'resolution': 'report_only_v1'}
    if (value['method'] not in METHODS or type(value['max_calls']) is not int
            or not 0 <= value['max_calls'] <= 50
            or any(type(value[k]) is not bool for k in DEFAULTS if k.endswith('enabled'))):
        raise ValueError('invalid consolidation settings')
    return value


def run_settings(conn, run_id):
    row = conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?', (run_id,)).fetchone()
    return {**DEFAULTS, **json.loads(row[0]), 'resolution': 'report_only_v1'}


def pair_policy(settings):
    return {key: settings[key] for key in ('merge_enabled', 'conflict_enabled')}


def gendered_pronoun(text):
    return bool(GENDERED.search(text)) or any(
        word in {"他", "她", "他们", "她们", "他俩", "她俩", "他的", "她的"} for word in words(text))

REFERENCE = re.compile(r"(记忆|来源消息|消息|证据)\s*(?:id\s*)?[:：=#]?\s*(\d+)", re.I)
NUMBERED = re.compile(r"(?<![A-Za-z0-9_])(?:[PMS]\d+|#\d+)|\[\d+\]", re.I)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

def visible_sources(payload, evidence):
    return [s for m in payload['memories'] for s in material(m)['sources'] if s['id'] in evidence]


def same_write_sequences(left, right):
    return (_same_claim_sequences(left,right)
            and re.findall(r"\d+(?:\.\d+)?",left)==re.findall(r"\d+(?:\.\d+)?",right)
            and re.findall(r"\b(?:no|not|never|without)\b",left.lower())==re.findall(r"\b(?:no|not|never|without)\b",right.lower()))


def validate_merge_body(memory, payload, evidence):
    """Validate the retained existing body, never report prose or protocol words.

    Identity-like tokens must be grounded in visible source text or legal material
    metadata. Ordinary Latin words are not assumed to be subject IDs.
    """
    text=memory['content']
    sources=visible_sources(payload,evidence)
    entry_refs={str(source[k]) for source in sources for k in ('entry_id','key') if source.get(k)}
    reference_text=IDENTIFIER.sub(lambda match: '' if match[0] in entry_refs else match[0],text)
    if gendered_pronoun(text):
        raise UnsafeWrite('gendered_pronoun')
    try:
        LearningEngine._subject_reference(reference_text,{'participant_refs':{},'subjects':[],'aliases':[]})
    except ValueError:
        raise UnsafeWrite('participant_reference') from None
    if PARTICIPANT_NUMBER.search(reference_text) or NUMBERED.search(reference_text) or REFERENCE.search(reference_text):
        raise UnsafeWrite('numbered_reference')
    subjects={m['speaker'] for m in payload['memories']} | {s for m in payload['memories'] for s in m['about']}
    declared={memory['speaker'],*memory['about']}
    known=subjects | {str(v) for source in sources for k,v in source.items()
                      if k in ('sender','quote_author','entry_id','key') and v is not None}
    grounding=' '.join((source.get('text') or '')+' '+(source.get('quote_content') or '') for source in sources)
    known |= set(IDENTIFIER.findall(grounding))
    for identifier in IDENTIFIER.findall(text):
        if identifier in subjects and identifier not in declared:
            raise UnsafeWrite('subject_attribution')
        if ('_' in identifier or '-' in identifier) and identifier not in known:
            raise UnsafeWrite('unsupported_identifier')
    # The original broad method selects an existing body; it never rewrites one.
    # Compare against that exact input, not the whole message (which may contain
    # unrelated numbers/negations, while the summary can date facts from metadata).
    original=next((m for m in payload['memories'] if m['id']==memory['id']),None)
    if original is None or not same_write_sequences(text,original['content']):
        raise UnsafeWrite('claim_sequences')
    if text!=original['content']:
        raise UnsafeWrite('merge_body_rewrite')


def suggestion_review(annotation):
    """Administrator-only review metadata; confirmation never changes a memory."""
    status='cleared' if annotation['cleared_at'] else 'confirmed' if annotation['confirmed_at'] else 'pending'
    return {'label':'整理建议（模型建议）','model_suggestion':True,'visibility':'admin_only',
            'review_status':status,'review_status_label':{'pending':'待确认','confirmed':'已确认','cleared':'已清除'}[status],
            'confirmed_at':annotation['confirmed_at'],'confirmed_by':annotation['confirmed_by'],
            'cleared_at':annotation['cleared_at'],'cleared_by':annotation['cleared_by']}


def memory_annotations(conn, memory_ids, *, include_reports=False):
    """Active suggestions for administrator details and consolidation audit snapshots only."""
    result={mid:[] for mid in memory_ids}
    if not result:
        return result
    rows=conn.execute("""SELECT a.*,m.revision AS current_revision FROM consolidation_annotations a
        JOIN memories m ON m.id=a.memory_id WHERE a.cleared_at IS NULL AND a.memory_id IN
        (SELECT value FROM json_each(?)) ORDER BY a.id""",(dumps(list(result)),))
    for row in rows:
        changed=row['current_revision']!=row['memory_revision']
        item={'id':row['id'],'kind':row['kind'],'text':row['text'],
              'related_memory_ids':json.loads(row['related_ids_json']), 'superseded_by':row['superseded_by'],
              'evidence':json.loads(row['evidence_json']), 'created_at':row['created_at'],
              'memory_revision':row['memory_revision'],'modified_since_annotation':changed,
              'status_label':'标注后已修改' if changed else '当前标注',
              'report':{'run_id':row['run_id'],'work_id':row['work_id'],
                        'url':f"/admin/api/maintenance/{row['run_id']}"}}
        item.update(suggestion_review(row))
        if include_reports:
            report=conn.execute("SELECT details_json FROM maintenance_items WHERE run_id=? AND phase='consolidation' AND item_key=?",
                                (row['run_id'],str(row['work_id']))).fetchone()
            details=json.loads(report[0]) if report else {}
            item['report'].update(conclusion=details.get('decision'),
                                  reason='模型建议：'+details.get('report',''),model_suggestion=True,
                                  source_excerpts=details.get('source_excerpts',[]))
        result[row['memory_id']].append(item)
    return result


def confirm_annotation(store, memory_id, annotation_id, expected_revision, *, clock=utc_now):
    """Accept a suggestion as reviewed metadata; never publish it to host recall."""
    with store.write() as conn:
        memory=conn.execute('SELECT revision FROM memories WHERE id=? AND purged_at IS NULL',(memory_id,)).fetchone()
        annotation=conn.execute('SELECT * FROM consolidation_annotations WHERE id=? AND memory_id=?',
                                (annotation_id,memory_id)).fetchone()
        if not memory or not annotation:
            raise KeyError(annotation_id)
        if memory[0]!=expected_revision or annotation['cleared_at'] is not None:
            return False
        if annotation['confirmed_at'] is None:
            stamp=clock().isoformat()
            conn.execute("UPDATE consolidation_annotations SET confirmed_at=?,confirmed_by='admin' WHERE id=?",(stamp,annotation_id))
            operation(conn,'consolidation_annotation_confirm','memory',memory_id,{'annotation_id':annotation_id,
                      'run_id':annotation['run_id'],'expected_revision':expected_revision},actor='admin',stamp=stamp)
        return True


def clear_annotation(store, memory_id, annotation_id, expected_revision, *, clock=utc_now):
    """Clear reversible metadata without changing content, belief or recall revision."""
    with store.write() as conn:
        memory=conn.execute('SELECT revision FROM memories WHERE id=? AND purged_at IS NULL',(memory_id,)).fetchone()
        annotation=conn.execute('SELECT * FROM consolidation_annotations WHERE id=? AND memory_id=?',
                                (annotation_id,memory_id)).fetchone()
        if not memory or not annotation:
            raise KeyError(annotation_id)
        if memory[0]!=expected_revision:
            return False
        if annotation['cleared_at'] is None:
            stamp=clock().isoformat()
            conn.execute("UPDATE consolidation_annotations SET cleared_at=?,cleared_by='admin' WHERE id=?",(stamp,annotation_id))
            operation(conn,'consolidation_annotation_clear','memory',memory_id,{'annotation_id':annotation_id,
                      'run_id':annotation['run_id'],'expected_revision':expected_revision},actor='admin',stamp=stamp)
        return True


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
    result['annotations'] = memory_annotations(conn,[mid])[mid]
    for annotation in result['annotations']:
        annotation['source_keys']=[r[0] for r in conn.execute(
            'SELECT dedupe_key FROM messages WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id',
            (dumps(annotation['evidence']),))]
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
            if any(p in prefix for p in ('下午','晚上')) and Decimal(value) < 12:
                value = str(Decimal(value)+12)
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


def excerpt(text, limit=300):
    # UTF-8 bytes conservatively bound byte-level token counts.
    return text.encode('utf-8')[:limit].decode('utf-8',errors='ignore')


def source_excerpt(source):
    # A quoted message shares the same 300-byte allowance with its body.
    quote = excerpt(source['quote_content'] or '', 150)
    body = excerpt(source['text'], 300-len(quote.encode('utf-8')))
    quote = excerpt(source['quote_content'] or '', 300-len(body.encode('utf-8')))
    return {**source, 'text': body, 'quote_content': quote}


def material(m):
    result = {k:v for k,v in m.items() if k not in ('updated_at','annotations','last_edit')}
    result['protected'] = protected(m)
    sources = m['sources']
    selected = sources if len(sources)<=4 else [sources[0],*sources[-3:]]
    result['sources'] = [source_excerpt(s) for s in selected]
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
    value = {'version':PROMPT_VERSION,'method':method,'kind':kind,
             'memories':[semantic(m) for m in payload['memories']],
             'losses':payload.get('losses',[]), 'resolution':payload.get('resolution','original_v1')}
    # Preserve already-completed default work across this upgrade. Only a
    # changed pair policy needs its own receipt; dependency work is independent.
    policy = payload.get('policy', pair_policy(DEFAULTS))
    if kind == 'pair' and policy != pair_policy(DEFAULTS):
        value['policy'] = policy
    return digest(value)


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
            # A generation retry/repair must still leave one call for the checker.
            required = 2 if purpose.startswith('persona_generate') else 1
            if count+required>settings['max_calls']:
                raise ModelError('budget','call budget',reason='call_budget')
            return conn.execute('INSERT INTO consolidation_calls(run_id,work_id,purpose,created_at) VALUES(?,?,?,?)',
                (self.run_id,work['id'],purpose,self.clock().isoformat())).lastrowid

    def finish_call(self,cid,result,duration,usage,finish=None,error=None,effort=None,raw=None):
        with self.store.write() as conn:
            conn.execute('''UPDATE consolidation_calls SET result=?,duration_ms=?,prompt_tokens=?,completion_tokens=?,
                finish_reason=?,error=?,reasoning_effort=?,raw_output=COALESCE(?,raw_output) WHERE id=?''',
                (result,duration,usage.get('prompt_tokens'),usage.get('completion_tokens'),finish,error,effort,raw,cid))

    def call(self,work,payload,*,report_only=False):
        purpose='consolidation_dependency' if work['kind']=='dependency' else (
            'consolidation_conflict' if report_only or not payload['merge_allowed'] else 'consolidation_merge')
        if work['kind']=='dependency':
            name='dependency_report_only_v1'
        elif report_only:
            name='conflict_report_only_v1'
        else:
            name='pair_v1'  # Complete original broad prompt frozen in 3d31c95.
        prompt=files('iris').joinpath(f'prompts/consolidation_{name}.md').read_text(encoding='utf-8')
        public={k:v for k,v in payload.items() if k not in ('resolution','policy')}
        public['memories']=[material(m) for m in payload['memories']]
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
                self.validate(result,work,payload,report_only=report_only or work['kind']=='dependency')
                return result
            except UnsafeWrite:
                raise
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
    def validate(value,work,payload,*,report_only=False):
        allowed={'decision','reason','evidence','updates','keep_id'}
        if not isinstance(value,dict) or set(value)-allowed or not {'decision','reason','evidence','updates'}<=set(value):
            raise ValueError('invalid_fields')
        decisions=('keep','weaken','review') if work['kind']=='dependency' else ('merge','conflict','separate','uncertain')
        if value['decision'] not in decisions or not isinstance(value['reason'],str) or not value['reason'].strip():
            raise ValueError('invalid_decision')
        ids=set(payload.get('pair_ids',[payload.get('target_id')]))
        evidence={s['id'] for m in payload['memories'] for s in material(m)['sources']}
        if not isinstance(value['evidence'],list) or any(type(i) is not int or i not in evidence for i in value['evidence']):
            raise UnsafeWrite('invalid_evidence')
        if value['decision'] in ('merge','conflict') and not value['evidence']:
            raise UnsafeWrite('missing_evidence')
        if not isinstance(value['updates'],list):
            raise ValueError('invalid_updates')
        seen=set()
        for update in value['updates']:
            if not isinstance(update,dict) or set(update)-{'id','content','belief','annotation','subject_ids','assessment','superseded_by'} or type(update.get('id')) is not int or update['id'] not in ids or update['id'] in seen:
                raise UnsafeWrite('invalid_update')
            seen.add(update['id'])
            if report_only and set(update)-{'id','annotation','assessment','superseded_by'}:
                raise UnsafeWrite('report_only_fields')
            if 'subject_ids' in update and (not isinstance(update['subject_ids'],list) or
                any(s not in {m['speaker'] for m in payload['memories']} | {s for m in payload['memories'] for s in m['about']} for s in update['subject_ids'])):
                raise UnsafeWrite('unknown_subject')
            if update.get('assessment')=='superseded':
                if type(update.get('superseded_by')) is not int or update['superseded_by'] not in ids-{update['id']}:
                    raise UnsafeWrite('invalid_superseded_target')
            elif update.get('superseded_by') is not None:
                raise UnsafeWrite('invalid_superseded_target')
            if 'assessment' in update and update['assessment'] not in ('superseded','disputed','unsupported','source_error'):
                raise UnsafeWrite('invalid_assessment')
            if work['kind']=='dependency' and update['id']!=payload['target_id']:
                raise UnsafeWrite('non_target_update')
            if 'belief' in update and (type(update['belief']) is not int or not 0<=update['belief']<=100):
                raise UnsafeWrite('invalid_belief')
            for key in ('content','annotation'):
                if key in update and (not isinstance(update[key],str) or not 0<len(update[key].strip())<=1000):
                    raise UnsafeWrite('invalid_text')
        if report_only and work['kind']=='pair' and value['decision']!='conflict':
            raise UnsafeWrite('invalid_report_decision')
        if value['decision']=='merge' and (type(value.get('keep_id')) is not int or value['keep_id'] not in ids or value['updates']):
            raise UnsafeWrite('invalid_merge')
        if value['decision'] in ('keep','separate','uncertain') and value['updates']:
            raise UnsafeWrite('unexpected_updates')
        if value['decision']=='weaken' and any('content' in u for u in value['updates']):
            raise UnsafeWrite('weakening_cannot_rewrite')

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
                if settings['dependency_enabled'] and m['derived_from']:
                    dep=dependency_material(conn,m)
                    if dep:
                        candidates.append(('dependency',dep))
                scan_key=[PROMPT_VERSION,settings['method'],settings['resolution'],semantic(m)]
                if pair_policy(settings) != pair_policy(DEFAULTS):
                    scan_key.append(pair_policy(settings))
                fp=digest(scan_key)
                if not settings['merge_enabled'] and not settings['conflict_enabled']:
                    continue
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
                    eligible=settings['merge_enabled'] and not exclusion and sim>=METHODS[settings['method']]
                    if not settings['conflict_enabled'] and not eligible:
                        continue
                    # Below the method threshold, still inspect genuinely incompatible
                    # facts; compatible low-similarity pairs wait for other evidence.
                    if settings['merge_enabled'] and not eligible and not exclusion:
                        continue
                    preferred=sorted((a,b),key=lambda x:(-len(x['content']),x['created_at'],x['id']))[0]['id']
                    candidates.append(('pair',{'pair_ids':[a['id'],b['id']],'memories':[a,b,*[get(i) for i in sorted(ma | na) if i not in pair]],'merge_allowed':eligible,
                        'merge_exclusion':exclusion or ('disabled' if not settings['merge_enabled'] else 'similarity' if not eligible else None),'preferred_keep_id':preferred}))
                scanned.append((m['id'],fp))
        for kind,payload in candidates:
            payload['resolution']=settings['resolution']
            if kind=='pair':
                payload['policy']=pair_policy(settings)
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
                SELECT ?,id FROM consolidation_work WHERE state IN ('pending','classified','decided')
                AND COALESCE(json_extract(payload_json,'$.resolution'),'original_v1')=?
                AND ((kind='dependency' AND ?)
                     OR (kind='pair' AND (? OR ?)
                         AND COALESCE(json_extract(payload_json,'$.policy.merge_enabled'),1)=?
                         AND COALESCE(json_extract(payload_json,'$.policy.conflict_enabled'),1)=?))
                ORDER BY importance DESC,julianday(changed_at) DESC,id LIMIT 256''',
                (run['id'],settings['resolution'],settings['dependency_enabled'],settings['merge_enabled'],
                 settings['conflict_enabled'],settings['merge_enabled'],settings['conflict_enabled']))
            conn.execute('UPDATE consolidation_runs SET planned=1 WHERE run_id=?',(run['id'],))

    def _stale(self,conn,payload):
        for old in payload['memories']:
            current=snapshot(conn,old['id'])
            if current is None or digest(semantic(current))!=digest(semantic(old)):
                return True
            if current['revision'] != old['revision']:
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
            if not conn.execute('SELECT 1 FROM consolidation_runs WHERE run_id=?',(self.run_id,)).fetchone():
                conn.execute('INSERT INTO consolidation_runs(run_id,settings_json) VALUES(?,?)',
                             (self.run_id,dumps(consolidation_settings(conn))))
            state=dict(conn.execute('SELECT * FROM consolidation_runs WHERE run_id=?',(self.run_id,)).fetchone())
        if state['finished']:
            return True
        config={**DEFAULTS,**json.loads(state['settings_json'])}
        config['resolution']='report_only_v1'  # Old policies cannot re-enable writes.
        if not config['enabled'] or not self.gateway or not callable(getattr(self.gateway,'chat',None)):
            self.halt('disabled' if not config['enabled'] else 'unconfigured')
            return True
        if not any(config[key] for key in ('merge_enabled','conflict_enabled','dependency_enabled')):
            self.halt('model_items_disabled')
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
            if work['state'] not in ('pending','classified'):
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
                if work['state']=='classified':
                    classification=json.loads(work['result_json'])
                else:
                    classification=self.call(work,payload)
                    if work['kind']=='pair' and classification['decision']=='conflict':
                        with self.store.write() as conn:
                            conn.execute("UPDATE consolidation_work SET state='classified',result_json=? WHERE id=?",
                                         (dumps(classification),work['id']))
                        if stop and stop():
                            return False
                if work['kind']=='pair' and classification['decision']=='conflict' and config['conflict_enabled']:
                    result=self.call(work,payload,report_only=True)
                else:
                    result=classification
            except ModelError as exc:
                reason=exc.reason or exc.category
                with self.store.write() as conn:
                    self.record(conn,work,'skipped' if reason in ('call_budget','usage_limit','paused','temporary_unavailable') or exc.paused else 'failed',reason,done=False)
                budget_reason=reason
                break
            except UnsafeWrite as exc:
                with self.store.write() as conn:
                    self.record(conn,work,'failed','unsafe_write',{'rejection':str(exc)})
                    conn.execute('INSERT OR IGNORE INTO consolidation_receipts VALUES(?)',(work['fingerprint'],))
                continue
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
            if work['kind']=='pair' and (work['state'] in ('pending','classified') or work['outcome']=='failed' or r.get('decision')=='conflict'):
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
                    if work['kind']=='pair' and result['decision']=='conflict' and not config['conflict_enabled']:
                        outcome,details='checked',{}
                    else:
                        outcome,details=self.apply(conn,work,payload,result)
                    self.record(conn,work,outcome,details=details)
                    conn.execute('INSERT OR IGNORE INTO consolidation_receipts VALUES(?)',(work['fingerprint'],))
                    after={**payload,'memories':[snapshot(conn,m['id']) for m in payload['memories']]}
                    conn.execute('INSERT OR IGNORE INTO consolidation_receipts VALUES(?)',(work_fingerprint(work['kind'],after,config['method']),))
                    conn.execute('RELEASE consolidation_item')
                except UnsafeWrite as exc:
                    conn.execute('ROLLBACK TO consolidation_item')
                    conn.execute('RELEASE consolidation_item')
                    self.record(conn,work,'failed','unsafe_write',{'rejection':str(exc)})
                    conn.execute('INSERT OR IGNORE INTO consolidation_receipts VALUES(?)',(work['fingerprint'],))
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
                 'report':result['reason'],'evidence':result['evidence'],
                 'source_excerpts':[{'memory_id':m['id'],'sources':material(m)['sources']} for m in before],
                 'conclusions':result['updates']}
        if decision in ('separate','uncertain','keep'):
            return 'checked',{}
        if decision=='merge':
            a,b=[m for m in before if m['id'] in payload['pair_ids']]
            if not payload['merge_allowed'] or merge_exclusion(a,b):
                raise ValueError('merge_forbidden')
            keep=next(m for m in before if m['id']==result['keep_id'])
            lose=next(m for m in (a,b) if m['id']!=result['keep_id'])
            validate_merge_body(keep,payload,result['evidence'])
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
            # Model text / belief updates have no executable path here. Only
            # typed, reversible annotations are stored; memory revision is unchanged.
            updates={u['id']:u for u in result['updates']}
            targets=[payload['target_id']] if work['kind']=='dependency' else payload['pair_ids']
            inserted=0
            for mid in (list(updates) or targets):
                m=next(m for m in before if m['id']==mid)
                u=updates.get(mid,{})
                assessment=u.get('assessment','unsupported' if decision=='weaken' else 'disputed')
                kind={'unsupported':'insufficient_support','source_error':'disputed'}.get(assessment,assessment)
                related=sorted({m['id'] for m in before})
                conclusion=digest([work['kind'],targets,kind,u.get('superseded_by'),
                                   sorted(l['id'] for l in payload.get('losses',[]))])
                inserted+=conn.execute("""INSERT OR IGNORE INTO consolidation_annotations(
                    memory_id,work_id,text,evidence_json,created_at,kind,superseded_by,memory_revision,
                    related_ids_json,run_id,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (mid,work['id'],u.get('annotation',result['reason']),dumps(result['evidence']),self.clock().isoformat(),
                     kind,u.get('superseded_by'),m['revision'],dumps(related),self.run_id,conclusion)).rowcount
            if not inserted:
                return 'checked',{}
            outcome='dependencies_reviewed' if work['kind']=='dependency' else 'conflicts'
        details['after_memories']=[snapshot(conn,m['id']) for m in before]
        operation(conn,'consolidation_'+('merge' if decision=='merge' else 'dependency' if work['kind']=='dependency' else 'conflict'),
                  'memory',before[0]['id'],{'run_id':self.run_id,'work_id':work['id'],'memory_ids':[m['id'] for m in before]},
                  actor='consolidation',stamp=self.clock().isoformat())
        return outcome,details


class _PersonaGateway:
    """Reuse persona's engine; account for every HTTP retry without double admission.

    Persona's preflight uses the real health gate. The inner Gateway's per-HTTP
    gate alone reserves budget, so its retries and persona JSON repairs count.
    """
    def __init__(self, owner, *, stop=None):
        self.owner, self.stop = owner, stop
        self.gateway = owner.gateway
        self.health = getattr(self.gateway, 'health', None)
        self.monotonic = getattr(self.gateway, 'monotonic', time.monotonic)
        if isinstance(self.gateway, Gateway):
            if self.health is None:
                from .model_health import ModelHealth
                self.health = ModelHealth(owner.store, self.gateway._raw_configs, clock=owner.clock)
            inner = object.__new__(_CallGateway)
            inner.__dict__ = self.gateway.__dict__.copy()
            inner._co = owner
            inner.health = _CallHealth(owner, self.health, {'id': None})
            self.gateway = inner

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        if self.stop and self.stop():
            raise ModelError('paused', 'maintenance stopped', paused=True, reason='interrupted')
        if isinstance(self.gateway, Gateway):
            return self.gateway.chat(messages, purpose, max_tokens, **kwargs)
        gate = _CallHealth(self.owner, self.health, {'id': None})
        gate.check('chat', purpose)
        started = self.monotonic()
        try:
            reply = self.gateway.chat(messages, purpose, max_tokens, **kwargs)
        except ModelError as error:
            self.owner.finish_call(gate.call_id, error.category, round((self.monotonic()-started)*1000), {},
                                   error=error.reason or error.category)
            raise
        self.owner.finish_call(gate.call_id, 'success', round((self.monotonic()-started)*1000), reply.usage,
                               reply.finish_reason, raw=reply.content)
        return reply


def _persona_result(store, run_id, status, reason, details, clock):
    """Outcome and report item commit together; recovering a completed attempt is read-only."""
    with store.write() as conn:
        row = conn.execute('SELECT * FROM maintenance_persona WHERE run_id=?', (run_id,)).fetchone()
        if row['status'] != 'running':
            return
        conn.execute('UPDATE maintenance_persona SET status=?,reason=?,details_json=? WHERE run_id=?',
                     (status, reason, dumps(details), run_id))
        if status in ('published', 'pending', 'rejected', 'failed', 'conflict'):
            outcome = 'persona_'+status if status in ('published','pending') else 'failed'
            conn.execute('''INSERT INTO maintenance_items(run_id,phase,item_key,object_id,outcome,reason,details_json,created_at)
                VALUES(?,'persona','update',?,?,?,?,?)''',
                (run_id, row['attempt_id'] or run_id, outcome, reason, dumps({**details,'status':status}), clock().isoformat()))


def update_persona_for_run(store, gateway, run, *, clock=utc_now, stop=None):
    """A durable link to persona's existing reservation/engine, never a second publisher."""
    from .persona import (PersonaEngine, PersonaBusy, PersonaConflict, persona_due,
                          _reserve_attempt, get_version)
    run_id = run['id']
    with store.read() as conn:
        row = conn.execute('SELECT * FROM maintenance_persona WHERE run_id=?', (run_id,)).fetchone()
        settings = run_settings(conn, run_id)
    if row and row['status'] != 'running':
        return
    if row is None:
        due = persona_due(store, clock=clock)
        reason, status = None, 'skipped'
        if not settings['enabled'] or not settings['persona_enabled']:
            reason = 'disabled'
        elif not due['due']:
            status, reason = 'not_due', due['reason']
        elif not gateway or not callable(getattr(gateway, 'chat', None)):
            reason = 'unconfigured'
        with store.write() as conn:
            count = conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE run_id=?', (run_id,)).fetchone()[0]
            if reason is None and count+2 > settings['max_calls']:
                reason = 'call_budget'
            attempt = None
            if reason is None:
                previous = conn.execute('SELECT id FROM persona_versions WHERE is_current=1').fetchone()
                try:
                    # The link and persona's own reservation are atomic. This is
                    # the same entry point used by PersonaJobs, with periodic due
                    # checking retained in _run. No model work in this transaction.
                    attempt = _reserve_attempt(conn, previous[0], 'periodic', clock().isoformat())
                except (PersonaBusy, PersonaConflict) as error:
                    reason = 'persona_busy' if isinstance(error, PersonaBusy) else 'current_version_changed'
            conn.execute('INSERT INTO maintenance_persona(run_id,attempt_id,status,reason,due_json) VALUES(?,?,?,?,?)',
                         (run_id, attempt, status if reason is not None else 'running', reason, dumps(due)))
        if reason is not None:
            return
    owner = Consolidation(store, gateway, clock=clock)
    owner.run_id = run_id
    engine = PersonaEngine(store, _PersonaGateway(owner, stop=stop), clock=clock)
    with store.read() as conn:
        attempt = dict(conn.execute('''SELECT a.* FROM persona_attempts a JOIN maintenance_persona p
            ON p.attempt_id=a.id WHERE p.run_id=?''', (run_id,)).fetchone())
    if attempt['state'] == 'queued':
        try:
            engine._run(attempt['base_version_id'], 'periodic', only_if_due=True, attempt_id=attempt['id'])
        except PersonaConflict:
            pass  # The existing engine has persisted the safe terminal reason.
        except Exception:
            # Never expose provider exceptions; engine normally already recorded it.
            with store.read() as conn:
                saved = conn.execute('SELECT state FROM persona_attempts WHERE id=?', (attempt['id'],)).fetchone()
            if saved['state'] in ('queued','running'):
                engine._finish_attempt(attempt['id'], 'failed', 'unexpected_error', {})
    elif attempt['state'] == 'running':
        # A process stopped between generation and its complete check. Never
        # regenerate in this run or publish a partial candidate on restart.
        engine._finish_attempt(attempt['id'], 'failed', 'interrupted', json.loads(attempt['outputs_json']))
    with store.read() as conn:
        attempt = dict(conn.execute('SELECT * FROM persona_attempts WHERE id=?', (attempt['id'],)).fetchone())
    status = {'current':'published'}.get(attempt['state'], attempt['state'])
    reason = attempt['reason']
    details = {'attempt_id':attempt['id'], 'base_version_id':attempt['base_version_id'],
               'version_id':attempt['version_id']}
    if attempt['version_id']:
        version = get_version(store, attempt['version_id'])
        details.update(content=version['content'], change_degree=version['change_degree'], checks=version['checks'])
    if status == 'rejected':
        reason = 'checks_rejected'
    _persona_result(store, run_id, status, reason, details, clock)
