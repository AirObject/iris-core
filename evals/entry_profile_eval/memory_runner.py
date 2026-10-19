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


def _incremental_stages(work, manual):
    """Chronological snapshots; evidence from later days never backfills earlier versions."""
    stages={name:copy.deepcopy(manual) for name in STAGES}
    accumulated={};pending_checked={}
    for item in work:
        payload=json.loads(item['payload_json']); result=json.loads(item['result_json'])
        if item['kind']=='observe':
            values=result.get('generated',[])
            directory={p['memory_id']:p for p in payload.get('profiles',[])}
            applied={r['index']:r for r in result.get('applied',[])}
            for index,raw in enumerate(values,1):
                target=directory.get(raw.get('memory_id'),{})
                mid=raw.get('memory_id',applied.get(index,{}).get('memory_id',f"{item['id']}:{index}"))
                body=raw.get('body',target.get('body',''))
                if raw.get('action')=='retire': continue
                candidate=dict(memory_id=mid,body=body,category=raw.get('category',target.get('category')),
                    author='admin' if target.get('manual_protected') else 'model',observed_at=payload['as_of'],
                    work_id=item['id'],sources=[])
                key=(mid,body)
                candidate['sources']=copy.deepcopy(accumulated.get(key,[]))
                for ref in raw.get('source_refs',[]):
                    try:
                        bound=_memory_bind_source(dict(ref='msg:'+str(ref['message_id']),field=ref['field'],
                            excerpt=ref['excerpt'],via_memory=None),dict(messages=payload['messages'],memories=[]))
                        candidate['sources'].append(_judge_source(bound))
                    except ValueError:
                        candidate['sources'].append({k:ref[k] for k in ('message_id','field','excerpt') if k in ref})
                stages['generated'].append(candidate)
                if index in applied:
                    effective=next((r for r in result.get('deterministic',[]) if r['index']==index),None)
                    previous=copy.deepcopy(accumulated.get(key,[]))
                    if effective:
                        previous.extend(_judge_source(v) for v in effective['sources'])
                        accumulated[(mid,effective.get('effective_body',body))]=previous
                    else: accumulated[key]=candidate['sources']
        else:
            memory=payload.get('memory')
            if not memory or memory.get('action')=='retire': continue
            sources=[]
            for source in memory['sources']:
                # Only frozen per-item evidence, without model verdict fields.
                value={k:source[k] for k in ('field','excerpt','speaker')}
                value['ref']='msg:'+str(source['message_id'])
                value['message']=dict(ref=value['ref'],entry_id=payload['entry_id'],kind='message',
                    speaker=source.get('message_speaker',source['speaker']),content=source['context'],date=source['date'],occurred_at=source['occurred_at'])
                if source.get('quote'): value['message']['quote']=_judge_message({'quote':source['quote']})['quote']
                sources.append(value)
            candidate={k:memory[k] for k in ('memory_id','revision','category','body')}
            candidate.update(author='model',sources=sources,observed_at=payload['as_of'],work_id=item['id'])
            stages['deterministic'].append(candidate)
            parsed=json.loads(item['parsed_json']) if item['parsed_json'] else {}
            if item['state']=='applied' and parsed.get('decision')=='accept':
                checked=copy.deepcopy(candidate);checked['sources']=[]
                for safe in parsed['safe_sources']:
                    match=next(s for s in sources if s['ref']=='msg:'+str(safe['message_id']) and s['field']==safe['field'] and safe['excerpt'] in s['excerpt'])
                    checked['sources'].append({**match,'excerpt':safe['excerpt']})
                bundle=payload.get('_snapshot',{}).get('bundle')
                if bundle:
                    pending=pending_checked.setdefault(bundle['key'],[])
                    pending.append(checked)
                    if result.get('admission_complete'):
                        merged=copy.deepcopy(checked);merged['sources']=[];seen=set()
                        for page in pending:
                            for source in page['sources']:
                                key=(source['ref'],source['field'],source['excerpt'])
                                if key not in seen: merged['sources'].append(source);seen.add(key)
                        stages['checked'].append(merged)
                else: stages['checked'].append(checked)
    return stages


def _replay_arrivals(store, case, messages, remaining_memories, ids):
    from iris.db import dumps
    from iris.entry_profile import _instant
    with store.write() as conn:
        for message in messages:
            sender=message.get('sender','speaker');stamp=message['at']
            conn.execute('INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES(?,?,?,?)',
                (sender,'self' if sender=='self' else 'person',sender,stamp))
            quote=message.get('quote',{})
            ids[message['id']]=conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,
                received_at,dedupe_key,quote_author_subject_id,quote_content) VALUES(?,?,?,?,?,?,?,?,?)''',
                (message.get('entry_id',case['entry']['id']),message.get('kind','message'),sender,message['text'],stamp,
                 stamp,message['id'],quote.get('sender'),quote.get('text'))).lastrowid
        for index,memory in list(remaining_memories.items()):
            if not set(memory['sources'])<=ids.keys(): continue
            stamps=[m['at'] for m in case['messages'] if m['id'] in memory['sources']]
            stamp=max(stamps,key=_instant) if stamps else case['as_of']
            mid=conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
                lifecycle,entry_id,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES(?,'事实','self',?,90,?,80,?,?,?,?,?,?)''',
                (memory['content'],memory.get('stance','亲历'),memory.get('importance',50),memory.get('lifecycle','active'),
                 case['entry']['id'],stamp,stamp,stamp,stamp)).lastrowid
            for key in dict.fromkeys(memory['sources']):
                conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",(mid,ids[key],stamp))
            del remaining_memories[index]


def run_sqlite(corpus, out, *, gateway_factory=None, configs=None, union_nomination=False):
    """Pre-registered daily_plus_final_flush, through production SQLite admission."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from unittest.mock import patch
    from iris.db import Store, dumps
    from iris.entry_profile import EntryProfileEngine, memory_profile_rows, profile_context, _instant, _memory_state, incremental_prompts
    from iris.memory_ops import write_entry_profile_memory
    from iris.maintenance import Maintenance
    from iris.models import Gateway, ModelError
    from .runner import _load_case, _model_metadata, _redacted
    if (gateway_factory is None)==(configs is None):
        raise ValueError('provide exactly one real configuration or fake gateway factory')
    if type(union_nomination) is not bool: raise ValueError('union_nomination must be boolean')
    root=Path(out);root.mkdir(parents=True,exist_ok=False)
    materials=root/'judging-materials';cases=load_corpus(corpus)
    entries=[];traces=[];invalid=None
    hashes={p['name']:p['sha256'] for p in incremental_prompts().values()}

    class EvaluationGateway(Gateway):
        abort_on_model_transport_error=True
        def _record(self,*args,**kwargs):
            super()._record(*args,**kwargs)
            status=kwargs.get('status_code',args[7] if len(args)>7 else None)
            if status==429 or kwargs.get('timed_out'):
                raise ModelError('retryable','evaluation interrupted',reason='rate_limited' if status==429 else 'timeout')

    for index,case in enumerate(cases,1):
        store=Store(root/'databases'/f'{index:04d}.db');gateway=None;results=[];checkpoints=[]
        entry_id=case['entry']['id'];current=_instant(case['as_of'])
        zone=ZoneInfo(case.get('timezone','Asia/Shanghai'));as_of=current.astimezone(zone)
        try:
            _load_case(store,{**case,'messages':[],'memories':[],'initial_profile':''})
            store.set_setting('consolidation',{'enabled':False})
            store.set_setting('scheduled_backup',{'enabled':False})
            if union_nomination:
                from iris.profile_common import setting
                with store.read() as conn: runtime=setting(conn,'entry_profile',{})
                store.set_setting('entry_profile',{**runtime,'union_nomination':True})
            clock=lambda:current
            if case.get('initial_profile'):
                with patch('iris.entry_profile.utc_now',clock):
                    write_entry_profile_memory(store,entry_id,op='new',content=case['initial_profile'],category=None,
                        importance=80,manual=True,current=clock())
            with store.read() as conn: manual=[_judge_memory(p) for p in memory_profile_rows(conn,entry_id) if p['author']=='admin']
            arrivals=sorted(enumerate(case['messages']),key=lambda pair:(_instant(pair[1]['at']),pair[0]))
            position=0;ids={};remaining=dict(enumerate(case.get('memories',[])))
            if not invalid:
                gateway=gateway_factory(case['id'],store) if gateway_factory else EvaluationGateway(configs,store=store)
                gateway.abort_on_model_transport_error=True
                engine=EntryProfileEngine(store,gateway,clock=clock)
                start=min((_instant(m['at']).astimezone(zone) for m in case['messages']),default=as_of)
                slot=start.replace(hour=3,minute=0,second=0,microsecond=0)+timedelta(days=1)
                times=[]
                while slot<=as_of:
                    times.append((slot,'daily'));slot+=timedelta(days=1)
                times.append((as_of,'final_flush'))
                for current,mode in times:
                    batch=[]
                    while position<len(arrivals) and _instant(arrivals[position][1]['at'])<=current:
                        batch.append(arrivals[position][1]);position+=1
                    with patch('iris.entry_profile.utc_now',clock):
                        _replay_arrivals(store,case,batch,remaining,ids)
                        if mode=='daily':
                            maintenance=Maintenance(store,clock=clock)
                            maintenance.run(maintenance.request(trigger='scheduled',schedule_key=current.date().isoformat()))
                        with store.read() as conn: expected=_memory_state(conn,entry_id)['version']
                        outcome=(engine.update if mode=='daily' else engine.regenerate)(entry_id,expected_version=expected)
                    result=outcome.get('result',dict(status='no_new_evidence',reason=outcome.get('reason'),work=[],calls=[]))
                    results.append(dict(as_of=current.isoformat(),mode=mode,**result))
                    with store.read() as conn:
                        checkpoints.append(dict(as_of=current.isoformat(),mode=mode,profiles=memory_profile_rows(conn,entry_id,as_of=current.isoformat())))
                    # Durable per-day results even if a later request interrupts the round.
                    _write(root/f'case-{case["id"]}-progress.json',_redacted(dict(steps=results,checkpoints=checkpoints),configs or {}))
                    if result.get('reason') in ('timeout','rate_limited') or any(c.get('reason') in ('timeout','rate_limited') for c in result.get('calls',[])):
                        invalid=dict(case_id=case['id'],step=len(results),reason=result.get('reason'));break
            current=as_of
            with store.read() as conn:
                profiles=memory_profile_rows(conn,entry_id,as_of=case['as_of'],usable_only=True)
                all_profiles=memory_profile_rows(conn,entry_id,as_of=case['as_of'])
                projection=profile_context(conn,entry_id)
                work=[dict(r) for r in conn.execute('SELECT * FROM entry_profile_incremental_work WHERE entry_id=? ORDER BY id',(entry_id,))]
                calls=[dict(r) for r in conn.execute('''SELECT purpose,model,status_code,result_category,timed_out,finish_reason,
                    reasoning_effort,prompt_tokens,completion_tokens,duration_ms FROM model_calls ORDER BY id''')]
                migration=[dict(r) for r in conn.execute('SELECT * FROM entry_profile_legacy_migration')]
            memories=[_judge_memory(p) for p in profiles]
            stages=_incremental_stages(work,manual)
            displayed=set((projection or {}).get('memory_ids',[]))
            stages['projected']=[m for m in memories if m['memory_id'] in displayed]
            payload=dict(entry={k:case['entry'][k] for k in ('id','kind')},as_of=case['as_of'],timezone=case.get('timezone','Asia/Shanghai'),
                memories=memories,projection=projection or dict(text='',memory_ids=[]),stages=stages,
                must_cover=case.get('must_cover',[]),forbidden=case.get('forbidden',[]))
            document=dict(format_version=2,evaluation='entry_profile_memory',case_id=case['id'],input=payload)
            relative=f'cases/{index:04d}.json';_write(materials/relative,document)
            status='committed' if any(w['state']=='applied' for w in work) else 'failed' if results and any(r.get('work') for r in results) else 'no_new_evidence' if results else 'not_run'
            if invalid and invalid['case_id']==case['id']: status='interrupted'
            diagnostics=dict(pages=len(results),observation_chunks=sum(w['kind']=='observe' for w in work),
                admission_checks=sum(w['kind']=='admit' for w in work),
                quote_total=sum(len(m['sources']) for m in memories),quote_matched=sum(len(m['sources']) for m in memories),
                omitted_anchors=0,omitted_memories=sum(json.loads(w['payload_json']).get('_snapshot',{}).get('omitted_profiles',0) for w in work),
                truncated_messages=0,failed_calls=sum(c.get('result')!='success' for r in results for c in r.get('calls',[])))
            traces.append(dict(case_id=case['id'],steps=results,checkpoints=checkpoints,work=work,candidates=all_profiles,model_calls=calls,migration=migration))
            entries.append(dict(case_id=case['id'],file=relative,sha256=evidence_hash(document),judgment_file=f'{index:04d}.json',execution_status=status,diagnostics=diagnostics))
            _write(root/'progress.json',_redacted(dict(cases=traces,invalidated=invalid),configs or {}))
        finally:
            if gateway is not None and callable(getattr(gateway,'close',None)): gateway.close()
            store.close()
    scoring=(HERE/'scoring_v3.md').read_text(encoding='utf-8')
    metadata=dict(format_version=2,evaluation='entry_profile_memory',scoring_version='entry_profile_scoring_v3',
        scoring_status=SCORING_STATUS,gate_eligible=configs is not None and invalid is None,repository_mode='sqlite',
        execution_kind='real' if configs is not None else 'fake',invalidated=invalid,replay_mode='daily_plus_final_flush',
        corpus_sha256=hashlib.sha256(Path(corpus).read_bytes()).hexdigest(),source_sha256=_source_hash(),
        scoring_sha256=hashlib.sha256(scoring.encode()).hexdigest(),prompt_versions=list(hashes),prompt_sha256=hashes,
        models=_model_metadata(configs or {}))
    if union_nomination: metadata['entry_profile_union_nomination']=True
    _write(root/'run.json',_redacted(dict(metadata=metadata,cases=traces),configs or {}))
    manifest=dict(metadata=metadata,cases=entries,run_sha256=hashlib.sha256((root/'run.json').read_bytes()).hexdigest())
    manifest['materials_sha256']=evidence_hash(manifest)
    _write(materials/'manifest.json',manifest);_write(materials/'round-template.json',manifest)
    (materials/'scoring.md').write_text(scoring,encoding='utf-8')
    return manifest


def _sealed(value, field):
    return {**value,field:evidence_hash(value)}


def _check_seal(value, field):
    if value.get(field)!=evidence_hash({k:v for k,v in value.items() if k!=field}):
        raise ValueError('summary/series fingerprint mismatch')


def _repeat_usage(root):
    run=_read(Path(root)/'run.json')
    calls=[call for case in run['cases'] for call in case.get('model_calls',[])]
    recorded=bool(calls)
    if not calls:
        # Fake gateways do not write provider call rows. Durable work traces
        # retain attempts without counting the same work again at each checkpoint.
        for case in run['cases']:
            unique={w['id']:w for w in case.get('work',[])}
            calls.extend(call for work in unique.values() for call in json.loads(work['calls_json']))
    return dict(calls=len(calls),provider_recorded=recorded,
        observe_calls=sum('observe' in c['purpose'] and 'repair' not in c['purpose'] for c in calls),
        admit_calls=sum('admit' in c['purpose'] and 'repair' not in c['purpose'] for c in calls),
        repair_calls=sum('repair' in c['purpose'] for c in calls),
        request_seconds=sum(c.get('duration_ms') or 0 for c in calls)/1000,
        prompt_tokens=sum(c.get('prompt_tokens') or c.get('usage',{}).get('prompt_tokens') or 0 for c in calls),
        completion_tokens=sum(c.get('completion_tokens') or c.get('usage',{}).get('completion_tokens') or 0 for c in calls))


def run_repeated(corpus, out, *, runs, union_nomination=False, gateway_factory=None, configs=None):
    """Independent complete replays, no automatic retry or selection of cases."""
    import time
    from .runner import _source_hash, _model_metadata
    if type(runs) is not int or runs<1: raise ValueError('runs must be a positive integer')
    if type(union_nomination) is not bool: raise ValueError('union_nomination must be boolean')
    if (gateway_factory is None)==(configs is None): raise ValueError('provide exactly one gateway/configuration')
    corpus=Path(corpus);root=Path(out);root.mkdir(parents=True,exist_ok=False)
    metadata=dict(corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest(),source_sha256=_source_hash(),
        scoring_sha256=hashlib.sha256((HERE/'scoring_v3.md').read_bytes()).hexdigest(),
        replay_mode='daily_plus_final_flush',models=_model_metadata(configs or {}))
    result=dict(format_version=1,evaluation='entry_profile_repeated',metadata=metadata,
        requested_runs=runs,settings=dict(union_nomination=union_nomination),runs=[],invalidated=None,gate_eligible=False)
    def save(): _write(root/'series.json',_sealed(result,'series_sha256'))
    save()
    for index in range(1,runs+1):
        if (hashlib.sha256(corpus.read_bytes()).hexdigest()!=metadata['corpus_sha256'] or _source_hash()!=metadata['source_sha256']):
            result['invalidated']=dict(run_index=index,reason='input_changed');save();break
        directory=f'run-{index:04d}';started=time.monotonic()
        try:
            manifest=run_sqlite(corpus,root/directory,gateway_factory=gateway_factory,configs=configs,union_nomination=union_nomination)
        except Exception as error:
            result['invalidated']=dict(run_index=index,reason='runner_error',error_type=type(error).__name__);save();raise
        result['runs'].append(dict(index=index,directory=directory,materials_sha256=manifest['materials_sha256'],
            gate_eligible=manifest['metadata']['gate_eligible'],invalidated=manifest['metadata']['invalidated'],
            usage=_repeat_usage(root/directory),wall_seconds=time.monotonic()-started))
        if manifest['metadata']['invalidated']:
            result['invalidated']=dict(run_index=index,**manifest['metadata']['invalidated']);save();break
        if (hashlib.sha256(corpus.read_bytes()).hexdigest()!=metadata['corpus_sha256']
                or _source_hash()!=metadata['source_sha256']
                or any(manifest['metadata'][k]!=metadata[k] for k in ('corpus_sha256','source_sha256','scoring_sha256','replay_mode','models'))):
            result['invalidated']=dict(run_index=index,reason='input_changed');save();break
        result['gate_eligible']=len(result['runs'])==runs and all(r['gate_eligible'] for r in result['runs'])
        save()
    return _sealed(result,'series_sha256')


def _repeat_inputs(root, record):
    materials=root/record['directory']/'judging-materials';manifest=_read(materials/'manifest.json')
    if (manifest.get('materials_sha256')!=record['materials_sha256']
            or manifest['materials_sha256']!=evidence_hash({k:v for k,v in manifest.items() if k!='materials_sha256'})
            or hashlib.sha256((root/record['directory']/'run.json').read_bytes()).hexdigest()!=manifest['run_sha256']
            or hashlib.sha256((materials/'scoring.md').read_bytes()).hexdigest()!=manifest['metadata']['scoring_sha256']):
        raise ValueError('run material fingerprint mismatch')
    values=[]
    for index,case in enumerate(manifest['cases'],1):
        if case['file']!=f'cases/{index:04d}.json': raise ValueError('invalid case filename')
        path=materials/case['file']
        if not path.resolve().is_relative_to(materials.resolve()): raise ValueError('material outside directory')
        doc=_read(path)
        if evidence_hash(doc)!=case['sha256'] or doc['case_id']!=case['case_id']: raise ValueError('case fingerprint mismatch')
        values.append((case['case_id'],doc['input']))
    if len({key for key,_ in values})!=len(values): raise ValueError('duplicate case ID')
    return manifest,values


def _case_statistics(case_id, payload, vote):
    rows=list(zip(payload['memories'],vote['memory_results'],strict=True))
    model=[v for m,v in rows if m['author']!='admin']
    return dict(case_id=case_id,label_sha256=evidence_hash(dict(must_cover=payload['must_cover'],forbidden=payload['forbidden'])),
        coverage=sum(vote['must_cover']),coverage_total=len(vote['must_cover']),
        model_items=len(model),grounded_items=sum(v['supported'] for v in model),
        violation_memory_items=sum(bool(v['violations']) for _,v in rows),
        model_violation_memory_items=sum(bool(v['violations']) for v in model),
        violation_occurrences=sum(len(v['violations']) for _,v in rows)+len(vote['projection_violations']),
        projection_violations=len(vote['projection_violations']),forbidden_occurrences=sum(vote['forbidden']),
        stage_coverage={key:sum(vote['stage_coverage'][key]) for key in STAGES})


_REPEAT_TOTALS=('coverage','coverage_total','model_items','grounded_items','violation_memory_items',
                'model_violation_memory_items','violation_occurrences','projection_violations','forbidden_occurrences')


def summarize_repeated(series, scores):
    """Describe existing independent scores; never grade or change vote merging."""
    from statistics import mean
    root=Path(series);description=_read(root/'series.json');_check_seal(description,'series_sha256')
    if (description['invalidated'] or len(description['runs'])!=description['requested_runs']
            or len(scores)!=description['requested_runs']):
        raise ValueError('incomplete/invalid series or missing scores; no runs may be omitted')
    runs=[];case_order=None;labels={}
    for index,(record,score_path) in enumerate(zip(description['runs'],scores,strict=True),1):
        if record['index']!=index or record['directory']!=f'run-{index:04d}' or record['invalidated']:
            raise ValueError('invalid repeated run order')
        manifest,inputs=_repeat_inputs(root,record);score=_read(Path(score_path))
        if (score['materials_sha256']!=manifest['materials_sha256'] or score['scoring_status']!=SCORING_STATUS
                or score['judge_runs'] not in (1,2) or len(score['judgments'])!=len(inputs)):
            raise ValueError('score fingerprint/format mismatch')
        if any(manifest['metadata'][k]!=description['metadata'][k] for k in ('corpus_sha256','source_sha256','scoring_sha256','replay_mode','models')):
            raise ValueError('repeated inputs changed')
        cases=[_case_statistics(key,payload,validate_vote(vote,payload))
            for (key,payload),vote in zip(inputs,score['judgments'],strict=True)]
        order=[c['case_id'] for c in cases]
        if case_order is not None and case_order!=order: raise ValueError('case pairing mismatch')
        case_order=order
        for case in cases:
            if case['case_id'] in labels and labels[case['case_id']]!=case['label_sha256']: raise ValueError('case labels changed')
            labels[case['case_id']]=case['label_sha256']
        totals={key:sum(c[key] for c in cases) for key in _REPEAT_TOTALS}
        if (totals['coverage']!=score['metrics']['must_cover_covered'] or totals['coverage_total']!=score['metrics']['must_cover_total']
                or totals['model_items']!=score['metrics']['model_items'] or totals['grounded_items']!=score['metrics']['grounded_items']
                or totals['forbidden_occurrences']!=score['metrics']['forbidden_occurrences']):
            raise ValueError('score metrics disagree with votes')
        totals['grounded_ratio']=totals['grounded_items']/totals['model_items'] if totals['model_items'] else None
        runs.append(dict(index=index,**totals,usage=_repeat_usage(root/record['directory']),
            wall_seconds=record['wall_seconds'],judge_model=score['judge_model'],judge_runs=score['judge_runs'],
            gate_eligible=score['gate_eligible'],cases=cases))
    means={key:mean(r[key] for r in runs) for key in _REPEAT_TOTALS}
    means.update(calls=mean(r['usage']['calls'] for r in runs),request_seconds=mean(r['usage']['request_seconds'] for r in runs),
                 wall_seconds=mean(r['wall_seconds'] for r in runs),
                 grounded_ratio=mean(r['grounded_ratio'] for r in runs) if all(r['grounded_ratio'] is not None for r in runs) else None)
    per_case=[]
    for offset,key in enumerate(case_order or []):
        values=[r['cases'][offset]['coverage'] for r in runs]
        per_case.append(dict(case_id=key,label_sha256=labels[key],coverage_counts=values,coverage_mean=mean(values),
            coverage_min=min(values),coverage_max=max(values),coverage_total=runs[0]['cases'][offset]['coverage_total']))
    return _sealed(dict(format_version=1,evaluation='entry_profile_repeated_summary',metadata=description['metadata'],
        settings=description['settings'],runs=runs,means=means,per_case=per_case,descriptive_only=True,gate_eligible=False),'summary_sha256')


def compare_repeated(left, right):
    """Paired by run ordinal and stable case ID, with no selection of good runs."""
    from statistics import mean
    for value in (left,right): _check_seal(value,'summary_sha256')
    for key in ('corpus_sha256','scoring_sha256','source_sha256','replay_mode','models'):
        if left['metadata'][key]!=right['metadata'][key]: raise ValueError('paired '+key+' mismatch')
    if not left['runs'] or len(left['runs'])!=len(right['runs']): raise ValueError('paired run count mismatch')
    pairs=[];run_pairs=[]
    for a,b in zip(left['runs'],right['runs'],strict=True):
        if a['index']!=b['index']: raise ValueError('paired run index mismatch')
        ac={c['case_id']:c for c in a['cases']};bc={c['case_id']:c for c in b['cases']}
        if ac.keys()!=bc.keys(): raise ValueError('paired case mismatch')
        for key,x in ac.items():
            y=bc[key]
            if x['label_sha256']!=y['label_sha256']: raise ValueError('paired labels mismatch')
            pairs.append(dict(index=a['index'],case_id=key,left_coverage=x['coverage'],right_coverage=y['coverage'],
                coverage_delta=y['coverage']-x['coverage'],
                left_violation_memory_items=x['violation_memory_items'],right_violation_memory_items=y['violation_memory_items'],
                left_forbidden_occurrences=x['forbidden_occurrences'],right_forbidden_occurrences=y['forbidden_occurrences']))
        run_pairs.append(dict(index=a['index'],coverage_delta=b['coverage']-a['coverage'],
            calls_delta=b['usage']['calls']-a['usage']['calls'],request_seconds_delta=b['usage']['request_seconds']-a['usage']['request_seconds'],
            wall_seconds_delta=b['wall_seconds']-a['wall_seconds']))
    per_case=[]
    for key in dict.fromkeys(p['case_id'] for p in pairs):
        values=[p['coverage_delta'] for p in pairs if p['case_id']==key]
        per_case.append(dict(case_id=key,coverage_deltas=values,coverage_delta_mean=mean(values)))
    totals={name:{**{key:sum(r[key] for r in value['runs']) for key in _REPEAT_TOTALS},
        'calls':sum(r['usage']['calls'] for r in value['runs']),
        'request_seconds':sum(r['usage']['request_seconds'] for r in value['runs']),
        'wall_seconds':sum(r['wall_seconds'] for r in value['runs'])} for name,value in (('left',left),('right',right))}
    return dict(format_version=1,evaluation='entry_profile_paired_comparison',descriptive_only=True,gate_eligible=False,
        left_summary_sha256=left['summary_sha256'],right_summary_sha256=right['summary_sha256'],
        left_settings=left['settings'],right_settings=right['settings'],pairs=pairs,run_pairs=run_pairs,per_case=per_case,totals=totals,
        means={key:mean(r[key] for r in run_pairs) for key in ('coverage_delta','calls_delta','request_seconds_delta','wall_seconds_delta')})


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Entry-profile memory execution / offline scoring / repeated paired comparison')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('--corpus',required=True);run.add_argument('--out',required=True)
    run.add_argument('--union-nomination',action='store_true')
    repeat=sub.add_parser('repeat');repeat.add_argument('--corpus',required=True);repeat.add_argument('--out',required=True)
    repeat.add_argument('--runs',type=int,required=True);repeat.add_argument('--union-nomination',action='store_true')
    score=sub.add_parser('score');score.add_argument('--materials',required=True)
    score.add_argument('--judgments',action='append',required=True);score.add_argument('--judge-model',required=True);score.add_argument('--out',required=True)
    summary=sub.add_parser('summarize');summary.add_argument('--series',required=True);summary.add_argument('--score',action='append',required=True);summary.add_argument('--out',required=True)
    compare=sub.add_parser('compare');compare.add_argument('--left',required=True);compare.add_argument('--right',required=True);compare.add_argument('--out',required=True)
    args=parser.parse_args()
    if args.command in ('run','repeat'):
        from iris.models import load_test_models
        if args.command=='repeat':
            result=run_repeated(args.corpus,args.out,runs=args.runs,union_nomination=args.union_nomination,configs=load_test_models())
            print(json.dumps(dict(runs=len(result['runs']),invalidated=result['invalidated']),ensure_ascii=False))
            if result['invalidated']: raise SystemExit(2)
        else:
            manifest=run_sqlite(args.corpus,args.out,configs=load_test_models(),union_nomination=args.union_nomination)
            print(json.dumps(dict(cases=len(manifest['cases']),gate_eligible=manifest['metadata']['gate_eligible'],
                                  invalidated=manifest['metadata']['invalidated']),ensure_ascii=False))
            if manifest['metadata']['invalidated']: raise SystemExit(2)
    elif args.command=='score':
        result=score_draft(args.materials,args.judgments,judge_model=args.judge_model)
        _write(Path(args.out),result);print(json.dumps(result['metrics'],ensure_ascii=False))
    elif args.command=='summarize':
        result=summarize_repeated(args.series,args.score);_write(Path(args.out),result)
        print(json.dumps(result['means'],ensure_ascii=False))
    else:
        result=compare_repeated(_read(Path(args.left)),_read(Path(args.right)));_write(Path(args.out),result)
        print(json.dumps(result['means'],ensure_ascii=False))


if __name__=='__main__':
    main()
