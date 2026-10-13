"""Real SQLite execution and offline independent scoring; explicit simulation remains non-gating."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from iris.entry_profile import (
    EntryProfileMemoryTask, EntryProfileConflict, MemoryProfileSnapshot,
    MemoryProfileUnavailable, MEMORY_PROFILE_VIOLATIONS, _memory_message,
    _memory_bind_source,
)
from iris.profile_common import evidence_hash
from .runner import load_corpus, _source_hash

SCORING_STATUS = 'approved_by_planner_20261013'
STAGES = ('generated', 'deterministic', 'checked', 'projected')
HERE = Path(__file__).resolve().parent


class SimulationRepository:
    """In-memory contract fake only. It cannot open or write an Iris database."""
    mode = 'simulation'

    def __init__(self, snapshot):
        self.base = copy.deepcopy(snapshot)
        self.messages = copy.deepcopy(list(snapshot.messages))
        self.anchors = copy.deepcopy(list(snapshot.anchors))
        self.memories = copy.deepcopy(list(snapshot.memories))
        self.profiles = copy.deepcopy(list(snapshot.profiles))
        self.cursor = snapshot.cursor
        self.settings_revision = snapshot.settings_revision
        self.receipts = set()
        self.in_transaction = False
        self.plans = {}

    def snapshot(self, entry_id, limits):
        if entry_id != self.base.entry_id:
            raise KeyError(entry_id)
        pending = [m for m in self.messages if m['position'] > self.cursor]
        page = pending[:limits.new_messages]
        history = [*self.anchors, *(m for m in self.messages if m['position'] <= self.cursor)]
        # Simple recent anchors for a fake, deliberately diagnosed as bounded;
        # production thematic history lookup is a future adapter obligation.
        anchors = history[-limits.anchors:]
        return replace(self.base, cursor=self.cursor, settings_revision=self.settings_revision,
            messages=tuple(copy.deepcopy(page)), anchors=tuple(copy.deepcopy(anchors)),
            memories=tuple(copy.deepcopy(self.memories)), profiles=tuple(copy.deepcopy(self.profiles)),
            scan_through=page[-1]['position'] if page else self.cursor,
            has_more=self.base.has_more or len(pending) > len(page),
            history_complete=self.base.history_complete and len(anchors) == len(history))

    def assert_model_call_allowed(self):
        if self.in_transaction:
            raise RuntimeError('model call in transaction')

    def commit(self, plan):
        if plan.plan_id in self.plans:
            return {**self.plans[plan.plan_id], 'replayed': True}
        self.in_transaction = True
        try:
            if self.cursor != plan.snapshot.cursor or self.settings_revision != plan.snapshot.settings_revision:
                raise EntryProfileConflict('cursor_or_settings_changed')
            if self.profiles != list(plan.snapshot.profiles):
                raise EntryProfileConflict('target_changed')
            live = {m['ref']: m for m in [*self.anchors, *self.messages]}
            for message in plan.material['messages']:
                if _memory_message(live.get(message['ref'], {}), plan.snapshot) != message:
                    raise EntryProfileConflict('source_changed')
            facts = {m['memory_id']: m for m in self.memories}
            original = {m['memory_id']: m for m in plan.snapshot.memories}
            for fact in plan.material['memories']:
                if facts.get(fact['memory_id']) != original.get(fact['memory_id']):
                    raise EntryProfileConflict('memory_source_changed')
            profiles, receipts = copy.deepcopy(self.profiles), set(self.receipts)
            applied = []
            for op in plan.operations:
                target = next((p for p in profiles if p['memory_id'] == op['target_id']), None)
                if op['op'] != 'new':
                    if (not target or target['revision'] != op['expected_revision']
                            or target['lifecycle'] == 'deleted'
                            or (target.get('manual_protected') and op['op'] in ('revise', 'retire'))):
                        raise EntryProfileConflict('write_guard')
                if op['op'] == 'new':
                    mid = 'epm' + str(len(profiles) + 1)
                    target = dict(memory_id=mid, revision=1, kind='入口画像', category=op['category'],
                        body=op['body'], saying=op['saying'], meaning=op['meaning'], author='model',
                        speaker='self', stance='转述' if op['category'] == 'rule' else '推断',
                        about=[plan.snapshot.entry_subject_id], owner_entry_id=plan.snapshot.entry_id,
                        lifecycle='active', event_time=None, importance=op['importance'], pinned=False,
                        manual_protected=False, retention=round(30 + op['importance'] * .4),
                        sources=copy.deepcopy(op['sources']), confirmed_evidence=[], history=[])
                    profiles.append(target)
                elif op['op'] == 'confirm':
                    new = [identity for identity in op['new_evidence']
                           if (plan.snapshot.entry_id, target['memory_id'], identity) not in receipts]
                    if not new:
                        raise EntryProfileConflict('evidence_already_confirmed')
                    target['retention'] = min(100, target['retention'] + 5)
                    if target['lifecycle'] == 'forgotten' and target['retention'] >= 35:
                        target['lifecycle'] = 'active'
                    existing = {s['evidence_identity'] for s in target['sources']}
                    target['sources'].extend(copy.deepcopy(s) for s in op['sources'] if s['evidence_identity'] not in existing)
                elif op['op'] == 'revise':
                    target['history'].append(dict(revision=target['revision'], body=target['body']))
                    target.update(body=op['body'], category=op['category'], saying=op['saying'],
                                  meaning=op['meaning'], importance=op['importance'],
                                  sources=copy.deepcopy(op['sources']), revision=target['revision'] + 1)
                else:
                    target['lifecycle'] = 'forgotten'
                    target['revision'] += 1
                for identity in op['new_evidence']:
                    receipts.add((plan.snapshot.entry_id, target['memory_id'], identity))
                target['confirmed_evidence'] = sorted(set(target['confirmed_evidence']) | set(op['new_evidence']))
                applied.append(dict(index=op['index'], op=op['op'], memory_id=target['memory_id']))
            self.profiles, self.receipts, self.cursor = profiles, receipts, plan.cursor_after
            receipt = dict(applied=applied, cursor_before=plan.snapshot.cursor,
                           cursor_after=self.cursor, replayed=False)
            self.plans[plan.plan_id] = receipt
            return receipt
        finally:
            self.in_transaction = False


def case_snapshot(case):
    """Existing frozen input keys unchanged, annotations never enter prompts."""
    entry = case['entry']; entry_id = entry['id']
    messages = []
    # Cursor follows frozen input order, not event timestamps.
    for position, raw in enumerate(case['messages'], 1):
        quote = raw.get('quote')
        message = dict(ref=raw['id'], position=position, entry_id=raw.get('entry_id', entry_id),
            kind=raw.get('kind', 'message'), speaker=raw.get('sender', 'speaker'),
            content=raw['text'], occurred_at=raw['at'])
        if quote:
            message['quote'] = dict(ref=quote.get('message_id'), speaker=quote.get('sender'), content=quote.get('text', ''))
        if message['entry_id'] == entry_id:
            messages.append(message)
    index = {m['ref']: m for m in messages}; memories = []
    for n, raw in enumerate(case.get('memories', []), 1):
        # A fact with foreign/missing roots is not entry-origin evidence.
        roots = [index[s] for s in raw['sources'] if s in index]
        memories.append(dict(memory_id=str(raw.get('id', f'm{n}')), revision=raw.get('revision', 1),
            kind=raw.get('kind', '事实'), body=raw['content'], lifecycle=raw.get('lifecycle', 'active'),
            visible=len(roots) == len(raw['sources']), source_messages=copy.deepcopy(roots)))
    profiles = []
    if case.get('initial_profile'):
        # Lossless legacy manual text. Classification/migration is not simulated.
        profiles.append(dict(memory_id='manual1', revision=1, kind='入口画像', category=None,
            body=case['initial_profile'], saying='', meaning='', author='admin', speaker='self', stance='设定',
            about=['entry:' + entry_id], owner_entry_id=entry_id, lifecycle='active', event_time=None,
            importance=80, pinned=False, manual_protected=True, sources=[], confirmed_evidence=[],
            retention=80, history=[]))
    return MemoryProfileSnapshot(entry_id=entry_id, entry_subject_id='entry:' + entry_id,
        entry_kind=entry['kind'], timezone=case.get('timezone', 'Asia/Shanghai'), as_of=case['as_of'],
        settings_revision=1, cursor=0, scan_through=messages[-1]['position'] if messages else 0,
        has_more=False, messages=tuple(messages), memories=tuple(memories), profiles=tuple(profiles))


def _judge_message(message):
    value = {k: copy.deepcopy(message[k]) for k in (
        'ref', 'entry_id', 'kind', 'speaker', 'content', 'occurred_at', 'date') if k in message}
    if isinstance(message.get('quote'), dict):
        value['quote'] = {k: message['quote'][k] for k in ('ref', 'speaker', 'content') if k in message['quote']}
    return value


def _judge_source(source):
    # Recursive allowlists: neither rejected model payloads nor stored metadata
    # may smuggle self-judgments into the evidence context.
    value = {k: copy.deepcopy(source[k]) for k in ('ref', 'field', 'excerpt', 'via_memory', 'speaker') if k in source}
    if isinstance(source.get('message'), dict):
        value['message'] = _judge_message(source['message'])
    if isinstance(source.get('memory'), dict):
        memory = source['memory']
        value['memory'] = {k: memory[k] for k in ('memory_id', 'revision', 'body', 'kind', 'lifecycle') if k in memory}
        value['memory']['source_messages'] = [_judge_message(m) for m in memory.get('source_messages', [])]
    return value


def _judge_memory(memory):
    keys = ('memory_id', 'revision', 'kind', 'category', 'body', 'author', 'speaker',
            'stance', 'about', 'owner_entry_id', 'lifecycle', 'event_time', 'importance')
    value = {k: copy.deepcopy(memory[k]) for k in keys if k in memory}
    value['sources'] = [_judge_source(s) for s in memory.get('sources', [])]
    return value


def _candidate(raw, material):
    if not isinstance(raw, dict):
        return {'body': '', 'sources': []}
    target = next((p for p in material['profiles'] if p['memory_id'] == raw.get('target_id')), {})
    body = raw.get('body', target.get('body', ''))
    category = raw.get('category', target.get('category'))
    value = {'body': body if isinstance(body, str) else '',
             'category': category if isinstance(category, str) else None,
             'op': raw.get('op') if isinstance(raw.get('op'), str) else None, 'sources': []}
    for source in raw.get('sources', []) if isinstance(raw.get('sources'), list) else []:
        try:
            # Even canonical stages are rebound: the presence of a `message`
            # field is NOT proof that it came from our resolver.
            claim = {k: source[k] for k in ('ref', 'field', 'excerpt', 'via_memory') if isinstance(source, dict) and k in source}
            bound = _memory_bind_source(claim, material)
            value['sources'].append(_judge_source(bound))
        except ValueError:
            # No forged context for invalid bindings; keep only claimed fields.
            value['sources'].append({k: source[k] for k in ('ref', 'field', 'excerpt', 'via_memory') if isinstance(source, dict) and k in source})
    return value


def stage_items(operations, material):
    """Hypothetical active snapshot, so preserved/manual coverage is not lost."""
    items = {p['memory_id']: _judge_memory(p) for p in material['profiles'] if p['lifecycle'] == 'active'}
    for index, raw in enumerate(operations):
        if not isinstance(raw, dict):
            continue
        target_id = raw.get('target_id')
        if not isinstance(target_id, (str, int)) or isinstance(target_id, bool):
            target_id = None
        target = next((p for p in material['profiles'] if p['memory_id'] == target_id), {})
        if target.get('manual_protected') and raw.get('op') != 'confirm':
            continue
        if raw.get('op') == 'retire':
            items.pop(target_id, None)
            continue
        item = _candidate(raw, material)
        item['author'] = target.get('author', 'model') if raw.get('op') == 'confirm' else 'model'
        if raw.get('op') == 'confirm':
            item['sources'] = [_judge_source(s) for s in target.get('sources', [])] + item['sources']
        items[target_id or f'candidate:{index}'] = item
    return list(items.values())


def judging_input(case, result, profiles):
    memories = [_judge_memory(p) for p in profiles if p['lifecycle'] == 'active']
    # Fake projection has no host budget claims. All active memories displayed.
    projection = dict(text='\n'.join(m['body'] for m in memories), memory_ids=[m['memory_id'] for m in memories])
    stages = {stage: stage_items(result['stages'][stage], result['material']) for stage in STAGES[:-1]}
    stages['projected'] = copy.deepcopy(memories)
    return dict(entry={k: case['entry'][k] for k in ('id', 'kind')}, as_of=case['as_of'],
        timezone=case.get('timezone', 'Asia/Shanghai'), memories=memories, projection=projection,
        stages=stages, must_cover=case.get('must_cover', []), forbidden=case.get('forbidden', []))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run_simulation(corpus, out, *, gateway_factory, limits=None):
    """Explicit fake gateway injection only; no configuration/secret loading."""
    cases = load_corpus(corpus); root = Path(out)
    root.mkdir(parents=True, exist_ok=False)
    materials = root / 'judging-materials'; rows = []; entries = []; versions = {}; hashes = {}
    for index, case in enumerate(cases, 1):
        repo = SimulationRepository(case_snapshot(case))
        gateway = gateway_factory(case['id'], repo)
        task = EntryProfileMemoryTask(repo, gateway, limits=limits)
        results = []
        while True:
            try:
                result = task.run(case['entry']['id'])
            except MemoryProfileUnavailable as error:
                result = dict(status='unavailable', reason=str(error), material={'profiles': []},
                    stages={s: [] for s in STAGES[:-1]}, calls=[], outputs={}, removed=[],
                    prompt_versions=[p['name'] for p in task.prompts.values()],
                    prompt_sha256={p['name']: p['sha256'] for p in task.prompts.values()})
            results.append(result)
            versions.update({v: v for v in result['prompt_versions']}); hashes.update(result['prompt_sha256'])
            if result['status'] != 'committed' or not result['material']['has_more']:
                break
        # Per-page stage snapshots remain in trace for backlog diagnostics. The
        # case judgment concerns the final checkpoint, not a union that could
        # count a subsequently retired statement as still available.
        for page, result in enumerate(results, 1):
            rows.append(dict(case_id=case['id'], page=page, result=result))
        active = repo.profiles if results[-1]['status'] != 'unavailable' else []
        payload = judging_input(case, results[-1], active)
        relative = f'cases/{index:04d}.json'
        document = dict(format_version=2, evaluation='entry_profile_memory', case_id=case['id'], input=payload)
        _write(materials / relative, document)
        entries.append(dict(case_id=case['id'], file=relative, sha256=evidence_hash(document),
            judgment_file=f'{index:04d}.json', execution_status=results[-1]['status'],
            diagnostics=dict(pages=len(results),
                quote_total=sum(r.get('quote_validation', {}).get('total', 0) for r in results),
                quote_matched=sum(r.get('quote_validation', {}).get('matched', 0) for r in results),
                omitted_anchors=sum(r['material'].get('diagnostics', {}).get('omitted_anchors', 0) for r in results),
                omitted_memories=sum(r['material'].get('diagnostics', {}).get('omitted_memories', 0) for r in results),
                truncated_messages=sum(r['material'].get('diagnostics', {}).get('truncated_messages', 0) for r in results),
                failed_calls=sum(c.get('result') != 'success' for r in results for c in r['calls']))))
    scoring = (HERE / 'scoring_v3.md').read_text(encoding='utf-8')
    metadata = dict(format_version=2, evaluation='entry_profile_memory', scoring_version='entry_profile_scoring_v3',
        scoring_status=SCORING_STATUS, gate_eligible=False, repository_mode='simulation',
        corpus_sha256=hashlib.sha256(Path(corpus).read_bytes()).hexdigest(), source_sha256=_source_hash(),
        scoring_sha256=hashlib.sha256(scoring.encode('utf-8')).hexdigest(), prompt_versions=list(versions), prompt_sha256=hashes)
    _write(root / 'run.json', dict(metadata=metadata, cases=rows))
    manifest = dict(metadata=metadata, cases=entries,
        run_sha256=hashlib.sha256((root / 'run.json').read_bytes()).hexdigest())
    manifest['materials_sha256'] = evidence_hash(manifest)
    _write(materials / 'manifest.json', manifest)
    _write(materials / 'round-template.json', manifest)
    (materials / 'scoring.md').write_text(scoring, encoding='utf-8')
    return manifest


def _violations(value):
    return (isinstance(value, list) and value == [v for v in MEMORY_PROFILE_VIOLATIONS if v in value])


def validate_vote(vote, payload):
    if not isinstance(vote, dict) or set(vote) != {'memory_results', 'projection_violations', 'must_cover', 'forbidden', 'stage_coverage'}:
        raise ValueError('invalid judgment fields')
    rows = vote['memory_results']
    if not isinstance(rows, list) or len(rows) != len(payload['memories']):
        raise ValueError('memory judgment count mismatch')
    for index, (row, memory) in enumerate(zip(rows, payload['memories'], strict=True), 1):
        if (not isinstance(row, dict) or set(row) != {'index', 'memory_id', 'revision', 'supported', 'violations', 'reason'}
                or type(row['index']) is not int or row['index'] != index
                or row['memory_id'] != memory['memory_id'] or type(row['revision']) is not int
                or row['revision'] != memory['revision'] or type(row['supported']) is not bool
                or not _violations(row['violations']) or not isinstance(row['reason'], str) or not row['reason'].strip()
                or ('无依据' in row['violations'] and row['supported'])):
            raise ValueError('invalid memory judgment')
    if not _violations(vote['projection_violations']):
        raise ValueError('invalid projection violations')
    def booleans(value, length):
        if not isinstance(value, list) or len(value) != length or any(type(b) is not bool for b in value):
            raise ValueError('invalid coverage/forbidden array')
    for key in ('must_cover', 'forbidden'):
        booleans(vote[key], len(payload[key]))
    if not isinstance(vote['stage_coverage'], dict) or set(vote['stage_coverage']) != set(STAGES):
        raise ValueError('invalid stages')
    for stage in STAGES:
        booleans(vote['stage_coverage'][stage], len(payload['must_cover']))
        if not payload['stages'][stage] and any(vote['stage_coverage'][stage]):
            raise ValueError('empty stage cannot cover')
    if not payload['memories'] and any(vote['must_cover']):
        raise ValueError('empty memories cannot cover')
    if not payload['projection']['text'] and vote['projection_violations']:
        raise ValueError('empty projection cannot violate')
    return vote


def _combine_votes(votes):
    if len(votes) == 1:
        return copy.deepcopy(votes[0]), []
    a, b = votes; result = copy.deepcopy(a); differences = []
    def combine(path, left, right, union=False):
        if left != right:
            differences.append(dict(field=path, round_1=left, round_2=right))
        if isinstance(left, list):
            return [v for v in MEMORY_PROFILE_VIOLATIONS if v in left or v in right]
        return (left or right) if union else (left and right)
    for i, (left, right) in enumerate(zip(a['memory_results'], b['memory_results'], strict=True)):
        for key in ('supported', 'violations'):
            result['memory_results'][i][key] = combine(f'memory_results[{i}].{key}', left[key], right[key])
        result['memory_results'][i]['reason'] = f"round 1: {left['reason']}; round 2: {right['reason']}"
    result['projection_violations'] = combine('projection_violations', a['projection_violations'], b['projection_violations'])
    for key in ('must_cover', 'forbidden'):
        result[key] = [combine(f'{key}[{i}]', l, r, key == 'forbidden') for i, (l, r) in enumerate(zip(a[key], b[key], strict=True))]
    for stage in STAGES:
        result['stage_coverage'][stage] = [combine(f'stage_coverage.{stage}[{i}]', l, r)
            for i, (l, r) in enumerate(zip(a['stage_coverage'][stage], b['stage_coverage'][stage], strict=True))]
    return result, differences


def _read(path):
    from iris.profile_common import json_object
    return json_object(path.read_text(encoding='utf-8'))


def score_draft(materials, judgments, *, judge_model):
    """Offline mechanism scorer; simulation can NEVER claim a passed gate."""
    materials = Path(materials); paths = list(map(Path, judgments))
    if len(paths) not in (1, 2) or len({p.resolve() for p in paths}) != len(paths) or not judge_model.strip():
        raise ValueError('one or two distinct independent rounds required')
    manifest = _read(materials / 'manifest.json'); metadata = manifest['metadata']
    if (metadata.get('format_version') != 2 or metadata.get('evaluation') != 'entry_profile_memory'
            or metadata.get('scoring_status') != SCORING_STATUS or type(metadata.get('gate_eligible')) is not bool
            or metadata.get('repository_mode') not in ('simulation','sqlite')
            or (metadata.get('gate_eligible') and (metadata.get('repository_mode')!='sqlite' or metadata.get('execution_kind')!='real' or metadata.get('invalidated')))
            or manifest.get('materials_sha256') != evidence_hash({k: v for k, v in manifest.items() if k != 'materials_sha256'})):
        raise ValueError('material fingerprint/format mismatch')
    scoring_path = materials / 'scoring.md'
    if (not scoring_path.resolve().is_relative_to(materials.resolve())
            or hashlib.sha256(scoring_path.read_bytes()).hexdigest() != metadata['scoring_sha256']):
        raise ValueError('scoring fingerprint mismatch')
    payloads = []
    for index, entry in enumerate(manifest['cases'], 1):
        if entry['file'] != f'cases/{index:04d}.json' or entry['judgment_file'] != f'{index:04d}.json':
            raise ValueError('invalid case filename')
        path = materials / entry['file']
        if not path.resolve().is_relative_to(materials.resolve()):
            raise ValueError('material outside directory')
        doc = _read(path)
        if (evidence_hash(doc) != entry['sha256'] or doc.get('case_id') != entry['case_id']
                or doc.get('format_version') != 2 or doc.get('evaluation') != 'entry_profile_memory'):
            raise ValueError('case fingerprint mismatch')
        payloads.append(doc['input'])
    rounds = []
    for path in paths:
        expected = {'manifest.json', *(entry['judgment_file'] for entry in manifest['cases'])}
        if {p.name for p in path.glob('*.json')} != expected:
            raise ValueError('missing or extra judgment files')
        if any(not (path / name).resolve().is_relative_to(path.resolve()) for name in expected):
            raise ValueError('judgment outside directory')
        if _read(path / 'manifest.json') != manifest:
            raise ValueError('round material fingerprint mismatch')
        rounds.append([validate_vote(_read(path / entry['judgment_file']), payload)
                       for entry, payload in zip(manifest['cases'], payloads, strict=True)])
    combined = []; differences = []; losses = []; total = grounded = manual = forbidden = covered = expected = 0
    prohibited = {}; stage_counts = {stage: 0 for stage in STAGES}
    stage_losses = {a + '_to_' + b: 0 for a, b in zip(STAGES, STAGES[1:])}
    for i, (entry, payload) in enumerate(zip(manifest['cases'], payloads, strict=True)):
        vote, diff = _combine_votes([r[i] for r in rounds]); combined.append(vote)
        differences.extend(dict(case_id=entry['case_id'], **d) for d in diff)
        for memory, row in zip(payload['memories'], vote['memory_results'], strict=True):
            if memory['author'] == 'admin':
                manual += 1
            else:
                total += 1; grounded += row['supported']
            for violation in row['violations']:
                if violation != '无依据':
                    prohibited[violation] = prohibited.get(violation, 0) + 1
        for violation in vote['projection_violations']:
            if violation != '无依据':
                prohibited[violation] = prohibited.get(violation, 0) + 1
        forbidden += sum(vote['forbidden']); covered += sum(vote['must_cover']); expected += len(vote['must_cover'])
        for stage in STAGES:
            stage_counts[stage] += sum(vote['stage_coverage'][stage])
        for a, b in zip(STAGES, STAGES[1:]):
            missing = [n for n, (l, r) in enumerate(zip(vote['stage_coverage'][a], vote['stage_coverage'][b], strict=True), 1) if l and not r]
            stage_losses[a + '_to_' + b] += len(missing)
            if missing:
                losses.append(dict(case_id=entry['case_id'], before=a, after=b, indices=missing))
    metrics = dict(model_items=total, grounded_items=grounded, grounded_ratio=grounded / total if total else None,
        manual_items=manual, prohibited_violations=prohibited, forbidden_occurrences=forbidden,
        must_cover_covered=covered, must_cover_total=expected, stage_counts=stage_counts, stage_losses=stage_losses,
        incomplete_cases=sum(e.get('execution_status') not in ('committed', 'no_new_evidence') for e in manifest['cases']),
        quote_total=sum(e['diagnostics']['quote_total'] for e in manifest['cases']),
        quote_matched=sum(e['diagnostics']['quote_matched'] for e in manifest['cases']),
        infrastructure=[dict(case_id=e['case_id'], status=e['execution_status'], **e['diagnostics']) for e in manifest['cases']],
        meets_threshold=None)
    if metadata['gate_eligible'] and len(rounds)==2:
        metrics['meets_threshold']=bool(total and grounded/total>=.9 and not prohibited and not forbidden and not metrics['incomplete_cases'])
    return dict(scoring_status=SCORING_STATUS, gate_eligible=metadata['gate_eligible'], repository_mode=metadata['repository_mode'],
        materials_sha256=manifest['materials_sha256'], judge_model=judge_model, judge_runs=len(rounds),
        original_judgments=rounds, judgments=combined, disagreements=differences, coverage_losses=losses, metrics=metrics)


def run_sqlite(corpus, out, *, gateway_factory=None, configs=None):
    """Sequential production engine, actual SQLite writes and host projection.

    Factory injection is explicitly fake and cannot claim a quality gate. A
    timeout/429 interrupts the run immediately; remaining cases are exported as
    not-run, all preceding traces are retained, and gate_eligible becomes false.
    """
    from unittest.mock import patch
    from iris.db import Store, dumps
    from iris.entry_profile import EntryProfileEngine, memory_profile_rows, profile_context, _instant
    from iris.memory_ops import write_entry_profile_memory
    from iris.models import Gateway, ModelError
    from .runner import _load_case, _model_metadata, _redacted
    if (gateway_factory is None)==(configs is None):
        raise ValueError('provide exactly one real configuration or fake gateway factory')
    root=Path(out); root.mkdir(parents=True,exist_ok=False)
    materials=root/'judging-materials'; cases=load_corpus(corpus)
    entries=[]; traces=[]; hashes={}; invalid=None

    class EvaluationGateway(Gateway):
        def _record(self,*args,**kwargs):
            super()._record(*args,**kwargs)
            status=kwargs.get('status_code',args[7] if len(args)>7 else None)
            if status==429 or kwargs.get('timed_out'):
                # Record the network outcome, but do not retry a tainted round.
                raise ModelError('retryable','evaluation interrupted',reason='rate_limited' if status==429 else 'timeout')

    for index,case in enumerate(cases,1):
        store=Store(root/'databases'/f'{index:04d}.db'); gateway=None
        try:
            _load_case(store,{**case,'initial_profile':''})
            entry_id=case['entry']['id']; clock=lambda:_instant(case['as_of'])
            if case.get('initial_profile'):
                with patch('iris.entry_profile.utc_now',clock):
                    write_entry_profile_memory(store,entry_id,op='new',content=case['initial_profile'],category=None,
                        importance=80,manual=True,current=clock())
            gateway=(gateway_factory(case['id'],store) if gateway_factory else EvaluationGateway(configs,store=store))
            engine=EntryProfileEngine(store,gateway,clock=clock)
            task=EntryProfileMemoryTask(None,gateway)
            hashes.update({p['name']:p['sha256'] for p in task.prompts.values()})
            results=[]
            while not invalid:
                with store.read() as conn:
                    from iris.entry_profile import _memory_state
                    expected=_memory_state(conn,entry_id)['version']
                # Use the case's observation time in the transaction projection
                # too. The public corpus may be later than the device clock.
                with patch('iris.entry_profile.utc_now',clock):
                    outcome=engine.regenerate(entry_id,expected_version=expected)
                result=outcome.get('result')
                if result is None:
                    result=dict(status='no_new_evidence' if outcome.get('reason')=='no_new_evidence' else 'unavailable',
                        reason=outcome.get('reason'),material={'profiles':[],'messages':[],'memories':[]},
                        stages={s:[] for s in STAGES[:-1]},calls=[],outputs={},removed=[])
                results.append(result)
                if result.get('reason') in ('timeout','rate_limited') or any(c.get('reason') in ('timeout','rate_limited') for c in result['calls']):
                    invalid=dict(case_id=case['id'],page=len(results),reason=result.get('reason'))
                    break
                if result['status']!='committed' or not result['material'].get('has_more'):
                    break
            if not results:
                results=[dict(status='not_run',reason='earlier_timeout_or_429',material={'profiles':[],'messages':[],'memories':[]},
                    stages={s:[] for s in STAGES[:-1]},calls=[],outputs={},removed=[])]
            with store.read() as conn:
                profiles=memory_profile_rows(conn,entry_id,as_of=case['as_of'],usable_only=True)
                projection=profile_context(conn,entry_id)
                calls=[dict(r) for r in conn.execute('''SELECT purpose,model,status_code,result_category,timed_out,finish_reason,
                    reasoning_effort,prompt_tokens,completion_tokens,duration_ms FROM model_calls ORDER BY id''')]
                migration=[dict(r) for r in conn.execute('SELECT * FROM entry_profile_legacy_migration')]
            payload=judging_input(case,results[-1],profiles)
            payload['projection']=projection or dict(text='',memory_ids=[])
            displayed=set(payload['projection']['memory_ids'])
            payload['stages']['projected']=[m for m in payload['memories'] if m['memory_id'] in displayed]
            document=dict(format_version=2,evaluation='entry_profile_memory',case_id=case['id'],input=payload)
            relative=f'cases/{index:04d}.json';_write(materials/relative,document)
            traces.append(dict(case_id=case['id'],pages=results,model_calls=calls,migration=migration))
            diagnostics=dict(pages=len(results),
                quote_total=sum(r.get('quote_validation',{}).get('total',0) for r in results),
                quote_matched=sum(r.get('quote_validation',{}).get('matched',0) for r in results),
                omitted_anchors=sum(r['material'].get('diagnostics',{}).get('omitted_anchors',0) for r in results),
                omitted_memories=sum(r['material'].get('diagnostics',{}).get('omitted_memories',0) for r in results),
                truncated_messages=sum(r['material'].get('diagnostics',{}).get('truncated_messages',0) for r in results),
                failed_calls=sum(c.get('result')!='success' for r in results for c in r['calls']))
            entries.append(dict(case_id=case['id'],file=relative,sha256=evidence_hash(document),
                judgment_file=f'{index:04d}.json',execution_status=results[-1]['status'],diagnostics=diagnostics))
            # Durable progress after each case; never lose earlier successful traces.
            _write(root/'progress.json',_redacted(dict(cases=traces,invalidated=invalid),configs or {}))
        finally:
            if gateway is not None and callable(getattr(gateway,'close',None)):
                gateway.close()
            store.close()
    scoring=(HERE/'scoring_v3.md').read_text(encoding='utf-8')
    metadata=dict(format_version=2,evaluation='entry_profile_memory',scoring_version='entry_profile_scoring_v3',
        scoring_status=SCORING_STATUS,gate_eligible=configs is not None and invalid is None,repository_mode='sqlite',
        execution_kind='real' if configs is not None else 'fake',invalidated=invalid,
        corpus_sha256=hashlib.sha256(Path(corpus).read_bytes()).hexdigest(),source_sha256=_source_hash(),
        scoring_sha256=hashlib.sha256(scoring.encode()).hexdigest(),prompt_versions=list(hashes),prompt_sha256=hashes,
        models=_model_metadata(configs or {}))
    _write(root/'run.json',_redacted(dict(metadata=metadata,cases=traces),configs or {}))
    manifest=dict(metadata=metadata,cases=entries,run_sha256=hashlib.sha256((root/'run.json').read_bytes()).hexdigest())
    manifest['materials_sha256']=evidence_hash(manifest)
    _write(materials/'manifest.json',manifest);_write(materials/'round-template.json',manifest)
    (materials/'scoring.md').write_text(scoring,encoding='utf-8')
    return manifest


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Entry-profile memory execution / offline scoring')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('--corpus',required=True);run.add_argument('--out',required=True)
    score=sub.add_parser('score');score.add_argument('--materials',required=True)
    score.add_argument('--judgments',action='append',required=True);score.add_argument('--judge-model',required=True);score.add_argument('--out',required=True)
    args=parser.parse_args()
    if args.command=='run':
        from iris.models import load_test_models
        manifest=run_sqlite(args.corpus,args.out,configs=load_test_models())
        print(json.dumps(dict(cases=len(manifest['cases']),gate_eligible=manifest['metadata']['gate_eligible'],
                              invalidated=manifest['metadata']['invalidated']),ensure_ascii=False))
        if manifest['metadata']['invalidated']:
            raise SystemExit(2)
    else:
        result=score_draft(args.materials,args.judgments,judge_model=args.judge_model)
        _write(Path(args.out),result)
        print(json.dumps(result['metrics'],ensure_ascii=False))


if __name__=='__main__':
    main()
