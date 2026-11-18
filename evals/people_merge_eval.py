"""Run daily identity review on the frozen public corpus and score actual merges."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from iris.db import Store
from iris.goals import AUTO_REVIEW_KEY, DUPLICATE_AUTO_KEY
from iris.maintenance import Maintenance, PHASES
from iris.model_health import ModelHealth
from iris.models import Gateway, load_test_models
from iris.people_review import PROMPT, PURPOSE, SETTING

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_FIELDS = ('now', 'entries', 'subjects', 'links', 'memories', 'messages')


def seed(store, material, *, zone, role_name):
    store.set_setting('timezone', zone)
    store.set_setting('role_name', role_name)
    store.set_setting('lifecycle', {SETTING: True})
    store.set_setting('goals', {AUTO_REVIEW_KEY: False, DUPLICATE_AUTO_KEY: False})
    messages, links = {}, {}
    current = datetime.fromisoformat(material['now'])
    with store.write() as conn:
        for entry in material['entries']:
            conn.execute('INSERT INTO entries(id,name,platform,kind) VALUES(?,?,?,?)',
                         (entry['id'], entry['name'], 'eval', entry['kind']))
        for person in material['subjects']:
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",
                         (person['id'], person['name'], person['created_at']))
            conn.executemany('INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)',
                             [(person['id'], alias) for alias in person['aliases']])
            if person['account']:
                conn.execute("INSERT INTO platform_identities(subject_id,platform,account_id,display_name) VALUES(?,'eval',?,?)",
                             (person['id'], person['account'], person['name']))
        for person in material['subjects']:
            if person['parent']:
                conn.execute('UPDATE subjects SET parent_id=? WHERE id=?', (person['parent'], person['id']))
        for message in material['messages']:
            if datetime.fromisoformat(message['at']) > current:
                continue
            messages[message['key']] = conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,
                content,quote_author_subject_id,quote_content,occurred_at,received_at,dedupe_key,learning_state)
                VALUES(?,?,?,?,?,?,?,?,?,'learned')''', (message['entry_id'], message['kind'], message['sender'],
                message['content'], message['quote_author'], message['quote_content'], message['at'], message['at'],
                message['key'])).lastrowid
        for link in material['links']:
            a, b = (sorted((link['a'], link['b'])) if link['kind'] == 'same_as' else (link['a'], link['b']))
            links[link['key']] = conn.execute('''INSERT INTO subject_links(subject_a,subject_b,kind,status,belief,
                source_message_id,created_at) VALUES(?,?,?,?,?,?,?)''',
                (a, b, link['kind'], link['status'], link['belief'], messages.get(link['source']), link['created_at'])).lastrowid
        for memory in material['memories']:
            stamp = memory['created_at']
            mid = conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,
                retention,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES(?,'关系',?,?,60,50,50,?,?,?,?)''',
                (memory['content'], memory['speaker'], memory['stance'], stamp, stamp, stamp, stamp)).lastrowid
            conn.executemany('INSERT INTO memory_subjects(memory_id,subject_id) VALUES(?,?)',
                             [(mid, sid) for sid in memory['about']])
            conn.executemany("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",
                             [(mid, messages[key], stamp) for key in memory['sources'] if key in messages])
        # These are historical fixture states. Install them after their original
        # evidence and from branches to roots, while each destination is live.
        merged = {person['id']: person['merged_into'] for person in material['subjects'] if person['merged_into']}
        while merged:
            leaves = sorted(merged.keys() - set(merged.values()))
            if not leaves:
                raise ValueError('cyclic fixture merge')
            for sid in leaves:
                conn.execute('UPDATE subjects SET merged_into=?,merged_at=? WHERE id=?',
                             (merged.pop(sid), material['now'], sid))
    return links, {mid: key for key, mid in messages.items()}


def run_case(case_id, material, configs, *, zone, role_name):
    current = datetime.fromisoformat(material['now'])

    def clock():
        return current

    with TemporaryDirectory(prefix='iris-people-eval-') as directory:
        store = Store(Path(directory) / 'iris.db')
        gateway = Gateway(configs, store, health=ModelHealth(store, configs, clock=clock), clock=clock)
        try:
            links, message_keys = seed(store, material, zone=zone, role_name=role_name)
            maintenance = Maintenance(store, gateway=gateway, clock=clock)
            run_id = maintenance.request()
            with store.write() as conn:
                conn.execute('UPDATE maintenance_runs SET phase=? WHERE id=?', (PHASES.index('goals'), run_id))
            maintenance.run(run_id)
            with store.read() as conn:
                items = {row['object_id']: dict(row) for row in conn.execute(
                    "SELECT object_id,outcome,reason,details_json FROM maintenance_items WHERE run_id=? AND phase='people'",
                    (run_id,))}
                merges = {row['link_id']: dict(row) for row in conn.execute(
                    "SELECT link_id,source_id,target_id,evidence_ids_json FROM subject_merges WHERE actor='maintenance'")}
                calls = [dict(row) for row in conn.execute('''SELECT purpose,status_code,result_category,
                    timed_out,finish_reason,prompt_tokens,completion_tokens,duration_ms FROM model_calls''')]
                foreign_key_errors = bool(conn.execute('PRAGMA foreign_key_check').fetchone())
            results = {}
            for key, link_id in links.items():
                item, merge = items.get(link_id, {}), merges.get(link_id)
                results[key] = {'target': merge['target_id'] if merge else None,
                                'outcome': item.get('outcome'), 'reason': item.get('reason'),
                                'evidence': [message_keys[mid] for mid in json.loads(merge['evidence_ids_json'])] if merge else []}
            return {'id': case_id, 'links': results, 'calls': calls, 'foreign_key_errors': foreign_key_errors}
        finally:
            gateway.close()
            store.close()


def score(corpus, results):
    links = []
    for case, result in zip(corpus['cases'], results, strict=True):
        for expected in case['expected']['links']:
            actual = result['links'][expected['key']]
            should_merge = expected['expected'] == 'merge'
            links.append({'case': case['id'], 'link': expected['key'], 'should_merge': should_merge, **actual,
                          'expected_target': expected['target'],
                          'correct': actual['target'] == (expected['target'] if should_merge else None)})
    positives = sum(row['should_merge'] for row in links)
    identified = sum(row['should_merge'] and row['correct'] for row in links)
    wrong_merges = sum(row['target'] is not None and not row['correct'] for row in links)
    failures = sum(row['outcome'] in (None, 'failed') for row in links)
    calls = [call for result in results for call in result['calls']]
    valid = not (failures or any(result['foreign_key_errors'] for result in results)
                 or any(call['timed_out'] or call['status_code'] == 429 for call in calls))
    return {'valid': valid, 'cases': len(results), 'links': len(links), 'correct': sum(row['correct'] for row in links),
            'expected_merges': positives, 'correct_merges': identified, 'wrong_merges': wrong_merges,
            'identification': identified / positives if positives else None, 'model_calls': len(calls),
            'failed_reviews': failures, 'meets_targets': bool(valid and positives and not wrong_merges and identified / positives >= .70),
            'link_results': links}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=ROOT / 'evals/people_merge_v1.json')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=range(1, 7), default=1)
    args = parser.parse_args()
    corpus_bytes = args.corpus.read_bytes()
    corpus = json.loads(corpus_bytes)
    if corpus['format_version'] != 1 or not corpus['cases']:
        parser.error('unsupported corpus format')
    case_ids = [case['id'] for case in corpus['cases']]
    if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', key) for key in case_ids) or len(case_ids) != len(set(case_ids)):
        parser.error('case IDs must be unique and safe as filenames')
    out = args.out.resolve()
    if out.is_relative_to(ROOT):
        parser.error('--out must be outside the repository')
    configs = {'chat': load_test_models()['chat']}
    out.mkdir(parents=True, exist_ok=False)
    source_files = sorted(path for path in (ROOT / 'src/iris').rglob('*') if path.suffix in ('.py', '.sql', '.md'))
    metadata = {'corpus': corpus['name'], 'corpus_sha256': hashlib.sha256(corpus_bytes).hexdigest(),
                'source_sha256': hashlib.sha256(b''.join(
                    path.relative_to(ROOT).as_posix().encode() + path.read_bytes() for path in source_files)).hexdigest(),
                'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'prompt_sha256': hashlib.sha256(PROMPT.encode()).hexdigest(),
                'purpose': PURPOSE, 'model': configs['chat'].model, 'reasoning_effort': configs['chat'].reasoning_effort}
    (out / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(run_case, case['id'], {field: case[field] for field in PRODUCT_FIELDS}, configs,
                            zone=corpus['timezone'], role_name=corpus['role_name']) for case in corpus['cases']]
        for job in jobs:
            result = job.result()
            results.append(result)
            (out / (result['id'] + '.json')).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            print(result['id'], 'calls', len(result['calls']), flush=True)
    report = {**metadata, **score(corpus, results)}
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'link_results'}, ensure_ascii=False), flush=True)
    return 0 if report['meets_targets'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
