"""Offline scoring; accepts one or two independent external judgment rounds."""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np
from run import ROOT, SCORING, SCORING_VERSION, SOURCE_FIELDS, REPORT_ONLY_SCORING
from iris.evaluation import _json_sha256, _read_json, _write_json, _verified_material_file


def require(condition,message):
    if not condition:
        raise ValueError(message)


def exact(value,keys):
    require(isinstance(value,dict) and set(value)==set(keys),'invalid result fields')


def boolean(value):
    require(type(value) is bool,'expected a JSON boolean')


def validate_payload(p):
    require(set(('role','case','before','actual_merges','actual_actions','after','retention_changes'))<=set(p),'incomplete input')
    case=p['case']
    keymap=p['before']['key_map']
    keys=[m['key'] for m in case['memories']]
    require(set(keymap)==set(keys) and len(set(keymap.values()))==len(keys),'invalid key map')
    require(all(type(i) is int for i in keymap.values()),'non-integer ID')
    require(p['after']['key_map']==keymap,'key map changed')
    sources={}
    for m in [*case['memories'],*case.get('events',[])]:
        for s in m.get('sources',[]):
            s={key:s[key] for key in SOURCE_FIELDS}
            if s['key'] in sources:
                require(sources[s['key']]==s,'source identity changed')
            sources[s['key']]=s
    def memory(m):
        required={'id','original_keys','content','type','belief','retention','importance','lifecycle','pinned','last_edit',
                  'event_time','created_at','speaker','stance','about','sources','derived_from','annotations','is_placeholder','merged_into'}
        require(required<=set(m),'incomplete memory snapshot')
        require(type(m['id']) is int and m['id'] in keymap.values(),'unknown memory id')
        require(isinstance(m['original_keys'],list) and m['original_keys'] and m['original_keys']==[k for k in keys if k in m['original_keys']],'invalid original keys')
        for k in ('belief','retention','importance'):
            require(type(m[k]) is int and 0<=m[k]<=100,'invalid score')
        for k in ('pinned','is_placeholder'):
            boolean(m[k])
        require(m['last_edit'] in ('admin','learning'),'invalid edit marker')
        require(m['merged_into'] is None or type(m['merged_into']) is int and m['merged_into'] in keymap.values(),'invalid merge target')
        require(m['is_placeholder']==(m['merged_into'] is not None),'placeholder flag mismatch')
        require(isinstance(m['content'],str) and isinstance(m['type'],str),'invalid text')
        require(set(m['derived_from'])<=set(keys),'unknown dependency key')
        for s in m['sources']:
            require(s.get('key') in sources and all(s.get(k)==v for k,v in sources[s['key']].items()),'source content changed')
        for a in m['annotations']:
            require(isinstance(a.get('text'),str) and isinstance(a.get('source_keys'),list) and set(a['source_keys'])<=set(sources),'invalid annotation')
    for stage in ('before','after'):
        require([m['id'] for m in p[stage]['memories']]==list(keymap.values()),'snapshot ID/order mismatch')
        for m in p[stage]['memories']:
            memory(m)
    action_ids=set()
    lineage={v:{k} for k,v in keymap.items()}
    for merge in p['actual_merges']:
        aid=merge['action_id']
        require(isinstance(aid,str) and aid and aid not in action_ids,'duplicate action ID')
        action_ids.add(aid)
        require(len(merge['input_ids'])>=2 and len(set(merge['input_ids']))==len(merge['input_ids']),'invalid merge inputs')
        require({m['id'] for m in merge['before_memories']}==set(merge['input_ids']),'missing immediate input snapshot')
        expanded=set().union(*(lineage[mid] for mid in merge['input_ids']))
        require(merge['input_keys']==[k for k in keys if k in expanded],'incorrect expanded merge lineage')
        for m in [*merge['before_memories'],merge['result'],*merge['absorbed_memories']]:
            memory(m)
        require(merge['result']['id'] in merge['input_ids'],'untracked result object')
        lineage[merge['result']['id']]=expanded
    for action in p['actual_actions']:
        require(isinstance(action['action_id'],str) and action['action_id'] and action['action_id'] not in action_ids,'invalid action ID')
        action_ids.add(action['action_id'])
        require(action['status'] in ('committed','proposed','skipped','rolled_back','failed'),'invalid action status')
        require(isinstance(action['report'],str) and set(action['keys'])<=set(keys),'invalid action keys/report')
        for m in [*action['before_memories'],*action['after_memories']]:
            memory(m)
    values={keymap[m['key']]:m['retention'] for m in case['memories']}
    for i,r in enumerate(p['retention_changes']):
        require(type(r['index']) is int and r['index']==i,'retention order mismatch')
        require(all(type(r[k]) is int for k in ('before','after','delta','memory_id')),'invalid retention values')
        require(r['memory_id'] in values and r['before']==values[r['memory_id']] and r['delta']==r['after']-r['before'],'retention arithmetic/sequence mismatch')
        require(r['cause'] in ('daily_decay','m2_dependency','model_consolidation'),'unsupported retention cause')
        require(r['reason'] and set(r['dependency_keys'])<=set(keys),'missing retention reason')
        require(all(type(e) is int and 0<=e<len(case.get('events',[])) for e in r['event_indexes']),'invalid event index')
        require(r['action_id'] is None or r['action_id'] in action_ids,'invalid retention action')
        values[r['memory_id']]=r['after']
    require(all(values[m['id']]==m['retention'] for m in p['after']['memories']),'unreported retention changes')


def load_materials(directory):
    manifest=_read_json(directory/'manifest.json')
    require(type(manifest['format_version']) is int and manifest['format_version']==1 and manifest['evaluation']=='consolidation','invalid format')
    require(manifest['materials_sha256']==_json_sha256({k:v for k,v in manifest.items() if k!='materials_sha256'}),'manifest digest mismatch')
    rows=_read_json(_verified_material_file(directory,'run.json',manifest['run_sha256']))['rows']
    require(_verified_material_file(directory,'scoring.md',manifest['scoring_sha256']).read_text(encoding='utf-8')==SCORING,'scoring rules differ')
    if manifest['run'].get('resolution')=='report_only_v1':
        require(_verified_material_file(directory,'report-only-scoring.md',manifest.get('report_only_scoring_sha256','')).read_text(encoding='utf-8')==REPORT_ONLY_SCORING,'report-only scoring policy differs')
    require(len(rows)==len(manifest['cases']),'case count mismatch')
    for i,(row,entry) in enumerate(zip(rows,manifest['cases'],strict=True)):
        exact(entry,('case_id','file','sha256','judgment_file'))
        require(entry['file']==f'cases/{i:04}.json' and entry['judgment_file']==f'{i:04}.json','invalid case path/order')
        doc=_read_json(_verified_material_file(directory,entry['file'],entry['sha256']))
        exact(doc,('format_version','evaluation','case_id','corpus_sha256','source_sha256','scoring_version','input'))
        require(doc['input']==row['input'] and doc['case_id']==entry['case_id']==row['input']['case']['id'],'case input mismatch')
        require(doc['corpus_sha256']==manifest['run']['corpus']['sha256'] and doc['source_sha256']==manifest['run']['source_sha256'] and doc['scoring_version']==SCORING_VERSION,'run binding mismatch')
        require(type(doc['format_version']) is int and doc['format_version']==1 and doc['evaluation']=='consolidation','case format mismatch')
        validate_payload(row['input'])
    return manifest,rows


def protected(p):
    return [m for m in p['before']['memories'] if m['pinned'] or m['last_edit']=='admin']


def validate_judgment(j,p):
    exact(j,('format_version','case_id','merge_results','contradiction_results','dependency_results','protection_results'))
    require(type(j['format_version']) is int and j['format_version']==1 and j['case_id']==p['case']['id'],'judgment case mismatch')
    expected=p['case']['expected']
    groups=[('merge_results',p['actual_merges'],('action_id','key_facts_preserved','source_grounded','attribution_correct','reason')),
            ('contradiction_results',expected.get('contradictions',[]),('index','keys','resolution_correct','source_grounded','must_keep','reason')),
            ('dependency_results',expected.get('dependencies',[]),('index','derived','handling_correct','source_grounded','reason')),
            ('protection_results',protected(p),('key','content_unchanged','belief_unchanged','reason'))]
    reverse={v:k for k,v in p['before']['key_map'].items()}
    for field,items,fields in groups:
        require(isinstance(j[field],list) and len(j[field])==len(items),'judgment array length mismatch')
        for i,(r,item) in enumerate(zip(j[field],items,strict=True)):
            exact(r,fields)
            require(isinstance(r['reason'],str) and bool(r['reason'].strip()),'reason required')
            if 'index' in fields:
                require(type(r['index']) is int and r['index']==i,'judgment index mismatch')
            if field=='merge_results':
                require(r['action_id']==item['action_id'],'merge action mismatch')
            elif field=='contradiction_results':
                require(r['keys']==item['keys'],'contradiction keys/order mismatch')
                require(isinstance(r['must_keep'],list) and len(r['must_keep'])==len(item['must_keep']),'must_keep mismatch')
                for b in r['must_keep']:
                    boolean(b)
            elif field=='dependency_results':
                require(r['derived']==item['derived'],'dependency key mismatch')
            else:
                require(r['key']==reverse[item['id']],'protected key/order mismatch')
            for key in set(fields)-{'reason','index','keys','derived','key','action_id','must_keep'}:
                boolean(r[key])
    return j


def combine(a,b,path='',disagreements=None):
    disagreements=disagreements if disagreements is not None else []
    if type(a) is bool:
        if a!=b:
            disagreements.append({'field':path,'round1':a,'round2':b})
        return a and b
    if isinstance(a,list):
        return [combine(x,y,f'{path}/{i}',disagreements) for i,(x,y) in enumerate(zip(a,b,strict=True))]
    if isinstance(a,dict):
        return {k:combine(v,b[k],f'{path}/{k}',disagreements) for k,v in a.items() if k!='reason'}
    return a


def ratio(n,d):
    return {'numerator':n,'denominator':d,'rate':n/d if d else None}



def run_validity(row):
    """Keep completed output failures in quality scoring, without retry filtering.

    Frozen runners marked invalid_output as an invalid execution. Reclassify only
    that explained case; retain raw flags and all cases/calls in the final report.
    Transport failures, skipped work and unexplained deferrals remain invalid.
    """
    phase=row['report']['consolidation']
    failures=[item for item in row['report']['items'] if item['outcome']=='failed']
    quality=lambda item: item['phase']=='consolidation' and item['reason'] in ('unsafe_write','invalid_output')
    formats=[item for item in failures if quality(item) and item['reason']=='invalid_output']
    reasons=[]
    if any(call['result']!='success' for call in row['calls']):
        reasons.append('model_call_failed')
    if formats and not row['calls']:
        reasons.append('output_failure_without_call')
    if phase['skip_reason']:
        reasons.append('model_phase_skipped')
    if any(action['status']=='skipped' for action in row['input']['actual_actions']):
        reasons.append('work_skipped')
    if any(not quality(item) for item in failures):
        reasons.append('non_output_failure')
    # A failed JSON decision is still pending for a later maintenance run. It was
    # attempted here; other pending work means this evaluation did not complete.
    if phase['deferred']>len(formats):
        reasons.append('unattempted_work')
    if not row['valid'] and not formats:
        reasons.append('unexplained_original_invalid')
    return {'case_id':row['input']['case']['id'],'original_valid':row['valid'],
        'valid':not reasons,'format_failures':len(formats),'invalid_reasons':reasons}


def score_case(p,j):
    keys=list(p['before']['key_map'])
    keymap=p['before']['key_map']
    exp=p['case']['expected']
    component={k:k for k in keys}
    def root(k):
        while component[k]!=k:
            k=component[k]
        return k
    for a,b in exp.get('merges',[]):
        component[root(b)]=root(a)
    after={m['id']:m for m in p['after']['memories']}
    executed_edges={a['id']:merge['result']['id'] for merge in p['actual_merges'] for a in merge['absorbed_memories']}
    def destination(mid):
        seen=set()
        while mid in after and after[mid]['merged_into'] is not None:
            if mid in seen or executed_edges.get(mid)!=after[mid]['merged_into']:
                return None
            seen.add(mid)
            mid=after[mid]['merged_into']
        return mid if mid in after and after[mid]['lifecycle']=='active' else None
    protection=[]
    for m,judgment in zip(protected(p),j['protection_results'],strict=True):
        mid=m['id']
        samples=[after[mid]]
        samples += [n for a in p['actual_actions'] if a['status']=='committed' and not a['kind'].startswith('event_') for n in a['after_memories'] if n['id']==mid]
        participated=any(mid in x['input_ids'] for x in p['actual_merges'])
        content=not participated and all(n['content']==m['content'] and n['belief']==m['belief'] and n['lifecycle']!='deleted' and n['pinned']==m['pinned'] and n['last_edit']==m['last_edit'] for n in samples)
        belief=not participated and all(n['belief']==m['belief'] and n['lifecycle']!='deleted' for n in samples)
        protection.append({'key':judgment['key'],'content_unchanged':content and judgment['content_unchanged'],'belief_unchanged':belief and judgment['belief_unchanged']})
    bad_protection={r['key'] for r in protection if not r['content_unchanged'] or not r['belief_unchanged']}
    failures=[]
    belief_checks=[]
    source_checks=[]
    semantic_failed=set()
    for m,r in zip(p['actual_merges'],j['merge_results'],strict=True):
        reasons=[]
        if any(root(a)!=root(b) for a,b in itertools.combinations(m['input_keys'],2)):
            reasons.append('pair_outside_merge_component')
        for field in ('key_facts_preserved','source_grounded','attribution_correct'):
            if not r[field]:
                reasons.append(field)
                semantic_failed.update(m['input_keys'])
        result=m['result']
        if result['lifecycle']!='active' or result['is_placeholder']:
            reasons.append('result_not_active')
        absorbed=m['absorbed_memories']
        if {x['id'] for x in absorbed}!=set(m['input_ids'])-{result['id']} or any(x['lifecycle']!='deleted' or not x['is_placeholder'] or x['merged_into']!=result['id'] for x in absorbed):
            reasons.append('invalid_placeholder')
        if any(x['pinned'] or x['last_edit']=='admin' for x in m['before_memories']):
            reasons.append('protected_merge')
        if any(destination(mid)!=destination(result['id']) for mid in m['input_ids']):
            reasons.append('broken_final_chain')
        source_keys={s['key'] for x in m['before_memories'] for s in x['sources']}
        source_checks.append({'action_id':m['action_id'],'passed':source_keys<={s['key'] for s in result['sources']} and {k for x in m['before_memories'] for k in x['derived_from']}<=set(result['derived_from'])})
        cap=max(x['belief'] for x in m['before_memories'])
        belief_checks.append({'action_id':m['action_id'],'input_max':cap,'result':result['belief'],'passed':result['belief']<=cap})
        if reasons:
            failures.append({'action_id':m['action_id'],'reasons':reasons})
    recognized=[pair for pair in exp.get('merges',[]) if destination(keymap[pair[0]]) is not None and destination(keymap[pair[0]])==destination(keymap[pair[1]])]
    contradictions=[]
    for label,r in zip(exp.get('contradictions',[]),j['contradiction_results'],strict=True):
        contradictions.append(r['resolution_correct'] and r['source_grounded'] and all(r['must_keep']) and not(set(label['keys'])&bad_protection)
                             and all(after[keymap[k]]['lifecycle']!='deleted' for k in label['keys']))
    dependencies=[]
    for label,r in zip(exp.get('dependencies',[]),j['dependency_results'],strict=True):
        dependencies.append(r['handling_correct'] and r['source_grounded'] and label['derived'] not in bad_protection)
    return {'case_id':p['case']['id'],'mismerge':ratio(len(failures),len(p['actual_merges'])),
        'recognition':ratio(len(recognized),len(exp.get('merges',[]))), 'merge_failures':failures,
        'recognized_semantic_failures':[pair for pair in recognized if set(pair)&semantic_failed],
        'contradictions':ratio(sum(contradictions),len(contradictions)),'dependencies':ratio(sum(dependencies),len(dependencies)),
        'protection':protection,'belief_cap':belief_checks,'source_inheritance':source_checks,'judgments':j}



def audit_writes(manifest, paths):
    require(1<=len(paths)<=2, 'one or two independent write audit rounds required')
    rounds=[]
    ids=[entry['case_id'] for entry in manifest['cases']]
    for path in paths:
        document=_read_json(path)
        exact(document,('materials_sha256','cases'))
        require(document['materials_sha256']==manifest['materials_sha256'],'write audit material mismatch')
        require(isinstance(document['cases'],list) and len(document['cases'])==len(ids),'write audit cases mismatch')
        for item,case_id in zip(document['cases'],ids,strict=True):
            exact(item,('case_id','has_damage','reason'))
            require(item['case_id']==case_id,'write audit case order mismatch')
            boolean(item['has_damage'])
            require(isinstance(item['reason'],str) and bool(item['reason'].strip()),'write audit reason required')
        rounds.append(document['cases'])
    details=[]
    for index,case_id in enumerate(ids):
        values=[r[index] for r in rounds]
        details.append({'case_id':case_id,'has_damage':any(v['has_damage'] for v in values),
            'disagreement':len({v['has_damage'] for v in values})>1,'judgments':values})
    return {'write_audit_rounds':len(rounds),'write_damage':ratio(sum(d['has_damage'] for d in details),len(details)),
            'write_audit_details':details}

def score(materials,judgments,out,judge_model,write_audits=()):
    require(1<=len(judgments)<=2 and judge_model.strip(),'one or two rounds and judge model required')
    manifest,rows=load_materials(materials)
    if write_audits:
        require(len(write_audits)==len(judgments),'write audit and scoring round counts must match')
        require('write_audit_sha256' in manifest,'materials lack frozen write audit instructions')
        _verified_material_file(materials,'write-audit.md',manifest['write_audit_sha256'])
    rounds=[]
    for directory in judgments:
        round_manifest=_read_json(directory/'manifest.json')
        exact(round_manifest,('materials_sha256',))
        require(round_manifest['materials_sha256']==manifest['materials_sha256'],'judgment material mismatch')
        expected={'manifest.json',*(e['judgment_file'] for e in manifest['cases'])}
        require({p.name for p in directory.iterdir() if p.is_file()}==expected,'judgment files missing or unexpected')
        rounds.append([validate_judgment(_read_json(directory/e['judgment_file']),r['input']) for e,r in zip(manifest['cases'],rows,strict=True)])
    disagreements=[]
    results=[]
    for i,row in enumerate(rows):
        j=rounds[0][i]
        if len(rounds)==2:
            j=combine(j,rounds[1][i],row['input']['case']['id'],disagreements)
        results.append(score_case(row['input'],j))
    validity=[run_validity(row) for row in rows]
    report={'format_version':1,'evaluation':'consolidation','method':manifest['run']['method'],
        'resolution':manifest['run'].get('resolution','original_v1'),'corpus_sha256':manifest['run']['corpus']['sha256'],
        'materials_sha256':manifest['materials_sha256'],'source_sha256':manifest['run']['source_sha256'],
        'judge_model':judge_model,'judge_rounds':len(rounds),'valid':all(r['valid'] for r in validity),
        'original_valid':all(r['valid'] for r in rows),'run_validity':validity,
        'format_failures':sum(r['format_failures'] for r in validity),'cases':results,'disagreements':disagreements}
    report['safety_rejections']=sum(r.get('safety_rejections',0) for r in rows)
    if write_audits:
        report.update(audit_writes(manifest,write_audits))
    for field in ('mismerge','recognition','contradictions','dependencies'):
        report[field]=ratio(sum(r[field]['numerator'] for r in results),sum(r[field]['denominator'] for r in results))
    report['conflict_dependency_consistency']=ratio(report['contradictions']['numerator']+report['dependencies']['numerator'],
        report['contradictions']['denominator']+report['dependencies']['denominator'])
    for field in ('protection','belief_cap','source_inheritance'):
        checks=[r for c in results for r in c[field]]
        success=lambda r:r.get('passed',r.get('content_unchanged',False) and r.get('belief_unchanged',False))
        report[field]=ratio(sum(success(r) for r in checks),len(checks))
    calls=[c for r in rows for c in r['calls']]
    report['calls']={'count':len(calls),'prompt_tokens':sum(c['prompt_tokens'] or 0 for c in calls),
        'completion_tokens':sum(c['completion_tokens'] or 0 for c in calls),
        'unknown_usage':sum(c['prompt_tokens'] is None or c['completion_tokens'] is None for c in calls),
        'p50_ms':float(np.percentile([c['duration_ms'] for c in calls],50)) if calls else None,
        'p95_ms':float(np.percentile([c['duration_ms'] for c in calls],95)) if calls else None}
    report['elapsed_seconds']=sum(r['elapsed_seconds'] for r in rows)
    report['case_p95_seconds']=float(np.percentile([r['elapsed_seconds'] for r in rows],95))
    report['feasible']=report['valid'] and report['mismerge']['rate'] is not None and report['mismerge']['rate']<=.05
    out.mkdir(parents=True,exist_ok=False)
    _write_json(out/'report.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--materials',type=Path,required=True)
    parser.add_argument('--judgments',type=Path,action='append',required=True)
    parser.add_argument('--judge-model',required=True)
    parser.add_argument('--write-audit',type=Path,action='append',default=[])
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    report=score(args.materials,args.judgments,args.out,args.judge_model,args.write_audit)
    print(json.dumps({k:v for k,v in report.items() if k not in ('cases','disagreements')},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
