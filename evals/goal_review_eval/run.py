"""Goal auto review (G1) evaluation with deterministic scoring; no model judging.

    uv run python evals/goal_review_eval/run.py --out /absolute/external/run
    uv run python evals/goal_review_eval/run.py --corpus /absolute/corpus.json --out /absolute/external/run
    uv run python evals/goal_review_eval/run.py --fake completed --out /absolute/external/fake

Each case gets its own SQLite database and a fixed clock at case.now. Entries,
subjects, messages up to now and one goal are installed literally; then the real
daily-maintenance goal phase runs with only G1 enabled (no consolidation,
persona, goal deduplication or G2). Only product material reaches the model:
expected, reason and category are stripped before seeding and read by
goal_review_scoring.py after every case ran. A real run reads IRIS_TEST_MODELS; --fake uses an
httpx MockTransport, reads no configuration and cannot reach the network
(simulation=true, never a gate result). A timeout, 429 or failed review stops
the run with valid=false; rerun into a fresh directory, never splice runs.
Databases are deleted after extraction unless --keep-databases.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from goal_review_scoring import report, score_case  # noqa: E402

from iris.db import Store  # noqa: E402
from iris.goals import (AUTO_REVIEW_KEY, AUTO_REVIEW_PROMPT_VERSION, AUTO_REVIEW_PURPOSE,  # noqa: E402
                        DUPLICATE_AUTO_KEY, _deadline)
from iris.maintenance import Maintenance, PHASES  # noqa: E402
from iris.model_health import ModelHealth  # noqa: E402
from iris.models import Gateway, ModelConfig, ModelError, load_test_models  # noqa: E402
from iris.state import role_zone  # noqa: E402

PRODUCT_FIELDS = ('now', 'entries', 'subjects', 'goal', 'messages')
LABEL_FIELDS = ('expected', 'reason', 'category')
FAKE_MODES = ('completed', 'abandoned', 'open', 'invalid', 'timeout')
PLATFORM = 'goal-review-eval'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def utc(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('timestamps require a timezone')
    return stamp.astimezone(timezone.utc)


def validate_corpus(corpus):
    if corpus.get('format_version') != 1 or not isinstance(corpus.get('cases'), list) or not corpus['cases']:
        raise ValueError('unsupported corpus format')
    ids = [case['id'] for case in corpus['cases']]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate case id')
    for case in corpus['cases']:
        utc(case['now'])
        keys = [m['key'] for m in case['messages']]
        if len(set(keys)) != len(keys):
            raise ValueError(f'{case["id"]}: duplicate message key')
        subjects = {s['id'] for s in case['subjects']} | {'self', 'scene'}
        entries = {e['id'] for e in case['entries']}
        goal = case['goal']
        if goal['kind'] not in ('normal', 'question') or goal['origin'] not in ('internal', 'host', 'admin'):
            raise ValueError(f'{case["id"]}: invalid goal kind or origin')
        utc(goal['created_at'])
        if not set(goal['sources']) <= set(keys) or not set(goal['people']) <= subjects - {'self', 'scene'}:
            raise ValueError(f'{case["id"]}: goal refers to an unknown message or person')
        if goal.get('entry_id') is not None and goal['entry_id'] not in entries:
            raise ValueError(f'{case["id"]}: unknown goal entry')
        for message in case['messages']:
            utc(message['at'])
            if message['entry_id'] not in entries or message['sender'] not in subjects:
                raise ValueError(f'{case["id"]}: message refers to an unknown entry or sender')
            if message.get('quote_author') is not None and message['quote_author'] not in subjects:
                raise ValueError(f'{case["id"]}: unknown quote author')
        expected = case['expected']
        if expected['state'] not in ('open', 'completed', 'abandoned') or not set(expected.get('evidence', [])) <= set(keys):
            raise ValueError(f'{case["id"]}: invalid expected label')
    return corpus


def product_input(case):
    """The only case fields that may reach the product (and so the model)."""
    return {field: copy.deepcopy(case[field]) for field in PRODUCT_FIELDS}


def seed(store, material, corpus):
    """Install facts literally; messages after now have not been received yet."""
    store.set_setting('timezone', corpus['timezone'])
    store.set_setting('role_name', corpus.get('role_name', 'Iris'))
    now = utc(material['now'])
    keys, skipped = {}, []
    with store.write() as conn:
        zone = role_zone(conn)
        for entry in material['entries']:
            conn.execute('INSERT INTO entries(id,name,platform,kind) VALUES(?,?,?,?)',
                         (entry['id'], entry.get('name') or entry['id'], PLATFORM, entry.get('kind', 'private')))
        for subject in material['subjects']:
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",
                         (subject['id'], subject['name'], now.isoformat()))
            conn.executemany('INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)',
                             [(subject['id'], alias) for alias in subject.get('aliases', [])])
        ids = {}
        for number, message in enumerate(material['messages'], 1):
            if utc(message['at']) > now:
                skipped.append(message['key'])
                continue
            mid = conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,quote_author_subject_id,
                quote_content,occurred_at,received_at,dedupe_key,learning_state) VALUES(?,?,?,?,?,?,?,?,?,'learned')''',
                (message['entry_id'], message['kind'], message['sender'], message['content'], message.get('quote_author'),
                 message.get('quote_content'), message['at'], message['at'], f'case-{number}')).lastrowid
            ids[message['key']] = mid
            keys[mid] = message['key']
        goal = material['goal']
        created = utc(goal['created_at']).isoformat()
        deadline = _deadline(goal.get('deadline'), zone) if goal.get('deadline') else None
        gid = conn.execute('''INSERT INTO goals(content,kind,origin,state,deadline,deadline_at,entry_id,created_at,
            updated_at,schedule_initialized) VALUES(?,?,?,?,?,?,?,?,?,1)''',
            (goal['content'], goal['kind'], goal['origin'], goal.get('state', 'open'), deadline, deadline,
             goal.get('entry_id'), created, created)).lastrowid
        conn.executemany('INSERT INTO goal_people(goal_id,subject_id) VALUES(?,?)', [(gid, p) for p in goal['people']])
        conn.executemany('INSERT INTO goal_sources(goal_id,message_id) VALUES(?,?)',
                         [(gid, ids[key]) for key in goal['sources'] if key in ids])
    return gid, keys, skipped


class RecordingGateway(Gateway):
    """Keeps the chat requests and completion content; never reasoning or headers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def _call(self, kind, purpose, payload, **kwargs):
        if kind != 'chat':
            return super()._call(kind, purpose, payload, **kwargs)
        record = {'purpose': purpose, 'messages': payload.get('messages')}
        self.records.append(record)
        try:
            result = super()._call(kind, purpose, payload, **kwargs)
            choice = (result.get('choices') or [{}])[0]
            record.update(content=(choice.get('message') or {}).get('content'), finish_reason=choice.get('finish_reason'),
                          usage=result.get('usage'))
            return result
        except ModelError as exc:
            record.update(error=exc.category, reason=exc.reason)
            raise


def fake_transport(mode):
    """Offline plumbing check: ignores labels and cannot access the network."""
    def respond(request):
        if mode == 'timeout':
            raise httpx.ReadTimeout('simulated goal review timeout', request=request)
        content = 'invalid unit response'
        if mode != 'invalid':
            material = json.loads(json.loads(request.content)['messages'][-1]['content'])
            evidence = [] if mode == 'open' else [material['messages'][-1]['id']]
            content = json.dumps({'decision': mode, 'evidence': evidence, 'reason': 'fake'})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    return httpx.MockTransport(respond)


def run_case(material, corpus, directory, configs, *, fake=None, keep_database=False):
    directory.mkdir(parents=True)
    path = directory / 'iris.db'
    store = Store(path)
    gateway = client = None
    now = utc(material['now'])
    clock = lambda: now  # noqa: E731
    try:
        gid, keys, skipped = seed(store, material, corpus)
        store.set_setting('consolidation', {'enabled': False})
        store.set_setting('scheduled_backup', {'enabled': False})
        store.set_setting('goal_dedup_judge', {'enabled': False})
        store.set_setting('goals', {AUTO_REVIEW_KEY: True, DUPLICATE_AUTO_KEY: False})
        client = httpx.Client(transport=fake_transport(fake)) if fake else None
        gateway = RecordingGateway(configs, store, health=ModelHealth(store, configs, clock=clock), clock=clock,
                                   client=client, **({'sleeper': lambda seconds: None} if fake else {}))
        engine = Maintenance(store, gateway=gateway, clock=clock)
        run_id = engine.request(trigger='manual')
        with store.write() as conn:
            conn.execute('UPDATE maintenance_runs SET phase=? WHERE id=?', (PHASES.index('goals'), run_id))
        engine.run(run_id)
        maintenance = engine.report(run_id)
        with store.read() as conn:
            goal = dict(conn.execute('SELECT id,origin,kind,state,closed_by,closed_at,revision FROM goals WHERE id=?',
                                     (gid,)).fetchone())
            reviews = [dict(r) for r in conn.execute('SELECT * FROM goal_auto_reviews WHERE goal_id=? ORDER BY id', (gid,))]
            items = [dict(r) for r in conn.execute(
                "SELECT item_key,object_id,outcome,reason,details_json FROM maintenance_items WHERE run_id=? AND phase='goals'",
                (run_id,))]
            calls = [dict(r) for r in conn.execute('''SELECT purpose,model,status_code,result_category,timed_out,
                finish_reason,reasoning_effort,prompt_tokens,completion_tokens,reasoning_tokens,duration_ms
                FROM model_calls ORDER BY id''')]
        review = reviews[-1] if reviews else None
        evidence = json.loads(review['evidence_json']) if review else []
        result = {'state': goal['state'], 'closed_by': goal['closed_by'], 'origin': goal['origin'],
                  'review_reason': review['reason'] if review else None,
                  'review_decision': review['decision'] if review else None,
                  'evidence_keys': [keys.get(mid, f'#{mid}') for mid in evidence],
                  'failed_items': [i['reason'] for i in items if i['outcome'] == 'failed'],
                  'skipped_future_messages': skipped, 'model_calls': len(calls),
                  'goal_auto_review': maintenance.get('goal_auto_review')}
        save(directory / 'result.json', {**result, 'goal': goal, 'reviews': reviews, 'items': items, 'calls': calls})
        save(directory / 'requests.json', gateway.records)
        return result, calls
    finally:
        if gateway:
            gateway.close()
        if client:
            client.close()
        store.close()
        if not keep_database:
            for candidate in directory.glob('iris.db*'):
                if candidate.is_dir():
                    shutil.rmtree(candidate, ignore_errors=True)
                else:
                    candidate.unlink(missing_ok=True)


def source_sha256():
    paths = sorted([*ROOT.joinpath('src/iris').rglob('*.py'), *ROOT.joinpath('src/iris').rglob('*.sql'),
                    *ROOT.joinpath('src/iris').rglob('*.md')])
    return hashlib.sha256(b''.join(p.relative_to(ROOT).as_posix().encode() + p.read_bytes() for p in paths)).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--corpus', type=Path, default=ROOT / 'evals/goal_review_v1.json')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--fake', choices=FAKE_MODES,
                        help='offline transport self-check; no configuration read or network calls; not a gate result')
    parser.add_argument('--keep-databases', action='store_true')
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out.is_relative_to(ROOT):
        parser.error('--out must be outside the repository')
    corpus_bytes = args.corpus.read_bytes()
    try:
        corpus = validate_corpus(json.loads(corpus_bytes))
    except (ValueError, KeyError, TypeError) as exc:
        parser.error(f'invalid corpus: {exc}')
    out.mkdir(parents=True, exist_ok=False)
    if args.fake:
        configs = {'chat': ModelConfig('https://goal-review.invalid/v1', '', 'fake-goal-review')}
    else:
        configs = {'chat': load_test_models()['chat']}
    prompt = ROOT / 'src/iris/prompts' / f'{AUTO_REVIEW_PROMPT_VERSION}.md'
    metadata = {'evaluation': 'goal_review', 'format_version': 1, 'corpus_name': corpus.get('name'),
                'corpus_sha256': hashlib.sha256(corpus_bytes).hexdigest(), 'expected_cases': len(corpus['cases']),
                'timezone': corpus['timezone'], 'simulation': bool(args.fake), 'fake_mode': args.fake,
                'purpose': AUTO_REVIEW_PURPOSE, 'prompt_version': AUTO_REVIEW_PROMPT_VERSION,
                'prompt_sha256': hashlib.sha256(prompt.read_bytes()).hexdigest(),
                'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'scoring_sha256': hashlib.sha256(Path(__file__).with_name('goal_review_scoring.py').read_bytes()).hexdigest(),
                'source_sha256': source_sha256(), 'model': configs['chat'].model,
                'reasoning_effort': configs['chat'].reasoning_effort}
    save(out / 'metadata.json', metadata)
    results, rows, calls, failures = {}, [], [], []
    for case in corpus['cases']:
        result, case_calls = run_case(product_input(case), corpus, out / 'cases' / case['id'], configs,
                                      fake=args.fake, keep_database=args.keep_databases)
        results[case['id']] = result
        calls.extend(case_calls)
        print(json.dumps({'case': case['id'], 'state': result['state'], 'calls': len(case_calls)}, ensure_ascii=False),
              flush=True)
        interrupted = [c for c in case_calls if c['timed_out'] or c['status_code'] == 429]
        if result['failed_items']:
            failures.append({'case_id': case['id'], 'reasons': result['failed_items']})
        if interrupted or result['failed_items']:
            break  # Preserve the invalid run; a retry must use a fresh output directory.
    # Labels are read only here, after every executed case.
    for case in corpus['cases']:
        if case['id'] in results:
            rows.append(score_case(case, results[case['id']]))
    summary = {**metadata, **report(rows, calls, expected_cases=len(corpus['cases']), failures=failures)}
    save(out / 'report.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('results', 'categories')}, ensure_ascii=False),
          flush=True)
    return 0 if summary['valid'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
